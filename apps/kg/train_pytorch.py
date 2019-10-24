from models import KEModel
from dataloader import create_test_sampler, create_train_sampler, NewBidirectionalOneShotIterator

from torch.utils.data import DataLoader
import torch.optim as optim
import torch as th
import torch.multiprocessing as mp

import dgl

from distutils.version import LooseVersion
TH_VERSION = LooseVersion(th.__version__)
if TH_VERSION.version[0] == 1 and TH_VERSION.version[1] < 2:
    raise Exception("DGL-ke has to work with Pytorch version >= 1.2")

import os
import logging
import time

def load_model(logger, args, n_entities, n_relations, ckpt=None):
    model = KEModel(args, args.model_name, n_entities, n_relations,
                    args.hidden_dim, args.gamma,
                    double_entity_emb=args.double_ent, double_relation_emb=args.double_rel)
    if ckpt is not None:
        # TODO: loading model emb only work for genernal Embedding, not for ExternalEmbedding
        model.load_state_dict(ckpt['model_state_dict'])
    return model


def load_model_from_checkpoint(logger, args, n_entities, n_relations, ckpt_path):
    model = load_model(logger, args, n_entities, n_relations)
    model.load_emb(ckpt_path, args.dataset)
    return model

def multi_gpu_train(args, model, graph, n_entities, edges, rank):
    if args.num_proc > 1:
        th.set_num_threads(1)
    gpu_id = rank % args.gpu if args.mix_cpu_gpu and args.num_proc > 1 else -1
    model.create_neg()
    train_sampler_head = create_train_sampler(graph, args.batch_size, args.neg_sample_size,
                                                       mode='PBG-head',
                                                       num_workers=args.num_worker,
                                                       shuffle=True,
                                                       exclude_positive=True)
    train_sampler_tail = create_train_sampler(graph, args.batch_size, args.neg_sample_size,
                                                       mode='PBG-tail',
                                                       num_workers=args.num_worker,
                                                       shuffle=True,
                                                       exclude_positive=True)
    train_sampler = NewBidirectionalOneShotIterator(train_sampler_head, train_sampler_tail,
                                                        True, n_entities)
    if args.valid:
        graph = dgl.contrib.graph_store.create_graph_from_store('Test', store_type="shared_mem")
        valid_sampler_head = create_test_sampler(graph, edges, args.batch_size_eval,
                                                            args.neg_sample_size_test,
                                                            mode='PBG-head',
                                                            num_workers=args.num_worker,
                                                            rank=rank, ranks=args.num_proc)
        valid_sampler_tail = create_test_sampler(graph, edges, args.batch_size_eval,
                                                            args.neg_sample_size_test,
                                                            mode='PBG-tail',
                                                            num_workers=args.num_worker,
                                                            rank=rank, ranks=args.num_proc)
        valid_samplers = [valid_sampler_head, valid_sampler_tail]
    logs = []
    for arg in vars(args):
        logging.info('{:20}:{}'.format(arg, getattr(args, arg)))

    start = time.time()
    update_time = 0
    forward_time = 0
    backward_time = 0
    for step in range(args.init_step, args.max_step):
        pos_g, neg_g = next(train_sampler)
        args.step = step

        start1 = time.time()
        loss, log = model.forward(pos_g, neg_g, gpu_id)
        forward_time += time.time() - start1

        start1 = time.time()
        loss.backward()
        backward_time += time.time() - start1

        start1 = time.time()
        model.update(gpu_id)
        update_time += time.time() - start1
        logs.append(log)

        if step % args.log_interval == 0:
            for k in logs[0].keys():
                v = sum(l[k] for l in logs) / len(logs)
                print('[Train]({}/{}) average {}: {}'.format(step, args.max_step, k, v))
            logs = []
            print('[Train] {} steps take {:.3f} seconds'.format(args.log_interval,
                                                            time.time() - start))
            print('forward: {:.3f}, backward: {:.3f}, update: {:.3f}'.format(forward_time,
                                                                             backward_time,
                                                                             update_time))
            update_time = 0
            forward_time = 0
            backward_time = 0
            start = time.time()

        if args.valid and step % args.eval_interval == 0 and step > 1 and valid_samplers is not None:
            start = time.time()
            test(args, model, valid_samplers, gpu_id, mode='Valid')
            print('test:', time.time() - start)

def test(args, model, test_samplers, gpu_id, mode='Test'):
    if args.num_proc > 1:
        th.set_num_threads(1)
    start = time.time()
    with th.no_grad():
        logs = []
        for sampler in test_samplers:
            count = 0
            for pos_g, neg_g in sampler:
                with th.no_grad():
                    model.forward_test(pos_g, neg_g, logs, gpu_id)

        metrics = {}
        if len(logs) > 0:
            for metric in logs[0].keys():
                metrics[metric] = sum([log[metric] for log in logs]) / len(logs)

        for k, v in metrics.items():
            print('{} average {} at [{}/{}]: {}'.format(mode, k, args.step, args.max_step, v))
    print('test:', time.time() - start)
    test_samplers[0] = test_samplers[0].reset()
    test_samplers[1] = test_samplers[1].reset()

def multi_gpu_test(args, model, graph_name, edges, rank, mode='Test'):
    if args.num_proc > 1:
        th.set_num_threads(1)
    gpu_id = rank % args.gpu if args.mix_cpu_gpu and args.num_proc > 1 else -1
    graph = dgl.contrib.graph_store.create_graph_from_store(graph_name, store_type="shared_mem")
    test_sampler_head = create_test_sampler(graph, edges, args.batch_size_eval,
                                                            args.neg_sample_size_test,
                                                            mode='PBG-head',
                                                            num_workers=args.num_worker,
                                                            rank=rank, ranks=args.num_proc)
    test_sampler_tail = create_test_sampler(graph, edges, args.batch_size_eval,
                                                            args.neg_sample_size_test,
                                                            mode='PBG-tail',
                                                            num_workers=args.num_worker,
                                                            rank=rank, ranks=args.num_proc)
    test_samplers = [test_sampler_head, test_sampler_tail]
    start = time.time()
    with th.no_grad():
        logs = []
        for sampler in test_samplers:
            count = 0
            for pos_g, neg_g in sampler:
                with th.no_grad():
                    model.forward_test(pos_g, neg_g, logs, gpu_id)

        metrics = {}
        if len(logs) > 0:
            for metric in logs[0].keys():
                metrics[metric] = sum([log[metric] for log in logs]) / len(logs)

        for k, v in metrics.items():
            print('{} average {} at [{}/{}]: {}'.format(mode, k, args.step, args.max_step, v))
    print('test:', time.time() - start)
    test_samplers[0] = test_samplers[0].reset()
    test_samplers[1] = test_samplers[1].reset()
