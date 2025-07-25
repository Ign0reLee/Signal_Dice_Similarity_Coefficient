import torch
import numpy as np
import torch.nn.functional as F

import pysdtw
from pysdtw import SoftDTW

from torch import nn
from metrics import SignalDice

class SignalDiceLoss(nn.Module):

    def __init__(self,  eps=1e-6):
        super(SignalDiceLoss, self).__init__()
        self.sdsc = SignalDice(eps)
        self.eps  = eps
    
    def forward(self, inputs, targets):
        sdsc_value = self.sdsc(inputs, targets)
        return 1 - sdsc_value

class mae_loss(nn.Module):
    def __init__(self):
        super(mae_loss, self).__init__()

    def forward(self, inputs, targets):
        return torch.mean(torch.abs(inputs - targets))

class dtw_loss(nn.Module):
    def __init__(self, approx=True, gamma=1.0, use_cuda=True):
        super(dtw_loss, self).__init__()

        if approx:
            fun = pysdtw.distance.pairwise_l2_squared
            self.dtw = SoftDTW(gamma = gamma, dist_func=fun, use_cuda=use_cuda)
        else:
            self.dtw = self.dtw_distance_torch

    def dtw_distance_torch(self, inputs: torch.Tensor, targets: torch.Tensor) -> torch.Tensor:
        """
        Compute DTW distance for a batch of 1D signal pairs.
        
        Args:
            x: tensor of shape (B, C, T)
            y: tensor of shape (B, C, T)
            
        Returns:
            dtw_distances: tensor of shape (B,)
        """
        print(inputs.shape)
        B, C, T = inputs.shape
        inputs = inputs.reshape(B*C, T)
        targets = targets.reshape(B*C, T)

        dtw_distances = []
        for b in range(B*C):
            x_b = inputs[b]
            y_b = targets[b]
            T1, T2 = len(x_b), len(y_b)

            dtw = torch.full((T1 + 1, T2 + 1), float('inf'), device=inputs.device)
            dtw[0, 0] = 0.0

            for i in range(1, T1 + 1):
                for j in range(1, T2 + 1):
                    cost = torch.abs(x_b[i - 1] - y_b[j - 1])
                    dtw[i, j] = cost + torch.min(
                        torch.stack([dtw[i - 1, j],
                        dtw[i, j - 1],
                        dtw[i - 1, j - 1]])
                    )

            dtw_distances.append(dtw[T1, T2] / T1)  # normalize

        return torch.stack(dtw_distances).contiguous().view(B, C).mean(dim=1)  # (B, C)

    
    def forward(self, inputs, targets):
        return self.dtw(inputs, targets).sum()


class AutomaticWeightedLoss(torch.nn.Module):
    """automatically weighted multi-task loss
    Params：
        num: int，the number of loss
        x: multi-task loss
    Examples：
        loss1=1
        loss2=2
        awl = AutomaticWeightedLoss(2)
        loss_sum = awl(loss1, loss2)
    """

    def __init__(self, num=2):
        super(AutomaticWeightedLoss, self).__init__()
        params = torch.ones(num, requires_grad=True)
        self.params = torch.nn.Parameter(params)

    def forward(self, *x):
        loss_sum = 0
        for i, loss in enumerate(x):
            loss_sum += 0.5 / (self.params[i] ** 2) * loss + torch.log(1 + self.params[i] ** 2)
        return loss_sum


class ContrastiveWeight(torch.nn.Module):

    def __init__(self, args):
        super(ContrastiveWeight, self).__init__()
        self.temperature = args.temperature

        self.bce = torch.nn.BCELoss()
        self.softmax = torch.nn.Softmax(dim=-1)
        self.log_softmax = torch.nn.LogSoftmax(dim=-1)
        self.kl = torch.nn.KLDivLoss(reduction='batchmean')
        self.positive_nums = args.positive_nums

    def get_positive_and_negative_mask(self, similarity_matrix, cur_batch_size):
        diag = np.eye(cur_batch_size)
        mask = torch.from_numpy(diag)
        mask = mask.type(torch.bool)

        oral_batch_size = cur_batch_size // (self.positive_nums + 1)

        positives_mask = np.zeros(similarity_matrix.size())
        for i in range(self.positive_nums + 1):
            ll = np.eye(cur_batch_size, cur_batch_size, k=oral_batch_size * i)
            lr = np.eye(cur_batch_size, cur_batch_size, k=-oral_batch_size * i)
            positives_mask += ll
            positives_mask += lr

        positives_mask = torch.from_numpy(positives_mask).to(similarity_matrix.device)
        positives_mask[mask] = 0

        negatives_mask = 1 - positives_mask
        negatives_mask[mask] = 0

        return positives_mask.type(torch.bool), negatives_mask.type(torch.bool)

    def forward(self, batch_emb_om):
        cur_batch_shape = batch_emb_om.shape

        # get similarity matrix among mask samples
        norm_emb = F.normalize(batch_emb_om, dim=1)
        similarity_matrix = torch.matmul(norm_emb, norm_emb.transpose(0, 1))

        # get positives and negatives similarity
        positives_mask, negatives_mask = self.get_positive_and_negative_mask(similarity_matrix, cur_batch_shape[0])

        positives = similarity_matrix[positives_mask].view(cur_batch_shape[0], -1)
        negatives = similarity_matrix[negatives_mask].view(cur_batch_shape[0], -1)

        # generate predict and target probability distributions matrix
        logits = torch.cat((positives, negatives), dim=-1)
        y_true = torch.cat(
            (torch.ones(cur_batch_shape[0], positives.shape[-1]), torch.zeros(cur_batch_shape[0], negatives.shape[-1])),
            dim=-1).to(batch_emb_om.device).float()

        # multiple positives - KL divergence
        predict = self.log_softmax(logits / self.temperature)
        loss = self.kl(predict, y_true)

        return loss, similarity_matrix, logits, positives_mask


class AggregationRebuild(torch.nn.Module):

    def __init__(self, args):
        super(AggregationRebuild, self).__init__()
        self.args = args
        self.temperature = args.temperature
        self.softmax = torch.nn.Softmax(dim=-1)
        self.mse = torch.nn.MSELoss()

    def forward(self, similarity_matrix, batch_emb_om):
        cur_batch_shape = batch_emb_om.shape

        # get the weight among (oral, oral's masks, others, others' masks)
        similarity_matrix /= self.temperature

        similarity_matrix = similarity_matrix - torch.eye(cur_batch_shape[0]).to(
            similarity_matrix.device).float() * 1e12
        rebuild_weight_matrix = self.softmax(similarity_matrix)

        batch_emb_om = batch_emb_om.reshape(cur_batch_shape[0], -1)

        # generate the rebuilt batch embedding (oral, others, oral's masks, others' masks)
        rebuild_batch_emb = torch.matmul(rebuild_weight_matrix, batch_emb_om)

        # get oral' rebuilt batch embedding
        rebuild_oral_batch_emb = rebuild_batch_emb.reshape(cur_batch_shape[0], cur_batch_shape[1], -1)

        return rebuild_weight_matrix, rebuild_oral_batch_emb