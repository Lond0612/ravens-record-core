"""
画面の縦横比の記録（16:9 以外のモニターの調査用。v1.4.11〜）

- 撮った画面の大きさ（"3440x1440" など）を試合ごとに記録する（試合の記録の capture_size。16:9 以外の画面を見つけるため）
- 16:9 以外で、黒い帯も付いていない画面（横長のモニターで、ゲームが横いっぱいに描いている）なら、
  場面ごとに 1 枚ずつ、画面の控えを data_dir/aspect_samples/ に保存する（読み取りの範囲を合わせるための見本。動画の代わり）。
  調べるときは、このフォルダの画像を見る（自動ではどこにも送らない）。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from ravens_record_core import logger, paths
from ravens_record_core.system import screencap

SAMPLES_DIR_NAME = "aspect_samples"
ASPECT_TOLERANCE = 0.02
MAX_SAMPLES = 40           # 保存する画面の数の上限（場面ごとに 1 枚。上書きはしない）

_lock = threading.Lock()
_logged = False


def size_text(frame: Optional[np.ndarray]) -> Optional[str]:
    if frame is None or frame.ndim < 2:
        return None
    h, w = frame.shape[:2]
    return f"{w}x{h}"


def is_16_9(frame: Optional[np.ndarray]) -> bool:
    if frame is None or frame.ndim < 2 or frame.shape[0] == 0:
        return True
    h, w = frame.shape[:2]
    return abs(w / h - screencap.GAME_ASPECT) <= ASPECT_TOLERANCE


def is_unsupported(frame: Optional[np.ndarray]) -> bool:
    """16:9 以外で、黒い帯を除いても 16:9 にならない画面（読み取りの範囲が合わない）"""
    if frame is None or is_16_9(frame):
        return False
    area, _ = screencap.game_area(frame)
    return not is_16_9(area)


def samples_dir() -> Path:
    return paths.data_dir() / SAMPLES_DIR_NAME


def observe(frame: Optional[np.ndarray], scene: str) -> None:
    """場面（lobby / result など）の画面を見る。16:9 に合わない画面なら、その場面の控えを 1 枚だけ保存する。失敗しても試合の記録は止めない"""
    global _logged
    try:
        if not is_unsupported(frame):
            return
        with _lock:
            if not _logged:
                _logged = True
                logger.log(f"[画面の比率] 16:9 ではない画面です（{size_text(frame)}）。読み取りがずれることがあります。"
                           f"調査用に、場面ごとの画面を {samples_dir()} に保存します")
            folder = samples_dir()
            folder.mkdir(parents=True, exist_ok=True)
            name = f"{scene}_{size_text(frame)}.png"
            if (folder / name).exists() or len(list(folder.glob("*.png"))) >= MAX_SAMPLES:
                return
            ok, buf = cv2.imencode(".png", frame)
            if ok:
                (folder / name).write_bytes(buf.tobytes())
    except Exception as e:
        logger.log(f"[画面の比率] [警告] 画面の控えを保存できませんでした: {e!r}")
