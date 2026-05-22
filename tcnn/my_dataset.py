import torch


class MyDataset(torch.utils.data.Dataset):
    def __init__(self, filename):
        self.data = []
        with open(filename, "r") as f:
            line = f.readline()
            while line:
                line = line.strip().split("\t")
                line = [eval(i) for i in line]
                self.data.append(line)
                line = f.readline()

    def __getitem__(self, index):
        return self.data[index]

    def __len__(self):
        return len(self.data)
