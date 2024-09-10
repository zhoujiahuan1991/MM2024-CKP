from __future__ import absolute_import
import os
import os.path as osp
from torch.utils.data import DataLoader, Dataset
import numpy as np
import random
import math
from PIL import Image

class Preprocessor(Dataset):
    def __init__(self, dataset, root=None, transform=None):
        super(Preprocessor, self).__init__()
        self.dataset = dataset
        self.root = root
        self.transform = transform

    def __len__(self):
        return len(self.dataset)

    def __getitem__(self, indices):
            return self._get_single_item(indices)

    def _get_single_item(self, index):
        # print(self.dataset)
        try:
            fname, pid, camid, domain = self.dataset[index]
            fpath = fname
        # except:
        #     fname, pid, camid, domain, image_id = self.dataset[index]
        #     domain=image_id
        except: # will return "img, image_id, pid, camid, clean_pid"
            fname, pid, camid, domain, image_id, clean_pid = self.dataset[index]
            fpath = fname# obtain the image path
            fname=image_id
            domain=clean_pid
        
        if self.root is not None:
            fpath = osp.join(self.root, fname)

        img = Image.open(fpath).convert('RGB')

        if self.transform is not None:
            img = self.transform(img)

        return img, fname, pid, camid, domain
