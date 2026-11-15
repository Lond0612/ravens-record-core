"""
決まった言葉（見出し・帯・状態の文・PT など）を、ゲームの文字の形（テンプレート）と照らし合わせて読む（Tesseract とは別の読み取りの経路）。

レート・順位の数字（rate_digits.py）と同じ考え方を、1 文字ずつではなく、言葉のかたまりごとに使う。
・文字の画素（対象ごとの色・明るさで決める）を囲む範囲を切り出し、高さを TEXT_H にそろえる（濃淡のまま。明るさは 0〜1 に伸ばし、少しぼかす）
・テンプレート（shape_templates.py）のうち、縦横の比が近いものと、重なり具合（正規化相互相関）を比べる
・いちばん近い値が MIN_SCORE 以上で、2 番目の値（別の言葉）より MARGIN 以上近いときだけ、その値にする。それ以外は None
  （None のときは、呼び出し側が今までどおり Tesseract で読む。テンプレートは運営者の録画（日本語のゲーム）から作るため、
   ほかの言語のゲーム（フォントが違う）では、たいてい None になり、Tesseract の読み方のまま）
"""

from __future__ import annotations

from typing import Optional

import cv2
import numpy as np

TEXT_H = 24
MIN_SCORE = 0.80
MARGIN = 0.05
MAX_ASPECT_DIFF = 0.15   # 縦横の比が、これより違うテンプレートとは比べない（言葉の長さが違う）
MIN_PART_HEIGHT = 0.25   # 範囲の高さに対する、文字とみなすかたまりの高さの下限（点のような汚れを外す）
MAX_PART_HEIGHT = 0.95   # 上限（範囲いっぱいの縦の線・枠を外す）


def text_box(mask: np.ndarray, min_height: float = MIN_PART_HEIGHT) -> Optional[tuple[int, int, int, int]]:
    """文字の画素（mask）のうち、文字らしい高さ（範囲の高さに対する割合 min_height〜MAX_PART_HEIGHT）のかたまりをすべて囲む範囲 (x0, y0, x1, y1)。無ければ None"""
    mask = mask.astype(np.uint8)
    h = mask.shape[0]
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    boxes = []
    for i in range(1, count):
        x, y, w, hh, area = stats[i]
        if hh < h * min_height or hh > h * MAX_PART_HEIGHT or area < 4:
            continue
        boxes.append((x, y, x + w, y + hh))
    if not boxes:
        return None
    return min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)


def normalize(gray: np.ndarray) -> np.ndarray:
    """切り出した文字の濃淡の画像を、比べる形にそろえる（縦横の比は保って高さを TEXT_H に。明るさを 0〜1 に伸ばし、少しぼかす）"""
    h, w = gray.shape[:2]
    scale = TEXT_H / h
    width = max(1, int(round(w * scale)))
    resized = cv2.resize(gray, (width, TEXT_H), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC).astype(np.float32)
    lo, hi = float(resized.min()), float(resized.max())
    resized = (resized - lo) / (hi - lo) if hi > lo else np.zeros_like(resized)
    return cv2.GaussianBlur(resized, (3, 3), 0)


def cut(gray: np.ndarray, mask: np.ndarray, min_height: float = MIN_PART_HEIGHT) -> Optional[np.ndarray]:
    """文字の範囲を切り出して、比べる形にしたもの。文字が無ければ None"""
    box = text_box(mask, min_height)
    if box is None:
        return None
    x0, y0, x1, y1 = box
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    # 文字でない画素（背景の模様）は暗くする。文字の画素の縁は残す
    keep = cv2.dilate(mask.astype(np.uint8), np.ones((3, 3), np.uint8))[y0:y1, x0:x1]
    return normalize(gray[y0:y1, x0:x1].astype(np.float32) * keep)


def _score(a: np.ndarray, b: np.ndarray) -> float:
    a = a - a.mean()
    b = b - b.mean()
    denom = float(np.sqrt((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / denom) if denom > 0 else 0.0


def scores(target: np.ndarray, templates: dict[str, list[np.ndarray]]) -> dict[str, float]:
    """言葉ごとの、いちばん近いテンプレートとの近さ（-1〜1）。縦横の比が近いテンプレートが無い言葉は入らない"""
    out: dict[str, float] = {}
    aspect = target.shape[1] / target.shape[0]
    for label, samples in templates.items():
        for sample in samples:
            sample_aspect = sample.shape[1] / sample.shape[0]
            if abs(aspect - sample_aspect) > sample_aspect * MAX_ASPECT_DIFF:
                continue
            resized = cv2.resize(target, (sample.shape[1], sample.shape[0]), interpolation=cv2.INTER_LINEAR)
            s = _score(resized, sample)
            if s > out.get(label, -1.0):
                out[label] = s
    return out


def classify(target: Optional[np.ndarray], templates: dict[str, list[np.ndarray]],
             min_score: float = MIN_SCORE, margin: float = MARGIN) -> tuple[Optional[str], float]:
    """いちばん近い言葉と、その近さ。十分に近くない・2 番目と差が無いときは (None, 近さ)"""
    if target is None or not templates:
        return None, 0.0
    ranked = sorted(scores(target, templates).items(), key=lambda kv: kv[1], reverse=True)
    if not ranked:
        return None, 0.0
    label, best = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else -1.0
    if best < min_score or best - second < margin:
        return None, best
    return label, best
