import math
import logging
import os
import torch
from torch.utils.data import DataLoader
from torch.optim import AdamW

import pytorch_lightning as pl
from pytorch_lightning.callbacks.early_stopping import EarlyStopping
from pytorch_lightning.callbacks import ModelSummary
from transformers import (
    MT5ForConditionalGeneration, T5ForConditionalGeneration,
    AutoConfig, AutoModel, AutoTokenizer,
    get_linear_schedule_with_warmup
)

from train_common import parse_hparams, load_TrainTestDataSet


def get_transform(hparams):
    def transform_t5(src, tgt):
        inputs = hparams.tokenizer.batch_encode_plus(
            [src],
            max_length=hparams.max_seq_length,
            truncation=True,
            pad_to_max_length=True,
            padding="max_length", return_tensors="pt")
        targets = hparams.tokenizer.batch_encode_plus(
            [tgt],
            max_length=hparams.target_max_seq_length,
            truncation=True,
            pad_to_max_length=True,
            padding="max_length", return_tensors="pt")

        source_ids = inputs["input_ids"].squeeze()
        source_mask = inputs["attention_mask"].squeeze()

        target_ids = targets["input_ids"].squeeze()
        target_mask = targets["attention_mask"].squeeze()

        return {
            "source_ids": source_ids.to(dtype=torch.long),
            "source_mask": source_mask.to(dtype=torch.long),
            "target_ids": target_ids.to(dtype=torch.long),
            "target_mask": target_mask.to(dtype=torch.long),
        }
    return transform_t5


# GPU利用有無
USE_GPU = torch.cuda.is_available()
N_GPU = torch.cuda.device_count()


class MT5FineTuner(pl.LightningModule):
    def __init__(self, hparams):
        super().__init__()
        self.save_hyperparameters(hparams)

        print(self.hparams.tokenizer.vocab_size, self.hparams.vocab_size)

        # # 事前学習済みモデルの読み込み
        # config = AutoConfig.from_pretrained(
        #     self.hparams.model_name_or_path)
        # config.vocab_size = max(config.vocab_size,
        #                         self.hparams.tokenizer.vocab_size,
        #                         self.hparams.vocab_size)
        # if '/mt5' in self.hparams.model_name_or_path:
        #     self.model = MT5ForConditionalGeneration(config)
        # else:
        #     self.model = MT5ForConditionalGeneration(config)
        if 'mt5' in self.hparams.model_name_or_path:
            self.model = MT5ForConditionalGeneration.from_pretrained(
                self.hparams.model_name_or_path)
        else:
            self.model = T5ForConditionalGeneration.from_pretrained(
                self.hparams.model_name_or_path)
        self.tokenizer = self.hparams.tokenizer
        print(self.model.config)
        print('vocab_size', self.hparams.tokenizer.vocab_size,
              self.model.config.vocab_size, self.hparams.vocab_size)
        self.train_dataset = None
        # self.nsteps_ = -1
        # self.nepochs_ = -1

    def forward(self, input_ids, attention_mask=None, decoder_input_ids=None,
                decoder_attention_mask=None, labels=None):
        """順伝搬"""
        return self.model(
            input_ids,
            attention_mask=attention_mask,
            decoder_input_ids=decoder_input_ids,
            decoder_attention_mask=decoder_attention_mask,
            labels=labels
        )

    def _step(self, batch):
        """ロス計算"""
        # input_ids = batch["source_ids"]
        # attention_mask = batch["source_mask"]
        # decoder_attention_mask = batch['target_mask']
        labels = batch["target_ids"]
        # All labels set to -100 are ignored (masked),
        # the loss is only computed for labels in [0, ..., config.vocab_size]
        labels[labels[:, :] == self.tokenizer.pad_token_id] = -100

        # (torch.LongTensor of ) — Indices of input sequence tokens in the vocabulary.
        outputs = self(
            input_ids=batch["source_ids"],
            attention_mask=batch["source_mask"],
            decoder_attention_mask=batch['target_mask'],
            labels=labels
        )
        loss = outputs[0]
        return loss

    def training_step(self, batch, batch_idx):
        """訓練ステップ処理"""
        loss = self._step(batch)
        # if self.nsteps_ >= 0:
        #     self.nsteps_ += 1
        return {"loss": loss}

    def training_epoch_end(self, outputs):
        # """訓練完了処理"""
        # print(self.epoch_, outputs)
        loss = torch.stack([x["loss"] for x in outputs]).mean()
        self.log("train_loss", loss, prog_bar=self.hparams.progress_bar)
        # if self.nepochs_ >= 0:
        #     self.nepochs_ += 1
        if not self.hparams.progress_bar:
            print(
                f'Epoch {self.current_epoch} train_loss {loss} train_PPL {math.exp(loss)}')
        # if self.hparams.save_checkpoint and self.nepochs_ > 1:
        #     output_dir = f'{self.hparams.output_dir}.{self.nepochs_}'
        #     print(f'saving checkpoint model to {output_dir}')
        #     if not os.path.isdir(output_dir):
        #         os.makedirs(output_dir)
        #     self.tokenizer.save_pretrained(output_dir)
        #     self.model.save_pretrained(output_dir)

    def validation_step(self, batch, batch_idx):
        """バリデーションステップ処理"""
        loss = self._step(batch)
        return {"val_loss": loss}

    def validation_epoch_end(self, outputs):
        # """バリデーション完了処理"""
        #print(self.epoch_, outputs)
        avg_loss = torch.stack([x["val_loss"] for x in outputs]).mean()
        self.log("val_loss", avg_loss, prog_bar=self.hparams.progress_bar)
        if not self.hparams.progress_bar:
            print(
                f'Epoch {self.current_epoch} val_loss {avg_loss} val_PPL {math.exp(avg_loss)}')
        # self.dataset.split()

    # def test_step(self, batch, batch_idx):
    #     """テストステップ処理"""
    #     loss = self._step(batch)
    #     self.log("test_loss", loss)
    #     return {"test_loss": loss}

    # def test_epoch_end(self, outputs):
    #     """テスト完了処理"""
    #     avg_loss = torch.stack([x["test_loss"] for x in outputs]).mean()
    #     self.log("test_loss", avg_loss, prog_bar=True)

    def configure_optimizers(self):
        """オプティマイザーとスケジューラーを作成する"""
        model = self.model
        no_decay = ["bias", "LayerNorm.weight"]
        optimizer_grouped_parameters = [
            {
                "params": [p for n, p in model.named_parameters()
                           if not any(nd in n for nd in no_decay)],
                "weight_decay": self.hparams.weight_decay,
            },
            {
                "params": [p for n, p in model.named_parameters()
                           if any(nd in n for nd in no_decay)],
                "weight_decay": 0.0,
            },
        ]
        optimizer = AdamW(optimizer_grouped_parameters,
                          lr=self.hparams.learning_rate,
                          eps=self.hparams.adam_epsilon)
        scheduler = get_linear_schedule_with_warmup(
            optimizer,
            num_warmup_steps=self.hparams.warmup_steps,
            num_training_steps=self.t_total
        )
        return [optimizer], [{
            "scheduler": scheduler,
            "interval": "step",
            "frequency": 1
        }]

    # def get_dataset(self):
    #     """データセットを作成する"""
    #     return KFoldDataset(DADataset(self.hparams))

    def setup(self, stage=None):
        """初期設定（データセットの読み込み）"""
        if stage == 'fit' or stage is None:
            if self.train_dataset is None:
                self.train_dataset, self.valid_dataset = load_TrainTestDataSet(
                    self.hparams, transform=get_transform(self.hparams))
            self.t_total = (
                (len(self.train_dataset) //
                 (self.hparams.batch_size * max(1, self.hparams.n_gpu)))
                // self.hparams.gradient_accumulation_steps
                * float(self.hparams.max_epochs)
            )

    def train_dataloader(self):
        """訓練データローダーを作成する"""
        #logging.info('loading train data loader')
        return DataLoader(self.train_dataset,
                          batch_size=self.hparams.batch_size,
                          drop_last=True, shuffle=True,
                          num_workers=self.hparams.num_workers)

    def val_dataloader(self):
        """バリデーションデータローダーを作成する"""
        return DataLoader(self.valid_dataset,
                          batch_size=self.hparams.batch_size,
                          num_workers=self.hparams.num_workers)


def make_generate(model, tokenizer):
    def greedy_search(s: str, max_length=128) -> str:
        input_ids = tokenizer.encode_plus(
            s,
            add_special_tokens=True,
            max_length=max_length,
            padding="do_not_pad",
            truncation=True,
            return_tensors='pt').input_ids.to(model.device)
        greedy_output = model.generate(input_ids, max_length=max_length)
        return tokenizer.decode(greedy_output[0], skip_special_tokens=True)
    return greedy_search


def _main():
    init_dict = dict(
        model_name_or_path='',
        tokenizer_name_or_path='',
        additional_tokens='<nl> <tab> <b> </b>',
        seed=42,
        encoding='utf_8',
        column=0, target_column=1,
        kfold=5,  # cross validation
        max_seq_length=128,
        target_max_seq_length=128,
        # da
        da_choice=0.4, da_shuffle=0.3, bos_token='',
        # unsupervised training option
        masking=False,
        masking_ratio=0.35,
        masking_style='denoising',
        # training
        learning_rate=3e-4,
        weight_decay=0.0,
        adam_epsilon=1e-8,
        warmup_steps=0,
        batch_size=8,
        num_workers=2,  # os.cpu_count(),
        # train_batch_size=8,
        save_checkpoint=False,
        progress_bar=False,
        # eval_batch_size=8,
        max_epochs=50,
        limit_batches=-1,
        gradient_accumulation_steps=1,  # 16
        n_gpu=1 if USE_GPU else 0,
        early_stop_callback=True,
        # if you want to enable 16-bit training then install apex and set this to true
        fp_16=False,
        opt_level='O2',
        max_grad_norm=1.0,
    )
    hparams = parse_hparams(init_dict, Tokenizer=AutoTokenizer)
    print(hparams)

    # logging.info(f'Start trainig: {hparams.start_date}')
    logging.info(f'Base model: {hparams.model_name_or_path} {hparams.files}')

    train_params = dict(
        enable_model_summary=True,
        accumulate_grad_batches=hparams.gradient_accumulation_steps,
        gpus=hparams.n_gpu,
        max_epochs=hparams.max_epochs,
        # early_stop_callback=False,
        precision=16 if hparams.fp_16 else 32,
        # amp_level=hparams.opt_level,
        gradient_clip_val=hparams.max_grad_norm,
        #    checkpoint_callback=checkpoint_callback,
        # callbacks=[LoggingCallback()],
        callbacks=[
            EarlyStopping(monitor="val_loss"),
            ModelSummary(max_depth=-1)
        ],
        # turn off automatic checkpointing
        enable_checkpointing=True,
        enable_progress_bar=hparams.progress_bar,
        # run batch size scaling, result overrides hparams.batch_size
        auto_scale_batch_size="binsearch" if hparams.batch_size <= 2 else None,
        # run learning rate finder, results override hparams.learning_rate
        # auto_lr_find=True,
        devices="auto", accelerator="auto",
        limit_train_batches=1.0 if hparams.limit_batches == -1 else hparams.limit_batches,
        limit_val_batches=1.0 if hparams.limit_batches == -1 else hparams.limit_batches//4,
    )

    model = MT5FineTuner(hparams)
    trainer = pl.Trainer(**train_params)
    trainer.tune(model)
    print(f'Start training: max {hparams.max_epochs} epochs')
    trainer.fit(model)

    # 最終エポックのモデルを保存
    tokenizer = model.tokenizer
    model = model.model
    print('saving pretrained ... ', hparams.output_dir)
    tokenizer.save_pretrained(hparams.output_dir)
    model.save_pretrained(hparams.output_dir)

    DEVICE = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
    model.to(DEVICE)
    train_dataset, valid_dataset = load_TrainTestDataSet(hparams)
    print('testing ... ', model.device)
    generate = make_generate(model, tokenizer)
    valid_dataset.test_and_save(
        generate, f'{hparams.output_dir}/result_test.tsv', max=1000)


if __name__ == '__main__':
    _main()
