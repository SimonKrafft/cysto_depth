from torch.utils.data import Dataset


class MemorizeCheck(Dataset):
    def __init__(self, batch, length):
        self.batch = batch
        self.length = length
        self.i = 0
        self.num_items_in_batch = len(self.batch)
        self.batch_size = len(self.batch[0])

    def __len__(self):
        return self.length

    def __getitem__(self, idx):
        if self.i == self.batch_size:
            self.i = 0
        self.i += 1
        return [self.batch[i][self.i - 1] for i in range(self.num_items_in_batch)]
