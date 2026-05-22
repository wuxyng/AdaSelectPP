import torch
import torch.nn as nn


class BinaryTreeConv(nn.Module):
    def __init__(self, _in_channels, _out_channels):
        super(BinaryTreeConv, self).__init__()

        self.in_channels = _in_channels
        self.out_channels = _out_channels
        # we can think of the tree conv as a single dense layer
        # that we "drag" across the tree.
        self.weights = nn.Conv1d(_in_channels, _out_channels, stride=3, kernel_size=3)

    def forward(self, flat_data):
        # trees.shape: (batch size, features, max tree nodes)
        trees, idxes = flat_data
        orig_idxes = idxes

        idxes = idxes.expand(-1, -1, self.in_channels).transpose(1, 2)
        # idxes.shape: (batch size, in_channels, max tree nodes * 3 - 3)
        expanded = torch.gather(trees, 2, idxes)
        # expanded.shape is the same as idxes.shape
        results = self.weights(expanded)
        # results.shape: (batch size, out_channels, max tree nodes * 1 - 1)

        # add a zero vector back on
        zero_vec = torch.zeros((trees.shape[0], self.out_channels)).unsqueeze(2)
        zero_vec = zero_vec.to(results.device)
        results = torch.cat((zero_vec, results), dim=2)
        return results, orig_idxes


class TreeActivation(nn.Module):
    def __init__(self, activation):
        super(TreeActivation, self).__init__()
        self.activation = activation

    def forward(self, x):
        return self.activation(x[0]), x[1]


class TreeLayerNorm(nn.Module):
    def forward(self, x):
        data, idxes = x
        mean = torch.mean(data, dim=(1, 2)).unsqueeze(1).unsqueeze(1)
        std = torch.std(data, dim=(1, 2)).unsqueeze(1).unsqueeze(1)
        normd = (data - mean) / (std + 0.00001)
        return normd, idxes


class DynamicPooling(nn.Module):
    def forward(self, x):
        return torch.max(x[0], dim=2).values
