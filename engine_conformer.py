"""
Train and eval functions used in main.py
"""
import math
import sys
from typing import Iterable, Optional

import torch

from timm.data import Mixup
from timm.utils import accuracy, ModelEma

import utils

from torch.utils.tensorboard import SummaryWriter

from losses import DistillationLoss

def train_one_epoch(model: torch.nn.Module, criterion: DistillationLoss,
                    data_loader: Iterable, optimizer: torch.optim.Optimizer,
                    device: torch.device, epoch: int, loss_scaler, max_norm: float = 0,
                    model_ema: Optional[ModelEma] = None, mixup_fn: Optional[Mixup] = None,
                    set_training_mode=True, log_writer:Optional[SummaryWriter]=None):

    model.train(set_training_mode)
    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter('lr', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))
    header = 'Epoch: [{}]'.format(epoch)
    print_freq = 10

    if log_writer is not None:
        print('log_dir: {}'.format(log_writer.log_dir))

    for data_iter_step, (samples, targets) in enumerate(metric_logger.log_every(data_loader, print_freq, header)):
        samples = samples.to(device, non_blocking=True)
        targets = targets.to(device, non_blocking=True)

        if mixup_fn is not None:
            samples, targets = mixup_fn(samples, targets)

        with torch.cuda.amp.autocast():
            outputs = model(samples)
            loss = criterion(samples, outputs, targets)

        loss_value = loss.item()

        if not math.isfinite(loss_value):
            print("Loss is {}, stopping training".format(loss_value))
            sys.exit(1)

        optimizer.zero_grad()

        # this attribute is added by timm on one optimizer (adahessian)
        is_second_order = hasattr(optimizer, 'is_second_order') and optimizer.is_second_order
        loss_scaler(loss, optimizer, clip_grad=max_norm,
                    parameters=model.parameters(), create_graph=is_second_order)

        torch.cuda.synchronize()
        if model_ema is not None:
            model_ema.update(model)

        metric_logger.update(loss=loss_value)
        
        lr = optimizer.param_groups[0]["lr"]
        metric_logger.update(lr=lr)

        loss_value_reduce = utils.all_reduce_mean(loss_value)
        if log_writer is not None:
            """ We use epoch_1000x as the x-axis in tensorboard.
            This calibrates different curves when batch size changes.
            """
            epoch_1000x = int((data_iter_step / len(data_loader) + epoch) * 1000)
            log_writer.add_scalar('loss', loss_value_reduce, epoch_1000x)
            log_writer.add_scalar('lr', lr, epoch_1000x)

    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print("Averaged stats:", metric_logger)
    return {k: meter.global_avg for k, meter in metric_logger.meters.items()}


@torch.no_grad()
def evaluate(data_loader, model, device):
    criterion = torch.nn.CrossEntropyLoss()

    metric_logger = utils.MetricLogger(delimiter="  ")
    header = 'Test:'

    # switch to evaluation mode
    model.eval()

    for images, target in metric_logger.log_every(data_loader, 10, header):
        images = images.to(device, non_blocking=True)
        target = target.to(device, non_blocking=True)

        # compute output
        with torch.cuda.amp.autocast():
            output = model(images)
            # Conformer
            if isinstance(output, list):
                loss_list = [criterion(o, target) / len(output)  for o in output]
                loss = sum(loss_list)
            # others
            else:
                loss = criterion(output, target)
        if isinstance(output, list):
            # Conformer
            acc1_head1 = accuracy(output[0], target, topk=(1,))[0]
            acc1_head2 = accuracy(output[1], target, topk=(1,))[0]
            acc1_total = accuracy(output[0] + output[1], target, topk=(1,))[0]
        else:
            # others
            acc1, acc5 = accuracy(output, target, topk=(1, 5))

        batch_size = images.shape[0]
        if isinstance(output, list):
            metric_logger.update(loss=loss.item())
            metric_logger.update(loss_0=loss_list[0].item())
            metric_logger.update(loss_1=loss_list[1].item())
            metric_logger.meters['acc1'].update(acc1_total.item(), n=batch_size)
            metric_logger.meters['acc1_head1'].update(acc1_head1.item(), n=batch_size)
            metric_logger.meters['acc1_head2'].update(acc1_head2.item(), n=batch_size)
        else:
            metric_logger.update(loss=loss.item())
            metric_logger.meters['acc1'].update(acc1.item(), n=batch_size)
            metric_logger.meters['acc5'].update(acc5.item(), n=batch_size)
    if isinstance(output, list):
        print('* Acc@heads_top1 {heads_top1.global_avg:.3f} Acc@head_1 {head1_top1.global_avg:.3f} Acc@head_2 {head2_top1.global_avg:.3f} '
              'loss@total {losses.global_avg:.3f} loss@1 {loss_0.global_avg:.3f} loss@2 {loss_1.global_avg:.3f} '
              .format(heads_top1=metric_logger.acc1, head1_top1=metric_logger.acc1_head1, head2_top1=metric_logger.acc1_head2,
                      losses=metric_logger.loss, loss_0=metric_logger.loss_0, loss_1=metric_logger.loss_1))
    else:
        print('* Acc@1 {top1.global_avg:.3f} Acc@5 {top5.global_avg:.3f} loss {losses.global_avg:.3f}'
          .format(top1=metric_logger.acc1, top5=metric_logger.acc5, losses=metric_logger.loss))
    return {k: meter.global_avg for k, meter in metric_logger.meters.items()}