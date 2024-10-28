#!/usr/bin/env python3
# @brief:    Pytorch Lightning module for KITTI Odometry
# @author    Benedikt Mersch    [mersch@igg.uni-bonn.de]
import os
import yaml
import torch
import numpy as np
from torch.utils.data import Dataset, DataLoader
import lightning.pytorch as pl
import sys

from utils.projection import projection
from utils.preprocess_data import prepare_data, compute_mean_and_std
from utils.utils import load_files


class NuScenesModule(pl.LightningDataModule):
    """A Pytorch Lightning module for nuscenes"""

    def __init__(self, cfg):
        """Method to initizalize the nuscenes dataset class

        Args:
          cfg: config dict

        Returns:
          None
        """
        super(NuScenesModule, self).__init__()
        self.cfg = cfg

    def prepare_data(self):
        """Call prepare_data method to generate npy range images from raw LiDAR data"""
        if self.cfg["DATA_CONFIG"]["GENERATE_FILES"]:
            prepare_data(self.cfg)

    def setup(self, stage=None):
        """Dataloader and iterators for training, validation and test data"""
        ########## Point dataset splits
        train_set = NuScenesRaw(self.cfg, split="train")

        val_set = NuScenesRaw(self.cfg, split="val")

        test_set = NuScenesRaw(self.cfg, split="test")

        ########## Generate dataloaders and iterables

        self.train_loader = DataLoader(
            dataset=train_set,
            batch_size=self.cfg["TRAIN"]["BATCH_SIZE"],
            shuffle=self.cfg["DATA_CONFIG"]["DATALOADER"]["SHUFFLE"],
            num_workers=self.cfg["DATA_CONFIG"]["DATALOADER"]["NUM_WORKER"],
            pin_memory=True,
            drop_last=False,
            timeout=0,
            persistent_workers=True
        )
        self.train_iter = iter(self.train_loader)

        self.valid_loader = DataLoader(
            dataset=val_set,
            batch_size=self.cfg["TRAIN"]["BATCH_SIZE"],
            shuffle=False,
            num_workers=self.cfg["DATA_CONFIG"]["DATALOADER"]["NUM_WORKER"],
            pin_memory=True,
            drop_last=False,
            timeout=0,
            persistent_workers=True
        )
        self.valid_iter = iter(self.valid_loader)

        self.test_loader = DataLoader(
            dataset=test_set,
            batch_size=self.cfg["TRAIN"]["BATCH_SIZE"],
            shuffle=False,
            num_workers=self.cfg["DATA_CONFIG"]["DATALOADER"]["NUM_WORKER"],
            pin_memory=True,
            drop_last=False,
            timeout=0,
            persistent_workers=True
        )
        self.test_iter = iter(self.test_loader)

        # Optionally compute statistics of training data
        if self.cfg["DATA_CONFIG"]["COMPUTE_MEAN_AND_STD"]:
            compute_mean_and_std(self.cfg, self.train_loader)

        print(
            "Loaded {:d} training, {:d} validation and {:d} test samples.".format(
                len(train_set), len(val_set), (len(test_set))
            )
        )

    def train_dataloader(self):
        return self.train_loader

    def val_dataloader(self):
        return self.valid_loader

    def test_dataloader(self):
        return self.test_loader


class NuScenesRaw(Dataset):
    """Dataset class for range image-based point cloud prediction"""

    def __init__(self, cfg, split):
        """Read parameters and scan data

        Args:
            cfg (dict): Config parameters
            split (str): Data split

        Raises:
            Exception: [description]
        """
        self.cfg = cfg
        self.root_dir = self.cfg["DATA_CONFIG"]["PROCESSED_PATH"]
        self.height = self.cfg["DATA_CONFIG"]["HEIGHT"]
        self.width = self.cfg["DATA_CONFIG"]["WIDTH"]
        self.n_channels = 4

        self.n_past_steps = self.cfg["MODEL"]["N_PAST_STEPS"]
        self.n_future_steps = self.cfg["MODEL"]["N_FUTURE_STEPS"]

        # Projection class for mapping from range image to 3D point cloud
        self.projection = projection(self.cfg)

        if split == "train":
            start = self.cfg["DATA_CONFIG"]["SPLIT"]["TRAIN"][0]["START"]
            end = self.cfg["DATA_CONFIG"]["SPLIT"]["TRAIN"][1]["END"]
        elif split == "val":
            start = self.cfg["DATA_CONFIG"]["SPLIT"]["VAL"][0]["START"]
            end = self.cfg["DATA_CONFIG"]["SPLIT"]["VAL"][1]["END"]
        elif split == "test":
            start = self.cfg["DATA_CONFIG"]["SPLIT"]["TEST"][0]["START"]
            end = self.cfg["DATA_CONFIG"]["SPLIT"]["TEST"][1]["END"]
        else:
            raise Exception("Split must be train/val/test")

        # Create a dict filenames that maps from a sequence number to a list of files in the dataset
        self.sequences = np.arange(start, end+1)
        self.filenames_range = {}
        self.filenames_xyz = {}
        self.filenames_intensity = {}
        self.filenames_semantic = {}

        # Create a dict idx_mapper that maps from a dataset idx to a sequence number and the index of the current scan
        self.dataset_size = 0
        self.idx_mapper = {}
        idx = 0
        for seq in self.sequences:
            seqstr = "{0:03d}".format(int(seq))
            scan_path_range = os.path.join(self.root_dir, seqstr, "processed", "range")
            self.filenames_range[seq] = load_files(scan_path_range)

            scan_path_xyz = os.path.join(self.root_dir, seqstr, "processed", "xyz")
            self.filenames_xyz[seq] = load_files(scan_path_xyz)
            assert len(self.filenames_range[seq]) == len(self.filenames_xyz[seq])

            scan_path_intensity = os.path.join(
                self.root_dir, seqstr, "processed", "intensity"
            )
            scan_path_semantic = os.path.join(
                self.root_dir, seqstr, "processed", "semantic"
            )
            # self.filenames_semantic[seq] = load_files(scan_path_semantic)
            # assert len(self.filenames_range[seq]) == len(self.filenames_semantic[seq])

            # Get number of sequences based on number of past and future steps
            n_samples_sequence = max(
                0,
                len(self.filenames_range[seq])
                - self.n_past_steps
                - self.n_future_steps
                + 1,
            )
            #print(n_samples_sequence)
            # Add to idx mapping
            for sample_idx in range(n_samples_sequence):
                scan_idx = self.n_past_steps + sample_idx - 1
                self.idx_mapper[idx] = (seq, scan_idx)
                idx += 1
            self.dataset_size += n_samples_sequence
        print(f"Total size of dataset {self.dataset_size}")
    def __len__(self):
        return self.dataset_size

    def __getitem__(self, idx):
        """Load and concatenate range image channels

        Args:
            idx (int): Sample index

        Returns:
            item: Dataset dictionary item
        """
        seq, scan_idx = self.idx_mapper[idx]

        # Load past data
        past_data = torch.empty(
            [self.n_past_steps, self.n_channels, self.height, self.width]
        )

        from_idx = scan_idx - self.n_past_steps + 1
        to_idx = scan_idx
        past_filenames_range = self.filenames_range[seq][from_idx : to_idx + 1]
        past_filenames_xyz = self.filenames_xyz[seq][from_idx : to_idx + 1]
        # past_filenames_intensity = self.filenames_semantic[seq][from_idx : to_idx + 1]

        for t in range(self.n_past_steps):
            past_data[t, 0, :, :] = self.load_range(past_filenames_range[t])
            past_data[t, 1:4, :, :] = self.load_xyz(past_filenames_xyz[t])
            #past_data[t, 4, :, :] = self.load_intensity(past_filenames_intensity[t])

        # Load future data
        fut_data = torch.empty(
            [self.n_future_steps, self.n_channels, self.height, self.width]
        )

        from_idx = scan_idx + 1
        to_idx = scan_idx + self.n_future_steps
        fut_filenames_range = self.filenames_range[seq][from_idx : to_idx + 1]
        fut_filenames_xyz = self.filenames_xyz[seq][from_idx : to_idx + 1]
        # fut_filenames_intensity = self.filenames_semantic[seq][from_idx : to_idx + 1]

        for t in range(self.n_future_steps):
            fut_data[t, 0, :, :] = self.load_range(fut_filenames_range[t])
            fut_data[t, 1:4, :, :] = self.load_xyz(fut_filenames_xyz[t])
            #fut_data[t, 4, :, :] = self.load_intensity(fut_filenames_intensity[t])

        item = {"past_data": past_data, "fut_data": fut_data, 
                "meta": (seq, scan_idx)}
        return item

    def load_range(self, filename):
        """Load .npy range image as (1,height,width) tensor"""
        rv = np.load(filename)
        rv = torch.Tensor(rv).float()
        return rv

    def load_xyz(self, filename):
        """Load .npy xyz values as (3,height,width) tensor"""
        xyz = torch.Tensor(np.load(filename)).float()[:, :, :3]
        xyz = xyz.permute(2, 0, 1)
        return xyz

    def load_intensity(self, filename):
        """Load .npy intensity values as (1,height,width) tensor"""
        semantic_np = np.load(filename)
        semantic_label = torch.Tensor(semantic_np)
        foreground_mask = (
                (semantic_label==10) | (semantic_label==11)\
                        | (semantic_label==13) | (semantic_label==15)\
                        | (semantic_label==18) | (semantic_label==20)\
                        | (semantic_label==30) | (semantic_label==31)\
                        | (semantic_label==32) | (semantic_label==51)\
                        | (semantic_label==71)| (semantic_label==80)\
                        | (semantic_label==81)| (semantic_label==252)\
                        | (semantic_label==253)| (semantic_label==234)\
                        | (semantic_label==255)| (semantic_label==257)\
                        | (semantic_label==258)| (semantic_label==259)\
                    ).type(torch.uint8)
        return foreground_mask


if __name__ == "__main__":
    config_filename = "config/nuscenes_parameters.yml"
    cfg = yaml.safe_load(open(config_filename))
    data = NuScenesModule(cfg)
    data.prepare_data()
    data.setup()

    item = data.valid_loader.dataset.__getitem__(0)

    def normalize(image):
        min = np.min(image)
        max = np.max(image)
        normalized_image = (image - min) / (max - min)
        return normalized_image

    import matplotlib.pyplot as plt

    fig, axs = plt.subplots(5, 1, sharex=True, figsize=(30, 30 * 5 * 64 / 2048))

    axs[0].imshow(normalize(item["fut_data"][0, 0, :, :].numpy()))
    axs[0].set_title("Range")
    axs[1].imshow(normalize(item["fut_data"][1, 0, :, :].numpy()))
    axs[1].set_title("X")
    axs[2].imshow(normalize(item["fut_data"][2, 0, :, :].numpy()))
    axs[2].set_title("Y")
    axs[3].imshow(normalize(item["fut_data"][3, 0, :, :].numpy()))
    axs[3].set_title("Z")
    axs[4].imshow(normalize(item["fut_data"][4, 0, :, :].numpy()))
    axs[4].set_title("Intensity")

    plt.show()
