import argparse
import os
import pickle

import matplotlib.pyplot as plt
import time, sys
import numpy as np
import torch
from torch import optim
import torch.nn as nn
import torch.nn.functional
from torch.utils.data import DataLoader, Dataset

import infolog
from hparams import hparams

from wavernn_vocoder.wavernn import Model

log = infolog.log

pp = 0
class AudiobookDataset(Dataset):
    def __init__(self, ids, path):
        self.path = path
        self.metadata = ids
       
    def __getitem__(self, index):
        file = self.metadata[index]
        m = np.load('{}/mels/{}.npy'.format(self.path ,file))
        x = np.load('{}/quant/{}.npy'.format(self.path, file))
        return m, x

    def __len__(self):
        return len(self.metadata)

class CustomCollator():
    def __init__(self, hparams):
        self.bits = hparams.wavernn_bits
        self.pad = hparams.wavernn_pad
        self.hop_size = hparams.hop_size

    def __call__(self, batch):
        mel_win = 5 + 2 * self.pad
        seq_len = self.hop_size * 5

        max_offsets = [x[0].shape[-1] - (mel_win + 2 * self.pad) for x in batch]
        mel_offsets = [np.random.randint(0, offset) for offset in max_offsets]
        sig_offsets = [(offset + self.pad) * self.hop_size for offset in mel_offsets]
    
        mels = [x[0][:, mel_offsets[i]:mel_offsets[i] + mel_win] \
            for i, x in enumerate(batch)]
    
        coarse = [x[1][sig_offsets[i]:sig_offsets[i] + seq_len + 1] \
              for i, x in enumerate(batch)]
    
        global pp
        pp += 1
        print(pp)
        mels = np.stack(mels).astype(np.float32)
        coarse = np.stack(coarse).astype(np.int64)
    
        mels = torch.FloatTensor(mels)
        coarse = torch.LongTensor(coarse)
    
        x_input = 2 * coarse[:, :seq_len].float() / (2**self.bits - 1.) - 1.
    
        y_coarse = coarse[:, 1:]

        return x_input, mels, y_coarse

def train(log_dir, args, hparams, input_path) :
    save_dir = os.path.join(log_dir, 'wave_pretrained')
    # plot_dir = os.path.join(log_dir, 'plots')
    wav_dir = os.path.join(log_dir, 'wavs')
    eval_dir = os.path.join(log_dir, 'eval-dir')
    # eval_plot_dir = os.path.join(eval_dir, 'plots')
    eval_wav_dir = os.path.join(eval_dir, 'wavs')
    # tensorboard_dir = os.path.join(log_dir, 'wavenet_events')
    # meta_folder = os.path.join(log_dir, 'metas')
    os.makedirs(save_dir, exist_ok=True)
    # os.makedirs(plot_dir, exist_ok=True)
    os.makedirs(wav_dir, exist_ok=True)
    os.makedirs(eval_dir, exist_ok=True)
    # os.makedirs(eval_plot_dir, exist_ok=True)
    os.makedirs(eval_wav_dir, exist_ok=True)
    # os.makedirs(tensorboard_dir, exist_ok=True)
    # os.makedirs(meta_folder, exist_ok=True)

    checkpoint_path = os.path.join(save_dir, 'wavernn_model.pyt')
    input_path = os.path.join(args.base_dir, input_path)

    log('Checkpoint_path: {}'.format(checkpoint_path))
    log('Loading training data from: {}'.format(input_path))
    log('Using model: {}'.format(args.model))
#    log(hparams_debug_string())

    device = torch.device('cuda' if args.use_cuda else 'cpu')

    # Load Dataset
    with open('{}/dataset_ids.pkl'.format(input_path), 'rb') as f:
        dataset = AudiobookDataset(pickle.load(f), input_path)

    collate = CustomCollator(hparams)
    batch_size = hparams.wavernn_batch_size * hparams.wavernn_num_gpus
    trn_loader = DataLoader(dataset, collate_fn=collate, batch_size=batch_size, shuffle=True, pin_memory=args.use_cuda)

    # Initialize Model
    model = Model(rnn_dims=hparams.rnn_dims, fc_dims=hparams.fc_dims, bits=hparams.wavernn_bits, pad=hparams.wavernn_pad, upsample_factors = hparams.upsample_scales,\
                 feat_dims=hparams.feat_dims, compute_dims=hparams.compute_dims, res_out_dims=hparams.res_out_dims, res_blocks=hparams.res_blocks,\
                 hop_length=hparams.hop_size, sample_rate=hparams.sample_rate).to(device)

    # Load Model
    if not os.path.exists(checkpoint_path):
        log('Created new model!!!', slack=True)
        torch.save({'state_dict': model.state_dict(), 'global_step': 0}, checkpoint_path)
    else:
        log('Loading model from {}'.format(checkpoint_path), slack=True)

    # Load Parameters
    if args.use_cuda:
        checkpoint = torch.load(checkpoint_path)
    else:
        checkpoint = torch.load(checkpoint_path, map_location=lambda storage, loc: storage)

    model.load_state_dict(checkpoint['state_dict'])

    if args.use_cuda:
        model = nn.DataParallel(model).to(device)

    step = checkpoint['global_step']
    log('Starting from {} step'.format(step), slack=True)

    optimiser = optim.Adam(model.parameters(), lr=hparams.wavernn_lr_rate)
    criterion = nn.NLLLoss().to(device)

    # for p in optimiser.param_groups : p['lr'] = lr
    
    for e in range(hparams.wavernn_epoch) :

    
        running_loss = 0.
        val_loss = 0.
        start = time.time()
        running_loss = 0.

        iters = len(trn_loader)

        for i, (x, m, y) in enumerate(trn_loader) :
            
            x, m, y = x.cuda(), m.cuda(), y.cuda()

            y_hat = model(x, m)
            y_hat = y_hat.transpose(1, 2).unsqueeze(-1)
            y = y.unsqueeze(-1)
            loss = criterion(y_hat, y)
            
            optimiser.zero_grad()
            loss.backward()
            optimiser.step()
            running_loss += loss.item()
            
            speed = (i + 1) / (time.time() - start)
            avg_loss = running_loss / (i + 1)
            
            step += 1
            k = step // 1000
            log('Epoch: {:d}/{:d} -- Batch: {:d}/{:d} -- Loss: {:.3f} -- {:.2f} steps/sec -- Step: {:d}k '.format(e + 1, hparams.wavernn_epoch, i + 1, iters, avg_loss, speed, k))

        if (e + 1) % 30 == 0:
            log('\nSaving model at step {}'.format(step), end='', slack=True)
            if args.use_cuda:
                torch.save({'state_dict': model.state_dict(), 'global_step': step}, checkpoint_path)
                # test_generate(model.module, step, test_dir, eval_wav_dir, hparams.sample_rate)
            else:
                torch.save({'state_dict': model.state_dict(), 'global_step': step}, checkpoint_path)
                # test_generate(model, step, test_dir, eval_wav_dir, hparams.sample_rate)

def wavernn_train(args, log_dir, hparams, input_path):
    input_dir = os.path.join(args.base_dir, 'wavernn_data')
    train(log_dir, args, hparams, input_dir)
    
        
