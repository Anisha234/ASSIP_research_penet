import cv2
import json
import pickle
import numpy as np
import os
import sklearn.metrics as sk_metrics
import torch
import torch.nn.functional as F
import util
import pandas as pd
from args import TestArgParser
from data_loader import CTDataLoader
from collections import defaultdict
from logger import TestLogger
from PIL import Image
from saver import ModelSaver
from tqdm import tqdm


def test(args):
    print ("Stage 1")
    model, ckpt_info = ModelSaver.load_model(args.ckpt_path, args.gpu_ids)
    print ("Stage 2")
    args.start_epoch = ckpt_info['epoch'] + 1
    model = model.to(args.device)
    print ("Stage 3")
    model.eval()
    print ("Stage 4")
    data_loader = CTDataLoader(args, phase=args.phase, is_training=False)
    study2slices = defaultdict(list)
    study2probs = defaultdict(list)
    study2labels = {}
    logger = TestLogger(args, len(data_loader.dataset), data_loader.dataset.pixel_dict)

    means = []

    # Get model outputs, log to TensorBoard, write masks to disk window-by-window
    util.print_err('Writing model outputs to {}...'.format(args.results_dir))
    patient_list = []
    patient_slice = []
    patient_prob = []
    patient_label = []
    patient_emb = []
    result_dict ={}
    with tqdm(total=len(data_loader.dataset), unit=' windows') as progress_bar:
        for i, (inputs, targets_dict) in enumerate(data_loader):
            means.append(inputs.mean().item())
            with torch.autocast(device_type=args.device, dtype=torch.float16, enabled=True):
                with torch.no_grad():
                    cls_logits,emb = model.forward(inputs.to(args.device))
                    if args.predict_num_slices:
                        cls_probs = torch.clip(cls_logits, 0, 1)
                    else:
                        cls_probs = F.sigmoid(cls_logits)

            if args.visualize_all:
                logger.visualize(inputs, cls_logits, targets_dict=None, phase=args.phase, unique_id=i)

            max_probs = cls_probs.to('cpu').numpy()
            emb = emb.to('cpu').numpy()
            cnt=0
            for study_num, slice_idx, prob in \
                    zip(targets_dict['study_num'], targets_dict['slice_idx'], list(max_probs)):
                
                # Convert to standard python data types
                study_num = int(study_num)
                slice_idx = int(slice_idx)
           
                # Save series num for aggregation
                study2slices[study_num].append(slice_idx)
                study2probs[study_num].append(prob.item())

                series = data_loader.get_series(study_num)
                if study_num not in study2labels:
                    study2labels[study_num] = int(series.is_positive) and (len(series.pe_idxs)>0)
                patient_list.append(study_num)
                patient_slice.append(slice_idx//args.num_slices)
                patient_prob.append(prob.item())
                patient_label.append(int(series.is_positive) and (len(series.pe_idxs) > 0))
                patient_emb.append(emb[cnt,:])
                cnt = cnt+1

            progress_bar.update(inputs.size(0))
    print(len(patient_emb),len(patient_list), patient_emb[0].shape)
    result_dict = {'patient':patient_list, 'slice': patient_slice, 'prob':patient_prob, 'label':patient_label}
    result_pkl = {'patient':patient_list, 'slice': patient_slice, 'prob':patient_prob, 'label':patient_label, 'emb':patient_emb}
    #Save predictions to file, indexed by study number
    print("Saving embeddings to embs.pickle")
    with open('{}/embs.pickle'.format(args.results_dir),"wb") as fp:
        pickle.dump(result_pkl,fp)

    df = pd.DataFrame(result_dict)
    df.to_csv('{}/test_results.csv'.format(args.results_dir))
    # Combine masks
    util.print_err('Combining masks...')
    max_probs = []
    labels = []
    model_pred=[]

    
    predictions = {}
    print("Get max probability")
    for study_num in tqdm(study2slices):

        # Sort by slice index and get max probability
        slice_list, prob_list = (list(t) for t in zip(*sorted(zip(study2slices[study_num], study2probs[study_num]),
                                                              key=lambda slice_and_prob: slice_and_prob[0])))
        study2slices[study_num] = slice_list
        study2probs[study_num] = prob_list
        max_prob = max(prob_list)
        max_probs.append(max_prob)
        model_pred.append(max_prob>0.5)
        label = study2labels[study_num]
        labels.append(label)
        predictions[study_num] = {'label':label, 'pred':max_prob}

    #Save predictions to file, indexed by study number
    print("Save to pickle")
    with open('{}/preds.pickle'.format(args.results_dir),"wb") as fp:
        pickle.dump(predictions,fp)
        
    # Write features for XGBoost
    save_for_xgb(args.results_dir, study2probs, study2labels)
    # Write the slice indices used for the features
    print("Write slice indices")
    with open(os.path.join(args.results_dir, 'xgb', 'series2slices.json'), 'w') as json_fh:
        json.dump(study2slices, json_fh, sort_keys=True, indent=4)

    # Compute AUROC and AUPRC using max aggregation, write to files
    max_probs, labels = np.array(max_probs), np.array(labels)
    conf_mtx = sk_metrics.confusion_matrix(labels, model_pred)
    metrics = {
        args.phase + '_' + 'AUPRC': sk_metrics.average_precision_score(labels, max_probs),
        args.phase + '_' + 'AUROC': sk_metrics.roc_auc_score(labels, max_probs),
        args.phase + '_' + 'cm[0,0]': conf_mtx[0,0],
        args.phase + '_' + 'cm[0,1]': conf_mtx[0,1],
        args.phase + '_' + 'cm[1,0]': conf_mtx[1,0],
        args.phase + '_' + 'cm[1,1]': conf_mtx[1,1],
    }
    print("Write metrics")
    with open(os.path.join(args.results_dir, 'metrics.txt'), 'w') as metrics_fh:
        metrics_fh.write(args.ckpt_path)
        for k, v in metrics.items():
            metrics_fh.write('{}: {:.5f}\n'.format(k, v))

    curves = {
        args.phase + '_' + 'PRC': sk_metrics.precision_recall_curve(labels, max_probs),
        args.phase + '_' + 'ROC': sk_metrics.roc_curve(labels, max_probs)
    }
    for name, curve in curves.items():
        curve_np = util.get_plot(name, curve)
        curve_img = Image.fromarray(curve_np)
        curve_img.save(os.path.join(args.results_dir, '{}.png'.format(name)))


def save_for_xgb(results_dir, series2probs, series2labels):
    """Write window-level and series-level features to train an XGBoost classifier.
    Args:
        results_dir: Path to results directory for writing outputs.
        series2probs: Dict mapping series numbers to probabilities.
        series2labels: Dict mapping series numbers to labels.
    """

    # Convert to numpy
    xgb_inputs = np.zeros([len(series2probs), max(len(p) for p in series2probs.values())])
    xgb_labels = np.zeros(len(series2labels))
    for i, (series_num, probs) in enumerate(series2probs.items()):
        xgb_inputs[i, :len(probs)] = np.array(probs).ravel()
        xgb_labels[i] = series2labels[series_num]

    # Write to disk
    os.makedirs(os.path.join(results_dir, 'xgb'), exist_ok=True)
    xgb_inputs_path = os.path.join(results_dir, 'xgb', 'inputs.npy')
    xgb_labels_path = os.path.join(results_dir, 'xgb', 'labels.npy')
    np.save(xgb_inputs_path, xgb_inputs)
    np.save(xgb_labels_path, xgb_labels)


if __name__ == '__main__':
    util.set_spawn_enabled()
    parser = TestArgParser()
    args_ = parser.parse_args()
    test(args_)
