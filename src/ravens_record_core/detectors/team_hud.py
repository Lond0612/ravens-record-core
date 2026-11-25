"""
チーム戦の試合中の表示の読み取り（v1.8〜）

読むもの（座標は運営者の録画 1920x1080 で実測。ほかの解像度は 16:9 の画面を 1920x1080 にそろえてから読む）:
  - 上部のポイント（左: 自チーム・右寄せ / 右: 相手・左寄せ）、上部中央の残り時間（03:00 から減る）、左下の自分の SCORE。
    どれも等幅の数字なので、決まった枠ごとに、数字のテンプレートと照らし合わせる（Tesseract は使わない）
  - 右下の流れる表示: 「撃破 ▮NN」（撃破された機体）と「プライオリティ・ターゲット更新 ▮NN」。
    ▮の色（緑 = ALPHA・紫 = BETA）と番号（01〜03）。新しい行が下に足され、古い行が上から消える
  - 画面中央の「MAIN SYSTEM REBOOTING」（自分が撃破されて、再出撃を待っている）
  - 左の自チームの欄の、番号ごとのエンブレム（ロビーで撮った自分のエンブレムと照らし合わせて、自分の番号を決める）
  - 左右の欄の、番号ごとの縦 3 マスの目盛り（被ターゲット数。光っているマスの数 = その機体をロックしている敵の数。上から光る。
    ロックされていない機体は目盛りが出ない。光っていないマスは、背景より暗い）
読まないもの: プレイヤー名、誰が撃破したか（ゲームは出さない）。

テンプレートは scripts/make_team_hud_templates.py でチーム戦の録画から作る（ocr/team_hud_templates.py。ゲームの画面から作るため同梱しない）。
無ければ、ここの読み取りは何も読まない（None・空）。
"""

from __future__ import annotations

from typing import Optional

import cv2
import numpy as np

from ravens_record_core.system import screencap

try:
    from ravens_record_core.ocr.team_hud_templates import RAW as _RAW
except ImportError:
    _RAW = {}

W, H = 1920, 1080
CELL_W, CELL_H = 12, 16

# 数字の欄（box: 欄の範囲。edge: 揃えの端の x。pitch: 1 文字の幅。y: 数字の上下。align: right なら右寄せ）
FIELDS = {
    "own": {"box": (500, 92, 580, 124), "edge": 570.0, "pitch": 16.0, "y": (101, 115), "align": "right"},
    "opp": {"box": (1340, 92, 1425, 124), "edge": 1345.0, "pitch": 16.25, "y": (101, 115), "align": "left"},
    "score": {"box": (205, 796, 300, 828), "edge": 222.0, "pitch": 16.0, "y": (804, 819), "align": "left"},
}
MAX_DIGITS = 4
# 残り時間（MM:SS の 4 つの数字の枠）
TIMER_BOX = (880, 196, 1040, 240)
TIMER_CELLS = ((896, 922), (927, 952), (968, 993), (998, 1024))
TIMER_Y = (205, 230)
# 流れる表示: ▮の色の棒を探す列と、縦の範囲
FEED_BAR_X = (1816, 1830)
FEED_Y = (560, 900)
FEED_LONG_TEXT_X = (1500, 1765)   # 「プライオリティ・ターゲット更新」だけが、ここまで伸びる
# 「MAIN SYSTEM REBOOTING」の文字の範囲
REBOOT_BOX = (780, 722, 1140, 750)
# 自チームの欄のエンブレム（番号 1〜3 の順）とロビーのネームプレートのエンブレム
EMBLEM_BOXES = ((126, 565, 170, 609), (126, 643, 170, 687), (126, 721, 170, 765))
LOBBY_EMBLEM_BOX = (616, 302, 690, 376)
# 被ターゲット数の目盛り: 列の x（幅 3px の中心）と、番号ごとの上のマスの上端の y。マスは 22px おきに 3 つ（高さ約 16px）
TARGET_X = {"own": 177, "opp": 1740}
TARGET_SLOT_Y = {"own": (550, 628, 706), "opp": (315, 385, 455)}
TARGET_SEG_DY = (0, 22, 44)
TARGET_SEG_ROWS = (3, 14)   # マスの中の、読む行（上端からの範囲。縁のにじみを避ける）
TARGET_LIT = 40.0           # 光っているマス: 両脇の背景より、これだけ明るい（光っていないマスは背景より暗い）

MIN_DIGIT = 0.55       # 数字のテンプレートとの近さ。これ未満の枠は、数字の無い枠（欄の端）とみなす
MIN_TIMER = 0.5
MIN_SLOT = 0.6
MIN_REBOOT = 0.6
MIN_EMBLEM = 0.6       # 自分のエンブレムとみなす近さ
EMBLEM_MARGIN = 0.2    # 2 番目に近い欄との差（同じエンブレムの味方がいるときは、決めない）


def _decode(rows: list[str]) -> np.ndarray:
    return np.array([[int(c) for c in row] for row in rows], dtype=np.float32)


def _unit(a: np.ndarray) -> np.ndarray:
    a = a.astype(np.float32) - float(a.mean())
    n = float(np.linalg.norm(a))
    return a / n if n > 1e-6 else a


_TEMPLATES = {k: _unit(_decode(v)) for k, v in _RAW.items()}


def available() -> bool:
    return bool(_TEMPLATES)


def _group(prefix: str) -> dict[str, np.ndarray]:
    return {k.split(":", 1)[1]: v for k, v in _TEMPLATES.items() if k.startswith(prefix + ":")}


_DIGITS = {f: _group(f) for f in ("own", "opp", "score", "timer")}


# ---------------------------------------------------------------------------
# 切り出し
# ---------------------------------------------------------------------------

def to_1080(frame: np.ndarray) -> np.ndarray:
    """黒い帯を除いた 16:9 の画面を、1920x1080 にそろえる"""
    area, _ = screencap.game_area(frame)
    if area is None or area.size == 0:
        area = frame
    if area.shape[1] == W and area.shape[0] == H:
        return area
    interp = cv2.INTER_AREA if area.shape[1] > W else cv2.INTER_LINEAR
    return cv2.resize(area, (W, H), interpolation=interp)


def crop(f: np.ndarray, box) -> np.ndarray:
    x0, y0, x1, y1 = box
    return f[int(round(y0)):int(round(y1)), int(round(x0)):int(round(x1))]


def _gray(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img


def norm_cell(cell: np.ndarray) -> np.ndarray:
    return _unit(cv2.resize(_gray(cell).astype(np.float32), (CELL_W, CELL_H), interpolation=cv2.INTER_AREA))


def field_cells(f: np.ndarray, field: str, n: int = MAX_DIGITS) -> list[np.ndarray]:
    """数字の枠 n 個を、左から右の順で（右寄せの欄は、右端から n 個）"""
    spec = FIELDS[field]
    y0, y1 = spec["y"]
    cells = []
    for i in range(n):
        if spec["align"] == "right":
            a, b = spec["edge"] - (i + 1) * spec["pitch"], spec["edge"] - i * spec["pitch"]
        else:
            a, b = spec["edge"] + i * spec["pitch"], spec["edge"] + (i + 1) * spec["pitch"]
        cells.append(crop(f, (a, y0, b, y1)))
    return cells[::-1] if spec["align"] == "right" else cells


def timer_cells(f: np.ndarray) -> list[np.ndarray]:
    return [crop(f, (a, TIMER_Y[0], b, TIMER_Y[1])) for a, b in TIMER_CELLS]


def _best(x: np.ndarray, templates: dict[str, np.ndarray]) -> tuple[Optional[str], float]:
    best, score = None, -1.0
    for key, t in templates.items():
        s = float((t * x).sum())
        if s > score:
            best, score = key, s
    return best, score


# ---------------------------------------------------------------------------
# 数字
# ---------------------------------------------------------------------------

def read_field(f: np.ndarray, field: str) -> Optional[int]:
    """上部のポイント（own / opp）・自分の SCORE（score）。読めなければ None"""
    templates = _DIGITS.get(field)
    if not templates:
        return None
    spec = FIELDS[field]
    # 揃えの端から 1 枠ずつ読み、数字に近くない枠で止める（欄の端）
    cells = field_cells(f, field, MAX_DIGITS)
    order = cells[::-1] if spec["align"] == "right" else cells
    digits = []
    for cell in order:
        if cell.size == 0:
            break
        d, s = _best(norm_cell(cell), templates)
        if s < MIN_DIGIT:
            break
        digits.append(d)
    if not digits:
        return None
    text = "".join(digits[::-1] if spec["align"] == "right" else digits)
    return int(text)


def read_timer(f: np.ndarray) -> Optional[int]:
    """残り時間（秒）。読めなければ None"""
    templates = _DIGITS.get("timer")
    if not templates:
        return None
    digits = []
    for cell in timer_cells(f):
        d, s = _best(norm_cell(cell), templates)
        if s < MIN_TIMER:
            return None
        digits.append(d)
    mm, ss = int("".join(digits[:2])), int("".join(digits[2:]))
    if ss >= 60 or mm * 60 + ss > 180:
        return None
    return mm * 60 + ss


# ---------------------------------------------------------------------------
# 流れる表示
# ---------------------------------------------------------------------------

def feed_bars(f: np.ndarray) -> list[tuple[int, str]]:
    """▮の色の棒の行 (中心の y, "ALPHA" | "BETA")。上から順"""
    strip = crop(f, (FEED_BAR_X[0], FEED_Y[0], FEED_BAR_X[1], FEED_Y[1])).astype(np.int16)
    b, g, r = strip[..., 0], strip[..., 1], strip[..., 2]
    green = ((g > 140) & (g - r > 50) & (g - b > 50)).sum(axis=1) >= 2
    purple = ((r > 110) & (b > 140) & (b - g > 50) & (r - g > 30)).sum(axis=1) >= 2
    rows = []
    y = 0
    n = len(green)
    while y < n:
        mask = green if green[y] else purple if purple[y] else None
        if mask is None:
            y += 1
            continue
        s = y
        while y < n and mask[y]:
            y += 1
        if 6 <= y - s <= 22:
            rows.append((FEED_Y[0] + (s + y) // 2, "ALPHA" if mask is green else "BETA"))
    return rows


def feed_slot_box(cy: int) -> tuple:
    return (1828, cy - 8, 1856, cy + 8)


def slot_image(f: np.ndarray, cy: int) -> np.ndarray:
    return _unit(cv2.resize(_gray(crop(f, feed_slot_box(cy))).astype(np.float32), (28, 16), interpolation=cv2.INTER_AREA))


def read_slot(f: np.ndarray, cy: int) -> Optional[int]:
    templates = {k[len("slot"):]: v for k, v in _TEMPLATES.items() if k.startswith("slot")}
    if not templates:
        return None
    d, s = _best(slot_image(f, cy), templates)
    return int(d) if d is not None and s >= MIN_SLOT else None


def feed_kind(f: np.ndarray, cy: int) -> str:
    """"priority"（プライオリティ・ターゲット更新）/ "destroyed"（撃破）。長い文字が左まで伸びていれば priority"""
    band = _gray(crop(f, (FEED_LONG_TEXT_X[0], cy - 9, FEED_LONG_TEXT_X[1], cy + 9)))
    return "priority" if float((band > 200).mean()) > 0.03 else "destroyed"


def read_feed(f: np.ndarray) -> list[tuple[str, str, int]]:
    """流れる表示の行 (種類, 陣営, 番号) を上から順に。番号を読めなかった行は入れない"""
    out = []
    for cy, side in feed_bars(f):
        slot = read_slot(f, cy)
        if slot is not None:
            out.append((feed_kind(f, cy), side, slot))
    return out


# ---------------------------------------------------------------------------
# 被ターゲット数（左右の欄の目盛り）
# ---------------------------------------------------------------------------

def target_contrast(g: np.ndarray, x: int, y: int) -> float:
    """目盛りの 1 マスの明るさ − 両脇の背景の明るさ（g: グレーの 1920x1080）"""
    a, b = y + TARGET_SEG_ROWS[0], y + TARGET_SEG_ROWS[1]
    cell = float(g[a:b, x - 1:x + 2].mean())
    side = np.concatenate([g[a:b, x - 6:x - 3].ravel(), g[a:b, x + 4:x + 7].ravel()])
    return cell - float(np.median(side))


def read_targets(f: np.ndarray) -> Optional[dict[str, list[int]]]:
    """被ターゲット数 {"own": [1番, 2番, 3番], "opp": [...]}（0〜3）。上のマスから続けて光っている数（途中が暗ければ、そこまで）。
    f は to_1080 でそろえた画面（大きさが違えば None）"""
    if f is None or f.shape[0] != H or f.shape[1] != W:
        return None
    g = _gray(f).astype(np.float32)
    out: dict[str, list[int]] = {}
    for side, x in TARGET_X.items():
        counts = []
        for y0 in TARGET_SLOT_Y[side]:
            n = 0
            for dy in TARGET_SEG_DY:
                if target_contrast(g, x, y0 + dy) < TARGET_LIT:
                    break
                n += 1
            counts.append(n)
        out[side] = counts
    return out


# ---------------------------------------------------------------------------
# 自分
# ---------------------------------------------------------------------------

def reboot_image(f: np.ndarray) -> np.ndarray:
    return _unit(cv2.resize(_gray(crop(f, REBOOT_BOX)).astype(np.float32), (120, 10), interpolation=cv2.INTER_AREA))


def is_rebooting(f: np.ndarray) -> bool:
    t = _TEMPLATES.get("reboot")
    return t is not None and float((t * reboot_image(f)).sum()) >= MIN_REBOOT


def lobby_emblem(frame: np.ndarray) -> Optional[np.ndarray]:
    """ロビー（RANK MATCH: TEAM の MATCH INFO）のネームプレートの、自分のエンブレム"""
    area, _ = screencap.menu_area(frame)
    if area is None or area.size == 0:
        return None
    f = cv2.resize(area, (W, H), interpolation=cv2.INTER_AREA) if area.shape[:2] != (H, W) else area
    img = crop(f, LOBBY_EMBLEM_BOX)
    return img.copy() if img.size else None


def emblem_scores(f: np.ndarray, emblem: np.ndarray) -> list[float]:
    """自チームの欄の番号 1〜3 のエンブレムと、自分のエンブレムの近さ"""
    size = (EMBLEM_BOXES[0][2] - EMBLEM_BOXES[0][0], EMBLEM_BOXES[0][3] - EMBLEM_BOXES[0][1])
    mine = _unit(cv2.resize(_gray(emblem).astype(np.float32), size, interpolation=cv2.INTER_AREA))
    return [float((mine * _unit(_gray(crop(f, box)).astype(np.float32))).sum()) for box in EMBLEM_BOXES]


def own_slot_from_emblem(scores: list[float]) -> tuple[Optional[int], list[int]]:
    """(自分の番号 | None, 候補の番号)。近いものが 1 つだけなら決める。同じエンブレムが複数あれば、候補だけ返す"""
    candidates = [i + 1 for i, s in enumerate(scores) if s >= MIN_EMBLEM]
    ranked = sorted(scores, reverse=True)
    if len(candidates) == 1 and ranked[0] - ranked[1] >= EMBLEM_MARGIN:
        return candidates[0], candidates
    return None, candidates
