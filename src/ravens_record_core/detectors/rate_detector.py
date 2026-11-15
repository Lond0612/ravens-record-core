"""
レート・順位判定（Phase4 要件5章 対応 / 基本設計書 4.4節）

マッチング待機画面（RANK MATCH: SINGLE の MATCH INFO パネル）から、
Sランクの場合のみ表示される「順位（PLACE）」欄をOCRし、
読み取りに成功した場合のみSランクとみなして順位・レートを取得する。
読み取れなければSランク以外とみなし、両方の取得をスキップする。

バッジ画像そのものの分類（S/A/B/C/D/UNRANKED）はここでは行わない。
自身のランク判定は別モジュール（rank_detector.py、初期ビューのプロフィールカード経由）で
対応済みのため、ここでは重複した判定を持たせない。
バッジ画像分類（pHash）は、テンプレートの取得元と実運用画面（本モジュールが対象とする
RANK MATCH: SINGLE画面）との差異により、実データでの信頼性が確認できなかった
（同一Sランクのバッジ同士でハミング距離144、A/UNRANKEDより遠いという逆転が発生）ため、
この用途では採用を見送っている。

依存: opencv-python, numpy, pytesseract（+ システム側にtesseract本体が必要）
  uv pip install opencv-python numpy pytesseract
  Windows: tesseractバイナリは https://github.com/UB-Mannheim/tesseract/wiki から別途インストール
"""

import sys
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import pytesseract

from ravens_record_core.detectors.rate import is_plausible_rate
from ravens_record_core.system import tesseract_setup


def imread_unicode(path: Path) -> np.ndarray | None:
    """Windowsで非ASCIIパスのファイルも読めるようにする"""
    if not path.exists():
        return None
    data = np.fromfile(str(path), dtype=np.uint8)
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def crop_roi(image: np.ndarray, roi: tuple[float, float, float, float]) -> np.ndarray:
    h, w = image.shape[:2]
    x0, y0, x1, y1 = roi
    return image[int(h * y0):int(h * y1), int(w * x0):int(w * x1)]


# --- ROI定義 --------------------------------------------------------------
# RANK MATCH: SINGLE 画面（1920x1080）で実測した相対座標。
# MATCH INFOパネル内、RANK表示のPLACE / RATING 数値欄。
# 解像度・UIスケーリングが異なる環境では要調整。

HOME_PLACE_ROI = (0.3932, 0.4472, 0.4688, 0.4769)
HOME_RATING_ROI = (0.3932, 0.4935, 0.4688, 0.5231)


OCR_CONFIG = "--psm 7 -c tessedit_char_whitelist=0123456789"
OCR_UPSCALE = 3  # 数字OCRの精度を上げるための拡大倍率（実測で709/782/1443/1390すべて正解）


def _ocr_digits(image: np.ndarray) -> int | None:
    h, w = image.shape[:2]
    resized = cv2.resize(image, (w * OCR_UPSCALE, h * OCR_UPSCALE), interpolation=cv2.INTER_LANCZOS4)
    tesseract_setup.ensure()
    text = pytesseract.image_to_string(resized, config=OCR_CONFIG).strip()
    if not text.isdigit():
        return None
    return int(text)


def _ocr_digits_binary(image: np.ndarray) -> int | None:
    """Tesseract のもう一つの読み方: 白黒にして反転（白地に黒い数字）してから読む（色・明るさの違いに左右されにくい）"""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    h, w = gray.shape[:2]
    resized = cv2.resize(gray, (w * OCR_UPSCALE, h * OCR_UPSCALE), interpolation=cv2.INTER_CUBIC)
    _, binary = cv2.threshold(resized, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    binary = cv2.copyMakeBorder(binary, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=255)
    tesseract_setup.ensure()
    text = pytesseract.image_to_string(binary, config=OCR_CONFIG).strip()
    if not text.isdigit():
        return None
    return int(text)


# 数字を読む経路（それぞれ独立に読み、2 つ以上が同じ値を出したときだけ採用する。
# 1 つの読み方だけが毎回同じ読み誤りをする（例: Tesseract が 1 を 7 と読む）ことがあるため、1 つだけの値は使わない）
#  ・tesseract: 拡大して、そのまま Tesseract で読む（従来の読み方）
#  ・tesseract_binary: 白黒・反転してから Tesseract で読む
#  ・template: ゲームの数字の形と照らし合わせて読む（rate_digits）
def _readers():
    from ravens_record_core.ocr.rate_digits import read_digits
    # 速い・確かなものから順に（最初の 2 つが一致すれば、3 つ目は読まない）
    return {"template": read_digits, "tesseract": _ocr_digits, "tesseract_binary": _ocr_digits_binary}


def _read_one(reader, image: np.ndarray) -> int | None:
    try:
        return reader(image)
    except Exception:
        return None  # 1 つの経路が失敗しても、ほかの経路で読む


def _read_paths(image: np.ndarray) -> dict[str, int | None]:
    """すべての経路で読む（調査・テスト用）"""
    return {name: _read_one(reader, image) for name, reader in _readers().items()}


def read_voted(image: np.ndarray, valid=lambda v: True) -> int | None:
    """経路を順に読み、2 つが一致した時点でその値にする（一致しなければ None）"""
    readings: dict[str, int | None] = {}
    for name, reader in _readers().items():
        readings[name] = _read_one(reader, image)
        value = vote_digits(readings, valid)
        if value is not None:
            return value
    return None


def vote_digits(readings: dict[str, int | None], valid=lambda v: True) -> int | None:
    """経路ごとの読みから、2 つ以上が一致した値（ありえない値は外す）。一致しなければ None"""
    counts: dict[int, int] = {}
    for value in readings.values():
        if value is not None and valid(value):
            counts[value] = counts.get(value, 0) + 1
    if not counts:
        return None
    value, n = max(counts.items(), key=lambda kv: kv[1])
    return value if n >= 2 else None


@dataclass
class RateRankResult:
    is_s_rank: bool
    s_rank_position: int | None
    rate: int | None


def detect_rate_and_rank(frame: np.ndarray) -> RateRankResult:
    """PLACE欄のOCRを試み、成功すればSランクとみなして順位・レートを取得する。
    失敗すればSランク以外とみなし、両方ともNoneのまま処理をスキップする
    （要件5.1: 順位表示を持つのはSランクのプレイヤーのみ、という仕様を逆手に取った判定）。"""

    # 横長のモニターでは、ロビーの表示は中央の 16:9 にある（screencap.menu_area）
    from ravens_record_core.system import screencap
    frame, _ = screencap.menu_area(frame)

    # 順位・レートとも、3 つの読み方のうち 2 つ以上が一致した値だけを使う（vote_digits）
    place_img = crop_roi(frame, HOME_PLACE_ROI)
    s_rank_position = read_voted(place_img, valid=lambda v: v > 0)

    rating_img = crop_roi(frame, HOME_RATING_ROI)
    rate = read_voted(rating_img, valid=is_plausible_rate)  # ありえない値（読み誤り）は、読めなかった扱い

    return RateRankResult(is_s_rank=(s_rank_position is not None), s_rank_position=s_rank_position, rate=rate)


def capture_stable_rate(
    capture_func,
    max_attempts: int = 15,
    interval_sec: float = 0.2,
    required_matches: int = 3,
    ignore_s_rank: bool = False,
    trace: dict | None = None,
) -> RateRankResult | None:
    """複数回フレームを取得し、指定回数連続で同じOCR結果が得られたらその値を採用する。

    Args:
        capture_func: フレーム（np.ndarray）を返す、または None を返すキャプチャ関数
        max_attempts: 最大試行回数
        interval_sec: 試行間隔（秒）
        required_matches: 確定に必要な連続一致回数
        ignore_s_rank: Sランク判定（PLACE欄取得）を無視してレート取得のみでも成功とみなすか
        trace: 渡すと、読めなかった理由を書き込む（failure_reason の形。調査用）

    Returns:
        安定した RateRankResult が得られた場合はそのオブジェクト。
        規定回数以内に安定しなかった、または画像取得失敗続きの場合は None。
    """
    history = []
    no_frame = 0
    last_frame = None

    def done(result):
        if trace is not None:
            trace.update(failure_reason(history, no_frame, last_frame, result))
        return result

    for _ in range(max_attempts):
        frame = capture_func()
        if frame is None:
            no_frame += 1
        else:
            last_frame = frame
            res = detect_rate_and_rank(frame)

            if not ignore_s_rank and not res.is_s_rank:
                history.append("NOT_S_RANK")
            else:
                if ignore_s_rank:
                    # Sランク無視の場合、レートさえ取れればOK
                    if res.rate is not None:
                        history.append((res.rate, res.s_rank_position))
                    else:
                        history.append("READ_ERROR")
                else:
                    if res.rate is not None and res.s_rank_position is not None:
                        history.append((res.rate, res.s_rank_position))
                    else:
                        history.append("READ_ERROR")

            if len(history) >= required_matches:
                recent = history[-required_matches:]
                if all(x == recent[0] for x in recent):
                    # 全て一致した場合、結果を返す
                    val = recent[0]
                    if val == "NOT_S_RANK":
                        return done(RateRankResult(is_s_rank=False, s_rank_position=None, rate=None))
                    elif val != "READ_ERROR":
                        # val は (rate, position) のタプル
                        return done(res)

        time.sleep(interval_sec)

    return done(None)


def failure_reason(history: list, no_frame: int, last_frame, result) -> dict:
    """読めなかった理由（調査用。ログと、試合の記録の rate_*_note に書く短い記号）
    reason: ok / not_s_rank（PLACE 欄が読めず S ランク以外とみなした）/ no_frame（画面を取れない）
            / read_error（レートか PLACE 欄の読みがそろわない）/ unstable（読めたが、続けて同じ値にならない）
    readings: 最後の画面の、経路ごとの読み（レート・PLACE 欄）"""
    if result is not None:
        reason = "not_s_rank" if not result.is_s_rank else "ok"
    elif not history:
        reason = "no_frame"
    elif all(h == "READ_ERROR" for h in history):
        reason = "read_error"
    else:
        reason = "unstable"
    info = {
        "reason": reason,
        "frames": len(history),
        "no_frame": no_frame,
        "not_s_rank": sum(1 for h in history if h == "NOT_S_RANK"),
        "read_error": sum(1 for h in history if h == "READ_ERROR"),
        "values": sorted({h[0] for h in history if isinstance(h, tuple)}),
    }
    if reason != "ok" and last_frame is not None:
        try:
            info["rate_paths"] = _read_paths(crop_roi(last_frame, HOME_RATING_ROI))
            info["place_paths"] = _read_paths(crop_roi(last_frame, HOME_PLACE_ROI))
        except Exception:
            pass  # 読めなかった理由の調査用の値。取れなければ、付けないだけ
    return info



# --- 動作確認用 --------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("使い方: python rate_detector.py <RANK MATCH: SINGLE画面のスクショ>")
        sys.exit(1)

    frame = imread_unicode(Path(sys.argv[1]))
    if frame is None:
        print("画像の読み込みに失敗しました")
        sys.exit(1)

    result = detect_rate_and_rank(frame)

    if not result.is_s_rank:
        print("Sランクではない（PLACE欄が読み取れない）ため、順位・レートの取得をスキップしました")
    else:
        print(f"順位: {result.s_rank_position if result.s_rank_position is not None else '取得失敗'}")
        print(f"レート: {result.rate if result.rate is not None else '取得失敗'}")
