"""
実行時の設定の置き場所（init で決める）

ライブラリのモジュールは、読み込んだだけでは何もしない（ファイル・環境変数・Tesseract の設定を変えない）。
設定は init(config) で一度だけ渡す。init を呼ばずに使ったとき（子プロセス・テストなど）は、
初めて使う時点で Config.from_env() を使う。
"""

from __future__ import annotations

import threading

from ravens_record_core.config import Config

_lock = threading.Lock()
_config: Config | None = None


def init(config: Config | None = None) -> Config:
    """設定を決め、ログの形式と Tesseract を整える（何度呼んでもよい。最後に渡したものを使う）"""
    global _config
    from ravens_record_core import logger
    from ravens_record_core.system import tesseract_setup

    with _lock:
        _config = config or Config.from_env()
    if _config.json_logs:
        logger.set_json_mode(True)
    tesseract_setup.configure(_config.tesseract_dir)
    return _config


def config() -> Config:
    """いまの設定（init を呼んでいなければ、環境変数から作る）"""
    global _config
    if _config is None:
        with _lock:
            if _config is None:
                _config = Config.from_env()
    return _config


def reset() -> None:
    """設定を忘れる（テスト用）"""
    global _config
    with _lock:
        _config = None
