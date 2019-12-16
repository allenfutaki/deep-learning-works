from database.datasets import *
from PIL import Image

class build_reid_dataset(data.Dataset):
    def __init__(self, data, transform=None, return_indice=False):
        self.data = data
        self.transform = transform
        self.return_indice = return_indice
           
    def __getitem__(self, index):
        img_path, pid, camid = self.data[index]

        img = Image.open(img_path)

        if self.transform is not None:
            img = self.transform(img)
            if isinstance(img, tuple):
                img = img[0]
        if self.return_indice:
            return img, pid, camid, index
            
        return img, pid, camid

    def __len__(self):
        return len(self.data)