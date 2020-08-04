import argparse
import numpy as np
import os.path as osp
import time
import torch
import torch.nn as nn
import torch.nn.functional as F

from torch_geometric.datasets import Reddit
from torch_geometric.nn import MessagePassing
from torch_geometric.nn.inits import glorot, zeros
from torch_geometric.utils import remove_self_loops, add_self_loops, softmax

class GATConv(MessagePassing):
    r"""The graph attentional operator from the `"Graph Attention Networks"
    <https://arxiv.org/abs/1710.10903>`_ paper
    .. math::
        \mathbf{x}^{\prime}_i = \alpha_{i,i}\mathbf{\Theta}\mathbf{x}_{i} +
        \sum_{j \in \mathcal{N}(i)} \alpha_{i,j}\mathbf{\Theta}\mathbf{x}_{j},
    where the attention coefficients :math:`\alpha_{i,j}` are computed as
    .. math::
        \alpha_{i,j} =
        \frac{
        \exp\left(\mathrm{LeakyReLU}\left(\mathbf{a}^{\top}
        [\mathbf{\Theta}\mathbf{x}_i \, \Vert \, \mathbf{\Theta}\mathbf{x}_j]
        \right)\right)}
        {\sum_{k \in \mathcal{N}(i) \cup \{ i \}}
        \exp\left(\mathrm{LeakyReLU}\left(\mathbf{a}^{\top}
        [\mathbf{\Theta}\mathbf{x}_i \, \Vert \, \mathbf{\Theta}\mathbf{x}_k]
        \right)\right)}.
    Args:
        in_feats (int): Size of each input sample.
        out_feats (int): Size of each output sample.
        num_heads (int): Number of multi-head-attentions.
        concat (bool, optional): If set to :obj:`False`, the multi-head
            attentions are averaged instead of concatenated.
            (default: :obj:`True`)
        dropout (float, optional): Dropout probability of the normalized
            attention coefficients which exposes each node to a stochastically
            sampled neighborhood during training. (default: :obj:`0`)
        negative_slope (float, optional): LeakyReLU angle of the negative
            slope. (default: :obj:`0.2`)
        activation (callable, optional): Activation function to apply. (default: None)
        **kwargs (optional): Additional arguments of
            :class:`torch_geometric.nn.conv.MessagePassing`.
    """
    def __init__(self,
                 in_feats,
                 out_feats,
                 num_heads,
                 concat=True,
                 dropout=0.,
                 negative_slope=0.2,
                 activation=None,
                 **kwargs):
        super(GATConv, self).__init__(aggr='add', **kwargs)

        self._in_feats = in_feats
        self._out_feats = out_feats
        self._num_heads = num_heads
        self.concat = concat
        self.negative_slope = negative_slope
        self.dropout = nn.Dropout(dropout)

        self.lin = nn.Linear(in_feats, num_heads * out_feats, bias=False)

        self.att_i = nn.Parameter(torch.Tensor(1, num_heads, out_feats))
        self.att_j = nn.Parameter(torch.Tensor(1, num_heads, out_feats))

        self.reset_parameters()
        self.activation = activation

    def reset_parameters(self):
        glorot(self.lin.weight)
        glorot(self.att_i)
        glorot(self.att_j)

    def forward(self, x, edge_index):
        if torch.is_tensor(x):
            x = self.dropout(x)
            x = self.lin(x)
            x = (x, x)
        else:
            x = (self.dropout(x[0]), self.dropout(x[1]))
            x = (self.lin(x[0]), self.lin(x[1]))

        out = self.propagate(edge_index, x=x)

        if self.activation is not None:
            out = self.activation(out)

        if self.concat:
            out = out.view(-1, self._num_heads * self._out_feats)
        else:
            out = out.mean(dim=1)

        return out

    def message(self, x_i, x_j, edge_index_i, size_i):
        # Compute attention coefficients.
        x_i = x_i.view(-1, self._num_heads, self._out_feats)
        x_j = x_j.view(-1, self._num_heads, self._out_feats)

        alpha = (x_i * self.att_i).sum(-1) + (x_j * self.att_j).sum(-1)
        alpha = F.leaky_relu(alpha, self.negative_slope)
        alpha = softmax(alpha, edge_index_i, size_i)

        # Sample attention coefficients stochastically.
        alpha = self.dropout(alpha)

        return x_j * alpha.view(-1, self._num_heads, 1)

class GAT(nn.Module):
    def __init__(self,
                 num_layers,
                 in_feats,
                 num_hidden,
                 num_classes,
                 heads,
                 activation=F.elu,
                 dropout=0.,
                 negative_slope=0.2):
        super(GAT, self).__init__()

        self.num_layers = num_layers
        self.gat_layers = nn.ModuleList()
        self.gat_layers.append(GATConv(in_feats=in_feats,
                                       out_feats=num_hidden,
                                       num_heads=heads[0],
                                       dropout=0.,
                                       negative_slope=negative_slope,
                                       activation=activation))

        # hidden layers
        for l in range(num_layers - 2):
            # due to multi-head, the in_feats = num_hidden * num_heads
            self.gat_layers.append(GATConv(in_feats=num_hidden * heads[l],
                                           out_feats=num_hidden,
                                           num_heads=heads[l + 1],
                                           dropout=dropout,
                                           negative_slope=negative_slope,
                                           activation=activation))
        # output projection
        self.gat_layers.append(GATConv(in_feats=num_hidden * heads[-2],
                                       out_feats=num_classes,
                                       num_heads=heads[-1],
                                       concat=False,
                                       dropout=dropout,
                                       negative_slope=negative_slope,
                                       activation=None))

    def reset_parameters(self):
        for layer in self.gat_layers:
            layer.reset_parameters()

    def forward(self, x, adj):
        for l in range(self.num_layers):
            x = self.gat_layers[l](x, adj)
        return x

def evaluate(model, adj, features, labels, mask):
    model.eval()
    with torch.no_grad():
        logits = model(features, adj)
        logits = logits[mask]
        labels = labels[mask]
        _, indices = torch.max(logits, dim=1)
        correct = torch.sum(indices == labels)
        return correct.item() * 1.0 / len(labels)

def main(args):
    path = osp.join('..', 'data', 'Reddit')
    dataset = Reddit(path)
    data = dataset[0]

    features = data.x
    labels = data.y
    train_mask = torch.BoolTensor(data.train_mask)
    val_mask = torch.BoolTensor(data.val_mask)
    test_mask = torch.BoolTensor(data.test_mask)

    edge_index = data.edge_index
    edge_index, _ = remove_self_loops(edge_index)
    edge_index, _ = add_self_loops(edge_index, num_nodes=features.size(0))

    model = GAT(num_layers=args.num_layers,
                in_feats=features.size(-1),
                num_hidden=args.num_hidden,
                num_classes=dataset.num_classes,
                heads=[1, 1, 1],
                dropout=args.dropout)

    loss_fcn = nn.CrossEntropyLoss()

    # use optimizer
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)

    dur = []
    for epoch in range(1, 1 + args.epochs):
        model.train()
        if epoch >= 3:
            t0 = time.time()
        # forward
        logits = model(features, edge_index)
        loss = loss_fcn(logits[train_mask], labels[train_mask])

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        if epoch >= 3:
            dur.append(time.time() - t0)
            print('Training time/epoch {}'.format(np.mean(dur)))

        if args.eval:
            acc = evaluate(model, edge_index, features, labels, val_mask)
        else:
            acc = 0
        print("Epoch {:05d} | Time(s) {:.4f} | Loss {:.4f} | Accuracy {:.4f} ".format(
            epoch, np.mean(dur), loss.item(), acc))

    if args.eval:
        print()
        acc = evaluate(model, edge_index, features, labels, test_mask)
        print("Test Accuracy {:.4f}".format(acc))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='GAT')
    parser.add_argument("--num-layers", type=int, default=3,
                        help="number of hidden layers")
    parser.add_argument("--lr", type=float, default=0.0029739421726400865,
                        help="learning rate")
    parser.add_argument('--weight-decay', type=float, default=2.4222556964495987e-05,
                        help="weight decay")
    parser.add_argument("--num-hidden", type=int, default=16,
                        help="number of hidden units")
    parser.add_argument("--dropout", type=float, default=0.18074706609292976,
                        help="Dropout to use")
    parser.add_argument('--epochs', type=int, default=500)
    parser.add_argument("--eval", action='store_true',
                        help='If not set, we will only do the training part.')
    args = parser.parse_args()
    print(args)

    main(args)
