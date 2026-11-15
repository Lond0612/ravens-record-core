"""
保存先と同梱データの場所

  data_dir()          … 実行時に書き込むデータ（ログ・試合ごとの JSON・判定の控え）。Config.data_dir
  logs_dir()          … data_dir()/logs
  bundled_data_dir()  … ライブラリに同梱したデータ（labels_pool.json）。PyInstaller でまとめたときも、モジュールの隣
"""

from pathlib import Path

from ravens_record_core import runtime
from ravens_record_core.config import is_frozen  # noqa: F401 （ほかのモジュールが paths.is_frozen として使う）


def data_dir() -> Path:
    return runtime.config().data_dir


def logs_dir() -> Path:
    return runtime.config().logs_dir


def bundled_data_dir() -> Path:
    return Path(__file__).resolve().parent / "data"


def trigger_exe() -> str:
    """監視するゲームの実行ファイル名（Config.trigger_exe）"""
    return runtime.config().trigger_exe
