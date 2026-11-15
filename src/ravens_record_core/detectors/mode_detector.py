"""
AC6ランクマッチ戦歴記録サービス - モード判定（SINGLE / TEAM）

画面左上の「RANK MATCH: SINGLE」/「RANK MATCH: TEAM」表示をOCRで読み取り、
現在のマッチングモードを判定する。

- AC6のウィンドウをPrintWindow APIで直接キャプチャする（ウィンドウが裏に回っていても、
  別の仮想デスクトップにいても取得できる）。
- 排他的フルスクリーンモードの場合はDWMを経由しないためPrintWindowが失敗することがある。
  ボーダーレスウィンドウ/ウィンドウモードであれば問題なく動作するはず。

DEBUGフラグをTrueにすると、内部処理の詳細ログ（ウィンドウ検出結果・ROI取得元・
デバッグ画像の保存など）が出力される。通常運用時はFalseのままでよい。
"""

import os
import re
import time
from ctypes import windll
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np
import pytesseract
import win32gui
import win32ui
from mss import mss
from mss.exception import ScreenShotError

from ravens_record_core import logger, paths
from ravens_record_core.ocr import ocr_shapes
from ravens_record_core.system import gamewindow, screencap, tesseract_setup

# 画面左上のモード表示ROI（AC6ウィンドウのクライアント領域に対する相対座標 %）
MODE_ROI_PERCENT = {
    "left": 0.02,
    "top": 0.01,
    "width": 0.25,
    "height": 0.08,
}

# PrintWindowのフラグ。2 = PW_RENDERFULLCONTENT（DirectX等の描画内容も含めて取得を試みる）
PW_RENDERFULLCONTENT = 2
# 1 = PW_CLIENTONLY。クライアント領域だけを撮る（付けないと、ウィンドウモードでは枠とタイトルバーの分だけ
# 中身が右下にずれ、下端・右端が切れる。2026-10 修正）
PW_CLIENTONLY = 1

# 内部処理の詳細ログを出力するかどうか。通常運用時はFalseのままでよい。
DEBUG = False


def _log(message: str) -> None:
    """ログ出力（logger。JSON モードでは JSON Lines）"""
    logger.log(message)


def _debug_log(message: str) -> None:
    """開発者向けの詳細ログ。DEBUG=Trueのときのみ出力される"""
    if DEBUG:
        logger.log(f"[DEBUG] {message}", level="debug")


@dataclass
class ModeResult:
    mode: Optional[str]      # "SINGLE" / "TEAM" / "CUSTOM"（カスタムマッチのルーム画面） / None（判定不能）
    raw_text: str            # OCR生テキスト（デバッグ用）


def find_target_window(exe_name: Optional[str] = None):
    """指定した実行ファイル名を持つプロセスのメインウィンドウhwndを返す

    フルスクリーン/ボーダーレス時はウィンドウタイトルが空文字になることがあるため、
    タイトルの有無では絞り込まず、プロセス名の一致のみで判定する。
    """
    exe_name = exe_name or paths.trigger_exe()
    candidates = []

    def enum_handler(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        # プロセス名で判定（名前を取得できないときは、ウィンドウのタイトルで判定する。gamewindow を参照）
        if not gamewindow.is_game_window(hwnd, exe_name):
            return
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        area = max(0, right - left) * max(0, bottom - top)
        if area <= 0:
            return
        candidates.append((area, hwnd))

    win32gui.EnumWindows(enum_handler, None)
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[0][1]


def _capture_window_content(hwnd) -> Optional[np.ndarray]:
    """PrintWindowでウィンドウのクライアント領域をキャプチャし、BGR画像として返す。

    フォアグラウンドにない（裏に回っている／別の仮想デスクトップにいる）状態でも取得できる。
    排他的フルスクリーンのDirectXアプリではPrintWindowが失敗し、真っ黒/取得失敗になることがある。
    """
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return None

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()

    save_bitmap = win32ui.CreateBitmap()
    save_bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
    save_dc.SelectObject(save_bitmap)

    try:
        result = windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), PW_CLIENTONLY | PW_RENDERFULLCONTENT)
        if result != 1:
            _debug_log(f"PrintWindow戻り値={result}（1以外は失敗の可能性）")

        bmp_info = save_bitmap.GetInfo()
        bmp_bits = save_bitmap.GetBitmapBits(True)
        img = np.frombuffer(bmp_bits, dtype="uint8").reshape(
            (bmp_info["bmHeight"], bmp_info["bmWidth"], 4)
        )  # BGRA
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    finally:
        win32gui.DeleteObject(save_bitmap.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)


def _crop_roi_percent(img_bgr: np.ndarray, roi_percent: dict) -> np.ndarray:
    """画像全体に対する相対座標(%)でROIを切り出す"""
    h, w = img_bgr.shape[:2]
    left = int(w * roi_percent["left"])
    top = int(h * roi_percent["top"])
    width = int(w * roi_percent["width"])
    height = int(h * roi_percent["height"])
    return img_bgr[top:top + height, left:left + width]


def _grab_roi_from_monitor(sct: mss, monitor: dict, roi_percent: dict) -> np.ndarray:
    """フォールバック用：モニタ全体からスクリーン座標ベースでROIを切り出す"""
    mon_w, mon_h = monitor["width"], monitor["height"]
    left = monitor["left"] + int(mon_w * roi_percent["left"])
    top = monitor["top"] + int(mon_h * roi_percent["top"])
    width = int(mon_w * roi_percent["width"])
    height = int(mon_h * roi_percent["height"])

    shot = sct.grab({"left": left, "top": top, "width": width, "height": height})
    img = np.array(shot)  # BGRA
    return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)


def _preprocess_for_ocr(img_bgr: np.ndarray) -> np.ndarray:
    """OCR精度向上のための前処理（拡大＋二値化＋反転）"""
    resized = cv2.resize(img_bgr, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 170, 255, cv2.THRESH_BINARY)
    return cv2.bitwise_not(binary)


def _is_rank_match_heading(text_upper: str) -> bool:
    """見出しが「RANK MATCH」か（先頭の R が読み落とされることがあるため、「ANK」があればよいとする）。"""
    return "ANK" in text_upper.replace(" ", "")


def _is_custom_match_heading(text_upper: str) -> bool:
    """見出しが「CUSTOM MATCH」か（先頭・末尾の読み落としを許容し、「USTOM」か「CUSTO」があればよいとする）。"""
    compact = text_upper.replace(" ", "")
    return "USTOM" in compact or "CUSTO" in compact


# 試合中（スコアの監視が動いている間）に、見出しの欄を Tesseract で読むかの目安（v1.5.5）。
# 試合中は見出しが無いので、形の照合で読めず、毎回 Tesseract に回っていた（1 秒に 4 回。カクつきの原因になりうる）。
#   ・2 値化した白い画素（文字の候補）が、欄のこの割合より少なければ、文字は無いので読まない
#   ・この割合より多ければ、文字ではなく明るい背景（空など）なので読まない（見出しは暗い背景に白い文字）
#   ・それ以外でも、読むのは IN_MATCH_OCR_INTERVAL_SEC 秒に 1 回まで
TEXT_MIN_RATIO = 0.002
IN_MATCH_TEXT_MAX_RATIO = 0.4
IN_MATCH_OCR_INTERVAL_SEC = 3.0
_last_in_match_ocr = 0.0


def _text_ratio(processed: np.ndarray) -> float:
    """前処理した画像（白地に黒い文字）のうち、文字の候補（黒）の割合"""
    return float((processed < 128).mean()) if processed.size else 0.0


def detect_mode(img_bgr: np.ndarray, in_match: bool = False) -> ModeResult:
    """ROI画像からSINGLE/TEAMを判定する。先に見出しの形をテンプレートと照らし合わせ（ocr_shapes）、読めなければ Tesseract で読む。

    in_match: 試合中（スコアの監視が動いている間）。見出しが無いのが普通なので、Tesseract を呼ぶ回を減らす
    """
    global _last_in_match_ocr
    shape = ocr_shapes.read("mode", img_bgr)
    if shape is not None:
        return ModeResult(mode=shape, raw_text=f"[形] {shape}")
    processed = _preprocess_for_ocr(img_bgr)
    ratio = _text_ratio(processed)
    if ratio < TEXT_MIN_RATIO:
        return ModeResult(mode=None, raw_text="")  # 文字が無い（読んでも何も出ない）
    if in_match:
        now = time.monotonic()
        if ratio > IN_MATCH_TEXT_MAX_RATIO or now - _last_in_match_ocr < IN_MATCH_OCR_INTERVAL_SEC:
            return ModeResult(mode=None, raw_text="")
        _last_in_match_ocr = now

    config = "--psm 6 -c tessedit_char_whitelist=RANKMATCH:SINGLETEAM "
    tesseract_setup.ensure()
    raw_text = pytesseract.image_to_string(processed, config=config).strip()
    text_upper = raw_text.upper()

    # SINGLE は「RANK MATCH: SINGLE」の見出しのときだけ（RANK の一部「ANK」も読めていること）。
    # 「SINGLE」の文字だけで判定すると、カスタムマッチ等の別の画面を、ランクマッチと取り違えるおそれがあるため
    if re.search(r"TEAM", text_upper):
        mode = "TEAM"
    elif re.search(r"SINGL", text_upper) and _is_rank_match_heading(text_upper):
        mode = "SINGLE"
    else:
        raw_text_fallback = pytesseract.image_to_string(processed, config="--psm 6").strip()
        text_fb_upper = raw_text_fallback.upper()
        if "TEAM" in text_fb_upper:
            mode = "TEAM"
            raw_text = raw_text_fallback
        elif "SINGL" in text_fb_upper and _is_rank_match_heading(text_fb_upper):
            mode = "SINGLE"
            raw_text = raw_text_fallback
        elif _is_custom_match_heading(text_fb_upper):
            # カスタムマッチのルーム画面（見出し「CUSTOM MATCH」）。ランクマッチとしては扱わない
            mode = "CUSTOM"
            raw_text = raw_text_fallback
        else:
            mode = None
            if not raw_text:
                raw_text = raw_text_fallback

    return ModeResult(mode=mode, raw_text=raw_text)


def detect_current_screen_mode(target_exe: Optional[str] = None, monitor_index: int = 1, save_debug: bool = False,
                               in_match: bool = False) -> ModeResult:
    """AC6ウィンドウの中身を直接キャプチャしてモード判定する（裏に回っていても取得可能）。in_match は detect_mode を参照"""
    target_exe = target_exe or paths.trigger_exe()
    hwnd = find_target_window(target_exe)
    roi_img = None
    source = "unknown"

    if hwnd is not None:
        _debug_log(f"ウィンドウ検出: hwnd={hwnd}, title='{win32gui.GetWindowText(hwnd)}'")
        if not win32gui.IsIconic(hwnd):
            # 手前なら画面から直接（DXGI）、裏なら PrintWindow で撮る（screencap.grab_client。PrintWindow だけだと古い画面が返り続けることがある）
            window_img = screencap.grab_client(hwnd)
            if window_img is not None:
                window_img, _ = screencap.menu_area(window_img)  # 黒い帯・横長のモニターの左右を除く（MatchStatusDetector と同じ）
                roi_img = _crop_roi_percent(window_img, MODE_ROI_PERCENT)
                source = "ac6_window_printwindow"
            else:
                _debug_log("PrintWindowによるキャプチャに失敗（クライアント領域取得不可）")
    else:
        _debug_log(f"ウィンドウ未検出: プロセス '{target_exe}' が見つかりません")

    if roi_img is None:
        if not screencap.monitor_fallback_allowed(hwnd):
            # ゲームが見つからない・最小化中・裏にあるときは、モニターに写っているのはゲームではない（別のアプリを読まない）
            return ModeResult(mode=None, raw_text="")
        _debug_log("モニタ全体にフォールバックします（フォアグラウンド表示が必要）")
        try:
            with mss() as sct:
                monitor = sct.monitors[monitor_index] if monitor_index < len(sct.monitors) else sct.monitors[1]
                roi_img = _grab_roi_from_monitor(sct, monitor, MODE_ROI_PERCENT)
                source = "monitor_fallback"
        except ScreenShotError as e:
            # UAC の確認・ロック画面などで、画面を撮れない間（読めなかったとして、次の回に読み直す）
            _debug_log(f"モニタ全体の撮影に失敗: {e!r}")
            return ModeResult(mode=None, raw_text="")

    _debug_log(f"ROI取得元={source}, サイズ={roi_img.shape[1]}x{roi_img.shape[0]}, 平均輝度={roi_img.mean():.1f}")

    result = detect_mode(roi_img, in_match=in_match)

    if save_debug:
        os.makedirs("debug", exist_ok=True)
        cv2.imwrite("debug/debug_mode_roi.png", roi_img)
        cv2.imwrite("debug/debug_mode_processed.png", _preprocess_for_ocr(roi_img))
        _debug_log("debug/debug_mode_roi.png / debug_mode_processed.png を保存しました")

    return result


def poll_mode(interval_sec: float = 2.0, save_debug: bool = False):
    """監視ループの実行用。モードが変化したときだけユーザ向けログを出す"""
    _log("モード監視を開始します（Ctrl+C で停止）")
    last_mode = None

    while True:
        result = detect_current_screen_mode(save_debug=save_debug)
        _debug_log(f"判定結果: モード={result.mode}, 生テキスト='{result.raw_text}'")

        if result.mode is not None and result.mode != last_mode:
            if result.mode == "TEAM":
                _log("チーム戦を検出しました。監視をスキップします。")
            else:
                _log("シングル戦を検出しました。")
            last_mode = result.mode

        time.sleep(interval_sec)


if __name__ == "__main__":
    poll_mode()
