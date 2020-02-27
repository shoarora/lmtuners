"""Pytorch lightning module for Discriminative LM task from ELECTRA.

https://openreview.net/forum?id=r1xMH1BtvB
"""
import logging
import os
from argparse import Namespace

import numpy as np

import pytorch_lightning as pl
import torch
from pytorch_lamb import Lamb
from transformers import get_linear_schedule_with_warmup

logger = logging.getLogger(__name__)


class DiscLMTrainingModuleConfig(Namespace):
    """Config class for DiscLMTrainingModule."""
    def __init__(self,
                 num_steps,
                 d_loss_weight=50,
                 save_path=None,
                 weight_decay=0.0,
                 learning_rate=5e-5,
                 epsilon=1e-8,
                 warmup_steps=0):
        super().__init__(d_loss_weight=d_loss_weight,
                         num_steps=num_steps,
                         save_path=save_path,
                         weight_decay=weight_decay,
                         learning_rate=learning_rate,
                         epsilon=epsilon,
                         warmup_steps=warmup_steps)


class DiscLMTrainingModule(pl.LightningModule):
    def __init__(self,
                 generator,
                 discriminator,
                 config,
                 checkpoint_fn=None,
                 ddp_fn=None):
        super().__init__()

        self.config = config
        self.hparams = config
        self.checkpoint_fn = checkpoint_fn
        self.ddp_fn = ddp_fn
        if ddp_fn:
            logger.warning('ddp_fn functionality is not implemented yet.')

        print('set hparams:', self.hparams)

        self.vocab_size = generator.config.vocab_size

        self.generator = generator
        self.discriminator = discriminator

    def forward(self, inputs, labels, attention_mask):
        # copy the variables for use with discriminator.
        d_inputs, d_labels = inputs.clone(), labels.clone()

        # run masked LM.
        g_out = self.generator(inputs,
                               masked_lm_labels=labels,
                               attention_mask=attention_mask)

        # get samples from masked LM.
        sample_probs = torch.softmax(g_out[1], dim=-1, dtype=torch.float32)
        sample_probs = sample_probs.view(-1, self.vocab_size)

        sampled_tokens = torch.multinomial(sample_probs, 1).view(-1)
        sampled_tokens = sampled_tokens.view(d_inputs.shape[0], -1)

        # labels have a -100 value to mask out loss from unchanged tokens.
        mask = labels.eq(-100)

        # replace the masked out tokens of the input with the generator predictions.
        d_inputs[mask] = sampled_tokens[mask]

        # turn mask into new target labels.  1 (True) for corrupted, 0 otherwise.
        # if the prediction was correct, mark it as uncorrupted.
        correct_preds = sampled_tokens == labels
        d_labels[correct_preds] = False
        d_labels = mask.long()

        # run token classification, predict whether each token was corrupted.
        d_out = self.discriminator(d_inputs,
                                   labels=d_labels,
                                   attention_mask=attention_mask)

        g_loss = g_out[0]
        d_loss = d_out[0]
        d_scores = d_out[1]
        return g_loss, d_loss, d_scores, d_labels

    def training_step(self, batch, batch_idx):
        inputs, labels, attention_mask = batch
        g_loss, d_loss, d_scores, d_labels = self.forward(
            inputs, labels, attention_mask)

        preds = torch.argmax(d_scores, dim=-1)
        acc = torch.sum(preds == d_labels) / np.prod(d_labels.shape)

        # weight the discriminator loss.
        total_loss = g_loss + (self.config.d_loss_weight * d_loss)

        self._log_and_step_lr()

        tensorboard_logs = {
            'train/loss': total_loss,
            'train/d_loss': d_loss,
            'train/g_loss': g_loss,
            'train/d_acc': acc
        }
        return {'loss': total_loss, 'log': tensorboard_logs}

    def validation_step(self, batch, batch_idx):
        inputs, labels, attention_mask = batch
        g_loss, d_loss, d_scores, d_labels = self.forward(
            inputs, labels, attention_mask)

        preds = torch.argmax(d_scores, dim=-1)
        acc = torch.sum(preds == d_labels) / np.prod(d_labels.shape)

        # weight the discriminator loss.
        total_loss = g_loss + (self.config.d_loss_weight * d_loss)
        return {
            'val_loss': total_loss,
            'val_d_loss': d_loss,
            'val_g_loss': g_loss,
            'val_d_acc': acc
        }

    def validation_end(self, outputs):
        avg_loss = torch.stack([x['val_loss'] for x in outputs]).mean()
        avg_d_loss = torch.stack([x['val_d_loss'] for x in outputs]).mean()
        avg_g_loss = torch.stack([x['val_g_loss'] for x in outputs]).mean()
        avg_d_acc = torch.stack([x['val_d_acc'] for x in outputs]).mean()

        perplexity = torch.exp(avg_g_loss)

        self._save_model(self.generator.base_model, 'generator')
        self._save_model(self.discriminator.base_model, 'discriminator')

        output_dir = os.path.join(self.config.save_path,
                                  f"{self.current_epoch}-{self.global_step}")
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        if self.checkpoint_fn:
            self.checkpoint_fn(self)

        tensorboard_logs = {
            'val_loss': avg_loss,
            'val/loss': avg_loss,
            'val/d_loss': avg_d_loss,
            'val/g_loss': avg_g_loss,
            'val/perplexity': perplexity,
            'val/d_acc': avg_d_acc
        }
        return {'avg_val_loss': avg_loss, 'log': tensorboard_logs}

    def _save_model(self, model, name):
        output_dir = os.path.join(self.config.save_path, name,
                                  f"{self.current_epoch}-{self.global_step}")
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        model_to_save = (model.module if hasattr(model, "module") else model)
        model_to_save.save_pretrained(output_dir)

    def configure_optimizers(self):
        no_decay = ["bias", "LayerNorm.weight"]
        optimizer_grouped_parameters = [
            {
                "params": [
                    p for n, p in self.generator.named_parameters()
                    if not any(nd in n for nd in no_decay)
                ] + [
                    p for n, p in self.discriminator.named_parameters()
                    if not any(nd in n for nd in no_decay)
                ],
                "weight_decay":
                self.config.weight_decay,
            },
            {
                "params": [
                    p for n, p in self.generator.named_parameters()
                    if any(nd in n for nd in no_decay)
                ] + [
                    p for n, p in self.discriminator.named_parameters()
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

        return [optimizer], [scheduler]

    def _log_and_step_lr(self):
        """Logs learning rate to tensorboard.
        """
        # get LR schedulers from the pytorch-lightning trainer object.
        scheduler = self.trainer.lr_schedulers[0]

        # tie LR stepping to global step.
        scheduler.step(epoch=self.global_step)
        for i, lr in enumerate(scheduler.get_lr()):
            # add the scalar to the test_tube Experiment object.
            self.logger.experiment.add_scalar(f'lr_{i}', lr, self.global_step)
