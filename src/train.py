"""
@author:    Patrik Purgai
@copyright: Copyright 2019, dialogue-generation
@license:   MIT
@email:     purgai.patrik@gmail.com
@date:      2019.07.12.
"""

# pylint: disable=import-error
# pylint: disable=no-name-in-module
# pylint: disable=no-member
# pylint: disable=not-callable
# pylint: disable=used-before-assignment

import torch
import argparse
import logging
import os

import numpy as np

from src.model import (
    compute_size,
    create_model,
    setup_model_args)

from src.data import (
    create_dataset,
    setup_data_args)

from tensorboardX import SummaryWriter
from collections import OrderedDict
from tqdm import tqdm
from math import ceil
from datetime import datetime
from statistics import mean
from functools import wraps

try:
    from apex import amp
    APEX_INSTALLED = True
except ImportError:
    APEX_INSTALLED = False

from torch.nn.functional import (
    cross_entropy, softmax,
    kl_div, log_softmax,
    nll_loss)

from torch.distributed import (
    all_reduce, reduce_op)

from torch.optim.lr_scheduler import LambdaLR
from torch.nn.utils import clip_grad_norm_
from torch.nn.parallel import (
    DistributedDataParallel)

from pytorch_transformers import (
    WarmupLinearSchedule, AdamW)

from os.path import (
    exists, join)


def setup_train_args(parser):
    """
    Sets up the training arguments.
    """
    group = parser.add_argument_group('train')
    group.add_argument(
        '--max_epochs',
        type=int,
        default=15,
        help='Maximum number of epochs for training.')
    group.add_argument(
        '--cuda',
        type=bool,
        default=torch.cuda.is_available(),
        help='Device for training.')
    # TODO XLNet produces NaN with apex
    group.add_argument(
        '--mixed',
        type=bool,
        default=APEX_INSTALLED,
        help='Use mixed precision training.')
    group.add_argument(
        '--learning_rate',
        type=float,
        default=1e-4,
        help='Learning rate for the model.')
    group.add_argument(
        '--batch_size',
        type=int,
        default=64,
        help='Batch size during training.')
    group.add_argument(
        '--patience',
        type=int,
        default=5,
        help='Number of patience epochs before termination.')
    group.add_argument(
        '--grad_accum_steps',
        type=int,
        default=2,
        help='Number of steps for grad accum.')
    group.add_argument(
        '--eval_every_step',
        type=int,
        default=3000,
        help='Evaluation frequency in steps.')


def load_state(model_dir, model, optimizer, logger, 
               device):
    """
    Loads the model and optimizer state.
    """
    try:
        model_path = join(model_dir, 'model.pt')
        state_dict = torch.load(
            model_path, map_location=device)

        model.load_state_dict(state_dict['model'])
        optimizer.load_state_dict(state_dict['optimizer'])

        logger.info('Loading model from {}'.format(
            model_path))

        return (
            state_dict['val_loss'],
            state_dict['epoch'],
            state_dict['step']
        )

    except FileNotFoundError:
        return np.inf, 0, 0
    

def create_logger(args):
    """
    Creates a logger that outputs information to a
    file and the standard output as well.
    """
    model_dir = join(args.model_dir, args.model_name)

    logger = logging.getLogger(__name__)
    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s')

    # setting up logging to the console
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)
    logger.addHandler(console_handler)

    # setting up logging to a file
    filename = '{date}.log'.format(
        date=datetime.today().strftime(
            '%m-%d-%H-%M'))

    log_path = join(model_dir, filename)
    file_handler = logging.FileHandler(
        filename=log_path)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


def create_optimizer(args, parameters):
    """
    Creates an adam optimizer.
    """
    optimizer = AdamW(
        lr=args.learning_rate, 
        params=parameters, 
        weight_decay=1e-6)

    return optimizer


def compute_lr(step, factor=3e-3, warmup=3, eps=1e-7):
    """
    Calculates learning rate with warm up.
    """
    if step < warmup:
        return (1 + factor) ** step
    else:
        # after reaching maximum number of steps
        # the lr is decreased by factor as well
        return max(((1 + factor) ** warmup) *
                   ((1 - factor) ** (step - warmup)), eps)


def compute_loss(outputs, targets, ignore_idx):
    """
    Computes the loss and accuracy.
    """
    logits = outputs[0]

    logits_view = logits.view(-1, logits.size(-1))
    targets_view = targets.view(-1)

    log_probs = log_softmax(logits_view, dim=-1)

    loss = nll_loss(
        log_probs, targets_view, 
        ignore_index=ignore_idx,
        reduction='sum')

    _, preds = log_probs.max(dim=-1)

    # computing accuracy without including the
    # values at the ignore indices
    not_ignore = targets_view.ne(ignore_idx)
    target_tokens = not_ignore.long().sum().item()
    
    correct = (targets_view == preds) * not_ignore
    correct = correct.sum()

    accuracy = correct / target_tokens
    loss = loss / target_tokens

    return loss, accuracy


def main(rank, args):
    """
    Performs training, validation and testing.
    """
    master_process = rank in [0, -1]

    model_dir = join(args.model_dir, args.model_name)

    if args.distributed:
        # use distributed training if local rank is given
        # and GPU training is requested
        os.environ['RANK'] = str(rank)

        torch.cuda.set_device(rank)
        device = torch.device('cuda', rank)

        torch.distributed.init_process_group(
            backend='nccl', init_method='env://')

    else:
        device = torch.device(
            'cuda' if args.cuda else 'cpu')

    # creating dataset and storing dataset splits
    # as individual variables for convenience
    datasets, tokenizer = create_dataset(args=args)
        
    pad_idx = tokenizer.convert_tokens_to_ids(
        tokenizer.pad_token)
    vocab_size = len(tokenizer)

    # TODO fix xlnet nan with mixed precision
    if 'xlnet' in args.model_name:
        args.mixed = False

    model = create_model(
        args=args, vocab_size=vocab_size, 
        device=device)

    optimizer = create_optimizer(
        args=args, parameters=model.parameters())

    if master_process:
        writer = SummaryWriter(
            logdir=model_dir,
            flush_secs=100)

    logger = create_logger(args=args)

    # loading previous state of the training
    best_val_loss, init_epoch, step = load_state(
        model_dir=model_dir, model=model, 
        optimizer=optimizer, logger=logger,
        device=device)

    mixed = args.mixed and args.cuda

    if mixed:
        model, optimizer = amp.initialize(
            model, optimizer, opt_level='O2')

    if args.distributed:
        model = DistributedDataParallel(
            model, device_ids=[rank], 
            output_device=rank)

    world_size = max(args.num_devices, 1)

    train, valid, test = [
        (split, ceil(
            size / args.batch_size / world_size)) 
        for split, size in datasets]

    # computing the sizes of the dataset splits
    train_dataset, num_train_steps = train
    valid_dataset, num_valid_steps = valid
    test_dataset, num_test_steps = test

    patience, skip, loss, acc = 0, 0, 0, 0

    def reduce_tensor(tensor):
        """
        Averages a tensor across gpus.
        """
        reduced = tensor.clone()
        all_reduce(reduced, op=reduce_op.SUM)
        reduced /= args.world_size

        return reduced

    def forward_step(batch):
        """
        Applies forward pass with the given batch.
        """
        inputs, targets = batch

        outputs = model(
            inputs=inputs, 
            half=mixed)

        # converting targets from ndarray
        targets = torch.as_tensor(targets)
        targets = targets.long().to(device)

        loss, accuracy = compute_loss(
            outputs=outputs, 
            targets=targets,
            ignore_idx=pad_idx)

        if args.distributed:
            # reducing accuracy accross devices 
            # for more accurate logging
            accuracy = reduce_tensor(accuracy)

        return loss, accuracy.item()
    
    def train_step(batch):
        """
        Performs a single step of training.
        """
        nonlocal step, skip

        loss, accuracy = forward_step(batch)

        if torch.isnan(loss).item():
            logger.debug('skipping step (nan)')
            # returning None values when a NaN loss
            # is encountered and skipping backprop
            # so model grads will not be corrupted
            skip += 1
            return None, None

        loss /= args.grad_accum_steps

        backward(loss)
        clip_grad_norm(1.0)

        step += 1

        if step % args.grad_accum_steps == 0:
            update()

        if args.distributed:
            # reducing loss accross devices for
            # more accurate logging
            loss = reduce_tensor(loss)

        return loss.item(), accuracy

    def backward(loss):
        """
        Backpropagates the loss in either mixed or
        normal precision mode.
        """
        # cuda is required for mixed precision training.
        if mixed:
            with amp.scale_loss(loss, optimizer) as scaled:
                scaled.backward()
        else:
            loss.backward()

    def update():
        """
        Updates the model parameters.
        """
        optimizer.step()
        optimizer.zero_grad()

    def clip_grad_norm(max_norm):
        """
        Applies gradient clipping.
        """
        if mixed:
            clip_grad_norm_(
                amp.master_params(optimizer), max_norm)
        else:
            clip_grad_norm_(model.parameters(), max_norm)

    def evaluate(dataset, num_steps):
        """
        Constructs a validation loader and evaluates
        the model.
        """
        loop = tqdm(
            dataset(), 
            total=num_steps,
            disable=not master_process,
            desc='Eval')

        model.eval()

        for batch in loop:
            loss, acc = forward_step(batch)

            loop.set_postfix(ordered_dict=OrderedDict(
                loss=loss.item(), acc=acc))

            yield loss.item()

    def save_state():
        """
        Saves the model and optimizer state.
        """
        model_path = join(model_dir, 'model.pt')

        state = {
            'model': model.state_dict(),
            'optimizer': optimizer.state_dict(),
            'val_loss': val_loss,
            'epoch': epoch + 1,
            'step': step
        }

        logger.info('Saving model to {}'.format(model_path))
        # making sure the model saving is not left in a
        # corrupted state after a keyboard interrupt
        while True:
            try:
                torch.save(state, model_path)
                break
            except KeyboardInterrupt:
                pass

    scheduler = LambdaLR(optimizer, compute_lr)
        
    if master_process:
        logger.info(str(vars(args)))

    for epoch in range(init_epoch, args.max_epochs):
        # running training loop
        loop = tqdm(
            train_dataset(), 
            total=num_train_steps,
            disable=not master_process,
            desc='Train {}'.format(epoch))

        train_loss = []

        model.train()

        for batch in loop:
            try:
                loss, acc = train_step(batch)

                if master_process and loss is not None:
                    train_loss.append(loss)

                    # logging to tensorboard    
                    writer.add_scalar('train/loss', loss, step)
                    writer.add_scalar('train/acc', acc, step)

                if not step % args.eval_every_step:
                    with torch.no_grad():
                        val_loss = mean(evaluate(
                            dataset=valid_dataset,
                            num_steps=num_valid_steps))
                    
                    # switching back to training
                    model.train()

                    if master_process:
                        logger.info('val loss: {:.4}'.format(
                            val_loss))

                        # logging to tensorboard    
                        writer.add_scalar('val/loss', val_loss, step)

                    if val_loss < best_val_loss:
                        patience = 0
                        best_val_loss = val_loss

                        if master_process:
                            save_state()

                    else:
                        patience += 1
                        if patience == args.patience:
                            # terminate when max patience 
                            # level is hit
                            break

            except RuntimeError as e:
                if 'out of memory' in str(e):
                    logger.debug('skipping step (oom)')
                    skip += 1

                else:
                    raise RuntimeError(e)

            loop.set_postfix(ordered_dict=OrderedDict(
                loss=loss, acc=acc, skip=skip))

        if len(train_loss) > 0:
            train_loss = mean(train_loss)
        else:
            train_loss = 0.0

        if master_process:
            logger.info('train loss: {:.4}'.format(
                train_loss))

        scheduler.step()

    if master_process:
        writer.close()

    with torch.no_grad():
        test_loss = mean(evaluate(
            dataset=test_dataset,
            num_steps=num_test_steps))

    if master_process:
        logger.info('test loss: {:.4}'.format(
            test_loss))
