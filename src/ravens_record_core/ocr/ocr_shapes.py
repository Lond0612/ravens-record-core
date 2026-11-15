"""
画面ごとの、文字の形での読み取り（shape_match の使い方）。Tesseract の読み取りの前に試し、読めなければ None を返す
（呼び出し側は、None のとき今までどおり Tesseract で読む）。

対象ごとに、文字の画素の取り出し方（色・明るさ）だけが違う。テンプレートの作り方（scripts/make_shape_templates.py）も、ここの cut_* を使う。
"""

from __future__ import annotations

from typing import Optional

import cv2
import numpy as np

from ravens_record_core.ocr import shape_match

try:
    from ravens_record_core.ocr.shape_templates import TEMPLATES
except ImportError:  # テンプレートを作る前（scripts/make_shape_templates.py）
    TEMPLATES = {}


def _gray(image: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image


def _bright(gray: np.ndarray, floor: int) -> np.ndarray:
    """暗い背景の明るい文字の画素（大津の値。低すぎるときは floor）"""
    threshold, _ = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return gray > max(threshold, floor)


def _empty(image: Optional[np.ndarray]) -> bool:
    return image is None or image.size == 0 or image.ndim != 3


# --- 対象ごとの切り出し ---------------------------------------------------------

def status_mask(roi: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """マッチングの状態の黄色い文字の画素と、HSV の明るさ（V）。cut_status と、帯が出ているかの判定（MatchStatusDetector.band_visible）で使う"""
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    mask = (hsv[..., 0] >= 18) & (hsv[..., 0] <= 38) & (hsv[..., 1] >= 80) & (hsv[..., 2] >= 50)
    return mask, hsv[..., 2]


def cut_status(roi: np.ndarray) -> Optional[np.ndarray]:
    """マッチングの状態（右上の黄色い文。右の回るマークは白なので入らない）"""
    if _empty(roi):
        return None
    mask, value = status_mask(roi)
    return shape_match.cut(value, mask)


def cut_mode(roi: np.ndarray) -> Optional[np.ndarray]:
    """左上の見出し（RANK MATCH: SINGLE など。白い文字）"""
    if _empty(roi):
        return None
    gray = _gray(roi)
    # 見出しの背景の板も明るいので、細い線（文字）だけを浮かせてから（トップハット）明るい画素を取る
    k = max(3, int(roi.shape[0] * 0.25)) | 1
    tophat = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    return shape_match.cut(tophat, _bright(tophat, 40), min_height=0.15)  # 範囲が文字の高さの 4 倍ほどある


def cut_banner(region: np.ndarray, mask: np.ndarray) -> Optional[np.ndarray]:
    """中央の帯（PHASE n: YOU WIN など）。文字の画素は、帯の色（match_end._banner_mask）"""
    if _empty(region):
        return None
    return shape_match.cut(mask.astype(np.float32) * 255, mask)


def cut_result_heading(region: np.ndarray) -> Optional[np.ndarray]:
    """リザルト画面の左上の見出し（RESULT）"""
    if _empty(region):
        return None
    gray = _stretch(_gray(region))
    return shape_match.cut(gray, _bright(gray, 90))


def cut_points(part: np.ndarray) -> Optional[np.ndarray]:
    """リザルト画面の行の右端（n PT）"""
    if _empty(part):
        return None
    gray = _stretch(_gray(part))
    return shape_match.cut(gray, _bright(gray, 90))


def cut_ap(roi: np.ndarray) -> Optional[np.ndarray]:
    """対戦中の左下の「AP」の文字（右の数字は値が変わるので入れない。範囲の左の AP_PART 分だけ）"""
    if _empty(roi):
        return None
    part = roi[:, :int(roi.shape[1] * AP_PART)]
    gray = _gray(part)
    return shape_match.cut(gray, _bright(gray, 120))


AP_PART = 0.15


def _stretch(gray: np.ndarray) -> np.ndarray:
    top = float(gray.max()) if gray.size else 0.0
    if top <= 0:
        return gray
    return np.clip(gray.astype(np.float32) * (255.0 / top), 0, 255).astype(np.uint8)


CUTTERS = {
    "status": cut_status,
    "mode": cut_mode,
    "result_heading": cut_result_heading,
    "result_points": cut_points,
    "ap": cut_ap,
}


# 対象ごとの、ありうる値のすべて。テンプレートが全部そろっている対象だけ使う
# （見本の無い値の画面を、1 文字だけ違う別の値（PHASE 2 と PHASE 3 など）と取り違えないため）
EXPECTED = {
    "status": {"MATCHING", "MATCH_START"},
    "mode": {"SINGLE", "TEAM", "CUSTOM"},
    # 2 本先取なので、PHASE の帯は 1・2 だけ（3 ラウンド目は必ず決着がつき、試合の終わりの帯になる）
    "banner": {f"phase{n}_{o}" for n in (1, 2) for o in ("win", "lose")} | {"final_win", "final_lose"},
    "result_heading": {"RESULT"},
    "result_points": {"0", "1", "2"},
    "ap": {"AP"},
}


def usable(target: str) -> bool:
    templates = TEMPLATES.get(target) or {}
    return bool(templates) and EXPECTED.get(target, set()) <= set(templates)


# --- 読み取り -------------------------------------------------------------------

def read(target: str, image: np.ndarray, *extra) -> Optional[str]:
    """対象の画像を、テンプレートと照らし合わせて読む。読めなければ None"""
    if not usable(target):
        return None
    templates = TEMPLATES[target]
    try:
        cutter = cut_banner if target == "banner" else CUTTERS[target]
        label, _ = shape_match.classify(cutter(image, *extra), templates)
    except Exception:
        return None  # 形の読み取りに失敗しても、Tesseract の読み取りは続ける
    return label
