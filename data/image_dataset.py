import os
from torch.utils.data import Dataset
from torchvision.io import read_image
from torchvision import transforms as torch_transforms
from utils.exr_utils import exr_2_numpy
import numpy as np
import torch
from typing import *


class EndlessDataset(Dataset):
    """ A Dataset wrapper to keep feeding its own randomized contents regardless of the length set. """
    def __init__(self, torch_dataset: Dataset, length: int, seed: int = None):
        self.dataset: Union[Sized, Dataset] = torch_dataset
        self.length = length
        self.rng = np.random.default_rng(seed)
        self._rand_buffer = self.rng.permutation(np.linspace(0, len(self.dataset) - 1, len(self.dataset)))
        self._rand_index = -1

    def __len__(self):
        return self.length

    def _reset_randomization(self):
        self._rand_buffer = self.rng.permutation(np.linspace(0, len(self.dataset) - 1, len(self.dataset)))
        self._rand_index = -1

    def _get_random_index(self):
        idx = self._rand_index
        self._rand_index += 1
        if self._rand_index == len(self.dataset):
            self._reset_randomization()
        return idx

    def __getitem__(self, idx):
        return self.dataset[self._get_random_index()]


class ImageDataset(Dataset):
    def __init__(self,
                 files: Union[List[str], List[Tuple[str, ...]]],
                 transforms: Union[Callable, List[Callable]] = None,
                 randomize: bool = False,
                 device: torch.device = torch.device('cpu'),
                 seed: int = None
                 ):
        """ A pytorch dataset that returns images or associated images with modifications (if desired). Accepts files of
        types: png, jpeg, exr, npy, npz.  Expects all images to be stored channels last on disk (note for numpy files).

        :param files: a list of image files or a list of tuples of paired image files.
        :param transforms: a list containing separate compositions of transforms from torchvision corresponding to the
                          item in each tuple in files. If each tuple is only one item, this can also be a single
                          Callable. Use None if no transforms desired.
        :param randomize: whether to maintain an internal random number and ignore the idx passed to the __getitem__
                          method.
        """
        self.files = files
        self.randomize = randomize
        self._rand_index: int = -1
        self._rand_buffer: np.ndarray = None
        self.device = device
        self.rng = np.random.default_rng(seed)
        num_images = 1
        if isinstance(files[0], tuple) and len(files[0]) > 1:
            num_images = len(files[0])

        self.transforms = [transforms] if not None and callable(transforms) else transforms
        if self.transforms is not None:
            assert len(self.transforms) == num_images
        else:
            self.transforms = [[] for _ in range(num_images)]

    def __len__(self):
        return len(self.files)

    def _reset_randomization(self):
        self._rand_buffer = self.rng.permutation(np.linspace(0, len(self) - 1, len(self)))
        self._rand_index = -1

    def _get_random_index(self):
        idx = self._rand_index
        self._rand_index += 1
        if self._rand_index == len(self):
            self._reset_randomization()
        return idx

    def __getitem__(self, idx) -> List[torch.Tensor]:
        if self.randomize:
            idx = self._get_random_index()
        data_files = self.files[idx]
        data_files = [data_files] if isinstance(data_files, str) else data_files
        final_images = []
        for i, image_file in enumerate(data_files):
            if os.path.splitext(image_file)[-1].lower() == '.exr':
                raw_image = torch.Tensor(exr_2_numpy(image_file))
                if raw_image.shape[-1] in [1, 3, 4]:
                    raw_image = torch.permute(raw_image, [2, 0, 1])
            elif os.path.splitext(image_file)[-1].lower() in ['.npy', '.npz']:
                raw_image = np.load(image_file)
                if isinstance(raw_image, dict):
                    raw_image = raw_image[list(raw_image.keys())[0]]
                raw_image = torch.Tensor(raw_image)
                if raw_image.shape[-1] in [1, 3, 4]:
                    raw_image = torch.permute(raw_image, [2, 0, 1])
            else:
                raw_image = read_image(image_file)
            image = raw_image
            if self.transforms[i]:
                image = self.transforms[i](image)
            final_images.append(image)

        return final_images


if __name__ == '__main__':
    from pathlib import Path
    import matplotlib.pyplot as plt
    from utils.image_utils import matplotlib_show
    from torch.utils.data import DataLoader
    from data.data_transforms import SynchronizedTransform, RandomAffine
    from data.picklable_generator import TorchPicklableGenerator

    test_directory = r'/Users/peter/isys/2023_01_29/color'
    image_files = [(str(p), str(p)) for p in Path(test_directory).rglob('*') if p.is_file() and p.name[0] != '.']

    rng = TorchPicklableGenerator(4242)

    random_shift = SynchronizedTransform(RandomAffine(degrees=(0, 0), translate=(.2, .2), rng=rng),
                                         num_synchros=2,
                                         rng=rng)
    # random_rotations = SynchronizedTransform(torch_transforms.RandomRotation(degrees=360),
    #                                          num_synchros=2)

    color_transforms = torch_transforms.Compose([torch_transforms.RandomGrayscale(.5),
                                                 random_shift,
                                                 # random_rotations,
                                                 torch_transforms.Resize((150, 150))])
    depth_transforms = torch_transforms.Compose([random_shift,
                                                 # random_rotations,
                                                 torch_transforms.Resize((150, 150))])

    dataset = ImageDataset(files=image_files,
                           transforms=[color_transforms, depth_transforms],
                           randomize=True)
    data_loader = DataLoader(dataset, batch_size=3, num_workers=3, pin_memory=True)
    img = dataset[5]
    matplotlib_show(torch.stack(img, dim=0))
    matplotlib_show(*next(iter(data_loader)))
    plt.show(block=True)
