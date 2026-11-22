"""
チーム戦（RANK MATCH: TEAM）の画面の読み取り（v1.8〜）

チーム戦は 3 対 3・制限時間 3 分のポイント制で、撃破されても再出撃できる。記録に使うのは、試合の終わりのリザルト画面:
- 見出し「RESULT」・陣営ごとの行（ALPHA / BETA の WIN / LOSE と合計の「n PT」）の位置は、シングルのリザルト画面と同じ
  （match_end の is_result_screen・RESULT_ROWS・_row_outcome を使う）
- 日時の行の右に、マップの名前（ゲームの言語の表記。例「LOCステーション31」「ボナ・デア砂丘 - A」）
- 陣営ごとに 3 人の行（その陣営の中で、ポイントの多い順）。自分の行には白い枠が付く。右端にその人の「n PT」
- 単独で見えるのは 2 秒ほど（そのあと、ランクポイント変動のダイアログが重なる）

読まないもの: プレイヤー名・エンブレムなどのプレイヤーの情報、試合の ID（MATCH ID）。

座標は、運営者の録画（1920x1080）で実測した。ほかの解像度は比で読む（16:9 の中央。screencap.menu_area）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np
import pytesseract

from ravens_record_core.detectors import custom_match, match_end
from ravens_record_core.system import screencap, tesseract_setup

SIDES = ("ALPHA", "BETA")

# 日時の行の右側（マップの名前）
MAP_BOX = (0.300, 0.216, 0.497, 0.244)
# マップの名前の照合で、これ未満の近さなら、照合の結果として使わない
MAP_MIN_SCORE = 0.6
# 陣営ごとの 3 人の行の枠 (x0, x1) と、行ごとの (y0, y1)
PLAYER_COLUMNS = {"ALPHA": (0.1141, 0.4839), "BETA": (0.5151, 0.8849)}
PLAYER_ROWS = ((0.3407, 0.5148), (0.5204, 0.6944), (0.7000, 0.8750))
# 行の中の「n PT」の範囲（行の枠に対する x の割合）と、縦の範囲（行の上からの割合）
PLAYER_POINTS_X = (0.83, 0.99)
PLAYER_POINTS_Y = (0.38, 0.62)
# 自分の行の白い枠: 枠の線（上下の辺）の明るさが、ほかの行よりこれ以上明るければ自分の行
OWN_FRAME_MIN_DIFF = 40.0


@dataclass
class TeamResult:
    """リザルト画面の読み取り。読めなかったものは None"""
    outcomes: dict = field(default_factory=dict)      # {"ALPHA": "win" | "lose" | None, "BETA": ...}
    points: dict = field(default_factory=dict)        # {"ALPHA": int | None, "BETA": ...}（陣営の合計）
    own_side: Optional[str] = None                    # 白い枠の付いた行のある陣営
    own_place: Optional[int] = None                   # 自分の行の順番（陣営の中で 1〜3。ポイントの多い順）
    own_points: Optional[int] = None                  # 自分のポイント
    map_raw: str = ""                                 # マップの名前（読んだ文字のまま）
    map_match: Optional[str] = None                   # 照合したマップの名前（日本語の表記）
    map_score: float = 0.0
    map_lang: Optional[str] = None

    @property
    def complete(self) -> bool:
        """記録に要るもの（自分の陣営・勝敗・両陣営のポイント）がそろったか"""
        return (self.own_side is not None and all(self.points.get(s) is not None for s in SIDES)
                and self.result is not None)

    @property
    def opp_side(self) -> Optional[str]:
        return None if self.own_side is None else ("BETA" if self.own_side == "ALPHA" else "ALPHA")

    @property
    def result(self) -> Optional[str]:
        """自分の陣営の勝敗（win / lose / draw）。勝敗の文字が読めなければ、ポイントから決める"""
        if self.own_side is None:
            return None
        own = self.outcomes.get(self.own_side)
        if own in ("win", "lose"):
            return own
        a, b = self.points.get(self.own_side), self.points.get(self.opp_side)
        if a is None or b is None:
            return None
        return "win" if a > b else "lose" if a < b else "draw"

    def merge(self, other: "TeamResult") -> None:
        """もう 1 枚の読み取りで、読めなかったところを埋める（2 秒ほどの間に何枚か読む）"""
        for side in SIDES:
            if self.outcomes.get(side) is None:
                self.outcomes[side] = other.outcomes.get(side)
            if self.points.get(side) is None:
                self.points[side] = other.points.get(side)
        if self.own_side is None and other.own_side is not None:
            self.own_side, self.own_place = other.own_side, other.own_place
        if self.own_points is None and other.own_side == self.own_side:
            self.own_points = other.own_points
        if other.map_score > self.map_score:
            self.map_raw, self.map_match, self.map_score, self.map_lang = other.map_raw, other.map_match, other.map_score, other.map_lang


def _box(frame: np.ndarray, x0: float, y0: float, x1: float, y1: float) -> np.ndarray:
    h, w = frame.shape[:2]
    return frame[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)]


def _ocr_digits(img: np.ndarray) -> str:
    """明るい文字の数字（「1,119PT」）を読む"""
    if img is None or img.size == 0:
        return ""
    gray = match_end._stretch(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
    gray = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    binary = cv2.copyMakeBorder(binary, 12, 12, 12, 12, cv2.BORDER_CONSTANT, value=255)
    try:
        tesseract_setup.ensure()
        return pytesseract.image_to_string(binary, lang="eng", config="--psm 7 -c tessedit_char_whitelist=0123456789,PT").strip()
    except Exception:
        return ""


def parse_points(text: str) -> Optional[int]:
    """「1,119PT」「610 PT」→ 1119 / 610。読めなければ None"""
    m = re.search(r"(\d[\d,]*)\s*P", (text or "").replace(" ", ""))
    if not m:
        return None
    digits = m.group(1).replace(",", "")
    return int(digits) if digits.isdigit() and len(digits) <= 4 else None


def _team_points(row: np.ndarray) -> Optional[int]:
    """陣営の行の右端の合計ポイント"""
    w = row.shape[1]
    return parse_points(_ocr_digits(row[:, int(w * 0.80):]))


def _frame_brightness(cell: np.ndarray) -> float:
    """行の枠の上下の辺の明るさ（白い枠が付いていれば明るい）"""
    gray = cv2.cvtColor(cell, cv2.COLOR_BGR2GRAY)
    h = gray.shape[0]
    edge = max(2, h // 60)
    # 辺の線は細いので、上下の端から少しの範囲の、行ごとの明るさの最大を見る
    top = gray[:edge * 3].mean(axis=1).max()
    bottom = gray[h - edge * 3:].mean(axis=1).max()
    return float(min(top, bottom))


def _find_own_row(frame: np.ndarray) -> tuple[Optional[str], Optional[int], Optional[np.ndarray]]:
    """白い枠の付いた行（自分の行）の (陣営, 順番 1〜3, 行の画像)。見つからなければ (None, None, None)"""
    cells = []
    for side in SIDES:
        x0, x1 = PLAYER_COLUMNS[side]
        for i, (y0, y1) in enumerate(PLAYER_ROWS):
            cell = _box(frame, x0 - 0.004, y0 - 0.004, x1 + 0.004, y1 + 0.004)
            if cell.size:
                cells.append((side, i + 1, _frame_brightness(cell), _box(frame, x0, y0, x1, y1)))
    if len(cells) < 2:
        return None, None, None
    cells.sort(key=lambda c: c[2], reverse=True)
    if cells[0][2] - cells[1][2] < OWN_FRAME_MIN_DIFF:
        return None, None, None
    side, place, _, cell = cells[0]
    return side, place, cell


def _player_points(cell: np.ndarray) -> Optional[int]:
    h, w = cell.shape[:2]
    part = cell[int(h * PLAYER_POINTS_Y[0]):int(h * PLAYER_POINTS_Y[1]), int(w * PLAYER_POINTS_X[0]):int(w * PLAYER_POINTS_X[1])]
    return parse_points(_ocr_digits(part))


def read_map(frame: np.ndarray) -> tuple[str, Optional[str], float, Optional[str]]:
    """マップの名前を読み、既知のマップ（カスタムマッチのルームと同じ一覧・3 言語）と照らし合わせる。
    (読んだ文字, 照合した日本語の表記, 近さ, ゲームの言語)"""
    img = _box(frame, *MAP_BOX)
    best = ("", None, 0.0, None)
    for lang, tess in custom_match.OCR_LANGS.items():
        for threshold in (150, 110):
            raw = custom_match.clean_text(custom_match._ocr(img, threshold, tess))
            if not raw:
                continue
            names = custom_match.CUSTOM_MAP_NAMES[lang]
            match, score = custom_match.best_match(raw, [n for n, ja in names.items() if ja != "ランダム"])
            if score > best[2]:
                best = (raw, names.get(match) if match else None, score, lang)
            if score >= 0.85:
                return best
    if best[2] < MAP_MIN_SCORE:
        return best[0], None, best[2], None  # 既知のマップに近くない（読み誤り・新しいマップ）: 照合の結果は入れない
    return best


def read_team_result(frame: np.ndarray, with_map: bool = True) -> Optional[TeamResult]:
    """チーム戦のリザルト画面を読む。リザルト画面でなければ None"""
    frame, _ = screencap.menu_area(frame)
    if frame is None or not match_end.is_result_screen(frame):
        return None
    out = TeamResult()
    for side, box in match_end.RESULT_ROWS.items():
        row = match_end._crop(frame, box)
        out.outcomes[side] = match_end._row_outcome(row)
        out.points[side] = _team_points(row)
    out.own_side, out.own_place, cell = _find_own_row(frame)
    if cell is not None:
        out.own_points = _player_points(cell)
    if with_map:
        out.map_raw, out.map_match, out.map_score, out.map_lang = read_map(frame)
    return out


# ---------------------------------------------------------------------------
# 記録（試合ごとの JSON の中身。使う側のアプリへも、この形で渡す）
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1


def build_payload(played_at, ended_at, result: Optional[TeamResult], banner_outcome: Optional[str], hud_side: Optional[str],
                  rate_before: Optional[int], rate_after: Optional[int], app_version: str) -> dict:
    """チーム戦の試合 1 回の記録。リザルト画面を読めなかったときは、試合の終わりの帯（YOU WIN / YOU LOSE）の勝敗と、
    試合中の画面で読んだ自分の陣営だけを入れる（ポイントは空）。プレイヤー名・試合の ID は入れない"""
    import uuid
    r = result or TeamResult()
    own_side = r.own_side or hud_side
    own = r.points.get(own_side) if own_side else None
    opp_side = None if own_side is None else ("BETA" if own_side == "ALPHA" else "ALPHA")
    opp = r.points.get(opp_side) if opp_side else None
    outcome = r.result if r.own_side else None
    basis = "result_screen" if outcome else None
    if outcome is None and banner_outcome in ("win", "lose"):
        outcome, basis = banner_outcome, "banner"
    return {
        "schema_version": SCHEMA_VERSION,
        "client_match_id": str(uuid.uuid4()),
        "app_version": app_version,
        "played_at": played_at.astimezone().isoformat(timespec="seconds"),
        "ended_at": ended_at.astimezone().isoformat(timespec="seconds"),
        "result": outcome,
        "result_basis": basis,
        "own_side": own_side,
        "own_points": own,
        "opp_points": opp,
        "own_score": r.own_points if r.own_side else None,
        "own_place": r.own_place if r.own_side else None,
        "map_raw": r.map_raw or None,
        # 照合の結果（参考）。正規化は、記録を使う側で、生の文字から行う
        "map_hint": {"match": r.map_match, "score": r.map_score, "lang": r.map_lang} if r.map_match else None,
        "rate_before": rate_before,
        "rate_after": rate_after,
    }
