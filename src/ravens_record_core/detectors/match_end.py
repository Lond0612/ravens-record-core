"""
試合の終わりの読み取り（v1.4.1〜。ScoreDetector の補助）

スコアのマス（ScoreDetector の本来の判定）を、試合の最後に読み逃すことがある
（利用者のログ 3 件: 自分の 2 点目だけを読み逃し、1-0 のままロビーに戻った）。そこで、マスとは別に次の表示も読む。

- 帯の表示（試合中の画面の中央）: ラウンドの終わりは「PHASE n: YOU WIN / YOU LOSE」、試合の終わりは「YOU WIN / YOU LOSE」。
  2.5〜4 秒出る。ゲームの言語によらず英語。文字の色は、勝ちが水色・負けが赤橙。
- リザルト画面（RESULT）: ALPHA・BETA それぞれの WIN / LOSE と、取ったラウンド数（n PT）。
  単独で見えるのは 1〜1.5 秒（そのあと、ランクポイント変動のダイアログが重なる）。
- 自分の陣営: 試合中の画面の上の左の見出し（ALPHA / BETA）は、参加者の画面では自分の陣営。
  見出しの左の小さな縦棒の色（ALPHA は緑、BETA はピンク）で見分ける。

座標は、運営者の録画（1920x1080）で実測した。ほかの解像度は、高さに合わせて比で読む（16:9 前提。ScoreDetector と同じ）。
"""

import re
from typing import Optional

import cv2
import numpy as np
import pytesseract

from ravens_record_core.ocr import ocr_shapes
from ravens_record_core.system import screencap, tesseract_setup

# 試合の終わりのスコアとして、ありうる組み合わせ (自分, 相手)
VALID_SCORES = {(2, 0), (2, 1), (1, 2), (0, 2), (2, 2)}


def is_valid_score(my: Optional[int], enemy: Optional[int]) -> bool:
    """試合の終わりのスコアとして、ありうる組み合わせか"""
    return (my, enemy) in VALID_SCORES


# --- 帯の表示 ------------------------------------------------------------------
BANNER_BOX = (0.312, 0.430, 0.688, 0.495)  # 文字の入る範囲 (x0, y0, x1, y1)
BANNER_MIN_TEXT_RATIO = 0.04               # 範囲の中で、帯の文字の色の画素がこれ以上あれば読む
BANNER_PATTERN = re.compile(r"(?:PHASE\s*(\d)\s*[:;.]?\s*)?Y\s*O\s*U\s*(WIN|LOSE|L0SE|W1N)")

# --- 自分の陣営（試合中の上の見出しの縦棒） -------------------------------------
SIDE_BAR_BOX = (0.2984, 0.0759, 0.3047, 0.0917)

# --- リザルト画面 ---------------------------------------------------------------
RESULT_HEADING_BOX = (0.036, 0.037, 0.188, 0.088)
RESULT_ROWS = {
    "ALPHA": (0.115, 0.289, 0.484, 0.331),
    "BETA": (0.516, 0.289, 0.885, 0.331),
}


def _crop(frame: np.ndarray, box: tuple) -> np.ndarray:
    frame, _ = screencap.game_area(frame)  # 黒い帯の内側に対する割合で切る
    h, w = frame.shape[:2]
    x0, y0, x1, y1 = box
    return frame[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)]


def _ocr(img: np.ndarray, whitelist: str) -> str:
    if img is None or img.size == 0:
        return ""
    try:
        tesseract_setup.ensure()
        return pytesseract.image_to_string(img, lang="eng", config=f"--psm 7 -c tessedit_char_whitelist={whitelist}").strip().upper()
    except Exception:
        return ""  # 読めなかった扱い（Tesseract の失敗で、スコアの監視を止めない）


def _banner_mask(region: np.ndarray) -> np.ndarray:
    """帯の文字の色（水色・赤橙）の画素"""
    b, g, r = (region[..., i].astype(np.int16) for i in range(3))
    cyan = (g > 170) & (b > 170) & (r < g - 40)
    orange = (r > 190) & (g < 170) & (b < 150) & (r - g > 60)
    return cyan | orange


def read_banner(frame: np.ndarray) -> Optional[tuple[str, Optional[int], str]]:
    """帯の表示を読む。("final" | "phase", ラウンドの番号, "win" | "lose")。無ければ None。

    重い文字の読み取りは、帯の文字の色の画素が十分あるときだけ行う（ふだんの試合中は、色の数え上げだけ）。
    """
    region = _crop(frame, BANNER_BOX)
    if region.size == 0:
        return None
    mask = _banner_mask(region)
    if mask.mean() < BANNER_MIN_TEXT_RATIO:
        return None
    shape = ocr_shapes.read("banner", region, mask)
    if shape is not None:
        return _banner_from_label(shape)
    # 文字の色の画素だけを黒、ほかを白にして読む
    h = region.shape[0]
    scale = 64 / max(h, 1)
    binary = np.where(mask, 0, 255).astype(np.uint8)
    binary = cv2.resize(binary, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
    binary = cv2.copyMakeBorder(binary, 16, 16, 16, 16, cv2.BORDER_CONSTANT, value=255)
    text = _ocr(binary, "PHASEYOUWINL0123456789: ")
    m = BANNER_PATTERN.search(text.replace(" ", " "))
    if not m:
        return None
    outcome = "win" if m.group(2) in ("WIN", "W1N") else "lose"
    if m.group(1):
        return "phase", int(m.group(1)), outcome
    if "PHASE" in text:
        return None  # 「PHASE」は読めたのに番号が読めない: ラウンドの終わりか試合の終わりか分からないので使わない
    return "final", None, outcome


def _banner_from_label(label: str) -> tuple[str, Optional[int], str]:
    """テンプレートの名前（phase2_win / final_lose）を、read_banner の返す形にする"""
    kind, outcome = label.split("_")
    if kind == "final":
        return "final", None, outcome
    return "phase", int(kind[len("phase"):]), outcome


def _is_narrow_bar(mask: np.ndarray) -> bool:
    """色の画素が、細い縦棒の形か（範囲の幅の半分以下の、ひとつながりの列。左右の端の列には無い）。
    空などの広い範囲が同じ色のとき（ピンクの空のマップの紹介の映像など）を、見出しの縦棒と取り違えないため"""
    if mask.size == 0:
        return False
    cols = mask.mean(axis=0) >= 0.4
    idx = np.flatnonzero(cols)
    width = mask.shape[1]
    if idx.size == 0 or idx.size > width // 2:
        return False
    if idx[-1] - idx[0] + 1 != idx.size:
        return False  # ひとつながりでない
    return idx[0] >= 2 and idx[-1] <= width - 3


def read_own_side(frame: np.ndarray) -> Optional[str]:
    """試合中の画面の上の左の見出しの縦棒の色から、自分の陣営（"ALPHA" / "BETA"）を返す。分からなければ None。"""
    region = _crop(frame, SIDE_BAR_BOX).astype(np.int16)
    if region.size == 0:
        return None
    b, g, r = region[..., 0], region[..., 1], region[..., 2]
    green = (g > 150) & (g - r > 60) & (g - b > 60)
    pink = (r > 150) & (b > 120) & (r - g > 60) & (b - g > 40)
    if green.mean() >= 0.08 and green.mean() > pink.mean() * 3 and _is_narrow_bar(green):
        return "ALPHA"
    if pink.mean() >= 0.08 and pink.mean() > green.mean() * 3 and _is_narrow_bar(pink):
        return "BETA"
    return None


def _stretch(img: np.ndarray) -> np.ndarray:
    """明るさを引き伸ばす（リザルト画面は、暗く表示されてから明るくなる。暗い間も読めるように）"""
    top = float(img.max()) if img.size else 0.0
    if top <= 0:
        return img
    return np.clip(img.astype(np.float32) * (255.0 / top), 0, 255).astype(np.uint8)


def _row_outcome(row: np.ndarray) -> Optional[str]:
    """リザルト画面の行の WIN（緑がかった水色）/ LOSE（赤）の色"""
    row = _stretch(row)
    b, g, r = (row[..., i].astype(np.int16) for i in range(3))
    red = ((r > 140) & (r - g > 70) & (r - b > 60)).mean()
    teal = ((g > 110) & (g - r > 40) & (b > 80)).mean()
    if red > 0.004 and red > teal * 3:
        return "lose"
    if teal > 0.004 and teal > red * 3:
        return "win"
    return None


def _row_points(row: np.ndarray) -> Optional[int]:
    """リザルト画面の行の右端の「n PT」の数"""
    w = row.shape[1]
    part = row[:, int(w * 0.84):]
    shape = ocr_shapes.read("result_points", part)
    if shape is not None:
        return int(shape)
    gray = _stretch(cv2.cvtColor(part, cv2.COLOR_BGR2GRAY))
    gray = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    text = _ocr(binary, "0123PT")
    m = re.search(r"(?<!\d)([0-3])\s*P", text)  # 「501PT」（チーム戦のポイント）の末尾の 1 を、ラウンド数と読まない
    return int(m.group(1)) if m else None


def is_result_screen(frame: np.ndarray) -> bool:
    """リザルト画面か（左上の見出し「RESULT」）。画面全体が暗いときだけ、見出しを読む。"""
    if float(frame[::16, ::16].mean()) > 70:
        return False
    heading = _crop(frame, RESULT_HEADING_BOX)
    if ocr_shapes.read("result_heading", heading) == "RESULT":
        return True
    gray = _stretch(cv2.cvtColor(heading, cv2.COLOR_BGR2GRAY))
    gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    return "RESULT" in _ocr(binary, "RESULT").replace(" ", "")


def read_result_screen(frame: np.ndarray) -> Optional[dict]:
    """リザルト画面の、陣営ごとの {"ALPHA": (勝敗, PT), "BETA": (勝敗, PT)}。リザルト画面でなければ None。
    勝敗・PT が読めなかったものは None のまま入れる。横長のモニターでは、中央の 16:9 を読む（screencap.menu_area）"""
    frame, _ = screencap.menu_area(frame)
    if not is_result_screen(frame):
        return None
    out = {}
    for side, box in RESULT_ROWS.items():
        row = _crop(frame, box)
        out[side] = (_row_outcome(row), _row_points(row))
    return out


def final_score(own_side: Optional[str], result: dict) -> Optional[tuple[int, int]]:
    """リザルト画面の読み取りから、(自分, 相手) のラウンド数。自分の陣営か、両方の PT が分からなければ None。"""
    if own_side not in ("ALPHA", "BETA"):
        return None
    other = "BETA" if own_side == "ALPHA" else "ALPHA"
    mine, theirs = result.get(own_side, (None, None))[1], result.get(other, (None, None))[1]
    if mine is None or theirs is None:
        return None
    return mine, theirs


# ---------------------------------------------------------------------------
# 試合の終わりの判断（ScoreDetector から、画面を 1 枚ずつ渡す）
# ---------------------------------------------------------------------------

BANNER_INTERVAL_SEC = 0.4   # 帯の文字を読む間隔の下限（色の数え上げは毎回）
RESULT_INTERVAL_SEC = 0.3   # リザルト画面を読む間隔の下限
CONFIRM_READS = 2           # 同じ読み取りが、これだけ続いたら確定


class EndWatcher:
    """帯の表示・リザルト画面・自分の陣営を読み、マスの判定とは別に、試合の最終スコアを決める。

    - 自分の陣営: 試合中の見出しの縦棒の色を、毎回数えて多い方。
    - 帯: 同じ読み取りが CONFIRM_READS 回続いたら確定。ラウンドごと（PHASE n）と、試合の終わりを分けて覚える。
    - リザルト画面: 両方の PT が読め、同じ値が CONFIRM_READS 回続いたら確定（自分の陣営が分かっているときだけ使う）。
    """

    def __init__(self) -> None:
        self.side_votes = {"ALPHA": 0, "BETA": 0}
        self.phases: dict[int, str] = {}
        self.final: Optional[str] = None
        self.result: Optional[dict] = None
        self._banner_last: Optional[tuple] = None
        self._banner_streak = 0
        self._result_last: Optional[tuple] = None
        self._result_streak = 0
        self._next_banner = 0.0
        self._next_result = 0.0
        self.events: list[str] = []  # 確定したこと（ログに出す）

    @property
    def own_side(self) -> Optional[str]:
        a, b = self.side_votes["ALPHA"], self.side_votes["BETA"]
        if max(a, b) < 3 or a == b:
            return None
        return "ALPHA" if a > b else "BETA"

    def observe(self, frame: np.ndarray, now: float) -> None:
        side = read_own_side(frame)
        if side:
            self.side_votes[side] += 1

        if now >= self._next_banner:
            banner = read_banner(frame)
            if banner is not None:
                self._next_banner = now + BANNER_INTERVAL_SEC
                self._accept_banner(banner)
            else:
                self._banner_last, self._banner_streak = None, 0

        if self.result is None and now >= self._next_result:
            self._next_result = now + RESULT_INTERVAL_SEC
            result = read_result_screen(frame)
            if result is not None:
                self._accept_result(result)

    def _accept_banner(self, banner: tuple) -> None:
        self._banner_streak = self._banner_streak + 1 if banner == self._banner_last else 1
        self._banner_last = banner
        if self._banner_streak != CONFIRM_READS:
            return
        kind, number, outcome = banner
        if kind == "phase" and number is not None and number not in self.phases:
            self.phases[number] = outcome
            self.events.append(f"帯「PHASE {number}: YOU {outcome.upper()}」を読みました")
        elif kind == "final" and self.final is None:
            self.final = outcome
            self.events.append(f"帯「YOU {outcome.upper()}」（試合の終わり）を読みました")

    def _accept_result(self, result: dict) -> None:
        key = tuple(result[s][1] for s in ("ALPHA", "BETA"))
        if None in key:
            self._result_last, self._result_streak = None, 0
            return
        self._result_streak = self._result_streak + 1 if key == self._result_last else 1
        self._result_last = key
        if self._result_streak >= CONFIRM_READS:
            self.result = result
            self.events.append(f"リザルト画面を読みました: ALPHA {key[0]}PT / BETA {key[1]}PT（自分の陣営: {self.own_side or '不明'}）")

    def decided_score(self, box_my: int, box_enemy: int) -> Optional[tuple[int, int, str]]:
        """マスの判定（ラッチ済みのスコア）と合わせた最終スコア (自分, 相手, 根拠)。まだ決められなければ None。

        リザルト画面（自分の陣営が分かるとき）を優先する。次に、試合の終わりの帯（勝った側を 2 点にし、
        負けた側は、マス・ラウンドの帯で分かっている点数。最大 1 点）。
        """
        if self.result is not None:
            score = final_score(self.own_side, self.result)
            if score is not None:
                return score[0], score[1], "リザルト画面"
        if self.final is not None:
            phase_my = sum(1 for o in self.phases.values() if o == "win")
            phase_enemy = sum(1 for o in self.phases.values() if o == "lose")
            if self.final == "win":
                return 2, min(1, max(box_enemy, phase_enemy)), "試合の終わりの帯"
            return min(1, max(box_my, phase_my)), 2, "試合の終わりの帯"
        return None
