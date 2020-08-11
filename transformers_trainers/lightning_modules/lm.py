"""Pytorch lightning module for language modelling."""
import logging
import os
from argparse import Namespace

import pytorch_lightning as pl
import torch
from pytorch_lamb import Lamb
from transformers import get_linear_schedule_with_warmup

logger = logging.getLogger(__name__)


class LMTrainingModuleConfig(Namespace):
    """Config class LMTrainingModule."""
    def __init__(
        self,
        num_steps,
        mlm=True,
        save_path=None,
        weight_decay=0.0,
        learning_rate=5e-5,
        epsilon=1e-8,
        warmup_steps=0,
        save_on_val=False,
    ):
        super().__init__(num_steps=num_steps,
                         mlm=mlm,
                         save_path=save_path,
                         weight_decay=weight_decay,
                         learning_rate=learning_rate,
                         epsilon=epsilon,
                         warmup_steps=warmup_steps,
                         save_on_val=save_on_val)


class LMTrainingModule(pl.LightningModule):
    def __init__(self, model, config, checkpoint_fn=None):
        super().__init__()
        self.config = config
        self.hparams = config
        self.checkpoint_fn = checkpoint_fn

        self.vocab_size = model.config.vocab_size

        self.model = model

    def forward(self, inputs, labels, attention_mask, token_type_ids):
        if self.config.mlm:
            outputs = self.model(inputs,
                                 masked_lm_labels=labels,
                                 attention_mask=attention_mask,
                                 token_type_ids=token_type_ids)
        else:
            outputs = self.model(inputs,
                                 labels=labels,
                                 attention_mask=attention_mask,
                                 token_type_ids=token_type_ids)
        return outputs

    def training_step(self, batch, batch_idx):
        inputs, labels, attention_mask, token_type_ids = batch
        outputs = self.forward(inputs, labels, attention_mask, token_type_ids)
        loss = outputs[0]
        perplexity = torch.exp(loss)

        preds = torch.argmax(outputs[1], dim=-1)
        correct_preds = (preds == labels)[labels.ne(-100)]
        acc = torch.sum(correct_preds).float() / correct_preds.numel()

        self._log_lr()
        tensorboard_logs = {
            'train/loss': loss,
            'train/perplexity': perplexity,
            'train/acc': acc
        }
        return {'loss': loss, 'log': tensorboard_logs}

    def validation_step(self, batch, batch_idx):
        inputs, labels, attention_mask, token_type_ids = batch
        outputs = self.forward(inputs, labels, attention_mask, token_type_ids)
        loss = outputs[0]

        preds = torch.argmax(outputs[1], dim=-1)
        correct_preds = (preds == labels)[labels.ne(-100)]
        acc = torch.sum(correct_preds).float() / correct_preds.numel()

        return {'val_loss': loss, 'val_acc': acc}

    def validation_end(self, outputs):
        avg_loss = torch.stack([x['val_loss'] for x in outputs]).mean()
        avg_acc = torch.stack([x['val_acc'] for x in outputs]).mean()

        perplexity = torch.exp(avg_loss)

        if self.trainer.proc_rank == 0:
            if self.config.save_on_val:
                output_dir = os.path.join(
                    self.config.save_path,
                    f"{self.current_epoch}-{self.global_step}")
                if not os.path.exists(output_dir):
                    os.makedirs(output_dir)
                model_to_save = (self.model.module if hasattr(
                    self.model, "module") else self.model)
                model_to_save.base_model.save_pretrained(output_dir)

            if self.checkpoint_fn:
                self.checkpoint_fn(self)

        tensorboard_logs = {
            'val_loss': avg_loss,
            'val/loss': avg_loss,
            'val/acc': avg_acc,
            'val/perplexity': perplexity
        }
        return {'avg_val_loss': avg_loss, 'log': tensorboard_logs}

    def configure_optimizers(self):
        no_decay = ["bias", "LayerNorm.weight"]
        optimizer_grouped_parameters = [
            {
                "params": [
                    p for n, p in self.model.named_parameters()
                    if not any(nd in n for nd in no_decay)
                ],
                "weight_decay":
                self.config.weight_decay,
            },
            {
                "params": [
                    p for n, p in self.model.named_parameters()
                    if any(nd in n for nd in no_decay)
                ],
                "weight_decay":
                0.0
            },
        ]

        t_total = self.config.num_steps

        optimizer = Lamb(optimizer_grouped_parameters,
                         lr=self.config.learning_rate,
                         eps=self.config.epsilon)
        scheduler = get_linear_schedule_with_warmup(
            optimizer,
            num_warmup_steps=self.config.warmup_steps,
            num_training_steps=t_total)

        scheduler_config = {'scheduler': scheduler, 'interval': 'step'}

        return [optimizer], [scheduler_config]

    def _log_lr(self):
        """Logs learning rate to tensorboard.
        """
        # get LR schedulers from the pytorch-lightning trainer object.
        scheduler = self.trainer.lr_schedulers[0]['scheduler']

        # tie LR stepping to global step.
        for i, lr in enumerate(scheduler.get_lr()):
            # add the scalar to the Experiment object.
            self.logger.experiment.add_scalar(f'lr_{i}', lr, self.global_step)
