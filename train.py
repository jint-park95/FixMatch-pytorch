import argparse
import logging
import math
import os
import random
import shutil
import time
from copy import deepcopy
from collections import OrderedDict

import numpy as np
import torch
import torch.backends.cudnn as cudnn
import torch.nn.functional as F
import torch.optim as optim
from torch.optim.lr_scheduler import LambdaLR
from torch.utils.data import DataLoader, RandomSampler, SequentialSampler
from torch.utils.data.distributed import DistributedSampler
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm

from dataset.cifar import get_cifar10, get_cifar100
from utils import AverageMeter, accuracy

logger = logging.getLogger(__name__)

DATASET_GETTERS = {'cifar10': get_cifar10,
                   'cifar100': get_cifar100}

best_acc = 0


def save_checkpoint(state, is_best, checkpoint, filename='checkpoint.pth.tar'):
    filepath = os.path.join(checkpoint, filename)
    torch.save(state, filepath)
    if is_best:
        shutil.copyfile(filepath, os.path.join(checkpoint,
                                               'model_best.pth.tar'))


def set_seed(args):
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)


def get_cosine_schedule_with_warmup(optimizer,
                                    num_warmup_steps,
                                    num_training_steps,
                                    num_cycles=7./16.,
                                    last_epoch=-1):
    def _lr_lambda(current_step):
        if current_step < num_warmup_steps:
            return float(current_step) / float(max(1, num_warmup_steps))
        no_progress = float(current_step - num_warmup_steps) / \
            float(max(1, num_training_steps - num_warmup_steps))
        return max(0., math.cos(math.pi * num_cycles * no_progress))

    return LambdaLR(optimizer, _lr_lambda, last_epoch)


def main():
    parser = argparse.ArgumentParser(description='PyTorch FixMatch Training')
    parser.add_argument('--gpu-id', default='0', type=int,
                        help='id(s) for CUDA_VISIBLE_DEVICES')
    parser.add_argument('--num-workers', type=int, default=4,
                        help='number of workers')
    parser.add_argument('--dataset', default='cifar10', type=str,
                        choices=['cifar10', 'cifar100'],
                        help='dataset name')
    parser.add_argument('--num-labeled', type=int, default=4000,
                        help='number of labeled data')
    parser.add_argument('--arch', default='wideresnet', type=str,
                        choices=['wideresnet', 'resnext'],
                        help='dataset name')
    parser.add_argument('--epochs', default=1024, type=int,
                        help='number of total epochs to run')
    parser.add_argument('--start-epoch', default=0, type=int,
                        help='manual epoch number (useful on restarts)')
    parser.add_argument('--batch-size', default=64, type=int,
                        help='train batchsize')
    parser.add_argument('--lr', '--learning-rate', default=0.03, type=float,
                        help='initial learning rate')
    parser.add_argument('--warmup', default=0, type=float,
                        help='warmup epochs (unlabeled data based)')
    parser.add_argument('--wdecay', default=5e-4, type=float,
                        help='weight decay')
    parser.add_argument('--nesterov', action='store_true', default=True,
                        help='use nesterov momentum')
    parser.add_argument('--use-ema', action='store_true', default=True,
                        help='use EMA model')
    parser.add_argument('--ema-decay', default=0.999, type=float,
                        help='EMA decay rate')
    parser.add_argument('--mu', default=7, type=int,
                        help='coefficient of unlabeled batch size')
    parser.add_argument('--lambda-u', default=1, type=float,
                        help='coefficient of unlabeled loss')
    parser.add_argument('--threshold', default=0.95, type=float,
                        help='pseudo label threshold')
    parser.add_argument('--k-img', default=65536, type=int,
                        help='number of labeled examples')
    parser.add_argument('--out', default='result',
                        help='directory to output the result')
    parser.add_argument('--resume', default='', type=str,
                        help='path to latest checkpoint (default: none)')
    parser.add_argument('--seed', type=int, default=-1,
                        help="random seed (-1: don't use random seed)")
    parser.add_argument("--amp", action="store_true",
                        help="use 16-bit (mixed) precision through NVIDIA apex AMP")
    parser.add_argument("--opt_level", type=str, default="O1",
                        help="apex AMP optimization level selected in ['O0', 'O1', 'O2', and 'O3']."
                        "See details at https://nvidia.github.io/apex/amp.html")
    parser.add_argument("--local_rank", type=int, default=-1,
                        help="For distributed training: local_rank")
    parser.add_argument('--no-progress', action='store_true',
                        help="don't use progress bar")

    args = parser.parse_args()
    global best_acc

    def create_model(args):
        if args.arch == 'wideresnet':
            import models.wideresnet as models
            model = models.build_wideresnet(depth=args.model_depth,
                                            widen_factor=args.model_width,
                                            dropout=0,
                                            num_classes=args.num_classes)
        elif args.arch == 'resnext':
            import models.resnext as models
            model = models.build_resnext(cardinality=args.model_cardinality,
                                         depth=args.model_depth,
                                         width=args.model_width,
                                         num_classes=args.num_classes)

        logger.info('Total params: {:.2f}M'.format(
            sum(p.numel() for p in model.parameters())/1e6))

        return model

    if args.local_rank == -1:
        device = torch.device("cuda", args.gpu_id)
        args.world_size = 1
        args.n_gpu = torch.cuda.device_count()
    else:
        torch.cuda.set_device(args.local_rank)
        device = torch.device("cuda", args.local_rank)
        torch.distributed.init_process_group(backend="nccl")
        args.world_size = torch.distributed.get_world_size()
        args.n_gpu = 1

    args.device = device

    if args.dataset == 'cifar10':
        args.num_classes = 10
        if args.arch == 'wideresnet':
            args.model_depth = 28
            args.model_width = 2
        if args.arch == 'resnext':
            args.model_cardinality = 4
            args.model_depth = 28
            args.model_width = 4

    elif args.dataset == 'cifar100':
        args.num_classes = 100
        if args.arch == 'wideresnet':
            args.model_depth = 28
            args.model_width = 10
        if args.arch == 'resnext':
            args.model_cardinality = 8
            args.model_depth = 29
            args.model_width = 64

    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO if args.local_rank in [-1, 0] else logging.WARN)

    logger.warning(
        "Process rank: %s, device: %s, n_gpu: %s, distributed training: %s, 16-bits training: %s",
        args.local_rank,
        args.device,
        args.n_gpu,
        bool(args.local_rank != -1),
        args.amp)

    logger.info(dict(args._get_kwargs()))

    if args.seed != -1:
        set_seed(args)

    if args.local_rank in [-1, 0]:
        os.makedirs(args.out, exist_ok=True)
        writer = SummaryWriter(args.out)

    if args.local_rank not in [-1, 0]:
        torch.distributed.barrier()

    labeled_dataset, unlabeled_dataset, test_dataset = DATASET_GETTERS[args.dataset](
        './data', args.num_labeled, args.k_img, args.k_img * args.mu)

    model = create_model(args)

    if args.local_rank == 0:
        torch.distributed.barrier()

    model.to(args.device)

    # train_sampler = RandomSampler if args.local_rank == -1 else DistributedSampler
    train_sampler = SequentialSampler if args.local_rank == -1 else DistributedSampler
    labeled_trainloader = DataLoader(
        labeled_dataset,
        sampler=train_sampler(labeled_dataset),
        batch_size=args.batch_size,
        num_workers=args.num_workers,
        drop_last=True)

    unlabeled_trainloader = DataLoader(
        unlabeled_dataset,
        sampler=train_sampler(unlabeled_dataset),
        batch_size=args.batch_size,
        num_workers=args.num_workers,
        drop_last=True)

    test_loader = DataLoader(
        test_dataset,
        sampler=SequentialSampler(test_dataset),
        batch_size=args.batch_size,
        num_workers=args.num_workers)

    no_decay = ["bias", "bn"]
    grouped_parameters = [
        {
            "params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
            "weight_decay": args.wdecay * 2,  # L2 regulization
        },
        {
            "params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)],
            "weight_decay": 0.0
        }]

    optimizer = optim.SGD(grouped_parameters, lr=args.lr,
                          momentum=0.9,
                          nesterov=args.nesterov)

    args.iteration = int(args.k_img / args.batch_size)
    args.total_steps = args.epochs * args.iteration
    scheduler = get_cosine_schedule_with_warmup(
        optimizer, args.warmup * len(unlabeled_trainloader), args.total_steps)

    ema_model = None
    if args.use_ema:
        ema_model = ModelEMA(model, args.ema_decay, device)

    start_epoch = 0

    if args.local_rank not in [-1, 0]:
        torch.distributed.barrier()

    if args.resume:
        logger.info('==> Resuming from checkpoint..')
        assert os.path.isfile(
            args.resume), 'Error: no checkpoint directory found!'
        args.out = os.path.dirname(args.resume)
        checkpoint = torch.load(args.resume)
        best_acc = checkpoint['best_acc']
        start_epoch = checkpoint['epoch']
        model.load_state_dict(checkpoint['state_dict'])
        ema_model.ema.load_state_dict(checkpoint['ema_state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer'])
        scheduler.load_state_dict(checkpoint['schduler'])

    if args.local_rank == 0:
        torch.distributed.barrier()

    if args.amp:
        from apex import amp
        model, optimizer = amp.initialize(
            model, optimizer, opt_level=args.opt_level)

    if args.local_rank != -1:
        model = torch.nn.parallel.DistributedDataParallel(
            model, device_ids=[args.local_rank],
            output_device=args.local_rank, find_unused_parameters=True)

    logger.info("***** Running training *****")
    logger.info(f"  Task = {args.dataset}@{args.num_labeled}")
    logger.info(f"  Num Epochs = {args.epochs}")
    logger.info(f"  Batch size per GPU = {args.batch_size}")
    logger.info(
        f"  Total train batch size = {args.batch_size*args.world_size}")
    logger.info(f"  Total optimization steps = {args.total_steps}")

    test_accs = []
    model.zero_grad()
    for epoch in range(start_epoch, args.epochs):

        train_loss, train_loss_x, train_loss_u, mask_prob = train(
            args, labeled_trainloader, unlabeled_trainloader,
            model, optimizer, ema_model, scheduler, epoch)

        if args.no_progress:
            logger.info('Epoch {}. train_loss: {:.4f}. train_loss_x: {:.4f}. train_loss_u: {:.4f}.'
                        .format(epoch+1, train_loss, train_loss_x, train_loss_u))

        if args.use_ema:
            test_model = ema_model.ema
        else:
            test_model = model

        test_loss, test_acc = test(args, test_loader, test_model, epoch)

        if args.local_rank in [-1, 0]:
            writer.add_scalar('train/1.train_loss', train_loss, epoch)
            writer.add_scalar('train/2.train_loss_x', train_loss_x, epoch)
            writer.add_scalar('train/3.train_loss_u', train_loss_u, epoch)
            writer.add_scalar('train/4.mask', mask_prob, epoch)
            writer.add_scalar('test/1.test_acc', test_acc, epoch)
            writer.add_scalar('test/2.test_loss', test_loss, epoch)

        is_best = test_acc > best_acc
        best_acc = max(test_acc, best_acc)
        if args.local_rank == -1 or torch.distributed.get_rank() == 0:
            model_to_save = model.module if hasattr(model, "module") else model
            if args.use_ema:
                ema_to_save = ema_model.ema.module if hasattr(
                    ema_model.ema, "module") else ema_model.ema
            save_checkpoint({
                'epoch': epoch + 1,
                'state_dict': model_to_save.state_dict(),
                'ema_state_dict': ema_to_save.state_dict() if args.use_ema else None,
                'acc': test_acc,
                'best_acc': best_acc,
                'optimizer': optimizer.state_dict(),
                'scheduler': scheduler.state_dict(),
            }, is_best, args.out)

        test_accs.append(test_acc)
        logger.info('Best top-1 acc: {:.2f}'.format(best_acc))
        logger.info('Median top-1 acc: {:.2f}\n'.format(
            np.median(test_accs[-20:])))

    if args.local_rank in [-1, 0]:
        writer.close()


def train(args, labeled_trainloader, unlabeled_trainloader,
          model, optimizer, ema_model, scheduler, epoch):
    if args.amp:
        from apex import amp
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    losses_x = AverageMeter()
    losses_u = AverageMeter()
    end = time.time()

    model.train()

    train_loader = zip(labeled_trainloader, unlabeled_trainloader)

    if not args.no_progress:
        p_bar = tqdm(range(args.iteration),
                     disable=args.local_rank not in [-1, 0])

    for batch_idx, (data_x, data_u) in enumerate(train_loader):
        inputs_x, targets_x = data_x
        (inputs_u_w, inputs_u_s), _ = data_u
        data_time.update(time.time() - end)
        batch_size = inputs_x.shape[0]
        inputs = torch.cat((inputs_x, inputs_u_w, inputs_u_s)).to(args.device)
        logits = model(inputs)
        logits_x = logits[:batch_size]
        logits_u_w, logits_u_s = logits[batch_size:].chunk(2)
        targets_x = targets_x.to(args.device)
        del logits

        Lx = F.cross_entropy(logits_x, targets_x, reduction='mean')

        pseudo_label = torch.softmax(logits_u_w.detach_(), dim=-1)
        max_probs, targets_u = torch.max(pseudo_label, dim=-1)
        mask = max_probs.ge(args.threshold).float()

        Lu = (F.cross_entropy(logits_u_s, targets_u,
                              reduction='none') * mask).mean()
        loss = Lx + args.lambda_u * Lu

        if args.amp:
            with amp.scale_loss(loss, optimizer) as scaled_loss:
                scaled_loss.backward()
        else:
            loss.backward()

        losses.update(loss.item())
        losses_x.update(Lx.item())
        losses_u.update(Lu.item())

        optimizer.step()
        scheduler.step()
        if ema_model is not None:
            ema_model.update(model)
        model.zero_grad()

        batch_time.update(time.time() - end)
        end = time.time()
        mask_prob = mask.mean().item()
        if not args.no_progress:
            p_bar.set_description('Train Epoch: {epoch}/{epochs:4}. Iter: {batch:4}/{iter:4}. LR: {lr:.6f}. Data: {data:.3f}s. Batch: {bt:.3f}s. Loss: {loss:.4f}. Loss_x: {loss_x:.4f}. Loss_u: {loss_u:.4f}. Mask: {mask:.4f}. '.format(
                epoch=epoch + 1,
                epochs=args.epochs,
                batch=batch_idx + 1,
                iter=args.iteration,
                lr=scheduler.get_last_lr()[0],
                data=data_time.avg,
                bt=batch_time.avg,
                loss=losses.avg,
                loss_x=losses_x.avg,
                loss_u=losses_u.avg,
                mask=mask_prob))
            p_bar.update()

    if not args.no_progress:
        p_bar.close()
    return losses.avg, losses_x.avg, losses_u.avg, mask_prob


def test(args, test_loader, model, epoch):
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()
    end = time.time()

    if not args.no_progress:
        test_loader = tqdm(test_loader,
                           disable=args.local_rank not in [-1, 0])

    with torch.no_grad():
        for batch_idx, (inputs, targets) in enumerate(test_loader):
            data_time.update(time.time() - end)
            model.eval()

            inputs = inputs.to(args.device)
            targets = targets.to(args.device)
            outputs = model(inputs)
            loss = F.cross_entropy(outputs, targets)

            prec1, prec5 = accuracy(outputs, targets, topk=(1, 5))
            losses.update(loss.item(), inputs.shape[0])
            top1.update(prec1.item(), inputs.shape[0])
            top5.update(prec5.item(), inputs.shape[0])

            batch_time.update(time.time() - end)
            end = time.time()
            if not args.no_progress:
                test_loader.set_description('Test Iter: {batch:4}/{iter:4}. Data: {data:.3f}s. Batch: {bt:.3f}s. Loss: {loss:.4f}. top1: {top1:.2f}. top5: {top5:.2f}. '.format(
                    batch=batch_idx + 1,
                    iter=len(test_loader),
                    data=data_time.avg,
                    bt=batch_time.avg,
                    loss=losses.avg,
                    top1=top1.avg,
                    top5=top5.avg,
                ))
        if not args.no_progress:
            test_loader.close()

    logger.info("top-1 acc: {:.2f}".format(top1.avg))
    logger.info("top-5 acc: {:.2f}".format(top5.avg))
    return (losses.avg, top1.avg)


class ModelEMA(object):
    def __init__(self, model, decay, device='', resume=''):
        self.ema = deepcopy(model)
        self.ema.eval()
        self.decay = decay
        self.device = device
        if device:
            self.ema.to(device=device)
        self.ema_has_module = hasattr(self.ema, 'module')
        if resume:
            self._load_checkpoint(resume)
        for p in self.ema.parameters():
            p.requires_grad_(False)

    def _load_checkpoint(self, checkpoint_path):
        checkpoint = torch.load(checkpoint_path)
        assert isinstance(checkpoint, dict)
        if 'ema_state_dict' in checkpoint:
            new_state_dict = OrderedDict()
            for k, v in checkpoint['ema_state_dict'].items():
                if self.ema_has_module:
                    name = 'module.' + k if not k.startswith('module') else k
                else:
                    name = k
                new_state_dict[name] = v
            self.ema.load_state_dict(new_state_dict)

    def update(self, model):
        needs_module = hasattr(model, 'module') and not self.ema_has_module
        with torch.no_grad():
            msd = model.state_dict()
            for k, ema_v in self.ema.state_dict().items():
                if needs_module:
                    k = 'module.' + k
                model_v = msd[k].detach()
                if self.device:
                    model_v = model_v.to(device=self.device)
                ema_v.copy_(ema_v * self.decay + (1. - self.decay) * model_v)


if __name__ == '__main__':
    cudnn.benchmark = True
    main()
