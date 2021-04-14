from typing import List

import os
import os.path as osp
import pickle
import numpy as np
import cv2

import torch
import torch_geometric.transforms as T
from torch_geometric.data import Data, DataLoader

from datapreparation.kitti360.utils import CLASS_TO_LABEL, LABEL_TO_CLASS, CLASS_TO_MINPOINTS, SCENE_NAMES, COLORS, COLOR_NAMES
from datapreparation.kitti360.imports import Object3d, Cell
from datapreparation.kitti360.drawing import show_pptk, show_objects, plot_cell
from dataloading.kitti360.base import Kitti360BaseDataset

class Kitti360ObjectsDataset(Kitti360BaseDataset):
    """Dataset for Kitti360 object classification training.
    CARE: should be shuffled so that objects aren't ordered by class
    Objects will often have less than 2k points, T.FixedPoints() will sample w/ replace by default
    """    
    def __init__(self, base_path, scene_name, split=None, transform=T.Compose([T.FixedPoints(2048), T.NormalizeScale()])):
        super().__init__(base_path, scene_name, split)
        self.transform = transform

        # Note: objects are retrieved from cells, not the (un-clustered) scene-objects
        self.objects = [obj for cell in self.cells for obj in cell.objects]

        # print('Before', len(self))
        # self.objects = [obj for obj in self.objects if len(obj.xyz) >=2048]
        # print('After', len(self))        
        
        print(self)

    def __getitem__(self, idx):
        obj = self.objects[idx]
        points = torch.tensor(np.float32(obj.xyz))
        colors = torch.tensor(np.float32(obj.rgb))
        label = self.class_to_index[obj.label]

        data = Data(x=colors, y=label, pos=points) # 'x' refers to point-attributes in PyG, 'pos' is xyz
        data = self.transform(data)
        return data

    def __len__(self):
        return len(self.objects)

    def __repr__(self):
        return f'Kitti360ObjectsDataset: {len(self)} objects from {len(self.class_to_index)} classes'

if __name__ == '__main__':
    base_path = './data/kitti360'
    folder_name = '2013_05_28_drive_0000_sync'    

    datasets = [Kitti360ObjectsDataset(base_path, sn) for sn in SCENE_NAMES]
    objects = [obj for ds in datasets for obj in ds.objects]
    
    # dataset = Kitti360ObjectsDataset(base_path, folder_name)          
    # data = dataset[0]

    # dataloader = DataLoader(dataset, batch_size=2)
    # batch = next(iter(dataloader))
    unique,counts=np.unique([obj.get_color() for obj in objects if obj.label=='road'],return_counts=True)