import argparse
import itertools
import sys
import warnings
from glob import glob
from typing import List

import numpy as np
import yaml
from tqdm import tqdm

sys.path.append('../')
from utils.attribute_hashmap import AttributeHashmap
from utils.diffusion_condensation import continuous_renumber, most_persistent_structures
from utils.metrics import dice_coeff, ergas, rmse, ssim
from utils.parse import parse_settings
from utils.segmentation import point_hint_seg

warnings.filterwarnings("ignore")


def load_baselines(path: str) -> dict:
    numpy_array = np.load(path)
    hashmap = {}
    hashmap['image'] = numpy_array['image']
    hashmap['label_true'] = numpy_array['label']
    hashmap['label_random'] = numpy_array['label_random']
    hashmap['label_watershed'] = numpy_array['label_watershed']
    hashmap['label_felzenszwalb'] = numpy_array['label_felzenszwalb']
    return hashmap


def load_kmeans(path: str) -> dict:
    numpy_array = np.load(path)
    hashmap = {}
    hashmap['image'] = numpy_array['image']
    hashmap['label_true'] = numpy_array['label']
    hashmap['latent'] = numpy_array['latent']
    hashmap['label_kmeans'] = numpy_array['label_kmeans']
    hashmap['seg_kmeans'] = numpy_array['seg_kmeans']
    return hashmap


def load_diffusion(path: str) -> dict:
    numpy_array = np.load(path)
    hashmap = {}
    hashmap['image'] = numpy_array['image']
    hashmap['recon'] = numpy_array['recon']
    hashmap['label_true'] = numpy_array['label']
    hashmap['latent'] = numpy_array['latent']
    hashmap['granularities_diffusion'] = numpy_array['granularities_diffusion']
    hashmap['labels_diffusion'] = numpy_array['labels_diffusion']
    return hashmap


def combine_hashmaps(*args: dict) -> dict:
    combined = {}
    for hashmap in args:
        for k in hashmap.keys():
            combined[k] = hashmap[k]
    return combined


def segment_diffusion(hashmap: dict, hparams: dict) -> dict:
    '''
    Produce segmentation from the diffusion condensation results.
    '''
    label_true = hashmap['label_true']
    labels_diffusion = hashmap['labels_diffusion']

    H, W = label_true.shape
    B = labels_diffusion.shape[0]
    labels_diffusion = labels_diffusion.reshape((B, H, W))
    # persistent_label = continuous_renumber(labels_diffusion[B // 2, ...])
    persistent_label, _ = most_persistent_structures(
        labels_diffusion,
        min_frame_ratio=hparams.min_frame_ratio,
        min_area_ratio=hparams.min_area_ratio)

    seg = point_hint_seg(label_pred=persistent_label, label_true=label_true)
    hashmap['seg_diffusion'] = seg
    hashmap['label_diffusion'] = persistent_label

    return hashmap


# def metric_permuted_label(fn, mode: str, permutee: np.array,
#                           other_array: np.array) -> List[np.array]:
#     '''
#     Return the (min or max) metric:
#         fn(permutee, other_array)
#     as we permute the label indices of `permutee`.

#     NOTE: Nice try. But it takes too long as it runs each metric several million times.
#     '''
#     indices_from = sorted(list(set(np.unique(permutee)) - set([0])))

#     assert mode in ['min', 'max']

#     if mode == 'min':
#         best_metric = np.inf
#     elif mode == 'max':
#         best_metric = -np.inf

#     for indices_to in itertools.permutations(indices_from):
#         permuted = np.zeros_like(permutee)
#         assert len(indices_from) == len(indices_to)
#         for (i, j) in zip(indices_from, indices_to):
#             permuted[permutee == i] = j

#         metric = fn(permuted, other_array)
#         if mode == 'min':
#             best_metric = min(best_metric, metric)
#         elif mode == 'max':
#             best_metric = max(best_metric, metric)

#     return best_metric


def guided_relabel(label_pred: np.array, label_true: np.array) -> np.array:
    '''
    Relabel (i.e., update label index) `label_pred` such that it best matches `label_true`.

    For each label index, assign an one-hot vector (flattened pixel values),
    and compute the IOU among each pair of such one-hot vectors b/w `label_pred` and `label_true`.
    '''
    assert label_pred.shape == label_true.shape
    H, W = label_pred.shape

    label_pred_vec = np.array(
        [label_pred.reshape(H * W) == i for i in np.unique(label_pred)],
        dtype=np.int16)
    label_true_vec = np.array(
        [label_true.reshape(H * W) == i for i in np.unique(label_true)],
        dtype=np.int16)

    # Use matrix multiplication to get intersection matrix.
    intersection_matrix = np.matmul(label_pred_vec, label_true_vec.T)

    # Use matrix multiplication to get union matrix.
    union_matrix = H * W - np.matmul(1 - label_pred_vec,
                                     (1 - label_true_vec).T)

    iou_matrix = intersection_matrix / union_matrix

    renumbered_label_pred = np.zeros_like(label_pred)
    for i, label_pred_idx in enumerate(np.unique(label_pred)):
        pix_loc = label_pred == label_pred_idx
        label_true_idx = np.unique(label_true)[np.argmax(iou_matrix[i, :])]
        renumbered_label_pred[pix_loc] = label_true_idx

    return renumbered_label_pred


def range_aware_ssim(a: np.array, b: np.array) -> float:
    '''
    Surprisingly, skimage ssim infers data range from data type...
    It's okay within our neural network training since the scale is
    quite cloes to its guess (-1 to 1 for float numbers), but
    surely not okay here.
    '''
    data_max = max(a.max(), b.max())
    data_min = min(a.min(), b.min())
    data_range = data_max - data_min

    return ssim(a=a, b=b, data_range=data_range)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config',
                        help='Path to config yaml file.',
                        required=True)
    args = vars(parser.parse_args())
    args = AttributeHashmap(args)

    config = AttributeHashmap(yaml.safe_load(open(args.config)))
    config.config_file_name = args.config
    config = parse_settings(config, log_settings=False)

    if config.dataset_name == 'retina':
        hparams = AttributeHashmap({
            'is_binary': True,
            'min_frame_ratio': 1 / 2,
            'min_area_ratio': 1 / 200,
        })
    elif config.dataset_name == 'berkeley':
        hparams = AttributeHashmap({
            'is_binary': False,
            'min_frame_ratio': 1 / 2,
            'min_area_ratio': 1 / 500,
        })

    files_folder_baselines = '%s/%s' % (config.output_save_path,
                                        'numpy_files_seg_baselines')
    files_folder_kmeans = '%s/%s' % (config.output_save_path,
                                     'numpy_files_seg_kmeans')
    files_folder_diffusion = '%s/%s' % (config.output_save_path,
                                        'numpy_files_seg_diffusion')

    np_files_path_baselines = sorted(
        glob('%s/%s' % (files_folder_baselines, '*.npz')))
    np_files_path_kmeans = sorted(
        glob('%s/%s' % (files_folder_kmeans, '*.npz')))
    np_files_path_diffusion = sorted(
        glob('%s/%s' % (files_folder_diffusion, '*.npz')))

    assert len(np_files_path_baselines) == len(np_files_path_kmeans)
    assert len(np_files_path_baselines) == len(np_files_path_diffusion)

    entity_tuples = [
        ('random', 'label_true', 'label_random'),
        ('watershed', 'label_true', 'label_watershed'),
        ('felzenszwalb', 'label_true', 'label_felzenszwalb'),
        ('ours (kmeans, multiclass)', 'label_true', 'label_kmeans'),
        ('ours (kmeans, binary)', 'label_true', 'seg_kmeans'),
        ('ours (diffusion, multiclass)', 'label_true', 'label_diffusion'),
        ('ours (diffusion, binary)', 'label_true', 'seg_diffusion'),
    ]

    metrics = {
        'dice': {tup[0]: []
                 for tup in entity_tuples},
        'ssim': {tup[0]: []
                 for tup in entity_tuples},
        'ergas': {tup[0]: []
                  for tup in entity_tuples},
        'rmse': {tup[0]: []
                 for tup in entity_tuples},
    }

    for image_idx in tqdm(range(len(np_files_path_baselines))):
        baselines_hashmap = load_baselines(np_files_path_baselines[image_idx])
        kmeans_hashmap = load_kmeans(np_files_path_kmeans[image_idx])
        diffusion_hashmap = load_diffusion(np_files_path_diffusion[image_idx])

        assert (baselines_hashmap['image'] == kmeans_hashmap['image']
                ).all() and (baselines_hashmap['image']
                             == diffusion_hashmap['image']).all()
        assert (baselines_hashmap['label_true'] == kmeans_hashmap['label_true']
                ).all() and (baselines_hashmap['label_true']
                             == diffusion_hashmap['label_true']).all()

        hashmap = combine_hashmaps(baselines_hashmap, kmeans_hashmap,
                                   diffusion_hashmap)

        # hashmap['label_true'] = hashmap['label_true'].astype(np.int16)
        hashmap = segment_diffusion(hashmap, hparams)

        for (entry, p1, p2) in entity_tuples:
            if hparams.is_binary:
                metrics['dice'][entry].append(
                    dice_coeff(hashmap[p1], hashmap[p2]))
                metrics['ssim'][entry].append(
                    range_aware_ssim(hashmap[p1], hashmap[p2]))
                metrics['ergas'][entry].append(ergas(hashmap[p1], hashmap[p2]))
                metrics['rmse'][entry].append(rmse(hashmap[p1], hashmap[p2]))
            else:
                metrics['ssim'][entry].append(
                    range_aware_ssim(
                        hashmap[p1],
                        guided_relabel(label_pred=hashmap[p2],
                                       label_true=hashmap[p1])))
                metrics['ergas'][entry].append(
                    ergas(
                        hashmap[p1],
                        guided_relabel(label_pred=hashmap[p2],
                                       label_true=hashmap[p1])))
                metrics['rmse'][entry].append(
                    rmse(
                        hashmap[p1],
                        guided_relabel(label_pred=hashmap[p2],
                                       label_true=hashmap[p1])))

    if hparams.is_binary:
        print('\n\nDice Coefficient')
        for (entry, _, _) in entity_tuples:
            print('%s: %.3f \u00B1 %.3f' %
                  (entry, np.mean(
                      metrics['dice'][entry]), np.std(metrics['dice'][entry]) /
                   np.sqrt(len(metrics['dice'][entry]))))

    print('\n\nSSIM')
    for (entry, _, _) in entity_tuples:
        print('%s: %.3f \u00B1 %.3f' % (entry, np.mean(
            metrics['ssim'][entry]), np.std(metrics['ssim'][entry]) /
                                        np.sqrt(len(metrics['ssim'][entry]))))

    print('\n\nERGAS')
    for (entry, _, _) in entity_tuples:
        print('%s: %.3f \u00B1 %.3f' % (entry, np.mean(
            metrics['ergas'][entry]), np.std(metrics['ergas'][entry]) /
                                        np.sqrt(len(metrics['ergas'][entry]))))

    print('\n\nRMSE')
    for (entry, _, _) in entity_tuples:
        print('%s: %.3f \u00B1 %.3f' % (entry, np.mean(
            metrics['rmse'][entry]), np.std(metrics['rmse'][entry]) /
                                        np.sqrt(len(metrics['rmse'][entry]))))