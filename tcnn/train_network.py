import torch
import torch.nn as nn
import json
import time
import sys
import os
sys.path.append("")
from tcnn.tree_util import prepare_trees
from tcnn import tcnn_util
from tcnn.my_dataset import MyDataset


def collate(batch):
    batch = list(zip(*batch))
    return batch[0], batch[1]


def calculate_diff(network, test_loader, operators, columns, size, cuda):
    total_diff = 0.0
    for features, value in test_loader:
        prediction = network(prepare_trees(features, operators, columns, cuda))
        if cuda:
            prediction = prediction.cpu()
        prediction = float(torch.squeeze(prediction).detach().clone().numpy())
        total_diff += abs(prediction - value[0]) / value[0]
    return total_diff / size


if __name__ == "__main__":
    # all operator types in query plans
    operators = []
    with open("txt/operators.txt") as f:
        for line in f.readlines():
            operators.append(line.strip())

    # get settings from the json file
    with open(sys.argv[1], "r") as f:
        config = json.load(f)
    mode = config["mode"]  # train or test
    benchmark = config["benchmark"]
    before_episode = config["before_episode"]
    cuda = True
    if config["cuda"] == 0:
        cuda = False
    test_dataset = config["test_dataset"]
    net_file = f"tcnn/net/{benchmark}_{before_episode}.pkl"

    # all indexable columns
    columns = []
    with open(f"txt/{benchmark}_indexable_columns.txt") as f:
        for line in f.readlines():
            columns.append(line.strip().split(" ")[0])

    tree_conv_net = nn.Sequential(
        tcnn_util.BinaryTreeConv(len(operators) + len(columns) + 1, 256),
        tcnn_util.TreeLayerNorm(),
        tcnn_util.TreeActivation(nn.ReLU()),
        tcnn_util.BinaryTreeConv(256, 128),
        tcnn_util.TreeLayerNorm(),
        tcnn_util.TreeActivation(nn.ReLU()),
        tcnn_util.BinaryTreeConv(128, 64),
        tcnn_util.TreeLayerNorm(),
        tcnn_util.TreeActivation(nn.ReLU()),
        tcnn_util.DynamicPooling(),
        nn.Linear(64, 32),
        nn.ReLU(),
        nn.Linear(32, 16),
        nn.ReLU(),
        nn.Linear(16, 1))

    if cuda:
        tree_conv_net.cuda()

    test_data = MyDataset(test_dataset)
    test_loader = torch.utils.data.DataLoader(test_data, batch_size=1,
                                              collate_fn=collate, num_workers=4)

    if mode == "train":
        print(f"Train start: {time.strftime('%Y-%m-%d, %H:%M:%S', time.localtime())}")

        # get and print train parameters
        train_dataset = config["train_dataset"]
        episode_num = config["episode_num"]
        batch_size = config["batch_size"]
        save_interval = config["save_interval"]
        print(f"Train dataset: {train_dataset}\nBefore episode: {before_episode}")
        print(f"Episode num: {episode_num}\nBatch size: {batch_size}")
        print(f"Use GPU: {cuda == 1}\nSave interval: {save_interval}")

        # get optimizer and loss function
        optimizer = torch.optim.Adam(tree_conv_net.parameters())
        loss_func = torch.nn.MSELoss()

        # get train data
        train_data = MyDataset(train_dataset)
        train_loader = torch.utils.data.DataLoader(train_data, batch_size=batch_size,
                                                   shuffle=True, collate_fn=collate,
                                                   num_workers=4)

        # load tcnn's parameters from the file
        if before_episode != 0:
            tree_conv_net.load_state_dict(torch.load(net_file))

        # training
        for episode in range(before_episode, before_episode + episode_num):
            # save tcnn's parameters at regular intervals
            if episode % save_interval == 0:
                print(f"Episode: {episode}, ", end="")
                new_net_file = f"tcnn/net/{benchmark}_{episode}.pkl"
                torch.save(tree_conv_net.state_dict(), new_net_file)
                print(f"Difference: "
                      f"{calculate_diff(tree_conv_net, test_loader, operators, columns, len(test_data), cuda)}")

            # training process of one episode
            for features, value in train_loader:
                optimizer.zero_grad()
                prediction = tree_conv_net(prepare_trees(features, operators, columns, cuda))
                prediction = torch.squeeze(prediction)
                value = torch.tensor(value)
                if cuda:
                    value = value.cuda()
                loss = loss_func(prediction, value)
                loss.backward()
                optimizer.step()
        
        # test and save the final tcnn
        print(f"Episode: {before_episode + episode_num}, ", end="")
        new_net_file = f"tcnn/net/{benchmark}_{before_episode + episode_num}.pkl"
        torch.save(tree_conv_net.state_dict(), new_net_file)
        print(f"Difference: "
              f"{calculate_diff(tree_conv_net, test_loader, operators, columns, len(test_data), cuda)}")
        print(f"Train end: {time.strftime('%Y-%m-%d, %H:%M:%S', time.localtime())}\n")
    elif mode == "test":
        print(f"Test start: {time.strftime('%Y-%m-%d, %H:%M:%S', time.localtime())}")
        print(f"Test dataset: {test_dataset}\nNet file: {net_file}")
        if not os.path.exists(net_file):
            print("Net file doesn't exist.")
            exit()
        tree_conv_net.load_state_dict(torch.load(net_file))
        print(f"Difference: "
              f"{calculate_diff(tree_conv_net, test_loader, operators, columns, len(test_data), cuda)}")
        print(f"Test end: {time.strftime('%Y-%m-%d, %H:%M:%S', time.localtime())}\n")
    else:
        print("Mode parameter error!")
