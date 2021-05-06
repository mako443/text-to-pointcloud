import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import torch_geometric.nn as gnn
from torch.utils.data import Dataset, DataLoader

import time
import numpy as np
import os
import pickle
from easydict import EasyDict

from models.modules import get_mlp, LanguageEncoder
from models.object_encoder import ObjectEncoder
from models.pointcloud.pointnet2 import PointNet2

from dataloading.semantic3d.semantic3d import Semantic3dCellRetrievalDataset
from dataloading.semantic3d.semantic3d_poses import Semantic3dPosesDataset

'''
TODO:
'''

class CellRetrievalNetwork(torch.nn.Module):
    def __init__(self, known_classes, known_colors, known_words, args):
        super(CellRetrievalNetwork, self).__init__()
        self.embed_dim = args.embed_dim
        self.use_features = args.use_features
        self.variation = args.variation
        self.args = args
        embed_dim = self.embed_dim

        assert args.variation in (0, 1)

        '''
        Object path
        '''
        # Set idx=0 for padding
        self.known_classes = {c: (i+1) for i,c in enumerate(known_classes)}
        self.known_classes['<unk>'] = 0
        self.class_embedding = nn.Embedding(len(self.known_classes), embed_dim, padding_idx=0)

        self.pos_embedding = get_mlp([3, 64, embed_dim]) #OPTION: pos_embedding layers
        self.color_embedding = get_mlp([3, 64, embed_dim]) #OPTION: color_embedding layers

        self.mlp_merge = get_mlp([len(self.use_features)*embed_dim, embed_dim])

        # CARE: possibly handle variation in forward()!
        if self.variation == 0:
            self.graph1 = gnn.DynamicEdgeConv(get_mlp([2 * embed_dim, embed_dim, embed_dim], add_batchnorm=True), k=8, aggr='max') # Originally: k=4
            self.lin = get_mlp([embed_dim, embed_dim, embed_dim])
        elif self.variation == 1:
            self.graph1 = gnn.DynamicEdgeConv(get_mlp([2 * embed_dim, embed_dim, embed_dim], add_batchnorm=True), k=8, aggr='mean') # Originally: k=4            
            self.lin = get_mlp([embed_dim, embed_dim, embed_dim])


        # PointNet++
        # self.pointnet = PointNet2(len(known_classes), len(known_colors), args) # The known classes are all the same now, at least for K360
        # self.pointnet.load_state_dict(torch.load(args.pointnet_path))
        # self.pointnet_dim = self.pointnet.lin2.weight.size(0)      
        # self.mlp_pointnet = get_mlp([self.pointnet_dim, self.embed_dim, self.embed_dim])
        # if args.pointnet_freeze:
        #     print('CARE: freezing PN')  
        #     self.pointnet.requires_grad_(False)




        # self.mlp_object_merge = get_mlp([self.pointnet.lin2.weight.size(0) + self.embed_dim,
        #                                  max(self.pointnet.lin2.weight.size(0), self.embed_dim),
        #                                  self.embed_dim]) # TODO: other variation?        

        # self.mlp_class = get_mlp([self.pointnet_dim, self.pointnet_dim//2, len(known_classes)])                                        
        # self.mlp_color = get_mlp([self.pointnet_dim, self.pointnet_dim//2, len(known_colors)])                                            
        self.object_encoder = ObjectEncoder(embed_dim, known_classes, known_colors, args)

        '''
        Textual path
        '''
        self.language_encoder = LanguageEncoder(known_words, embed_dim, bi_dir=True)                   

        print(f'CellRetrievalNetwork embedding: {args.pointnet_embed}, variation: {self.variation}, dim: {embed_dim}, features: {self.use_features}')

        self.printed = False

    def encode_text(self, descriptions):
        batch_size = len(descriptions)
        description_encodings = self.language_encoder(descriptions) # [B, DIM]

        description_encodings = F.normalize(description_encodings)

        return description_encodings

    def encode_objects(self, objects, object_points):
        '''
        Process the objects in a flattened way to allow for the processing of batches with uneven sample counts
        '''
        batch_size = len(objects)

        ### PN++ version
        # if self.args.pointnet_embed is False:
        #     # object_features = [self.pointnet(pyg_batch.to(self.get_device())).features for pyg_batch in object_points] # [B, obj_counts, PN_dim]
        #     # object_features = torch.cat(object_features, dim=0) # [total_objects, PN_dim]
        #     # object_features = self.mlp_pointnet(object_features)
        #     object_class_indices = [self.pointnet(pyg_batch.to(self.get_device())).class_pred for pyg_batch in object_points] # [B, obj_counts]
        #     object_class_indices = torch.cat(object_class_indices, dim=0)
        #     print(object_class_indices.shape)
        #     object_class_indices = torch.argmax(object_class_indices, dim=-1)

        # object_features = F.normalize(object_features, dim=-1) # [total_objects, PN_dim]

        # positions = [obj.closest_point for objects_sample in objects for obj in objects_sample]
        # pos_embedding = self.pos_embedding(torch.tensor(positions, dtype=torch.float, device=self.get_device()))
        # # pos_embedding = F.normalize(pos_embedding, dim=-1) # [total_objects, DIM]

        # # Merge and norm
        # object_encodings = self.mlp_object_merge(torch.cat((object_features, pos_embedding), dim=-1)) # [total_objects, DIM]
        # # object_encodings = F.normalize(object_encodings, dim=-1) # [total_objects, DIM]

        # # Auxiliary predictions
        # object_class_preds = self.mlp_class(object_features) # [total_objects, num_classes]
        # object_color_preds = self.mlp_color(object_features) # [total_objects, num_colors]        

        # Build batch: assign all objects of a sample to a combined batch-idx
        # batch = []
        # for i_sample in range(batch_size):
        #     for i_obj in range(len(objects[i_sample])):
        #         batch.append(i_sample)
        # assert len(batch) == len(object_encodings)
        # batch = torch.tensor(batch, dtype=torch.long, device=self.device)

        # embeddings = object_encodings
        ### PN++ version

        ### Embedding version
        class_indices = []
        batch = [] #Batch tensor to send into PyG
        for i_batch, objects_sample in enumerate(objects):
            for obj in objects_sample:
                class_idx = self.known_classes.get(obj.label, 0)
                class_indices.append(class_idx)
                batch.append(i_batch)
        batch = torch.tensor(batch, dtype=torch.long, device=self.device)

        # embeddings = []
        # if 'class' in self.use_features:
        #     if self.args.pointnet_embed:
        #         class_embedding = self.class_embedding(torch.tensor(class_indices, dtype=torch.long, device=self.device))
        #         embeddings.append(F.normalize(class_embedding, dim=-1))
        #     else:
        #         # embeddings.append(F.normalize(object_features, dim=-1))
        #         class_embedding = self.class_embedding(object_class_indices)
        #         embeddings.append(F.normalize(class_embedding, dim=-1))
        # if 'color' in self.use_features:
        #     colors = []
        #     for objects_sample in objects:
        #         colors.extend([obj.get_color_rgb() for obj in objects_sample])
        #     color_embedding = self.color_embedding(torch.tensor(colors, dtype=torch.float, device=self.device))
        #     embeddings.append(F.normalize(color_embedding, dim=-1))
        # if 'position' in self.use_features:
        #     positions = []
        #     for objects_sample in objects:
        #         # positions.extend([obj.center_in_cell for obj in objects_sample])
        #         positions.extend([obj.closest_point for obj in objects_sample])
        #     pos_embedding = self.pos_embedding(torch.tensor(positions, dtype=torch.float, device=self.device))
        #     embeddings.append(F.normalize(pos_embedding, dim=-1))

        # if len(embeddings) > 1:
        #     embeddings = self.mlp_merge(torch.cat(embeddings, dim=-1))
        # else:
        #     embeddings = embeddings[0]
        ### Embedding version    

        # TODO: Norm embeddings or not?    
        embeddings = self.object_encoder(objects, object_points)
        embeddings = F.normalize(embeddings, dim=-1) # OPTION: normalize, this is new

        if self.variation == 0:
            x = self.graph1(embeddings, batch)
            x = gnn.global_max_pool(x, batch)
            x = self.lin(x)
        if self.variation == 1:
            x = self.graph1(embeddings, batch)
            x = gnn.global_mean_pool(x, batch)
            x = self.lin(x)

        x = F.normalize(x)
        # return x

        return x#, object_class_preds, object_color_preds

    def forward(self):
        raise Exception('Not implemented.')

    @property
    def device(self):
        return next(self.pos_embedding.parameters()).device   

    def get_device(self):
        return next(self.pos_embedding.parameters()).device           

if __name__ == "__main__":
    model = CellRetrievalNetwork(['high vegetation', 'low vegetation', 'buildings', 'hard scape', 'cars'], 'a b c d e'.split(), embed_dim=32, k=2)      

    dataset = Semantic3dPosesDataset('./data/numpy_merged/', './data/semantic3d')
    dataloader = DataLoader(dataset, batch_size=2, collate_fn=Semantic3dPosesDataset.collate_fn)
    data = dataset[0]        
    batch = next(iter(dataloader))

    # dataset = Semantic3dCellRetrievalDataset('./data/numpy_merged/', './data/semantic3d', ['class', 'color', 'position'])
    # dataloader = DataLoader(dataset, batch_size=2, collate_fn=Semantic3dCellRetrievalDataset.collate_fn)
    # data = dataset[0]
    # batch = next(iter(dataloader))

    # text = model.encode_text(batch['descriptions'])
    # objects = model.encode_objects(batch['objects'])

    