"""
ravens-record-core: ARMORED CORE VI の対戦を、画面から自動で判定して記録する（非公式）

    from ravens_record_core import Config, Hooks, Recorder

    Recorder(Config()).run()   # ゲームの起動を待ち、ランクマッチ・カスタムマッチを記録する（Ctrl+C で終わる）

読み込んだだけでは何もしない（画面・ファイル・環境変数・Tesseract の設定に触れない）。
"""

__version__ = "0.1.0"

__all__ = ["Config", "Hooks", "Recorder", "Status", "__version__", "watch"]


def __getattr__(name: str):
    # 重いモジュール（OpenCV など）は、使うときに読み込む
    if name == "Config":
        from ravens_record_core.config import Config
        return Config
    if name in ("Hooks", "Status"):
        from ravens_record_core import hooks
        return getattr(hooks, name)
    if name in ("Recorder", "watch"):
        from ravens_record_core import recorder
        return getattr(recorder, name)
    raise AttributeError(f"module 'ravens_record_core' has no attribute {name!r}")
