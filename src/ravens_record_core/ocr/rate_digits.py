"""
レート・順位の数字を、ゲームの数字の形（テンプレート）と照らし合わせて読む（Tesseract とは別の読み取りの経路）。

RANK MATCH: SINGLE 画面の PLACE / RATING 欄は、決まった書体の白い数字だけが右寄せで並ぶ。
・明るい画素だけを残し（大津の二値化）、縦の列の切れ目で 1 文字ずつに分ける
・1 文字ごとに、周りの余白を切って決まった大きさ（GLYPH_W × GLYPH_H）にそろえ（濃淡のまま。明るさは 0〜1 に伸ばし、少しぼかす）、
  テンプレート（rate_digit_templates.py）のそれぞれと重なり具合（正規化相互相関）を比べ、いちばん近い数字にする。
  白黒にせず濃淡で比べるのは、縮小・拡大やぼけ（撮影の方式・解像度の違い）で、細い隙間がつぶれても読めるようにするため
・どれとも十分に近くない文字があれば、読めなかった扱い（None）にする
Tesseract が取り違えやすい「1」と「7」（この書体の 1 は上に旗がある）も、形の比較で見分けやすい。
解像度が違っても、文字の高さでそろえてから比べるので、そのまま使える。
"""

from __future__ import annotations

import cv2
import numpy as np

try:
    from ravens_record_core.ocr.rate_digit_templates import GLYPH_H, GLYPH_W, TEMPLATES
except ImportError:  # テンプレートは同梱しない（ゲームの画面から作るため）。scripts/make_rate_digit_templates.py で作れる。無ければこの経路は読まない
    GLYPH_W, GLYPH_H, TEMPLATES = 20, 28, {}

MIN_SCORE = 0.70  # これより近い数字が無い文字があれば、読めなかった扱い
MIN_GLYPH_HEIGHT_RATIO = 0.35  # 欄の高さに対する、文字とみなす高さの下限（点のような汚れを外す）
MAX_DIGITS = 6


def _binarize(image: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    # 暗い背景の白い数字。大津の値が低すぎる（数字が無い・真っ暗）ときは、文字なしとみなせるよう高めに下限を置く
    threshold, _ = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    _, binary = cv2.threshold(gray, max(threshold, 110), 255, cv2.THRESH_BINARY)
    return binary


def split_glyphs(image: np.ndarray) -> list[np.ndarray]:
    """欄の画像を、1 文字ずつの濃淡の画像（余白を切ったもの）に分ける（左から順）。切れ目は、白黒にした画像の縦の列で探す"""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    binary = _binarize(gray)
    h = binary.shape[0]
    cols = binary.max(axis=0) > 0
    glyphs = []
    x = 0
    while x < len(cols):
        if not cols[x]:
            x += 1
            continue
        start = x
        while x < len(cols) and cols[x]:
            x += 1
        piece = binary[:, start:x]
        rows = np.where(piece.max(axis=1) > 0)[0]
        if rows.size == 0 or (rows[-1] - rows[0] + 1) < h * MIN_GLYPH_HEIGHT_RATIO:
            continue  # 小さな汚れ・欄の端の線
        glyphs.append(gray[rows[0]:rows[-1] + 1, start:x])
    return glyphs


def normalize_glyph(glyph: np.ndarray) -> np.ndarray:
    """1 文字の濃淡の画像を、比べる形にそろえる（縦横の比は保って高さを GLYPH_H に、横は中央。明るさを 0〜1 に伸ばし、少しぼかす）"""
    gh, gw = glyph.shape
    scale = GLYPH_H / gh
    w = max(1, min(GLYPH_W, int(round(gw * scale))))
    resized = cv2.resize(glyph, (w, GLYPH_H), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC).astype(np.float32)
    lo, hi = float(resized.min()), float(resized.max())
    resized = (resized - lo) / (hi - lo) if hi > lo else np.zeros_like(resized)
    canvas = np.zeros((GLYPH_H, GLYPH_W), dtype=np.float32)
    left = (GLYPH_W - w) // 2
    canvas[:, left:left + w] = resized
    return cv2.GaussianBlur(canvas, (3, 3), 0)


def _score(a: np.ndarray, b: np.ndarray) -> float:
    a = a - a.mean()
    b = b - b.mean()
    denom = float(np.sqrt((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / denom) if denom > 0 else 0.0


def classify_glyph(glyph: np.ndarray) -> tuple[str | None, float]:
    """いちばん近い数字と、その近さ（-1〜1）"""
    target = normalize_glyph(glyph)
    best_digit, best = None, -1.0
    for digit, samples in TEMPLATES.items():
        for sample in samples:
            s = _score(target, sample)
            if s > best:
                best_digit, best = digit, s
    return best_digit, best


def read_digits(image: np.ndarray) -> int | None:
    """欄の画像の数字を読む。読めなければ None"""
    if not TEMPLATES or image is None or image.size == 0:
        return None
    glyphs = split_glyphs(image)
    if not glyphs or len(glyphs) > MAX_DIGITS:
        return None
    text = ""
    for glyph in glyphs:
        digit, score = classify_glyph(glyph)
        if digit is None or score < MIN_SCORE:
            return None
        text += digit
    return int(text)
