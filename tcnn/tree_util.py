import numpy as np
import torch
import numpy
from treelib.tree import Tree


def __flatten(tree):
    """ Turn a tree into a flattened vector in preorder,
    all nodes in the tree should have zero or two children. """

    accum = []

    def recurse(x):
        if x.is_leaf():
            accum.append(x.data)
            return
        accum.append(x.data)
        recurse(tree.children(x.identifier)[0])
        recurse(tree.children(x.identifier)[1])

    recurse(tree.get_node(0))

    accum = [np.zeros(accum[0].shape)] + accum
    return np.array(accum)


def __preorder_indexes(tree, node, idx=1):
    """ Transform a tree into a tree of preorder indexes. """

    if node.is_leaf():
        return idx

    def rightmost(tree):
        if isinstance(tree, tuple):
            return rightmost(tree[2])
        return tree

    left_subtree = __preorder_indexes(tree, tree.children(node.identifier)[0],
                                      idx=idx + 1)
    max_index_in_left = rightmost(left_subtree)
    right_subtree = __preorder_indexes(tree, tree.children(node.identifier)[1],
                                       idx=max_index_in_left + 1)
    return idx, left_subtree, right_subtree


def __tree_conv_indexes(tree):
    """ Create indexes that are used with flatten trees such that 
    a stride-3 1D convolution is the same as a tree convolution. """

    index_tree = __preorder_indexes(tree, tree.get_node(0))

    def recurse(root):
        if isinstance(root, tuple):
            my_id = root[0]
            left_id = root[1][0] if isinstance(root[1], tuple) else root[1]
            right_id = root[2][0] if isinstance(root[2], tuple) else root[2]
            yield [my_id, left_id, right_id]

            yield from recurse(root[1])
            yield from recurse(root[2])
        else:
            yield [root, 0, 0]

    return np.array(list(recurse(index_tree))).flatten().reshape(-1, 1)


def __pad_and_combine(trees):
    """ Adjust all trees so that they have the same shape. """

    second_dim = trees[0].shape[1]
    max_first_dim = max(tree.shape[0] for tree in trees)

    vecs = []
    for tree in trees:
        padded = np.zeros((max_first_dim, second_dim))
        padded[0:tree.shape[0]] = tree
        vecs.append(padded)

    return np.array(vecs)


def __create_tree(node, id=0):
    """ Convert a query plan into a tree. Type of node is dict. """

    tree = Tree()
    child_nodes = None
    child_nodes_num = 0
    if "Plans" in node:
        child_nodes = node["Plans"]
        del node["Plans"]
    tree.create_node(identifier=id, data=node)
    if child_nodes is not None:
        for child_node in child_nodes:
            child_tree = __create_tree(child_node, id + 1 + child_nodes_num)
            child_nodes_num += len(child_tree)
            tree.paste(id, child_tree)
    return tree


def __convert_to_binary(tree):
    """ Convert a tree into a binary tree 
    (every node has zero or two children). """

    nodes = tree.all_nodes()
    num = len(nodes)
    for node in nodes:
        children = tree.children(node.identifier)
        if len(children) == 0:
            pass
        elif len(children) == 1:
            tree.create_node(identifier=num, parent=node.identifier, data=None)
            num = num + 1
        else:
            while len(children) > 2:
                tree.create_node(identifier=num, parent=node.identifier, data=None)
                tree.move_node(children[0].identifier, num)
                tree.move_node(children[1].identifier, num)
                children[0] = tree.get_node(num)
                del children[1]
                num = num + 1
    return tree


def __vectorize_data(tree, operators, columns):
    """ Convert each node's data into a vector. """

    for node in tree.all_nodes():
        new_data = numpy.zeros(len(operators) + len(columns) + 1)
        if node.data is not None:
            new_data[operators.index(node.data["Node Type"])] = 1.0
            data_text = ""
            for value in node.data.values():
                data_text += str(value)
            for i, column in enumerate(columns):
                if column in data_text:
                    new_data[len(operators) + i] = 1.0
            new_data[-1] = node.data["Total Cost"]
        node.data = new_data
    return tree


def prepare_trees(plans, operators, columns, cuda=False):
    """ Convert plans into the input of tree convolution network. """

    trees = [__vectorize_data(__convert_to_binary
                              (__create_tree(plan)), operators, columns) for plan in plans]
    flat_trees = [__flatten(tree) for tree in trees]
    flat_trees = __pad_and_combine(flat_trees)
    flat_trees = torch.Tensor(flat_trees)

    # (batch size, max tree nodes, features) -> (batch size, features, max tree nodes)
    flat_trees = flat_trees.transpose(1, 2)

    indexes = [__tree_conv_indexes(tree) for tree in trees]
    indexes = __pad_and_combine(indexes)
    indexes = torch.Tensor(indexes).long()  # (batch size, max tree nodes * 3 - 3, 1)

    if cuda:
        flat_trees = flat_trees.cuda()
        indexes = indexes.cuda()

    return flat_trees, indexes
