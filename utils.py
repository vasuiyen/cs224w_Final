# -*- coding: utf-8 -*-
"""
Created on Mon Mar  1 03:55:06 2021
"""

import numpy as np
import scipy.sparse.linalg as linalg
import networkx as nx

import logging
import os
import queue
import shutil
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.utils.data as data
import tqdm
import deepsnap
import copy
import sys

import models

def estimate_spectral_radius(graph):
    """    
    @graph: A networkx.Graph, undirected
    
    @return: An upper bound on the spectral radius
    
    Notes:
    - spectral radius is another name for "Perron-Frobenius eigenvalue"
    (i.e. largest eigenvalue of a real, square matrix).
    - The estimation relies on the Gershgorin Circle Theorem. 
    https://en.wikipedia.org/wiki/Gershgorin_circle_theorem    
    - For graph adjacency matrices, the theorem says:
    spectral radius <= the maximum node degree
    """
    # TODO
    return max(graph.degree)    

def compute_spectral_radius(A, l = None):
    """
    @param A: 
        Numpy matrix. Must be real-valued and square
    
    @param l: 
        initial estimate of the spectral radius of A.
        Recommended to use estimate_spectral_radius for this purpose. 
    
    Based on: 
    https://scicomp.stackexchange.com/questions/21727/numerical-computation-of-perron-frobenius-eigenvector
    """
    v = np.ones(A.shape[0])
    eigenvalues, eigenvectors = linalg.eigs(A, k=2, sigma=l, which='LM', v0=v)

class AverageMeter:
    """Keep track of average values over time.

    Adapted from:
        > https://github.com/pytorch/examples/blob/master/imagenet/main.py
    """
    def __init__(self):
        self.avg = 0
        self.sum = 0
        self.count = 0

    def reset(self):
        """Reset meter."""
        self.__init__()

    def update(self, val, num_samples=1):
        """Update meter with new value `val`, the average of `num` samples.

        Args:
            val (float): Average value to update the meter with.
            num_samples (int): Number of samples that were averaged to
                produce `val`.
        """
        self.count += num_samples
        self.sum += val * num_samples
        self.avg = self.sum / self.count

class CheckpointSaver:
    """Class to save_old and load model checkpoints.

    Save the best checkpoints as measured by a metric value passed into the
    `save_old` method. Overwrite checkpoints with better checkpoints once
    `max_checkpoints` have been saved.

    Args:
        save_dir (str): Directory to save_old checkpoints.
        max_checkpoints (int): Maximum number of checkpoints to keep before
            overwriting old ones.
        metric_name (str): Name of metric used to determine best model.
        maximize_metric (bool): If true, best checkpoint is that which maximizes
            the metric value passed in via `save_old`. Otherwise, best checkpoint
            minimizes the metric.
        log (logging.Logger): Optional logger for printing information.
    """
    def __init__(self, save_dir, max_checkpoints, metric_name,
                 maximize_metric=False, log=None):
        super(CheckpointSaver, self).__init__()

        self.save_dir = save_dir
        self.max_checkpoints = max_checkpoints
        self.metric_name = metric_name
        self.maximize_metric = maximize_metric
        self.best_val = None
        self.ckpt_paths = queue.PriorityQueue()
        self.log = log
        self._print(f"Saver will {'max' if maximize_metric else 'min'}imize {metric_name}...")

    def is_best(self, metric_val):
        """Check whether `metric_val` is the best seen so far.

        Args:
            metric_val (float): Metric value to compare to prior checkpoints.
        """
        if metric_val is None:
            # No metric reported
            return False

        if self.best_val is None:
            # No checkpoint saved yet
            return True

        return ((self.maximize_metric and self.best_val < metric_val)
                or (not self.maximize_metric and self.best_val > metric_val))

    def _print(self, message):
        """Print a message if logging is enabled."""
        if self.log is not None:
            self.log.info(message)

    def save(self, step, model, metric_val, device):
        """Save model parameters to disk.

        Args:
            step (int): Total number of examples seen during training so far.
            model (torch.nn.DataParallel): Model to save_old.
            metric_val (float): Determines whether checkpoint is best so far.
            device (torch.device): Device where model resides.
        """

        ckpt_dict = {
            'model_name': model.__class__.__name__,
            'model_state': model.cpu().state_dict(),
            'model_full': model.cpu(),
            'step': step
        }

        model.to(device)

        checkpoint_path = os.path.join(self.save_dir,
                                       f'step_{step}.pth.tar')

        torch.save(ckpt_dict, checkpoint_path)
        # self._print(f'Saved checkpoint: {checkpoint_path}')

        if self.is_best(metric_val):
            # Save the best model
            self.best_val = metric_val
            best_path = os.path.join(self.save_dir, 'best.pth.tar')
            shutil.copy(checkpoint_path, best_path)
            self._print(f'New best checkpoint at step {step}...')

        # Add checkpoint path to priority queue (lowest priority removed first)
        if self.maximize_metric:
            priority_order = metric_val
        else:
            priority_order = -metric_val

        self.ckpt_paths.put((priority_order, checkpoint_path))

        # Remove a checkpoint if more than max_checkpoints have been saved
        if self.ckpt_paths.qsize() > self.max_checkpoints:
            _, worst_ckpt = self.ckpt_paths.get()
            try:
                os.remove(worst_ckpt)
                # self._print(f'Removed checkpoint: {worst_ckpt}')
            except OSError:
                # Avoid crashing if checkpoint has been removed or protected
                pass


def load_model(model, checkpoint_path, gpu_ids):
    """Load model parameters from disk.

    Args:
        model (torch.nn.DataParallel): Load parameters into this model.
        checkpoint_path (str): Path to checkpoint to load.
        gpu_ids (list): GPU IDs for DataParallel.

    Returns:
        model (torch.nn.DataParallel): Model loaded from checkpoint.
    """
    device = f"cuda" if gpu_ids else 'cpu'
    ckpt_dict = torch.load(checkpoint_path, map_location=device)

    # Build model, load parameters
    model.load_state_dict(ckpt_dict['model_state'])

    return model

def load_full_model(checkpoint_path, gpu_ids):
    """Load full model from disk.

    Args:
        
        model_full_path (str): Path to full model to load.
        gpu_ids (list): GPU IDs for DataParallel.

    Returns:
        model (torch.nn.DataParallel): Model loaded from checkpoint.
    """
    device = f"cuda" if gpu_ids else 'cpu'

    ckpt_dict = torch.load(checkpoint_path, map_location=device)

    # Load full model
    model = ckpt_dict['model_full']

    return model


def get_available_devices():
    """Get IDs of all available GPUs.

    Returns:
        device (torch.device): Main device (GPU 0 or CPU).
        gpu_ids (list): List of IDs of all GPUs that are available.
    """
    gpu_ids = []
    if torch.cuda.is_available():
        gpu_ids += [gpu_id for gpu_id in range(torch.cuda.device_count())]

        device = torch.device('cuda')
        # torch.cuda.set_device(device)
    else:
        device = torch.device('cpu')

    return device, gpu_ids


def get_save_dir(base_dir, name, training, id_max=100):
    """Get a unique save_old directory by appending the smallest positive integer
    `id < id_max` that is not already taken (i.e., no dir exists with that id).

    Args:
        base_dir (str): Base directory in which to make save_old directories.
        name (str): Name to identify this training run. Need not be unique.
        training (bool): Save dir. is for training (determines subdirectory).
        id_max (int): Maximum ID number before raising an exception.

    Returns:
        save_dir (str): Path to a new directory with a unique name.
    """
    for uid in range(1, id_max):
        subdir = 'train' if training else 'test'
        save_dir = os.path.join(base_dir, subdir, f'{name}-{uid:02d}')
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)
            return save_dir

    raise RuntimeError('Too many save_old directories created with the same name. \
                       Delete old save_old directories or use another name.')


def get_logger(log_dir, name):
    """Get a `logging.Logger` instance that prints to the console
    and an auxiliary file.

    Args:
        log_dir (str): Directory in which to create the log file.
        name (str): Name to identify the logs.

    Returns:
        logger (logging.Logger): Logger instance for logging events.
    """
    class StreamHandlerWithTQDM(logging.Handler):
        """Let `logging` print without breaking `tqdm` progress bars.

        See Also:
            > https://stackoverflow.com/questions/38543506
        """
        def emit(self, record):
            try:
                msg = self.format(record)
                tqdm.tqdm.write(msg)
                self.flush()
            except (KeyboardInterrupt, SystemExit):
                raise
            except:
                self.handleError(record)

    # Create logger
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    # Log everything (i.e., DEBUG level and above) to a file
    log_path = os.path.join(log_dir, 'log.txt')
    file_handler = logging.FileHandler(log_path)
    file_handler.setLevel(logging.DEBUG)

    # Log everything except DEBUG level (i.e., INFO level and above) to console
    console_handler = StreamHandlerWithTQDM()
    console_handler.setLevel(logging.INFO)

    # Create format for the logs
    file_formatter = logging.Formatter('[%(asctime)s] %(message)s',
                                       datefmt='%m.%d.%y %H:%M:%S')
    file_handler.setFormatter(file_formatter)
    console_formatter = logging.Formatter('[%(asctime)s] %(message)s',
                                          datefmt='%m.%d.%y %H:%M:%S')
    console_handler.setFormatter(console_formatter)

    # add the handlers to the logger
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger

def load_pyg_dataset(dataset_name, root = 'dataset/'):
    source, name = dataset_name.split('-')
    assert source in ['ogbn', 'pyg']
    if source == 'ogbn':
        from ogb.nodeproppred import PygNodePropPredDataset
        return PygNodePropPredDataset(name = dataset_name, root = root)
    elif source == 'pyg':
        from torch_geometric.datasets import KarateClub
        if name == "karate":
            return KarateClub()
        else:
            raise Exception("Dataset not recognized")
    else:
        raise Exception("Dataset not recognized")

def build_deepsnap_dataset(pyg_dataset):
    """ Convert a torch geometric dataset to a DeepSnap dataset"""
    graphs = deepsnap.dataset.GraphDataset.pyg_to_graphs(pyg_dataset)
    dataset = deepsnap.dataset.GraphDataset(graphs, task='node')
    return dataset

def build_dataloaders(args, dataset, split_idx):

    # DeepSNAP does not provide an API to use already existing splitting indices. 
    # Will submit a request at the end of the class
    # In the meantime, long live debugging! Having our index based split for node prediction! 
    graph = dataset.graphs[0]   
    graph.node_index = graph.node_label_index.clone().view(-1,1)
    split_datasets = {}
    for split in ["train", "valid", "test"]:
        
        # shallow copy all attributes
        graph_new = copy.copy(graph)
        graph_new.node_label_index = split_idx[split]
        graph_new.node_label = graph.node_label[split_idx[split]]

        dataset_new = copy.copy(dataset)
        dataset_new.graphs = [graph_new]

        split_datasets[split] = dataset_new

    dataloaders = {}
    for split, ds in split_datasets.items():
        shuffle = False
        if split == 'train':
            shuffle = args.data_shuffle
        dataloaders[split] = torch.utils.data.DataLoader(
                ds, 
                collate_fn=deepsnap.batch.Batch.collate([]),
                batch_size=args.batch_size, 
                num_workers=args.num_workers,
                shuffle=shuffle)
    

    return dataloaders


def build_model(args, dataset):
    """
    Note: Hardcoded to use node features for now
    """
    # RGNN requires special handling
    if args.name == "RecurrentGraphNeuralNet":
        # Assume we are training on a dataset of 1 graph
        assert len(dataset) == 1, "Recurrent GNN assumes we are training on a dataset of size 1"
        num_nodes = dataset[0].node_label_index.shape[0]
        from models import RecurrentGraphNeuralNet, DeepSnapWrapper
        model = RecurrentGraphNeuralNet(
            node_channels=dataset.num_node_features, 
            hidden_channels=args.hidden_dim, 
            prediction_channels=dataset.num_node_labels, 
            num_nodes=num_nodes,
            debug=args.debug)
        model = DeepSnapWrapper(model)
        return model 

    # Create the model, optimizer and checkpoint
    model_class = str_to_attribute(sys.modules['models'], args.name)
    model = model_class(dataset.num_node_features, dataset.num_node_labels, args)

    return model

def str_to_attribute(obj, attr_name):
    try:
        identifier = getattr(obj, attr_name)
        return identifier
    except AttributeError:
        raise NameError("%s doesn't exist." % attr_name)