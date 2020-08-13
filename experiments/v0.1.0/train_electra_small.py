import os

import fire
from pytorch_lightning import Trainer
from tokenizers import BertWordPieceTokenizer
from torch.utils.data import DataLoader
from transformers import (BertConfig, BertForMaskedLM,
                          BertForTokenClassification)

from transformers_trainers import DiscLMTrainingModule, DiscLMTrainingModuleConfig
from transformers_trainers.datasets import PreTokenizedCollater, create_pretokenized_dataset
from transformers_trainers.utils import tie_weights


def main(tokenizer_path,
         dataset_path,
         save_path='electra-small',
         max_steps=1e6,
         accumulate_grad_batches=1,
         gpus=None,
         num_tpu_cores=None,
         distributed_backend=None,
         val_check_interval=0.25,
         mlm_prob=0.15,
         learning_rate=5e-4,
         warmup_steps=10000,
         batch_size=128,
         num_workers=2,
         resume_from_checkpoint=None,
         shuffle=True,
         use_polyaxon=False):
    # init tokenizer.  only need it for the special chars.
    tokenizer = BertWordPieceTokenizer(tokenizer_path)

    # init generator.
    generator_config = BertConfig(
        vocab_size=tokenizer._tokenizer.get_vocab_size(),
        hidden_size=256,
        num_hidden_layers=3,
        num_attention_heads=1,
        intermediate_size=256,
        max_position_embeddings=128)
    generator = BertForMaskedLM(generator_config)

    # init discriminator.
    discriminator_config = BertConfig(
        vocab_size=tokenizer._tokenizer.get_vocab_size(),
        hidden_size=256,
        num_hidden_layers=12,
        num_attention_heads=4,
        intermediate_size=1024,
        max_position_embeddings=128)
    discriminator = BertForTokenClassification(discriminator_config)

    # tie the embeddingg weights.
    tie_weights(generator.cls.predictions.decoder, generator.bert.embeddings.word_embeddings)
    tie_weights(discriminator.bert.embeddings.word_embeddings,
                generator.bert.embeddings.word_embeddings)
    tie_weights(discriminator.bert.embeddings.position_embeddings,
                generator.bert.embeddings.position_embeddings)
    tie_weights(discriminator.bert.embeddings.token_type_embeddings,
                generator.bert.embeddings.token_type_embeddings)

    # init training module.
    training_config = DiscLMTrainingModuleConfig(max_steps,
                                                 save_path=save_path,
                                                 weight_decay=0.01,
                                                 learning_rate=learning_rate,
                                                 epsilon=1e-6,
                                                 warmup_steps=warmup_steps)
    if use_polyaxon:
        checkpoint_fn = polyaxon_checkpoint_fn
    else:
        checkpoint_fn = None
    lightning_module = DiscLMTrainingModule(generator,
                                            discriminator,
                                            training_config,
                                            checkpoint_fn=checkpoint_fn)

    # init trainer.
    trainer = Trainer(accumulate_grad_batches=accumulate_grad_batches,
                      gpus=gpus,
                      num_tpu_cores=num_tpu_cores,
                      distributed_backend=distributed_backend,
                      max_steps=max_steps,
                      resume_from_checkpoint=resume_from_checkpoint,
                      val_check_interval=val_check_interval)

    # init dataloaders.
    train_loader, val_loader, _ = get_dataloaders(tokenizer, dataset_path,
                                                  trainer, mlm_prob,
                                                  batch_size, num_workers,
                                                  shuffle)

    # train.
    trainer.fit(lightning_module, train_loader, val_loader)

    # save the model.
    output_path = os.path.join(save_path, 'discriminator', 'final')
    os.makedirs(output_path, exist_ok=True)
    lightning_module.discriminator.base_model.save_pretrained(output_path)
    if checkpoint_fn:
        checkpoint_fn(lightning_module)


def polyaxon_checkpoint_fn(lightning_module):
    from polyaxon_client.tracking import Experiment
    exp = Experiment()
    if os.path.exists(lightning_module.config.save_path):
        exp.outputs_store.upload_dir(lightning_module.config.save_path)
    exp.outputs_store.upload_dir('lightning_logs')


def get_dataloaders(tokenizer, dataset_path, trainer, mlm_prob, batch_size,
                    num_workers, shuffle):
    def get_dataloader(path):
        paths = [os.path.join(path, name) for name in os.listdir(path)]
        dataset = create_pretokenized_dataset(paths)

        collater = PreTokenizedCollater(
            mlm=True,
            mlm_prob=mlm_prob,
            pad_token_id=tokenizer.token_to_id("[PAD]"),
            mask_token_id=tokenizer.token_to_id("[MASK]"),
            vocab_size=tokenizer._tokenizer.get_vocab_size(),
            rand_replace=False)

        return DataLoader(dataset,
                          batch_size=batch_size,
                          num_workers=num_workers,
                          collate_fn=collater,
                          shuffle=shuffle)

    train_loader = get_dataloader(os.path.join(dataset_path, 'train'))
    val_loader = get_dataloader(os.path.join(dataset_path, 'val'))
    test_loader = get_dataloader(os.path.join(dataset_path, 'test'))
    return train_loader, val_loader, test_loader


if __name__ == '__main__':
    fire.Fire(main)
