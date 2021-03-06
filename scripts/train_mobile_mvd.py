import torch.nn.functional as F
import argparse
import torch
import time
import os

from datasets.mapillary_vistas_loader import MapillaryVistasLoader
from tensorboardX import SummaryWriter
from torch.autograd import Variable
from torch.utils import data
from tqdm import tqdm

from scripts.loss import bootstrapped_cross_entropy2d
from models.mobilenetv2plus import MobileNetV2Plus
from scripts.metrics import RunningScore
from datasets.augmentations import *


def train(args, data_root, save_root):
    weight_dir = "{}weights/".format(save_root)
    log_dir = "{}logs/MobileNetV2Plus-{}".format(save_root, time.strftime("%Y-%m-%d-%H-%M-%S", time.localtime()))

    # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
    # 1. Setup Augmentations
    # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
    net_h, net_w = int(args.img_rows*args.crop_ratio), int(args.img_cols*args.crop_ratio)

    augment_train = Compose([RandomHorizontallyFlip(), RandomSized((0.625, 0.75)),
                             RandomRotate(6), RandomCrop((net_h, net_w))])
    augment_valid = Compose([RandomHorizontallyFlip(), RandomSized((0.625, 0.75)),
                             CenterCrop((net_h, net_w))])

    print("> # +++++++++++++++++++++++++++++++++++++++++++++++++++++++ #")
    print("> 0. Setting up DataLoader...")
    print("> # +++++++++++++++++++++++++++++++++++++++++++++++++++++++ #")
    train_loader = MapillaryVistasLoader(data_root, split="training", is_transform=True,
                                         img_size=(args.img_rows, args.img_cols), augmentations=augment_train)
    valid_loader = MapillaryVistasLoader(data_root, split="validation", is_transform=True,
                                         img_size=(args.img_rows, args.img_cols), augmentations=augment_valid)

    n_classes = train_loader.n_classes

    # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
    # 2. Setup Metrics
    # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
    running_metrics = RunningScore(n_classes)

    # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
    # 4. Setup Model
    # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
    print("> # +++++++++++++++++++++++++++++++++++++++++++++++++++++++ #")
    print("> 1. Setting up Model...")

    model = MobileNetV2Plus(n_class=n_classes, in_size=(net_h, net_w), width_mult=1.0,
                            out_sec=(252, 86), aspp_sec=(12, 24, 36))

    # np.arange(torch.cuda.device_count())
    model = torch.nn.DataParallel(model, device_ids=[0, 1]).cuda()

    # 4.1 Setup Optimizer
    # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
    # Check if model has custom optimizer / loss
    if hasattr(model.module, 'optimizer'):
        optimizer = model.module.optimizer
    else:
        optimizer = torch.optim.SGD(model.parameters(), lr=args.l_rate, momentum=0.90, weight_decay=5e-4, nesterov=True)

        # for pg in optimizer.param_groups:
        #     print(pg['lr'])

        # optimizer = torch.optim.Adam(model.parameters(), lr=0.001, betas=(0.9, 0.999),
        #                             eps=1e-08, weight_decay=0, amsgrad=True)
        # optimizer = YFOptimizer(model.parameters(), lr=2.5e-3, mu=0.9, clip_thresh=10000, weight_decay=5e-4)

    scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer, gamma=0.90)
    # scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=30, gamma=0.1)
    # scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones=[30, 80], gamma=0.1)

    # 4.2 Setup Loss
    # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
    if hasattr(model.module, 'loss'):
        print('> Using custom loss')
        loss_fn = model.module.loss
    else:
        # loss_fn = cross_entropy2d
        loss_fn = bootstrapped_cross_entropy2d

    # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
    # 5. Resume Model
    # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
    args.start_epoch = 0
    best_iou = -100.0
    if args.resume is not None:
        full_path = "{}{}".format(weight_dir, args.resume)
        if os.path.isfile(full_path):
            print("> Loading model and optimizer from checkpoint '{}'".format(args.resume))

            checkpoint = torch.load(full_path)
            args.start_epoch = checkpoint['epoch']
            best_iou = checkpoint['best_iou']
            model.load_state_dict(checkpoint['model_state'])          # weights

            optimizer.load_state_dict(checkpoint['optimizer_state'])  # gradient state

            # optimizer = YFOptimizer(model.parameters(), lr=2.5e-3, mu=0.9, clip_thresh=10000, weight_decay=5e-4)
            del checkpoint

            print("> Loaded checkpoint '{}' (epoch {})".format(args.resume, args.start_epoch))

        else:
            print("> No checkpoint found at '{}'".format(args.resume))
    else:
        if args.pre_trained is not None:
            print("> Loading weights from pre-trained model '{}'".format(args.pre_trained))
            full_path = "{}{}".format(weight_dir, args.pre_trained)

            pre_weight = torch.load(full_path)
            pre_weight = pre_weight["model_state"]
            # pre_weight = pre_weight["state_dict"]

            model_dict = model.state_dict()

            pretrained_dict = {k: v for k, v in pre_weight.items() if k in model_dict}
            model_dict.update(pretrained_dict)
            model.load_state_dict(model_dict)

            del pre_weight
            del model_dict
            del pretrained_dict

    # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
    # 3. Setup tensor_board for visualization
    # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
    writer = None
    if args.tensor_board:
        writer = SummaryWriter(log_dir=log_dir, comment="MobileNetV2")
        dummy_input = Variable(torch.rand(1, 3, net_h, net_w).cuda(), requires_grad=True)
        writer.add_graph(model, dummy_input)

    # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
    # 6. Train Model
    # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
    print("> 2. Model Training start...")
    train_loader = data.DataLoader(train_loader, batch_size=args.batch_size, num_workers=6, shuffle=True)
    valid_loader = data.DataLoader(valid_loader, batch_size=args.batch_size, num_workers=6)

    num_batches = int(math.ceil(len(train_loader.dataset.files[train_loader.dataset.split]) /
                                float(train_loader.batch_size)))

    loss_wgt1 = 1.0
    loss_wgt2 = 1.0
    loss_wgt3 = 1.0

    topk_base = 512
    # topk_multipliers = [64, 128, 256, 512]
    for epoch in np.arange(args.start_epoch, args.n_epoch):
        pbar = tqdm(np.arange(num_batches))
        last_loss = [0.0, 0.0, 0.0]

        # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
        # 7.1 Mini-Batch Learning
        # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
        # print("> Training Epoch [%d/%d]:" % (epoch + 1, args.n_epoch))
        model.train()

        for train_i, (images, labels) in enumerate(train_loader):  # One mini-Batch data, One iteration
            full_iter = (epoch * num_batches) + train_i + 1

            # poly_lr_scheduler(optimizer, init_lr=args.l_rate, iter=full_iter,
            #                   lr_decay_iter=1, max_iter=args.n_epoch*num_batches, power=0.9)

            pbar.update(1)
            pbar.set_description("> Epoch [%d/%d]" % (epoch + 1, args.n_epoch))

            images = Variable(images.cuda(), requires_grad=True)   # Image feed into the deep neural network
            labels = Variable(labels.cuda(), requires_grad=False)

            optimizer.zero_grad()
            out_stg1, out_stg2, out_stg3 = model(images)  # Here we have 3 output for 3 loss

            topk = topk_base * 256
            stg1_loss = loss_wgt1 * loss_fn(input=out_stg1, target=labels, K=topk)
            stg2_loss = loss_wgt2 * loss_fn(input=out_stg2, target=labels, K=topk)
            stg3_loss = loss_wgt3 * loss_fn(input=out_stg3, target=labels, K=topk)

            last_loss = [stg1_loss.data[0], stg2_loss.data[0], stg3_loss.data[0]]
            loss = [stg1_loss, stg2_loss, stg3_loss]
            torch.autograd.backward(loss)
            optimizer.step()

            pbar.set_postfix(Loss1=stg1_loss.data[0], Loss2=stg2_loss.data[0], Loss3=stg3_loss.data[0])

            if (train_i + 1) % 62 == 0:

                loss_log = "Epoch [%d/%d], Iter: %d Loss1: \t %.4f, Loss2: \t %.4f, " \
                           "Loss3: \t %.4f" % (epoch + 1, args.n_epoch, train_i + 1,
                                               last_loss[0], last_loss[1], last_loss[2])

                out_stg3 = F.softmax(out_stg3, dim=1)
                pred = out_stg3.data.max(1)[1].cpu().numpy()
                gt = labels.data.cpu().numpy()

                running_metrics.update(gt, pred)

                score, class_iou = running_metrics.get_scores()

                metric_log = ""
                for k, v in score.items():
                    metric_log += " {}: \t %.4f, ".format(k) % v
                running_metrics.reset()

                logs = loss_log + metric_log
                # print(logs)

                if args.tensor_board:
                    writer.add_scalars('Training/Losses',
                                       {'Loss_Stage1': last_loss[0],
                                        'Loss_Stage2': last_loss[1],
                                        'Loss_Stage3': last_loss[2]},
                                       full_iter)
                    writer.add_scalars('Training/Metrics', score, full_iter)

                    writer.add_text('Training/Text', logs, full_iter)

                    for name, param in model.named_parameters():
                        writer.add_histogram(name, param.clone().cpu().data.numpy(), full_iter)

        # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
        # 7.2 Mini-Batch Validation
        # +++++++++++++++++++++++++++++++++++++++++++++++++++ #
        # print("> Validation for Epoch [%d/%d]:" % (epoch + 1, args.n_epoch))
        model.eval()
        val_loss = [0.0, 0.0, 0.0]
        vali_count = 0
        for i_val, (images_val, labels_val) in enumerate(valid_loader):
            vali_count += 1

            images_val = Variable(images_val.cuda(), volatile=True)
            labels_val = Variable(labels_val.cuda(), volatile=True)

            out_stg1, out_stg2, out_stg3 = model(images_val)  # Here we have 4 output for 4 loss

            topk = topk_base * 256
            stg1_val_loss = loss_wgt1 * loss_fn(input=out_stg1, target=labels_val, K=topk)
            stg2_val_loss = loss_wgt2 * loss_fn(input=out_stg2, target=labels_val, K=topk)
            stg3_val_loss = loss_wgt3 * loss_fn(input=out_stg3, target=labels_val, K=topk)

            val_loss = [val_loss[0] + stg1_val_loss.data[0],
                        val_loss[1] + stg2_val_loss.data[0],
                        val_loss[2] + stg3_val_loss.data[0]]

            out_stg3 = F.softmax(out_stg3, dim=1)
            pred = out_stg3.data.max(1)[1].cpu().numpy()
            gt = labels_val.data.cpu().numpy()
            running_metrics.update(gt, pred)

        val_loss = [val_loss[0]/vali_count, val_loss[1]/vali_count, val_loss[2]/vali_count]

        loss_log = "Epoch [%d/%d] Loss1: \t %.4f, Loss2: \t %.4f, " \
                   "Loss3: \t %.4f" % (epoch + 1, args.n_epoch,
                                       val_loss[0], val_loss[1],  val_loss[2])
        metric_log = ""
        score, class_iou = running_metrics.get_scores()
        for k, v in score.items():
            metric_log += " {} \t %.4f, ".format(k) % v
        running_metrics.reset()

        logs = loss_log + metric_log
        # print(logs)
        pbar.set_postfix(Train_Loss=last_loss[1], Vali_Loss=val_loss[1]/loss_wgt2, Vali_mIoU=score['Mean_IoU'])

        if args.tensor_board:
            writer.add_scalars('Validation/Losses',
                               {'Loss_Stage1': val_loss[0],
                                'Loss_Stage2': val_loss[1],
                                'Loss_Stage3': val_loss[2]}, epoch)
            writer.add_scalars('Validation/Metrics', score, epoch)

            writer.add_text('Validation/Text', logs, epoch)

            for name, param in model.named_parameters():
                writer.add_histogram(name, param.clone().cpu().data.numpy(), epoch)

            # export scalar data to JSON for external processing
            # writer.export_scalars_to_json("{}/all_scalars.json".format(log_dir))

        if score['Mean_IoU'] >= best_iou:
            best_iou = score['Mean_IoU']
            state = {'epoch': epoch + 1,
                     "best_iou": best_iou,
                     'model_state': model.state_dict(),
                     'optimizer_state': optimizer.state_dict()}
            torch.save(state, "{}{}_mobilenetv2_gtfine_best_model.pkl".format(weight_dir, args.dataset))

        # Note that step should be called after validate()
        scheduler.step()
        pbar.close()

    if args.tensor_board:
        # export scalar data to JSON for external processing
        # writer.export_scalars_to_json("{}/all_scalars.json".format(log_dir))
        writer.close()
    print("> # +++++++++++++++++++++++++++++++++++++++++++++++++++++++ #")
    print("> Training Done!!!")
    print("> # +++++++++++++++++++++++++++++++++++++++++++++++++++++++ #")


if __name__ == '__main__':
    # +++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++ #
    # 0. Hyper-params
    # +++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++ #
    parser = argparse.ArgumentParser(description='Hyperparams')

    parser.add_argument('--dataset', nargs='?', type=str, default='mvd',
                        help='Dataset to use [\'cityscapes, mvd etc\']')
    parser.add_argument('--img_rows', nargs='?', type=int, default=512,
                        help='Height of the input image')
    parser.add_argument('--img_cols', nargs='?', type=int, default=1024,
                        help='Height of the input image')
    parser.add_argument('--n_epoch', nargs='?', type=int, default=256,
                        help='# of the epochs')
    parser.add_argument('--batch_size', nargs='?', type=int, default=2,
                        help='Batch Size')
    parser.add_argument('--l_rate', nargs='?', type=float, default=1e-3,
                        help='Learning Rate')
    parser.add_argument('--crop_ratio', nargs='?', type=float, default=0.875,
                        help='The ratio to crop the input image')
    parser.add_argument('--resume', nargs='?', type=str, default=None,
                        help='Path to previous saved model to restart from')
    parser.add_argument('--pre_trained', nargs='?', type=str, default="mobilenetv2plus_model.pkl",
                        help='Path to pre-trained  model to init from')
    parser.add_argument('--tensor_board', nargs='?', type=bool, default=True,
                        help='Show visualization(s) on tensor_board | True by  default')

    # +++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++ #
    # 1. Train the Deep Model
    # +++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++ #
    os.environ["CUDA_VISIBLE_DEVICES"] = "1,0"
    data_path = "/zfs/zhang/MVD"
    save_path = "/zfs/zhang/TrainLog/"
    train_args = parser.parse_args()
    train(train_args, data_path, save_path)
