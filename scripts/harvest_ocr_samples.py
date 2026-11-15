"""
録画（1920x1080 の mp4）から、文字の形のテンプレート（scripts/make_shape_templates.py）を作るための見本を切り出す。

使い方（ac6-app で）:
  .venv/Scripts/python.exe scripts/harvest_ocr_samples.py <出力フォルダ> <録画.mp4> [<録画.mp4> ...]
今の読み方（Tesseract）で読めた部分を、読めた値を名前にして「<出力フォルダ>/<対象>/<値>__<録画>_<秒>.png」に保存する。
Tesseract の読み誤りが混じることがあるので、作る前に目で確かめ、違うものは消すか名前を直す。
画像は git に含めない（tests/fixtures/ocr は .gitignore）。
"""

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ravens_record_core.detectors import custom_match, match_end, match_status_detector, mode_detector  # noqa: E402

STEP_SEC = 0.25        # 帯・リザルト・状態を見る間隔
SLOW_STEP_SEC = 2.0    # モードの見出し・AP を見る間隔（Tesseract が重いため）
SAME_LABEL_GAP_SEC = 1.0  # 同じ対象・同じ値は、この間隔より詰めて保存しない


def _save(out: Path, target: str, label: str, image: np.ndarray, tag: str, last: dict, sec: float) -> None:
    key = (target, label)
    if image is None or image.size == 0 or sec - last.get(key, -99) < SAME_LABEL_GAP_SEC:
        return
    last[key] = sec
    folder = out / target
    folder.mkdir(parents=True, exist_ok=True)
    cv2.imencode(".png", image)[1].tofile(str(folder / f"{label}__{tag}_{sec:07.2f}.png"))


def _status_has_text(roi: np.ndarray) -> bool:
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    yellow = (hsv[..., 0] >= 20) & (hsv[..., 0] <= 35) & (hsv[..., 1] >= 90) & (hsv[..., 2] >= 60)
    return yellow.mean() > 0.005


def harvest(video: Path, out: Path) -> None:
    cap = cv2.VideoCapture(str(video))
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    step = max(1, int(round(fps * STEP_SEC)))
    slow = max(1, int(round(SLOW_STEP_SEC / STEP_SEC)))
    tag = video.stem.split(" - ")[-1].replace(" ", "_")
    last: dict = {}
    index = 0
    n = 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if index % step:
            index += 1
            continue
        ok, frame = cap.retrieve()
        index += 1
        if not ok or frame.shape[:2] != (1080, 1920):
            continue
        sec = index / fps
        n += 1

        banner = match_end.read_banner(frame)
        if banner:
            kind, phase, outcome = banner
            label = f"phase{phase}_{outcome}" if kind == "phase" else f"final_{outcome}"
            _save(out, "banner", label, match_end._crop(frame, match_end.BANNER_BOX), tag, last, sec)

        result = match_end.read_result_screen(frame)
        if result:
            _save(out, "result_heading", "RESULT", match_end._crop(frame, match_end.RESULT_HEADING_BOX), tag, last, sec)
            for side, (_outcome, points) in result.items():
                if points is not None:
                    row = match_end._crop(frame, match_end.RESULT_ROWS[side])
                    _save(out, "result_points", f"{points}_{side}", row, tag, last, sec)

        status_roi = match_status_detector._crop_roi_percent(frame, match_status_detector.STATUS_ROI_PERCENT)
        if _status_has_text(status_roi):
            status = match_status_detector.detect_status(status_roi).status
            if status:
                _save(out, "status", status, status_roi, tag, last, sec)

        if n % slow == 0:
            mode = mode_detector.detect_mode(mode_detector._crop_roi_percent(frame, mode_detector.MODE_ROI_PERCENT)).mode
            if mode:
                _save(out, "mode", mode, mode_detector._crop_roi_percent(frame, mode_detector.MODE_ROI_PERCENT), tag, last, sec)
            if mode != "CUSTOM" and custom_match.has_ap_hud(frame):
                _save(out, "ap", "AP", custom_match._crop(frame, custom_match.AP_ROI), tag, last, sec)
    cap.release()
    print(f"{video.name}: {n} 枚を見ました", flush=True)


if __name__ == "__main__":
    out_dir = Path(sys.argv[1])
    for path in sys.argv[2:]:
        harvest(Path(path), out_dir)
