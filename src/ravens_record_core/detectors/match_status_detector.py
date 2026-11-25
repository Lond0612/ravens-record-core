"""
AC6ランクマッチ戦歴記録サービス - マッチング状態判定（マッチング中 / 対戦開始）

画面右上の「マッチングしています」「対戦開始します」に相当する表示をOCRで読み取り、
現在のマッチング状態を判定する。日本語・英語・韓国語の3言語に対応する。

前提：
- Tesseractに日本語(jpn)・韓国語(kor)の言語データが追加インストールされている必要がある。
  Windows版Tesseractインストーラでは、インストール時に "Additional language data" で
  Japanese / Korean にチェックを入れる（インストール後の追加は公式配布のtessdataファイルを
  Tesseract-OCR/tessdata フォルダに配置すればよい）。
- モード判定（mode_detector.py）と同様、AC6ウィンドウをPrintWindowで直接キャプチャする。
"""

import os
import re
import time
from ctypes import windll
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
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

# 画面右上のマッチング状態表示ROI（AC6ウィンドウのクライアント領域に対する相対座標 %）
# 言語によって文字列の長さが変わる（英語「Matchmaking」〜韓国語「대전을 시작합니다」等）ため、
# 右寄せ表示を前提に横幅を広めに取っている。
STATUS_ROI_PERCENT = {
    "left": 0.40,
    "top": 0.08,
    "width": 0.58,
    "height": 0.05,
}

PW_RENDERFULLCONTENT = 2
# 1 = PW_CLIENTONLY。クライアント領域だけを撮る（付けないと、ウィンドウモードでは枠とタイトルバーの分だけ
# 中身が右下にずれ、下端・右端が切れる。2026-10 修正）
PW_CLIENTONLY = 1
DEBUG = False

# OCRに渡す言語（日本語+韓国語+英語の複合認識）
OCR_LANGS = "jpn+kor+eng"

# 状態ごとの対応文言（言語ごとにOCRの誤読対策として複数パターンを許容する）
STATUS_DICTIONARY = {
    "MATCHING": [
        "マッチングしています",
        "매칭 중입니다",
        "Matchmaking",
    ],
    "MATCH_START": [
        "対戦を開始します",
        "대전을 시작합니다",
        "Commencing match",
    ],
}


def _log(message: str) -> None:
    logger.log(message)


def _debug_log(message: str) -> None:
    if DEBUG:
        logger.log(f"[DEBUG] {message}", level="debug")


def _pick_ocr_langs() -> str:
    """OCR に渡す言語。使える言語データだけにする（無い言語を渡すと、Tesseract はそれを飛ばして読むだけで、気づけないため）。
    足りない言語があれば、ログに出す（日本語が無いと「マッチングしています」は読めない）"""
    tesseract_setup.ensure()
    available = tesseract_setup.available_languages()
    langs, missing = tesseract_setup.pick_languages(tuple(OCR_LANGS.split("+")), available)
    _health["tesseract"] = {"cmd": pytesseract.pytesseract.tesseract_cmd, "languages": available, "missing": missing}
    if missing:
        _log(f"[警告] Tesseract に言語データ {missing} がありません（{pytesseract.pytesseract.tesseract_cmd}）。"
             f"ゲームの表示言語によっては「マッチングしています / 対戦を開始します」を読めず、対戦の開始を検出できません。")
    return langs


# 読み取りの様子（health_snapshot で取り出せる。status_unreadable を参照）
_health: dict = {
    "reads": 0,           # OCR まで行った回数
    "recognized": 0,      # 状態を読めた回数
    "band_unread": 0,     # 帯（黄色い文字）が出ているのに、状態を読めなかった回数（status_unreadable は、これで判断する）
    "band_unread_streak_max": 0,  # 帯が出ているのに読めなかった回が、続いたいちばん長い回数（reset_band_streak から。status_unreadable）
    "no_frame": 0,        # 画面を撮れなかった・最小化中の回数
    "last_frame": None,   # 最後に撮れたゲームの画面の大きさ（幅 x 高さ）と、黒い帯を除いたか
    "recent_raw": [],     # 最近の、読めなかったときの文字（重複を除いて 5 件まで）
    "strips": [],         # 端末に残した、読めなかったときの帯の画像（場所）
    "tesseract": None,
}
_ocr_langs: Optional[str] = None
# 帯が出ているのに読めなかった回が続いた数。帯が出ていない回が BAND_STREAK_GAP 回までなら、続いているとみなす（帯の点滅など）
BAND_STREAK_GAP = 2
_streak = {"n": 0, "gap": 0}


def reset_band_streak() -> None:
    """「読めなかった回が続いた数」を数え直す（ロビーで待ち始めるとき）"""
    _streak["n"] = _streak["gap"] = 0
    _health["band_unread_streak_max"] = 0


def health_snapshot() -> dict:
    """読み取りの様子（調査用。画像は含めない）"""
    return {k: (list(v) if isinstance(v, list) else (dict(v) if isinstance(v, dict) else v)) for k, v in _health.items()}


@dataclass
class StatusResult:
    status: Optional[str]    # "MATCHING" / "MATCH_START" / None（判定不能）
    raw_text: str


def find_target_window(exe_name: Optional[str] = None):
    """指定した実行ファイル名を持つプロセスのメインウィンドウhwndを返す"""
    exe_name = exe_name or paths.trigger_exe()
    candidates = []

    def enum_handler(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        # プロセス名で判定（名前を取得できないときは、ウィンドウのタイトルで判定する。gamewindow を参照）
        if not gamewindow.is_game_window(hwnd, exe_name):
            return
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        if max(0, right - left) * max(0, bottom - top) <= 0:
            return
        candidates.append((max(0, right - left) * max(0, bottom - top), hwnd))

    win32gui.EnumWindows(enum_handler, None)
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[0][1]


def _capture_window_content(hwnd) -> Optional[np.ndarray]:
    """PrintWindowでウィンドウのクライアント領域をキャプチャし、BGR画像として返す"""
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
        )
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    finally:
        win32gui.DeleteObject(save_bitmap.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)


def _crop_roi_percent(img_bgr: np.ndarray, roi_percent: dict) -> np.ndarray:
    h, w = img_bgr.shape[:2]
    left = int(w * roi_percent["left"])
    top = int(h * roi_percent["top"])
    width = int(w * roi_percent["width"])
    height = int(h * roi_percent["height"])
    return img_bgr[top:top + height, left:left + width]


def _grab_roi_from_monitor(sct: mss, monitor: dict, roi_percent: dict) -> np.ndarray:
    mon_w, mon_h = monitor["width"], monitor["height"]
    left = monitor["left"] + int(mon_w * roi_percent["left"])
    top = monitor["top"] + int(mon_h * roi_percent["top"])
    width = int(mon_w * roi_percent["width"])
    height = int(mon_h * roi_percent["height"])
    shot = sct.grab({"left": left, "top": top, "width": width, "height": height})
    return cv2.cvtColor(np.array(shot), cv2.COLOR_BGRA2BGR)


def _preprocess_for_ocr(img_bgr: np.ndarray) -> np.ndarray:
    """OCR精度向上のための前処理（拡大＋二値化＋反転）

    以前はノイズ除去（メディアンブラー＋モルフォロジー処理）を入れていたが、
    濁点（゛）のような小さい付随記号まで消えてしまう副作用があったため撤回。
    ROIをNP行を含まない狭い範囲に絞ったことで、背景ノイズの影響は小さいはず。
    """
    resized = cv2.resize(img_bgr, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 170, 255, cv2.THRESH_BINARY)
    return cv2.bitwise_not(binary)


def _normalize(text: str) -> str:
    """比較用に空白・改行を除去した文字列を返す"""
    return re.sub(r"\s+", "", text)


def _match_status(raw_text: str) -> Optional[str]:
    """OCR結果を辞書と照合し、状態キーを返す（部分一致で判定）"""
    normalized = _normalize(raw_text)
    if not normalized:
        return None

    for status_key, phrases in STATUS_DICTIONARY.items():
        for phrase in phrases:
            normalized_phrase = _normalize(phrase)
            if normalized_phrase and normalized_phrase in normalized:
                return status_key
    return None


def detect_status(img_bgr: np.ndarray) -> StatusResult:
    """ROI画像からマッチング状態を判定する。

    先に、文の形をテンプレートと照らし合わせる（ocr_shapes。Tesseract の言語データが無くても読める）。
    読めなかったときだけ、Tesseract で読む。
    """
    global _ocr_langs
    shape = ocr_shapes.read("status", img_bgr)
    if shape is not None:
        return StatusResult(status=shape, raw_text=f"[形] {shape}")
    if _ocr_langs is None:
        _ocr_langs = _pick_ocr_langs()
    processed = _preprocess_for_ocr(img_bgr)
    config = "--psm 7"
    tesseract_setup.ensure()
    raw_text = pytesseract.image_to_string(processed, lang=_ocr_langs, config=config).strip()

    status = _match_status(raw_text)
    return StatusResult(status=status, raw_text=raw_text)


# 読めなかったときの帯（ROI）を、端末に数枚だけ残す（keep_unread_strips が True の間。本体が、ランクマッチのロビーで対戦の開始を
# 待つ間だけ True にする）。場所はログに書く（調べるときの手がかり）
keep_unread_strips = False
STRIP_INTERVAL_SEC = 120.0   # 残す間隔
STRIP_KEEP = 5               # 残す枚数（古いものから消す）
_last_strip_at: Optional[float] = None


def _strip_dir() -> Path:
    from ravens_record_core import paths
    return paths.logs_dir() / "status_strips"


def _keep_strip(roi_img: np.ndarray, raw_text: str, clock=time.monotonic) -> Optional[Path]:
    global _last_strip_at
    now = clock()
    if _last_strip_at is not None and now - _last_strip_at < STRIP_INTERVAL_SEC:
        return None
    _last_strip_at = now
    try:
        folder = _strip_dir()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"status_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        ok, buf = cv2.imencode(".png", roi_img)  # cv2.imwrite は、パスに日本語などがあると書けない
        if not ok:
            return None
        path.write_bytes(buf.tobytes())
        for old in sorted(folder.glob("status_*.png"))[:-STRIP_KEEP]:
            old.unlink(missing_ok=True)
    except Exception as e:
        _log(f"[状態の読み取り] [警告] 読めなかったときの帯を保存できませんでした: {e!r}")
        return None
    strips = _health["strips"]
    strips.append(str(path))
    del strips[:-STRIP_KEEP]
    _log(f"[状態の読み取り] 「マッチングしています / 対戦を開始します」を読めなかったときの帯を残しました（送りません）: "
         f"{path} (raw='{raw_text[:40]}')")
    return path


# 読み取りの範囲のうち、黄色い文字の画素がこの割合以上なら「帯が出ている」とみなす。
# 見本（tests/fixtures/ocr/status）では、マッチング中 1.6〜1.8%・対戦開始 2.1〜3.4%。帯の出ていないロビーや
# ほかの画面（2026-11 に status_unreadable になった端末の帯）は 0〜0.02%
BAND_MIN_FRACTION = 0.005


def band_visible(roi_img: Optional[np.ndarray]) -> bool:
    """マッチングの状態の帯（黄色い文字）が出ているか。ロビーで放置しているだけの間は出ない"""
    if roi_img is None or roi_img.size == 0 or roi_img.ndim != 3:
        return False
    mask, _ = ocr_shapes.status_mask(roi_img)
    return float(mask.mean()) >= BAND_MIN_FRACTION


def _note_read(roi_img: np.ndarray, result: StatusResult) -> None:
    """読み取りの様子を数える。読めなかったときは、帯を残す（keep_unread_strips の間）"""
    _health["reads"] += 1
    if result.status is not None:
        _health["recognized"] += 1
        _streak["n"] = _streak["gap"] = 0
        return
    if band_visible(roi_img):
        _health["band_unread"] += 1
        _streak["n"] += 1
        _streak["gap"] = 0
        _health["band_unread_streak_max"] = max(_health["band_unread_streak_max"], _streak["n"])
    else:
        _streak["gap"] += 1
        if _streak["gap"] > BAND_STREAK_GAP:
            _streak["n"] = 0
    raw = (result.raw_text or "").strip()
    recent = _health["recent_raw"]
    if raw and raw[:40] not in recent:
        recent.append(raw[:40])
        del recent[:-5]
    if keep_unread_strips:
        _keep_strip(roi_img, raw)


def detect_current_match_status(target_exe: Optional[str] = None, monitor_index: int = 1, save_debug: bool = False) -> StatusResult:
    """AC6ウィンドウの中身を直接キャプチャしてマッチング状態を判定する"""
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
                # 16:9 以外の画面で付く黒い帯・横長のモニターの左右を除く（右上の表示が読み取りの範囲から外れるため。screencap.menu_area）
                window_img, trimmed = screencap.menu_area(window_img)
                _health["last_frame"] = {"width": int(window_img.shape[1]), "height": int(window_img.shape[0]), "bars_trimmed": trimmed}
                roi_img = _crop_roi_percent(window_img, STATUS_ROI_PERCENT)
                source = "ac6_window_printwindow"
            else:
                _debug_log("PrintWindowによるキャプチャに失敗")
    else:
        _debug_log(f"ウィンドウ未検出: プロセス '{target_exe}' が見つかりません")

    if roi_img is None:
        if not screencap.monitor_fallback_allowed(hwnd):
            # ゲームが見つからない・最小化中・裏にあるときは、モニターに写っているのはゲームではない（別のアプリを読まない）
            _health["no_frame"] += 1
            return StatusResult(status=_match_status(""), raw_text="")
        _debug_log("モニタ全体にフォールバックします（フォアグラウンド表示が必要）")
        try:
            with mss() as sct:
                monitor = sct.monitors[monitor_index] if monitor_index < len(sct.monitors) else sct.monitors[1]
                roi_img = _grab_roi_from_monitor(sct, monitor, STATUS_ROI_PERCENT)
                source = "monitor_fallback"
        except ScreenShotError as e:
            # UAC の確認・ロック画面などで、画面を撮れない間（読めなかったとして、次の回に読み直す）
            _debug_log(f"モニタ全体の撮影に失敗: {e!r}")
            _health["no_frame"] += 1
            return StatusResult(status=_match_status(""), raw_text="")

    _debug_log(f"ROI取得元={source}, サイズ={roi_img.shape[1]}x{roi_img.shape[0]}, 平均輝度={roi_img.mean():.1f}")

    result = detect_status(roi_img)
    _note_read(roi_img, result)

    if save_debug:
        os.makedirs("debug", exist_ok=True)
        cv2.imwrite("debug/debug_status_roi.png", roi_img)
        cv2.imwrite("debug/debug_status_processed.png", _preprocess_for_ocr(roi_img))
        _debug_log("debug/debug_status_roi.png / debug_status_processed.png を保存しました")

    return result


def poll_status(interval_sec: float = 1.0, save_debug: bool = False):
    """監視ループの実行用。状態が変化したときだけユーザ向けログを出す"""
    _log("マッチング状態の監視を開始します（Ctrl+C で停止）")
    last_status = None

    while True:
        result = detect_current_match_status(save_debug=save_debug)
        _debug_log(f"判定結果: status={result.status}, 生テキスト='{result.raw_text}'")

        if result.status is not None and result.status != last_status:
            if result.status == "MATCHING":
                _log("マッチング中を検出しました。")
            elif result.status == "MATCH_START":
                _log("対戦開始を検出しました。")
            last_status = result.status

        time.sleep(interval_sec)


if __name__ == "__main__":
    poll_status()
