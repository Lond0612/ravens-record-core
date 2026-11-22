"""
コマンドラインの入口（python -m ravens_record_core / ravens-record-core）

    python -m ravens_record_core                      ゲームの起動を待ち、試合を記録する（Ctrl+C で終わる）
    python -m ravens_record_core --log-file ev.jsonl  イベントを JSON Lines でファイルにも書く
    python -m ravens_record_core --json               標準出力のログを JSON Lines にする

子プロセス（監視の側が起動する。ふつうは直接使わない）:
    python -m ravens_record_core --score-detector --output PATH
    python -m ravens_record_core --loading-watcher --parent-pid PID
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ravens_record_core import __version__
from ravens_record_core.monitor.children import LOADING_WATCHER_FLAG, SCORE_DETECTOR_FLAG


def child_main(argv: list[str]) -> bool:
    """argv が子プロセスの起動なら、それを動かして True を返す（1 つの実行ファイルにまとめた側の入口からも呼ぶ）"""
    if SCORE_DETECTOR_FLAG in argv:
        _init_child()
        from ravens_record_core.detectors.score_detector import main as score_detector_main
        score_detector_main([a for a in argv if a != SCORE_DETECTOR_FLAG])
        return True
    if LOADING_WATCHER_FLAG in argv:
        _init_child()
        from ravens_record_core.detectors.loading_screen_watcher import main as loading_watcher_main
        loading_watcher_main([a for a in argv if a != LOADING_WATCHER_FLAG])
        return True
    return False


def _init_child() -> None:
    """子プロセスの設定（親が環境変数で渡したもの）"""
    from ravens_record_core import runtime
    from ravens_record_core.system import nowindow, screencap
    nowindow.install()
    screencap.enable_dpi_awareness()
    runtime.init()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ravens-record-core",
        description="ARMORED CORE VI のランクマッチ・カスタムマッチを、画面から自動で判定して記録します（非公式）。",
    )
    parser.add_argument("--version", action="version", version=f"ravens-record-core {__version__}")
    parser.add_argument("--data-dir", type=Path, help="保存先（ログ・試合ごとの JSON）。既定は %%LOCALAPPDATA%%\\RavensRecordCore")
    parser.add_argument("--log-file", type=Path, help="イベントを JSON Lines で追記するファイル")
    parser.add_argument("--events-only", action="store_true", help="--log-file に、ふつうのログ行を書かない（状態の変化・試合の記録だけ）")
    parser.add_argument("--json", action="store_true", help="標準出力のログを JSON Lines にする")
    parser.add_argument("--trigger-exe", help="監視するゲームの実行ファイル名（既定は armoredcore6.exe）")
    parser.add_argument("--tesseract-dir", type=Path, help="tesseract.exe のあるフォルダ")
    parser.add_argument("--no-custom", action="store_true", help="カスタムマッチを記録しない")
    parser.add_argument("--no-team", action="store_true", help="チーム戦を記録しない")
    return parser


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if child_main(argv):
        return

    args = build_parser().parse_args(argv)

    from ravens_record_core import logger
    from ravens_record_core.config import Config
    from ravens_record_core.matchlog import JsonlFileSink
    from ravens_record_core.recorder import Recorder
    from ravens_record_core.system import lowload, nowindow, screencap

    nowindow.install()
    # ウィンドウの大きさ・撮影を、画面の拡大率（DPI）の影響を受けない実際の画素で扱う
    screencap.enable_dpi_awareness()
    # ゲームを邪魔しない設定（読み取りを 1 コアに・優先度を通常以下に。子プロセスにも引き継がれる）
    lowload.apply()

    config = Config.from_env(
        data_dir=args.data_dir,
        trigger_exe=args.trigger_exe,
        tesseract_dir=args.tesseract_dir,
        json_logs=True if args.json else None,
        record_custom=False if args.no_custom else None,
        record_team=False if args.no_team else None,
    )
    if args.log_file:
        logger.add_sink(JsonlFileSink(args.log_file, include_logs=not args.events_only))
    Recorder(config).run()


if __name__ == "__main__":
    main()
