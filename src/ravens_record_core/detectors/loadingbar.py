"""
ロード画面の判定（右下の進行バー）

ランクマッチの「対戦を開始します」のあとのロード画面には、右下に横長の進行バーが出る
（1920x1080 で x 1568〜1828、y 1012〜1024。枠の線 / すき間 / 中の帯 / すき間 / 枠の線 の縞）。
中の帯は、進んだ部分が白、残りが灰色。どちらでも、縦に見ると「明・暗・明・暗・明」の同じ縞になるため、
縦 13 画素の縞の形（明暗の並び）との一致で探す。明るさ・色・進み具合には左右されない。

暗転（画面の明るさ）では判定しない。暗転は 1 秒前後と短く、見逃すと段階がずれるため（2026-10 の調査）。

解像度: 画面の高さを 1080 にそろえてから探す（UI は高さに合わせて拡大・縮小される）。
横の位置は決め打ちしない（右半分のどこにあってもよい。ウルトラワイドなどで位置が変わっても見つかるように）。
"""

from dataclasses import dataclass

import cv2
import numpy as np

from ravens_record_core.system import screencap

# 1080p での縦の縞（上から: 枠の線、すき間 2、中の帯 7、すき間 2、枠の線）。1 = 明るい、0 = 暗い
BAR_PATTERN = np.array([1, 0, 0, 1, 1, 1, 1, 1, 1, 1, 0, 0, 1], dtype=np.float32)
# 探す範囲（画面に対する割合）。縦は画面の下のほう、横は右半分
SEARCH_Y = (0.90, 0.98)
SEARCH_X = (0.50, 1.00)
# 1080p でのバーの長さ（中の帯の画素数）。この割合以上、縞が横に続いていればバーとみなす
BAR_LENGTH_1080 = 255
MIN_RUN_RATIO = 0.6
# 縞の形の一致度（正規化相関）と、明るい行と暗い行の明るさの差（0〜255）の下限
MIN_CORRELATION = 0.6
MIN_CONTRAST = 12.0
# 横に続いているかを数えるとき、途切れを許す画素数
MAX_GAP = 4
# 照合の前に、横方向にだけこの画素数（1080p 換算）でならす。低い解像度では縞の明暗の差が小さく、
# 圧縮などのノイズで 1 列ずつの一致がまだらになるため（720p で確認。2026-10）。縦の縞の形は変わらない
SMOOTH_WIDTH = 9


@dataclass
class BarResult:
    found: bool
    run_ratio: float   # 縞が横に続いた長さ / バーの長さ（1080p 換算）
    y: int             # 見つかった縞の上端（1080p 換算の、探す範囲の中での行）


def _longest_run(row: np.ndarray, max_gap: int) -> int:
    """True が続く最長の長さ（max_gap 以下の途切れはつなぐ）"""
    best = run = gap = 0
    for v in row:
        if v:
            run += gap + 1
            gap = 0
            best = max(best, run)
        elif run:
            gap += 1
            if gap > max_gap:
                run = gap = 0
    return best


_pattern_cache: dict[int, np.ndarray] = {}


def _pattern_for_height(h: int) -> np.ndarray:
    """縞の形（1080p 換算）。1080p より低い画面では、縞が数画素しかなく、1080p に拡大するとにじむため、
    同じように縮小 → 拡大してにじませた形と照合する（にじみ方をそろえる）。"""
    if h >= 1000:
        return BAR_PATTERN
    if h not in _pattern_cache:
        rows = len(BAR_PATTERN)
        native = max(3, round(rows * h / 1080))
        small = cv2.resize(BAR_PATTERN.reshape(-1, 1), (1, native), interpolation=cv2.INTER_AREA)
        _pattern_cache[h] = cv2.resize(small, (1, rows), interpolation=cv2.INTER_LINEAR).flatten()
    return _pattern_cache[h]


def detect_loading_bar(frame_bgr: np.ndarray) -> BarResult:
    """画面（ゲームのクライアント領域全体）に、ロード画面の進行バーがあるか"""
    if frame_bgr is None or frame_bgr.size == 0:
        return BarResult(False, 0.0, -1)
    frame_bgr, _ = screencap.game_area(frame_bgr)  # 黒い帯の内側に対する割合で探す
    h, w = frame_bgr.shape[:2]
    y0, y1 = int(h * SEARCH_Y[0]), int(h * SEARCH_Y[1])
    x0, x1 = int(w * SEARCH_X[0]), int(w * SEARCH_X[1])
    strip = frame_bgr[y0:y1, x0:x1]
    if strip.ndim == 3:
        strip = cv2.cvtColor(strip, cv2.COLOR_BGR2GRAY)

    scale = 1080.0 / h
    if abs(scale - 1.0) > 0.01:
        interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
        strip = cv2.resize(strip, None, fx=scale, fy=scale, interpolation=interp)
    strip = strip.astype(np.float32)
    if strip.shape[0] < len(BAR_PATTERN) or strip.shape[1] < 8:
        return BarResult(False, 0.0, -1)

    strip = cv2.blur(strip, (SMOOTH_WIDTH, 1))
    kernel = _pattern_for_height(h).reshape(-1, 1)
    corr = cv2.matchTemplate(strip, kernel, cv2.TM_CCOEFF_NORMED)
    # 明るい行の平均 − 暗い行の平均（平らな所・真っ暗な所の偶然の一致を除く）
    ones, zeros = BAR_PATTERN.sum(), (1 - BAR_PATTERN).sum()
    diff_kernel = (BAR_PATTERN / ones - (1 - BAR_PATTERN) / zeros).reshape(-1, 1)
    contrast = cv2.matchTemplate(strip, diff_kernel.astype(np.float32), cv2.TM_CCORR)
    good = (np.nan_to_num(corr, nan=0.0) >= MIN_CORRELATION) & (contrast >= MIN_CONTRAST)

    counts = good.sum(axis=1)
    best_y, best_run = -1, 0
    for y in np.argsort(counts)[::-1][:5]:  # 候補の多い行だけ、横に続いているかを数える
        if counts[y] < BAR_LENGTH_1080 * MIN_RUN_RATIO:
            break
        run = _longest_run(good[y], MAX_GAP)
        if run > best_run:
            best_y, best_run = int(y), run
    ratio = best_run / BAR_LENGTH_1080
    return BarResult(ratio >= MIN_RUN_RATIO, round(ratio, 3), best_y)
