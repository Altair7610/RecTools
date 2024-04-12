#  Copyright 2022 MTS (Mobile Telesystems)
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

"""DSSM model."""


# pylint: disable=abstract-method
from __future__ import annotations

import typing as tp
import warnings
from copy import deepcopy

import numpy as np
import torch
import torch.nn.functional as F

from rectools.models.dssm import DSSMModel

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    from pytorch_lightning import Callback, LightningModule, Trainer
    from pytorch_lightning.loggers import Logger

from torch import nn
from torch.utils.data import DataLoader
from torch.utils.data import Dataset as TorchDataset

from ..dataset.dataset import Dataset
from ..dataset.torch_datasets import ItemFeaturesDataset, UserFeaturesDatasetNoInteractions
from ..exceptions import NotFittedError
from .rank import Distance
from .vector import Factors, VectorModel


class ItemNet(nn.Module):
    def __init__(
        self,
        n_factors: int,
        dim_input: int,
        activation: tp.Callable[[torch.Tensor], torch.Tensor] = F.elu,
    ) -> None:
        super().__init__()
        self.embedding_layer = nn.Linear(dim_input, n_factors, bias=False)
        self.dense_layer = nn.Linear(n_factors, n_factors, bias=False)
        self.output_layer = nn.Linear(n_factors, n_factors, bias=False)
        self.activation = activation

    def forward(self, item_features: torch.Tensor) -> torch.Tensor:
        emb = self.activation(self.embedding_layer(item_features))
        features = self.activation(self.dense_layer(emb))
        x = emb + features

        output = self.output_layer(x)
        return output


class UserNet(nn.Module):
    def __init__(
        self,
        n_factors: int,
        dim_input: int,
        activation: tp.Callable[[torch.Tensor], torch.Tensor] = F.elu,
    ) -> None:
        super().__init__()
        self.embedding_features_layer = nn.Linear(dim_input, n_factors, bias=False)

        self.features_dense_layer = nn.Linear(n_factors, n_factors, bias=False)
        self.output_layer = nn.Linear(n_factors, n_factors, bias=False)
        self.activation = activation

    def forward(self, user_features: torch.Tensor) -> torch.Tensor:
        features_emb = self.activation(self.embedding_features_layer(user_features))
        features_dense = self.activation(self.features_dense_layer(features_emb))
        features_x = features_emb + features_dense

        output = self.output_layer(features_x)
        return output


class DSSMNoInteractions(LightningModule):
    def __init__(
        self,
        n_factors_user: int,
        n_factors_item: int,
        dim_input_user: int,
        dim_input_item: int,
        activation: tp.Callable[[torch.Tensor], torch.Tensor] = F.elu,
        lr: float = 0.01,
        triplet_loss_margin: float = 0.4,
        weight_decay: float = 1e-6,
        log_to_prog_bar: bool = True,
    ) -> None:
        super().__init__()
        self.user_net = UserNet(n_factors_user, dim_input_user, activation)
        self.item_net = ItemNet(n_factors_item, dim_input_item, activation)
        self.lr = lr
        self.triplet_loss_margin = triplet_loss_margin
        self.weight_decay = weight_decay
        self.log_to_prog_bar = log_to_prog_bar

    def forward(  # type: ignore
        self,
        item_features_pos: torch.Tensor,
        item_features_neg: torch.Tensor,
        user_features: torch.Tensor,
    ) -> tp.Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        anchor = self.user_net(user_features)
        pos = self.item_net(item_features_pos)
        neg = self.item_net(item_features_neg)

        return anchor, pos, neg

    def configure_optimizers(self) -> torch.optim.Adam:
        """Choose what optimizers and learning-rate schedulers to use in optimization"""
        optimizer = torch.optim.Adam(self.parameters(), lr=self.lr, weight_decay=self.weight_decay)
        return optimizer

    def training_step(self, batch: tp.Sequence[torch.Tensor], batch_idx: int) -> torch.Tensor:  # type: ignore
        """Compute and return the training loss"""
        user_features, pos, neg = batch
        anchor, positive, negative = self(pos, neg, user_features)
        loss = F.triplet_margin_loss(anchor, positive, negative, margin=self.triplet_loss_margin)
        self.log("loss", loss.item(), prog_bar=self.log_to_prog_bar)
        return loss

    def validation_step(self, batch: tp.Sequence[torch.Tensor], batch_idx: int) -> torch.Tensor:  # type: ignore
        user_features, pos, neg = batch
        anchor, positive, negative = self(pos, neg, user_features)
        val_loss = F.triplet_margin_loss(anchor, positive, negative, margin=self.triplet_loss_margin)
        self.log("val_loss", val_loss.item(), prog_bar=self.log_to_prog_bar)
        return val_loss

    def inference_items(self, dataloader: DataLoader[tp.Any]) -> np.ndarray:
        batches = []
        self.eval()
        for batch in dataloader:
            item_features = batch
            with torch.no_grad():
                v_batch = self.item_net(item_features.to(self.device))
            batches.append(v_batch)
        vectors = torch.cat(batches, dim=0).cpu().numpy()
        return vectors

    def inference_users(self, dataloader: DataLoader[tp.Any]) -> np.ndarray:
        batches = []
        self.eval()
        for batch in dataloader:
            user_features = batch
            with torch.no_grad():
                v_batch = self.user_net(user_features.to(self.device))
            batches.append(v_batch)
        vectors = torch.cat(batches, dim=0).cpu().numpy()
        return vectors


class DSSMModelNoInteractions(DSSMModel):

    def _fit(self, dataset: Dataset, dataset_valid: tp.Optional[Dataset] = None) -> None:  # type: ignore
        self.trainer = deepcopy(self._trainer)
        self.model = deepcopy(self._model)

        if self.model is None:
            self.model = DSSMNoInteractions(
                n_factors_user=128,
                n_factors_item=128,
                dim_input_user=dataset.user_features.get_sparse().shape[1],  # type: ignore
                dim_input_item=dataset.item_features.get_sparse().shape[1],  # type: ignore
            )
        train_dataset = self.dataset_type.from_dataset(dataset)  # type: ignore
        train_dataloader = DataLoader(
            train_dataset,
            batch_size=self.batch_size,
            num_workers=self.dataloader_num_workers,
            shuffle=True,
        )
        if dataset_valid is not None:
            valid_dataset = self.dataset_type.from_dataset(dataset_valid)  # type: ignore
            valid_dataloader = DataLoader(
                valid_dataset,
                batch_size=self.batch_size,
                num_workers=self.dataloader_num_workers,
                shuffle=False,
            )
            self.trainer.fit(
                model=self.model,
                train_dataloaders=train_dataloader,
                val_dataloaders=valid_dataloader,
            )
        else:
            self.trainer.fit(model=self.model, train_dataloaders=train_dataloader)

    def _get_users_factors(self, dataset: Dataset) -> Factors:
        dataloader = DataLoader(
            UserFeaturesDatasetNoInteractions.from_dataset(dataset),
            batch_size=self.batch_size,
            num_workers=self.dataloader_num_workers,
            shuffle=False,
        )
        vectors = self.model.inference_users(dataloader)  # type: ignore
        return Factors(vectors)

    def _get_items_factors(self, dataset: Dataset) -> Factors:
        dataloader = DataLoader(
            ItemFeaturesDataset.from_dataset(dataset),
            batch_size=self.batch_size,
            num_workers=self.dataloader_num_workers,
            shuffle=False,
        )
        vectors = self.model.inference_items(dataloader)  # type: ignore
        return Factors(vectors)