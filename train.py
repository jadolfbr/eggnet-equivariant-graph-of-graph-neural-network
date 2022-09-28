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
from torch.utils.data import DataLoader
import dgl

import torchmetrics

# custom imports
# from ppi.modules import GATModel, GVPModel
from ppi.model import LitGVPModel, LitHGVPModel, LitMultiStageGVPModel, LitMultiStageHGVPModel
from ppi.data import (
    prepare_pepbdb_data_list,
    PepBDBComplexDataset,
    PIGNetComplexDataset,
    PIGNetAtomicBigraphComplexDataset,
    PIGNetHeteroBigraphComplexDataset,
    PIGNetHeteroBigraphComplexDatasetForEnergyModel,
    PIGNetAtomicBigraphComplexEnergyDataset,
)
from ppi.data_utils import (
    get_residue_featurizer,
    BaseFeaturizer,
    NaturalComplexFeaturizer,
    PDBBindComplexFeaturizer,
    PIGNetHeteroBigraphComplexFeaturizer,
    PIGNetHeteroBigraphComplexFeaturizerForEnergyModel,
    PIGNetAtomicBigraphGeometricComplexFeaturizer,
    PIGNetAtomicBigraphPhysicalComplexFeaturizer,
)

# mapping model names to constructors
MODEL_CONSTRUCTORS = {
    "gvp": LitGVPModel,
    "hgvp": LitHGVPModel,
    "multistage-gvp": LitMultiStageGVPModel,
    "multistage-hgvp": LitMultiStageHGVPModel,
}


def init_model(datum=None, model_name="gvp", num_outputs=1, **kwargs):
    if model_name in ["gvp", "hgvp"]:
        node_in_dim = (
            datum.ndata["node_s"].shape[1],
            datum.ndata["node_v"].shape[1],
        )
        kwargs["node_h_dim"] = tuple(kwargs["node_h_dim"])
        kwargs["edge_h_dim"] = tuple(kwargs["edge_h_dim"])
        print("node_h_dim:", kwargs["node_h_dim"])
        print("edge_h_dim:", kwargs["edge_h_dim"])
        model = MODEL_CONSTRUCTORS[model_name](
            g=datum, num_outputs=num_outputs, **kwargs
        )
    elif model_name in ["multistage-gvp", "multistage-hgvp"]:
        protein_graph = datum["protein_graph"] 
        ligand_graph = datum["ligand_graph"] 
        complex_graph = datum["complex_graph"]

        kwargs["stage1_node_h_dim"] = tuple(kwargs["stage1_node_h_dim"])
        kwargs["stage1_edge_h_dim"] = tuple(kwargs["stage1_edge_h_dim"])
        print("stage1_node_h_dim:", kwargs["stage1_node_h_dim"])
        print("stage1_edge_h_dim:", kwargs["stage1_edge_h_dim"])

        kwargs["stage2_node_h_dim"] = tuple(kwargs["stage2_node_h_dim"])
        kwargs["stage2_edge_h_dim"] = tuple(kwargs["stage2_edge_h_dim"])
        print("stage2_node_h_dim:", kwargs["stage2_node_h_dim"])
        print("stage2_edge_h_dim:", kwargs["stage2_edge_h_dim"])

        # Protein inputs
        protein_node_in_dim = (
            protein_graph.ndata["node_s"].shape[1],
            protein_graph.ndata["node_v"].shape[1],
        )
        protein_edge_in_dim = (
            protein_graph.edata["edge_s"].shape[1],
            protein_graph.edata["edge_v"].shape[1],
        )

        # Ligand inputs
        ligand_node_in_dim = (
            ligand_graph.ndata["node_s"].shape[1],
            ligand_graph.ndata["node_v"].shape[1],
        )
        ligand_edge_in_dim = (
            ligand_graph.edata["edge_s"].shape[1],
            ligand_graph.edata["edge_v"].shape[1],
        )

        # Complex inputs 
        complex_edge_in_dim = (
            complex_graph.edata["edge_s"].shape[1],
            complex_graph.edata["edge_v"].shape[1],
        )

        model = MODEL_CONSTRUCTORS[model_name](
            protein_node_in_dim=protein_node_in_dim,
            protein_edge_in_dim=protein_edge_in_dim,
            ligand_node_in_dim=ligand_node_in_dim,
            ligand_edge_in_dim=ligand_edge_in_dim,
            complex_edge_in_dim=complex_edge_in_dim,
            num_outputs=num_outputs,
            **kwargs
        )
    else:
        model = MODEL_CONSTRUCTORS[model_name](
            in_feats=datum.ndata["node_s"].shape[1],
            num_outputs=num_outputs,
            **kwargs
        )

    return model


def get_datasets(
    name="PepBDB",
    input_type="complex",
    data_dir="",
    test_only=False,
    residue_featurizer_name="MACCS",
    use_energy_decoder=False
):
    # initialize residue featurizer
    if "grad" in residue_featurizer_name:
        # Do not init residue_featurizer if it involes grad
        # This will allow joint training of residue_featurizer with the
        # model
        residue_featurizer = None
    else:
        residue_featurizer = get_residue_featurizer(residue_featurizer_name)

    if name == "PepBDB":
        # load parsed PepBDB structures
        train_structs = pickle.load(
            open(
                "/home/ec2-user/SageMaker/efs/data/CAMP/structures_train.pkl",
                "rb",
            )
        )
        len(train_structs)
        test_structs = pickle.load(
            open(
                "/home/ec2-user/SageMaker/efs/data/CAMP/structures_test.pkl",
                "rb",
            )
        )
        len(test_structs)
        column_names = [
            "PDB ID",
            "peptide chain ID",
            "peptide length",
            "number of atoms in peptide",
            "protein chain ID",
            "number of atoms in protein",
            "number of atom contacts between peptide and protein",
            "?",
            "peptide with nonstandard amino acid?",
            "resolution",
            "molecular type",
        ]
        # load metadata
        DATA_DIR = "/home/ec2-user/SageMaker/efs/data/PepBDB"
        metadata = os.path.join(DATA_DIR, "peptidelist.txt")
        df = pd.read_csv(
            metadata, header=None, delim_whitespace=True, names=column_names
        )
        data_list_train = prepare_pepbdb_data_list(train_structs, df)
        data_list_test = prepare_pepbdb_data_list(test_structs, df)

        # split train/val
        n_train = int(0.8 * len(data_list_train))

        if input_type == "complex":
            # Protein complex as input
            complex_featurizer = NaturalComplexFeaturizer()
            train_dataset = PepBDBComplexDataset(
                data_list_train[:n_train],
                featurizer=complex_featurizer,
                preprocess=True,
            )
            valid_dataset = PepBDBComplexDataset(
                data_list_train[n_train:],
                featurizer=complex_featurizer,
                preprocess=True,
            )
            test_dataset = PepBDBComplexDataset(
                data_list_test, featurizer=complex_featurizer, preprocess=True
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
        if input_type == "complex":
            featurizer = PDBBindComplexFeaturizer(residue_featurizer=residue_featurizer)
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

                return train_dataset, valid_dataset, test_dataset
            else:
                return test_dataset
        elif input_type == "multistage-hetero":
            if use_energy_decoder:
                featurizer = PIGNetHeteroBigraphComplexFeaturizerForEnergyModel(residue_featurizer=residue_featurizer)
                test_dataset = PIGNetHeteroBigraphComplexDatasetForEnergyModel(
                    test_keys, data_dir, id_to_y, featurizer
                )
                if not test_only:
                    with open(
                        os.path.join(data_dir, "keys/train_keys.pkl"), "rb"
                    ) as f:
                        train_keys = pickle.load(f)
                    n_train = int(0.8 * len(train_keys))
                    train_dataset = PIGNetHeteroBigraphComplexDatasetForEnergyModel(
                        train_keys[:n_train], data_dir, id_to_y, featurizer
                    )
                    valid_dataset = PIGNetHeteroBigraphComplexDatasetForEnergyModel(
                        train_keys[n_train:], data_dir, id_to_y, featurizer
                    )

                    return train_dataset, valid_dataset, test_dataset
                else:
                    return test_dataset
            else:
                featurizer = PIGNetHeteroBigraphComplexFeaturizer(residue_featurizer=residue_featurizer)
                test_dataset = PIGNetHeteroBigraphComplexDataset(
                    test_keys, data_dir, id_to_y, featurizer
                )
                if not test_only:
                    with open(
                        os.path.join(data_dir, "keys/train_keys.pkl"), "rb"
                    ) as f:
                        train_keys = pickle.load(f)
                    n_train = int(0.8 * len(train_keys))
                    train_dataset = PIGNetHeteroBigraphComplexDataset(
                        train_keys[:n_train], data_dir, id_to_y, featurizer
                    )
                    valid_dataset = PIGNetHeteroBigraphComplexDataset(
                        train_keys[n_train:], data_dir, id_to_y, featurizer
                    )

                    return train_dataset, valid_dataset, test_dataset
                else:
                    return test_dataset
        elif input_type == "multistage-geometric":
            if use_energy_decoder:
                featurizer = PIGNetAtomicBigraphGeometricComplexFeaturizer(residue_featurizer=None, return_physics=True)
                test_dataset = PIGNetAtomicBigraphComplexEnergyDataset(
                    test_keys, data_dir, id_to_y, featurizer
                )
                if not test_only:
                    with open(
                        os.path.join(data_dir, "keys/train_keys.pkl"), "rb"
                    ) as f:
                        train_keys = pickle.load(f)
                    n_train = int(0.8 * len(train_keys))
                    train_dataset = PIGNetAtomicBigraphComplexEnergyDataset(
                        train_keys[:n_train], data_dir, id_to_y, featurizer
                    )
                    valid_dataset = PIGNetAtomicBigraphComplexEnergyDataset(
                        train_keys[n_train:], data_dir, id_to_y, featurizer
                    )

                    return train_dataset, valid_dataset, test_dataset
                else:
                    return test_dataset
            else:
                featurizer = PIGNetAtomicBigraphGeometricComplexFeaturizer(residue_featurizer=None)
                test_dataset = PIGNetAtomicBigraphComplexDataset(
                    test_keys, data_dir, id_to_y, featurizer
                )
                if not test_only:
                    with open(
                        os.path.join(data_dir, "keys/train_keys.pkl"), "rb"
                    ) as f:
                        train_keys = pickle.load(f)
                    n_train = int(0.8 * len(train_keys))
                    train_dataset = PIGNetAtomicBigraphComplexDataset(
                        train_keys[:n_train], data_dir, id_to_y, featurizer
                    )
                    valid_dataset = PIGNetAtomicBigraphComplexDataset(
                        train_keys[n_train:], data_dir, id_to_y, featurizer
                    )

                    return train_dataset, valid_dataset, test_dataset
                else:
                    return test_dataset
        elif input_type == "multistage-physical":
            if use_energy_decoder:
                featurizer = PIGNetAtomicBigraphPhysicalComplexFeaturizer(residue_featurizer=None, return_physics=True)
                test_dataset = PIGNetAtomicBigraphComplexEnergyDataset(
                    test_keys, data_dir, id_to_y, featurizer
                )
                if not test_only:
                    with open(
                        os.path.join(data_dir, "keys/train_keys.pkl"), "rb"
                    ) as f:
                        train_keys = pickle.load(f)
                    n_train = int(0.8 * len(train_keys))
                    train_dataset = PIGNetAtomicBigraphComplexEnergyDataset(
                        train_keys[:n_train], data_dir, id_to_y, featurizer
                    )
                    valid_dataset = PIGNetAtomicBigraphComplexEnergyDataset(
                        train_keys[n_train:], data_dir, id_to_y, featurizer
                    )

                    return train_dataset, valid_dataset, test_dataset
                else:
                    return test_dataset
            else:
                featurizer = PIGNetAtomicBigraphPhysicalComplexFeaturizer(residue_featurizer=None)
                test_dataset = PIGNetAtomicBigraphComplexDataset(
                    test_keys, data_dir, id_to_y, featurizer
                )
                if not test_only:
                    with open(
                        os.path.join(data_dir, "keys/train_keys.pkl"), "rb"
                    ) as f:
                        train_keys = pickle.load(f)
                    n_train = int(0.8 * len(train_keys))
                    train_dataset = PIGNetAtomicBigraphComplexDataset(
                        train_keys[:n_train], data_dir, id_to_y, featurizer
                    )
                    valid_dataset = PIGNetAtomicBigraphComplexDataset(
                        train_keys[n_train:], data_dir, id_to_y, featurizer
                    )

                    return train_dataset, valid_dataset, test_dataset
                else:
                    return test_dataset
        else:
            raise NotImplementedError


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

def evaluate_graph_regression(model, data_loader, model_name="gvp", use_energy_decoder=False, is_hetero=False):
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
            if model_name == "gvp":
                batch = {key: val.to(device) for key, val in batch.items()}
                _, preds = model(batch["graph"])
            elif model_name == "multistage-gvp":
                if use_energy_decoder:
                    batch["sample"] = {key: val.to(device) for key, val in batch["sample"].items()}
                    for key, val in batch.items():
                        if key not in ["sample", "atom_to_residue"]:
                            batch[key] = val.to(device)
                    if is_hetero:
                        energies, _, _ = model(batch["protein_graph"], batch["ligand_graph"], batch["complex_graph"], batch["sample"], cal_der_loss=False, atom_to_residue=batch["atom_to_residue"])
                    else:
                        energies, _, _ = model(batch["protein_graph"], batch["ligand_graph"], batch["complex_graph"], batch["sample"], cal_der_loss=False)
                    preds = energies.sum(-1).unsqueeze(-1)
                else:
                    batch = {key: val.to(device) for key, val in batch.items()}
                    _, preds = model(batch["protein_graph"], batch["ligand_graph"], batch["complex_graph"])
            else:
                raise NotImplementedError
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
        use_energy_decoder=args.use_energy_decoder
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
        if args.model_name in ["gvp", "hgvp"]:
            datum = train_dataset[0]["graph"]
        elif args.model_name in ["multistage-gvp", "multistage-hgvp"]:
            datum = train_dataset[0]
        else:
            raise NotImplementedError
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
    log_dir = trainer.log_dir
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
        scores = evaluate_graph_regression(model, test_loader, model_name=args.model_name, use_energy_decoder=args.use_energy_decoder, is_hetero=args.is_hetero)
    pprint(scores)
    # save scores to file
    json.dump(
        scores,
        open(os.path.join(log_dir, "scores.json"), "w"),
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

    parser.add_argument("--use_energy_decoder", action="store_true")
    parser.add_argument("--is_hetero", action="store_true")
    parser.set_defaults(use_energy_decoder=False, is_hetero=False)

    args = parser.parse_args()

    print("args:", args)
    # train
    main(args)
