from rdkit import Chem
from transformers import T5Tokenizer, T5EncoderModel
import pytorch_lightning as pl
from pytorch_lightning.callbacks.early_stopping import EarlyStopping
from pytorch_lightning.callbacks import ModelCheckpoint

import os
import json
import pickle
from pprint import pprint
import argparse

import numpy as np
import pandas as pd
import torch
from torch.utils.data import DataLoader, random_split
import dgl

import torchmetrics

# custom imports
# from ppi.modules import GATModel, GVPModel
from ppi.model import LitGVPModel, LitHGVPModel
from ppi.data import (
    PDBComplexDataset,
    PIGNetComplexDataset,
)
from ppi.data_utils import (
    NoncanonicalComplexFeaturizer,
    PDBBindComplexFeaturizer,
    get_residue_featurizer,
)

# mapping model names to constructors
MODEL_CONSTRUCTORS = {
    "gvp": LitGVPModel,
    "hgvp": LitHGVPModel,
    # "gat": GATModel,
}


def init_model(datum=None, model_name="gvp", num_outputs=1, **kwargs):
    if "gvp" in model_name:
        kwargs["node_h_dim"] = tuple(kwargs["node_h_dim"])
        kwargs["edge_h_dim"] = tuple(kwargs["edge_h_dim"])
        print("node_h_dim:", kwargs["node_h_dim"])
        print("edge_h_dim:", kwargs["edge_h_dim"])
        model = MODEL_CONSTRUCTORS[model_name](
            g=datum, num_outputs=num_outputs, **kwargs
        )
    else:
        model = MODEL_CONSTRUCTORS[model_name](
            in_feats=datum.ndata["node_s"].shape[1],
            num_outputs=num_outputs,
            **kwargs
        )

    return model


def get_datasets(
    name="PDBBind",
    input_type="complex",
    data_dir="",
    test_only=False,
    residue_featurizer_name="MACCS",
):
    # initialize residue featurizer
    if "grad" in residue_featurizer_name:
        # Do not init residue_featurizer if it involes grad
        # This will allow joint training of residue_featurizer with the
        # model
        residue_featurizer = None
    else:
        residue_featurizer = get_residue_featurizer(residue_featurizer_name)
    # initialize complex featurizer based on dataset type
    if name == "Propedia":
        featurizer = NoncanonicalComplexFeaturizer(residue_featurizer)
        # load Propedia metadata
        if input_type == "complex":
            test_dataset = PDBComplexDataset(
                os.path.join(data_dir, "test_09132022.csv"),
                featurizer=featurizer,
            )
            if not test_only:
                train_dataset = PDBComplexDataset(
                    os.path.join(data_dir, "train_09132022.csv"),
                    featurizer=featurizer,
                )
                # split train/val
                n_train = int(0.8 * len(train_dataset))
                train_dataset, valid_dataset = random_split(
                    train_dataset, [n_train, len(train_dataset) - n_train]
                )
        elif input_type == "polypeptides":
            raise NotImplementedError
    elif name == "PDBBind":
        # PIGNet parsed PDBBind datasets
        # read labels
        with open(os.path.join(data_dir, "pdb_to_affinity.txt")) as f:
            lines = f.readlines()
            lines = [l.split() for l in lines]
            id_to_y = {l[0]: float(l[1]) for l in lines}

        with open(os.path.join(data_dir, "keys/test_keys.pkl"), "rb") as f:
            test_keys = pickle.load(f)

        # featurizer for PDBBind
        featurizer = PDBBindComplexFeaturizer(residue_featurizer)
        test_dataset = PIGNetComplexDataset(
            test_keys, data_dir, id_to_y, featurizer
        )
        if not test_only:
            with open(
                os.path.join(data_dir, "keys/train_keys.pkl"), "rb"
            ) as f:
                train_keys = pickle.load(f)
            n_train = int(0.8 * len(train_keys))
            train_dataset = PIGNetComplexDataset(
                train_keys[:n_train], data_dir, id_to_y, featurizer
            )
            valid_dataset = PIGNetComplexDataset(
                train_keys[n_train:], data_dir, id_to_y, featurizer
            )

    if not test_only:
        return train_dataset, valid_dataset, test_dataset
    else:
        return test_dataset


def evaluate_node_classification(model, data_loader):
    """Evaluate model on dataset and return metrics."""
    # make predictions on test set
    device = torch.device("cuda:0")
    model = model.to(device)
    model.eval()

    MCC = torchmetrics.MatthewsCorrCoef(num_classes=2)
    AUPR = torchmetrics.AveragePrecision()
    AUROC = torchmetrics.AUROC()
    with torch.no_grad():
        for batch in data_loader:
            batch = batch.to(device)
            logits, _ = model(batch)
            targets = batch.ndata["target"]
            train_mask = batch.ndata["mask"]
            probs = torch.sigmoid(logits[train_mask]).to("cpu")
            targets = targets[train_mask].to(torch.int).to("cpu")

            mcc = MCC(probs, targets)
            aupr = AUPR(probs, targets)
            auroc = AUROC(probs, targets)

    results = {
        "MCC": MCC.compute().item(),
        "AUPR": AUPR.compute().item(),
        "AUROC": AUROC.compute().item(),
    }
    return results


def evaluate_graph_regression(model, data_loader):
    """Evaluate model on dataset and return metrics for graph-level regression."""
    # make predictions on test set
    device = torch.device("cuda:0")
    model = model.to(device)
    model.eval()

    R2Score = torchmetrics.R2Score()
    SpearmanCorrCoef = torchmetrics.SpearmanCorrCoef()
    MSE = torchmetrics.MeanSquaredError()
    with torch.no_grad():
        for batch in data_loader:
            batch["graph"] = batch["graph"].to(device)
            batch["g_targets"] = batch["g_targets"].to(device)
            _, preds = model(batch)
            preds = preds.to("cpu")
            targets = batch["g_targets"].to("cpu")

            r2 = R2Score(preds, targets)
            rho = SpearmanCorrCoef(preds, targets)
            mse = MSE(preds, targets)

    results = {
        "R2": R2Score.compute().item(),
        "rho": SpearmanCorrCoef.compute().item(),
        "MSE": MSE.compute().item(),
    }
    return results


def main(args):
    pl.seed_everything(42, workers=True)
    # 1. Load data
    train_dataset, valid_dataset, test_dataset = get_datasets(
        name=args.dataset_name,
        input_type=args.input_type,
        data_dir=args.data_dir,
        residue_featurizer_name=args.residue_featurizer_name,
    )
    print(
        "Data loaded:",
        len(train_dataset),
        len(valid_dataset),
        len(test_dataset),
    )
    # 2. Prepare data loaders
    train_loader = DataLoader(
        train_dataset,
        batch_size=args.bs,
        shuffle=True,
        num_workers=args.num_workers,
        collate_fn=train_dataset.collate_fn,
        persistent_workers=args.persistent_workers,
    )

    valid_loader = DataLoader(
        valid_dataset,
        batch_size=args.bs,
        shuffle=False,
        num_workers=args.num_workers,
        collate_fn=train_dataset.collate_fn,
        persistent_workers=args.persistent_workers,
    )

    test_loader = DataLoader(
        test_dataset,
        batch_size=args.bs,
        shuffle=False,
        num_workers=args.num_workers,
        collate_fn=test_dataset.collate_fn,
        persistent_workers=args.persistent_workers,
    )
    # 3. Prepare model
    if args.dataset_name == "PDBBind":
        datum = train_dataset[0]["graph"]
    else:
        datum = train_dataset[0][0]
    dict_args = vars(args)
    model = init_model(datum=datum, num_outputs=1, **dict_args)
    # 4. Training model
    # callbacks
    early_stop_callback = EarlyStopping(
        monitor="val_loss", patience=args.early_stopping_patience
    )
    # Init ModelCheckpoint callback, monitoring 'val_loss'
    checkpoint_callback = ModelCheckpoint(monitor="val_loss")
    # init pl.Trainer
    trainer = pl.Trainer.from_argparse_args(
        args,
        deterministic=True,
        callbacks=[early_stop_callback, checkpoint_callback],
    )
    # train
    trainer.fit(model, train_loader, valid_loader)
    print("Training finished")
    print(
        "checkpoint_callback.best_model_path:",
        checkpoint_callback.best_model_path,
    )
    # 5. Evaluation
    # load the best model
    model = model.load_from_checkpoint(
        checkpoint_path=checkpoint_callback.best_model_path,
    )
    print("Testing performance on test set")
    if args.dataset_name == "PepBDB":
        scores = evaluate_node_classification(model, test_loader)
    elif args.dataset_name == "PDBBind":
        scores = evaluate_graph_regression(model, test_loader)
    pprint(scores)
    # save scores to file
    json.dump(
        scores,
        open(os.path.join(trainer.log_dir, "scores.json"), "w"),
    )
    return


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    # add all the available trainer options to argparse
    parser = pl.Trainer.add_argparse_args(parser)
    # figure out which model to use
    parser.add_argument(
        "--model_name",
        type=str,
        default="gvp",
        help="Choose from %s" % ", ".join(list(MODEL_CONSTRUCTORS.keys())),
    )
    # THIS LINE IS KEY TO PULL THE MODEL NAME
    temp_args, _ = parser.parse_known_args()
    # add model specific args
    model_name = temp_args.model_name
    parser = MODEL_CONSTRUCTORS[model_name].add_model_specific_args(parser)

    # Additional params
    # dataset params
    parser.add_argument(
        "--dataset_name",
        help="dataset name",
        type=str,
        default="PepBDB",
    )
    parser.add_argument(
        "--input_type",
        help="data input type",
        type=str,
        default="complex",
    )
    parser.add_argument(
        "--data_dir",
        help="directory to dataset",
        type=str,
        default="",
    )
    # featurizer params
    parser.add_argument(
        "--residue_featurizer_name",
        help="name of the residue featurizer",
        type=str,
        default="MACCS",
    )
    # training hparams
    parser.add_argument("--lr", type=float, default=1e-4, help="learning rate")
    parser.add_argument("--bs", type=int, default=32, help="batch size")
    parser.add_argument("--early_stopping_patience", type=int, default=5)
    parser.add_argument(
        "--num_workers",
        type=int,
        default=0,
        help="num_workers used in DataLoader",
    )
    parser.add_argument(
        "--persistent_workers",
        type=bool,
        default=False,
        help="persistent_workers in DataLoader",
    )

    args = parser.parse_args()

    print("args:", args)
    # train
    main(args)
