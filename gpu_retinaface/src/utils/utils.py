# Copyright 2020 Huawei Technologies Co., Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
""" Utils. TODO: 函数过多，对函数进行归类放在不同的文件里面，TODO: 注释不规范 """
from itertools import product
import math
import numpy as np
import time
import cv2


def prior_box(image_sizes, min_sizes, steps, clip=False):
    """Getnerate anchor"""
    feature_maps = [
        [math.ceil(image_sizes[0] / step), math.ceil(image_sizes[1] / step)]
        for step in steps]

    anchors = []
    for k, f in enumerate(feature_maps):
        for i, j in product(range(f[0]), range(f[1])):
            for min_size in min_sizes[k]:
                s_kx = min_size / image_sizes[1]
                s_ky = min_size / image_sizes[0]
                cx = (j + 0.5) * steps[k] / image_sizes[1]
                cy = (i + 0.5) * steps[k] / image_sizes[0]
                anchors += [cx, cy, s_kx, s_ky]

    output = np.asarray(anchors).reshape([-1, 4]).astype(np.float32)

    if clip:
        output = np.clip(output, 0, 1)

    return output


def center_point_2_box(boxes):
    "Get box coordinate by center point."
    return np.concatenate((boxes[:, 0:2] - boxes[:, 2:4] / 2,
                           boxes[:, 0:2] + boxes[:, 2:4] / 2), axis=1)


def compute_intersect(a, b):
    "Compute the intersection area."
    A = a.shape[0]
    B = b.shape[0]

    max_xy = np.minimum(
        np.broadcast_to(np.expand_dims(a[:, 2:4], 1), [A, B, 2]),
        np.broadcast_to(np.expand_dims(b[:, 2:4], 0), [A, B, 2]))

    min_xy = np.maximum(
        np.broadcast_to(np.expand_dims(a[:, 0:2], 1), [A, B, 2]),
        np.broadcast_to(np.expand_dims(b[:, 0:2], 0), [A, B, 2]))

    inter = np.maximum((max_xy - min_xy), np.zeros_like(max_xy - min_xy))
    return inter[:, :, 0] * inter[:, :, 1]


def compute_overlaps(a, b):
    "Compute the IOU value."
    inter = compute_intersect(a, b)
    area_a = np.broadcast_to(
        np.expand_dims(
            (a[:, 2] - a[:, 0]) * (a[:, 3] - a[:, 1]), 1),
        np.shape(inter))
    area_b = np.broadcast_to(
        np.expand_dims(
            (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1]), 0),
        np.shape(inter))
    union = area_a + area_b - inter
    return inter / union


def match(threshold, boxes, priors, var, labels, landms):
    '''
    Match the origin label to the resize image. Get the resized label.
    Use it to train the network.TODO: 注释不规范
    '''
    overlaps = compute_overlaps(boxes, center_point_2_box(priors))

    best_prior_overlap = overlaps.max(1, keepdims=True)
    best_prior_idx = np.argsort(-overlaps, axis=1)[:, 0:1]

    valid_gt_idx = best_prior_overlap[:, 0] >= 0.2
    best_prior_idx_filter = best_prior_idx[valid_gt_idx, :]

    if best_prior_idx_filter.shape[0] <= 0:
        loc = np.zeros((priors.shape[0], 4), dtype=np.float32)
        conf = np.zeros((priors.shape[0],), dtype=np.int32)
        landm = np.zeros((priors.shape[0], 10), dtype=np.float32)
        return loc, conf, landm

    best_truth_overlap = overlaps.max(0, keepdims=True)
    best_truth_idx = np.argsort(-overlaps, axis=0)[:1, :]

    best_truth_idx = best_truth_idx.squeeze(0)
    best_truth_overlap = best_truth_overlap.squeeze(0)
    best_prior_idx = best_prior_idx.squeeze(1)
    best_prior_idx_filter = best_prior_idx_filter.squeeze(1)
    best_truth_overlap[best_prior_idx_filter] = 2

    for j in range(best_prior_idx.shape[0]):
        best_truth_idx[best_prior_idx[j]] = j

    matches = boxes[best_truth_idx]

    # encode boxes
    offset_cxcy = (matches[:, 0:2] + matches[:, 2:4]) / 2 - priors[:, 0:2]
    offset_cxcy /= (var[0] * priors[:, 2:4])
    wh = (matches[:, 2:4] - matches[:, 0:2]) / priors[:, 2:4]
    wh[wh == 0] = 1e-12
    wh = np.log(wh) / var[1]
    loc = np.concatenate([offset_cxcy, wh], axis=1)

    conf = labels[best_truth_idx]
    conf[best_truth_overlap < threshold] = 0

    matches_landm = landms[best_truth_idx]

    # encode landms
    matched = np.reshape(matches_landm, [-1, 5, 2])
    priors = np.broadcast_to(np.expand_dims(priors, 1), [priors.shape[0], 5, 4])
    offset_cxcy = matched[:, :, 0:2] - priors[:, :, 0:2]
    offset_cxcy /= (priors[:, :, 2:4] * var[0])
    landm = np.reshape(offset_cxcy, [-1, 10])

    return loc, np.array(conf, dtype=np.int32), landm


class bbox_encode():
    "Use this function to adjust the label.TODO: 注释不规范"

    def __init__(self,
                 match_thresh=0.35,
                 variances=[0.1, 0.2],
                 image_size=640,
                 clip=False):
        self.match_thresh = match_thresh
        self.variances = variances
        self.priors = prior_box((image_size, image_size),
                                [[16, 32], [64, 128], [256, 512]],
                                [8, 16, 32],
                                clip)

    def __call__(self, image, targets):
        boxes = targets[:, :4]
        labels = targets[:, -1]
        landms = targets[:, 4:14]
        priors = self.priors

        loc_t, conf_t, landm_t = match(self.match_thresh, boxes, priors, self.variances, labels, landms)

        return image, loc_t, conf_t, landm_t


def decode_bbox(bbox, priors, var):
    "According to the encode method, use this function to get the box coordinate from anchor result."
    boxes = np.concatenate((
        priors[:, 0:2] + bbox[:, 0:2] * var[0] * priors[:, 2:4],
        priors[:, 2:4] * np.exp(bbox[:, 2:4] * var[1])), axis=1)  # (xc, yc, w, h)
    boxes[:, :2] -= boxes[:, 2:] / 2  # (x0, y0, w, h)
    boxes[:, 2:] += boxes[:, :2]  # (x0, y0, x1, y1)
    return boxes


def decode_landm(landm, priors, var):
    "According to the encode method, use this function to get the landmark coordinate from anchor result."
    return np.concatenate((priors[:, 0:2] + landm[:, 0:2] * var[0] * priors[:, 2:4],
                           priors[:, 0:2] + landm[:, 2:4] * var[0] * priors[:, 2:4],
                           priors[:, 0:2] + landm[:, 4:6] * var[0] * priors[:, 2:4],
                           priors[:, 0:2] + landm[:, 6:8] * var[0] * priors[:, 2:4],
                           priors[:, 0:2] + landm[:, 8:10] * var[0] * priors[:, 2:4],
                           ), axis=1)


class Timer():
    "Use to compute the time cost.TODO: 注释不规范"

    def __init__(self):
        self.start_time = 0.
        self.diff = 0.

    def start(self):
        self.start_time = time.time()

    def end(self):
        self.diff = time.time() - self.start_time


def drawPreds(frame, bbox_list, draw_conf=False, box_thickness=5, landmark_list=None, landmark_thickness=10):
    "Use to draw the box on the image. TODO: 注释不规范"
    frame_tmp = frame
    if landmark_list is None:
        for i in bbox_list:
            x, y, width, height, conf = i
            left = x
            right = x + width
            top = y
            bottom = y + height
            cv2.rectangle(frame_tmp, (left, top), (right, bottom), (255, 178, 50), box_thickness)
            if draw_conf:
                label = '%.4f' % conf
                label = '%s' % (label)
                labelSize, baseLine = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
                top = max(top, labelSize[1])
                frame_tmp = cv2.rectangle(frame_tmp, (left, int(top - round(1.5 * labelSize[1]))),
                                          (left + int(round(1.5 * labelSize[0])), top + baseLine), (255, 255, 255),
                                          cv2.FILLED)
                frame_tmp = cv2.putText(frame_tmp, label, (left, top), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 0, 0), 1)
    else:
        for i, j in enumerate(bbox_list):
            x, y, width, height, conf = j
            for k in range(5):
                cv2.circle(frame_tmp, (landmark_list[i][k * 2], landmark_list[i][k * 2 + 1]), 1, (250, 99, 40),
                           landmark_thickness)
            left = x
            right = x + width
            top = y
            bottom = y + height
            cv2.rectangle(frame_tmp, (left, top), (right, bottom), (255, 178, 50), box_thickness)
            if draw_conf:
                label = '%.4f' % conf
                label = '%s' % (label)
                labelSize, baseLine = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
                top = max(top, labelSize[1])
                frame_tmp = cv2.rectangle(frame_tmp, (left, int(top - round(1.5 * labelSize[1]))),
                                          (left + int(round(1.5 * labelSize[0])), top + baseLine), (255, 255, 255),
                                          cv2.FILLED)
                frame_tmp = cv2.putText(frame_tmp, label, (left, top), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 0, 0), 1)
    return frame_tmp


def preprocess(list):
    "Uset to process the landmark. TODO: 注释不规范"
    processed_list = []
    for i in list:
        cur_list = []
        for j in i:
            cur_list.append(int(j))
        processed_list.append(cur_list)
    return processed_list
