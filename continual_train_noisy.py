from __future__ import print_function, absolute_import
import argparse
import os.path as osp
import sys
import torch.nn as nn
import random
from reid.utils.logging import Logger
from reid.utils.serialization import load_checkpoint, save_checkpoint, copy_state_dict
from reid.utils.lr_scheduler import WarmupMultiStepLR
from reid.utils.feature_tools import *
from reid.models.layers import DataParallel
from reid.models.resnet import make_model
from reid.trainer_noisy import Trainer
from torch.utils.tensorboard import SummaryWriter

from lreid_dataset.datasets.get_data_loaders_noisy import build_data_loaders_noisy
from tools.Logger_results import Logger_res
from reid.evaluation.fast_test import fast_test_p_s
import datetime


def cur_timestamp_str():
    now = datetime.datetime.now()
    year = str(now.year)
    month = str(now.month).zfill(2)
    day = str(now.day).zfill(2)
    hour = str(now.hour).zfill(2)
    minute = str(now.minute).zfill(2)

    content = "{}-{}{}-{}{}".format(year, month, day, hour, minute)
    return content

def main():
    args = parser.parse_args()

    if args.seed is not None:
        print("setting the seed to",args.seed)
        random.seed(args.seed)
        np.random.seed(args.seed)
        torch.manual_seed(args.seed)
        torch.cuda.manual_seed(args.seed)
        torch.cuda.manual_seed_all(args.seed)

        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True   
    main_worker(args)


def main_worker(args):
    timestamp = cur_timestamp_str()
    log_name = f'log_{timestamp}.txt'

    if args.test_folder:
        args.logs_dir = args.test_folder
    sys.stdout = Logger(osp.join(args.logs_dir, log_name))
    print("==========\nArgs:{}\n==========".format(args))
    log_res_name=f'log_res_{timestamp}.txt'
    logger_res=Logger_res(osp.join(args.logs_dir, log_res_name))    # record the test results   

    """
    loading the datasets:
    setting： 1 or 2 
    """
    if 1 == args.setting:
        training_set = ['market1501', 'cuhk_sysu', 'dukemtmc', 'msmt17', 'cuhk03']
    else:
        training_set = ['dukemtmc', 'msmt17', 'market1501', 'cuhk_sysu', 'cuhk03']
    # all the revelent datasets
    all_set = ['market1501', 'dukemtmc', 'msmt17', 'cuhk_sysu', 'cuhk03',
               'cuhk01', 'cuhk02', 'grid', 'sense', 'viper', 'ilids', 'prid']  # 'sense','prid'
    # the datsets only used for testing
    testing_only_set = [x for x in all_set if x not in training_set]
    # get the loders of different datasets        
    all_train_sets, all_test_only_sets = build_data_loaders_noisy(args, training_set, testing_only_set)  
    
    first_train_set = all_train_sets[0]
    model_list=[]
    for i in range(args.n_model):
        model = make_model(args, num_class=first_train_set[1], camera_num=0, view_num=0)       
        model.cuda()
        model = DataParallel(model)    
        model_list.append(model)
   
    writer = SummaryWriter(log_dir='log-output/'+osp.basename(args.logs_dir))

    '''test the models under a folder'''
    if args.test_folder:
        ckpt_name = [x + '_checkpoint-{}.pth.tar'.format(args.n_model-1) for x in training_set]   # obatin pretrained model name
        checkpoint = load_checkpoint(osp.join(args.test_folder, ckpt_name[0]))  # load the first model
        copy_state_dict(checkpoint['state_dict'], model)     #    
        for step in range(len(ckpt_name) - 1):
            model_old = copy.deepcopy(model)    # backup the old model            
            checkpoint = load_checkpoint(osp.join(args.test_folder, ckpt_name[step + 1]))
            copy_state_dict(checkpoint['state_dict'], model)
                                    
            best_alpha = get_adaptive_alpha(args, model, model_old, all_train_sets, step + 1,checkpoint['psedo_dist'][args.n_model-1])
            model = linear_combination(args, model, model_old, best_alpha)

            save_name = '{}_checkpoint_adaptive_ema_{:.4f}.pth.tar'.format(training_set[step+1], best_alpha)
            save_checkpoint({
                'state_dict': model.state_dict(),
                'epoch': 0,
                'mAP': 0,
            }, True, fpath=osp.join(args.logs_dir, save_name))
        fast_test_p_s(model, all_train_sets, all_test_only_sets, set_index=len(all_train_sets)-1, logger=logger_res,
                      args=args,writer=writer)
        exit(0)
       
    # Evaluator
    if args.MODEL in ['50x']:
        out_channel = 2048
    else:
        raise AssertionError(f"the model {args.MODEL} is not supported!")

    # train on the datasets squentially    
    for set_index in range(len(training_set)):          
        model_old_list=[copy.deepcopy(m) for m in model_list]     
        model_list,psedo_dist = train_dataset(args, all_train_sets, all_test_only_sets, set_index,model_list, out_channel,
                                            writer,logger_res=logger_res)
        if set_index>0:
            for i in range(args.n_model):                                             
                best_alpha = get_adaptive_alpha(args, model_list[i], model_old_list[i], all_train_sets, set_index,psedo_dist[i])
               
                logger_res.append('********combining new model and old model with alpha {}********\n'.format(best_alpha))
                print('********combining new model and old model with alpha {}********\n'.format(best_alpha))
                model_list[i] = linear_combination(args, model_list[i], model_old_list[i], best_alpha)           
                
                logger_res.append("*******testing the model-{} for {}*********".format(i+1,all_train_sets[i][-1]))
                print("*******testing the model-{} for {}*********".format(i+1,all_train_sets[i][-1]))               
                mAP =fast_test_p_s(model_list[i], all_train_sets, all_test_only_sets, set_index=set_index, logger=logger_res,
                          args=args,writer=writer)  
    print('finished')
def get_normal_affinity(x,Norm=100):
    from reid.metric_learning.distance import cosine_similarity
    pre_matrix_origin=cosine_similarity(x,x)
    pre_affinity_matrix=F.softmax(pre_matrix_origin*Norm, dim=1)
    return pre_affinity_matrix
def get_adaptive_alpha(args, model, model_old, all_train_sets, set_index,psedo_dist):
    dataset_new, num_classes_new, train_loader_new, _, init_loader_new, name_new = all_train_sets[
        set_index]  # trainloader of current dataset
    features_all_new, labels_all, fnames_all, camids_all, features_mean_new, labels_named = extract_features_voro(model,
                                                                                                          init_loader_new,
                                                                                                          get_mean_feature=True)
    features_all_old, _, _, _, features_mean_old, _ = extract_features_voro(model_old,init_loader_new,get_mean_feature=True)
         
    # Keep=(psedo_dist<2.0).to(features_all_new[0].device)
    Keep=psedo_dist.to(features_all_new[0].device)
    Keep=(2-Keep)/2>args.T_o

    features_all_new=torch.stack(features_all_new, dim=0)[Keep]
    features_all_old=torch.stack(features_all_old,dim=0)[Keep]
    Affin_new = get_normal_affinity(features_all_new)
    Affin_old = get_normal_affinity(features_all_old)

    Difference= torch.abs(Affin_new-Affin_old).sum(-1).mean()

    alpha=float(1-Difference)
    return alpha



def train_dataset(args, all_train_sets, all_test_only_sets, set_index, model_list, out_channel, writer,logger_res=None):
    dataset, num_classes, train_loader, test_loader, init_loader, name = all_train_sets[
        set_index]  # status of current dataset   
    Epochs= args.epochs0 if 0==set_index else args.epochs          
   
    model_old_list=[]
    if set_index<=0:
        add_num = 0
        old_model=None
    elif set_index>0:        
        # after sampling rehearsal, recalculate the addnum(historical ID number)
        add_num = sum([all_train_sets[i][1] for i in range(set_index)])  # get model out_dim
        for i in range(args.n_model):
            model = model_list[i]   # fetch a model
            # Expand the dimension of classifier
            org_classifier_params = model.module.classifier.weight.data
            model.module.classifier = nn.Linear(out_channel, add_num + num_classes, bias=False) # reinitialize classifier
            model.module.classifier.weight.data[:add_num].copy_(org_classifier_params)  # store the learned paprameter
            model.cuda()    
            # Initialize classifer with class centers    
            class_centers = initial_classifier(model, init_loader)  # obtain the feature centers of new IDs
            model.module.classifier.weight.data[add_num:].copy_(class_centers)  # initialize the classifiers of the new IDs
            model.cuda()
            '''store the old model'''
            old_model = copy.deepcopy(model)    # copy the old model
            old_model = old_model.cuda()    # 
            old_model.eval()
            model_list[i]=model
            model_old_list.append(old_model)   
            

    optimizer_list=[]
    lr_scheduler_list=[]
    for i in range(args.n_model):
        model=model_list[i]
        # Re-initialize optimizer
        params = []
        for key, value in model.named_params(model):
            if not value.requires_grad:
                print('not requires_grad:', key)
                continue
            params += [{"params": [value], "lr": args.lr, "weight_decay": args.weight_decay}]
        if args.optimizer == 'Adam':
            optimizer = torch.optim.Adam(params)
        elif args.optimizer == 'SGD':
            optimizer = torch.optim.SGD(params, momentum=args.momentum)    
        Stones=args.milestones
        lr_scheduler = WarmupMultiStepLR(optimizer, Stones, gamma=0.1, warmup_factor=0.01, warmup_iters=args.warmup_step)

        optimizer_list.append(optimizer)
        lr_scheduler_list.append(lr_scheduler)
    
    trainer = Trainer(args, model_list, model_old_list, add_num + num_classes, dataset.train, writer=writer)

    if set_index>=0:
        trainer.obtain_cluster(init_loader, add_num,trainer.model_list, dataset_name=name) # execute clustering

    print('####### starting training on {} #######'.format(name))
    weight_r=torch.zeros(args.n_model).cuda()
    for epoch in range(0, Epochs):
        train_loader.new_epoch()
        weight_r=trainer.train(epoch, train_loader,  optimizer_list, training_phase=set_index + 1,
                      train_iters=len(train_loader), add_num=add_num,weight_r=weight_r, eval_loader=init_loader,dataset=dataset
                      )        
        for i in range(args.n_model):
            lr_scheduler_list[i].step()       
       
        if ((epoch + 1) % args.eval_epoch == 0 or epoch+1==Epochs):
            for i in range(args.n_model):
                model=model_list[i]
                save_checkpoint({
                    'state_dict': model.state_dict(),
                    'epoch': epoch + 1,
                    'mAP': 0.,
                }, True, fpath=osp.join(args.logs_dir, '{}_checkpoint-{}.pth.tar'.format(name,i)))

                logger_res.append('epoch: {}'.format(epoch + 1))
                
                mAP=0.
                args.middle_test=True
                if args.middle_test and set_index==0: 
                    mAP =fast_test_p_s(model, all_train_sets, all_test_only_sets, set_index=set_index, logger=logger_res,
                          args=args,writer=writer)                
                print("saving model to:",osp.join(args.logs_dir, '{}_checkpoint-{}.pth.tar'.format(name,i)))
                save_checkpoint({
                    'state_dict': model.state_dict(),
                    'epoch': epoch + 1,
                    'mAP': mAP,
                    'psedo_dist':trainer.psedo_dist_old
                }, True, fpath=osp.join(args.logs_dir, '{}_checkpoint-{}.pth.tar'.format(name,i)))    

    return model_list, trainer.psedo_dist_old



def linear_combination(args, model, model_old, alpha, model_old_id=-1):
    print('********combining new model and old model with alpha {}********\n'.format(alpha))
    '''old model '''
    model_old_state_dict = model_old.state_dict()
    '''latest trained model'''
    model_state_dict = model.state_dict()
    ''''create new model'''
    model_new = copy.deepcopy(model)
    model_new_state_dict = model_new.state_dict()
    '''fuse the parameters'''
    for k, v in model_state_dict.items():
        if model_old_state_dict[k].shape == v.shape:
            # print(k,'+++')
                model_new_state_dict[k] = alpha * v + (1 - alpha) * model_old_state_dict[k]
        else:
            print(k, '...')
            num_class_old = model_old_state_dict[k].shape[0]
            model_new_state_dict[k][:num_class_old] = alpha * v[:num_class_old] + (1 - alpha) * model_old_state_dict[k]
    model_new.load_state_dict(model_new_state_dict)
    return model_new


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Continual training for lifelong person re-identification")
    # data
    parser.add_argument('-b', '--batch-size', type=int, default=128)
    parser.add_argument('-j', '--workers', type=int, default=4)
    parser.add_argument('--height', type=int, default=256, help="input height")
    parser.add_argument('--width', type=int, default=128, help="input width")
    parser.add_argument('--num-instances', type=int, default=4,
                        help="each minibatch consist of "
                             "(batch_size // num_instances) identities, and "
                             "each identity has num_instances instances, "
                             "default: 0 (NOT USE)")
    # model    
    parser.add_argument('--MODEL', type=str, default='50x',
                        choices=['50x'])
    # optimizer
    parser.add_argument('--optimizer', type=str, default='SGD', choices=['SGD', 'Adam'],
                        help="optimizer ")
    parser.add_argument('--lr', type=float, default=0.008,
                        help="learning rate of new parameters, for pretrained ")
    parser.add_argument('--momentum', type=float, default=0.9)
    parser.add_argument('--weight-decay', type=float, default=1e-4)
    parser.add_argument('--warmup-step', type=int, default=10)
    parser.add_argument('--milestones', nargs='+', type=int, default=[20,50],
                        help='milestones for the learning rate decay')
    # training configs
    parser.add_argument('--resume', type=str, default=None, metavar='PATH')
    
    parser.add_argument('--epochs0', type=int, default=80)
    parser.add_argument('--epochs', type=int, default=60)
    parser.add_argument('--eval_epoch', type=int, default=100)
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--print-freq', type=int, default=200)
    parser.add_argument('--plot-freq', type=int, default=10)
    
    # path   
    parser.add_argument('--data-dir', type=str, metavar='PATH',
                        default='/home/xukunlun/DATA/PRID')
    parser.add_argument('--logs-dir', type=str, metavar='PATH',
                        default=osp.join('../logs/try'))

  
    parser.add_argument('--test_folder', type=str, default=None, help="test the models in a folder")

    parser.add_argument('--setting', type=int, default=1, choices=[1, 2], help="training order setting")
    parser.add_argument('--middle_test', action='store_true', help="test during middle step")
    parser.add_argument('--AF_weight', default=1.0, type=float, help="anti-forgetting weight")    

    parser.add_argument('--noise_ratio', type=float,default=0.1, help='noise_ratio')
    parser.add_argument('--noise', type=str,default='clean',choices=['clean','random','pattern'], help='noise type')
   
    
    parser.add_argument('--n_model', type=int,default=2, help="the number of models")
    parser.add_argument('--save_evaluation', action='store_true', help="save ranking results")
    
    parser.add_argument('--T_c', type=float,default=0.8, help="the threshold for noisy label filtering")
    parser.add_argument('--w_l', type=float,default=0.1, help="the threshold for noisy label filtering")
    parser.add_argument('--T_o', type=float,default=0.2, help="the threshold for noisy label filtering")
    parser.add_argument('--cluster_stride', type=int,default=5, help="the stride between cluster epochs")
    main()
