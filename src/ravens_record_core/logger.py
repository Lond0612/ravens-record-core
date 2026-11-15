"""
ログとイベントの出力（人が読むテキスト / JSON Lines を切り替える）

JSON モードでは 1 行 = 1 JSON オブジェクトで出力する:

    {"ts":"2026-09-30T13:00:00+09:00","level":"info","event":"status","message":"...","state":"in_match"}

- level : debug / info / warn / error
- event : log（通常のログ） / status（監視状態の変化） / match_detected / map_detected /
          score_updated / score_confirmed / score_missing / match_saved / custom_match_saved ...（events.py を参照）
- 追加フィールドは event ごとに付く（status なら state など）

JSON は ensure_ascii=True で出力する。パイプ経由の標準出力はコードページ（cp932 等）に左右されるため、
日本語を \\uXXXX に変換して文字化けを避ける（受信側の JSON パーサが元に戻す）。

JSON モードは環境変数 RRC_JSON_LOGS=1 で有効になる。set_json_mode(True) が設定するため、
子プロセス（ScoreDetector）にも引き継がれ、JSON で標準出力に書き込む。

出力先（add_sink）: イベント（dict）を受け取る関数を登録すると、すべてのイベントがそこへも渡る
（ファイルへの記録・別のプロセスへの配信など。matchlog.JsonlFileSink を参照）。
標準出力を使わない構成（set_stdout_enabled(False)）では、子プロセス（ScoreDetector）の標準出力を親が読み取り、
publish_line() で同じ出力先へ流す。
"""

import json
import os
import sys
import threading
from datetime import datetime

_lock = threading.Lock()
_stdout_enabled = True
_sinks: list = []


def set_json_mode(enabled: bool) -> None:
    if enabled:
        os.environ["RRC_JSON_LOGS"] = "1"
    else:
        os.environ.pop("RRC_JSON_LOGS", None)


def is_json_mode() -> bool:
    return os.environ.get("RRC_JSON_LOGS") == "1"


def set_stdout_enabled(enabled: bool) -> None:
    """標準出力への書き込みを切り替える（標準出力を使わない構成では切る）。"""
    global _stdout_enabled
    _stdout_enabled = enabled


def add_sink(sink) -> None:
    """イベント（dict）を受け取る出力先を追加する。sink(obj) が例外を出しても無視する。"""
    _sinks.append(sink)


def clear_sinks() -> None:
    _sinks.clear()


def _write_stdout(line: str) -> None:
    if not _stdout_enabled or sys.stdout is None:
        return
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except (OSError, ValueError):
        pass  # 閉じられた標準出力に書けなくても、監視は止めない


def _dispatch(obj: dict) -> None:
    for sink in list(_sinks):
        try:
            sink(obj)
        except Exception:
            pass  # 出力先の 1 つ（切れた接続など）が失敗しても、ほかの出力先と監視は止めない


def infer_level(message: str) -> str:
    """従来のログ文言（[エラー] / [警告] / [DEBUG] など）から level を推定する。"""
    if "[DEBUG]" in message:
        return "debug"
    if "[エラー]" in message or "エラー:" in message or "Error" in message:
        return "error"
    if "[警告]" in message or "警告:" in message or "Warning" in message:
        return "warn"
    return "info"


def emit(event: str, message: str = "", level: str = "info", **fields) -> None:
    """構造化イベントを出力する。通常モードでは message を従来形式で出す（message が空なら何も出さない）。"""
    obj = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "level": level,
        "event": event,
        "message": message,
    }
    obj.update(fields)
    if _sinks:
        _dispatch(obj)

    if is_json_mode():
        line = json.dumps(obj, ensure_ascii=True, separators=(",", ":"), default=str)
    else:
        if not message:
            return
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
    with _lock:
        _write_stdout(line)


def publish_line(line: str) -> None:
    """子プロセス（ScoreDetector）が標準出力に出した 1 行を、出力先へ流す。JSON でなければ通常のログとして扱う。"""
    line = line.strip()
    if not line:
        return
    try:
        obj = json.loads(line)
        if isinstance(obj, dict) and "event" in obj:
            _dispatch(obj)
            return
    except ValueError:
        pass
    _dispatch({"ts": datetime.now().astimezone().isoformat(timespec="seconds"),
               "level": infer_level(line), "event": "log", "message": line})


def log(message: str, event: str = "log", level: str | None = None, **fields) -> None:
    """通常のログ 1 行。各モジュールの _log / log はこれに委譲する。"""
    for line in str(message).splitlines() or [""]:
        emit(event, line, level or infer_level(line), **fields)


def prefixed(tag: str):
    """「[tag] メッセージ」の形でログを書く関数を返す（各モジュールの _log に使う）。"""

    def _log(message: str, **fields) -> None:
        log(f"[{tag}] {message}", **fields)

    return _log


def log_block(lines: list[str]) -> None:
    for line in lines:
        log(line)


def captures_child_output() -> bool:
    """子プロセス（ScoreDetector）の標準出力を親が読み取って出力先へ流す必要があるか（標準出力を使わない構成のとき）。"""
    return not _stdout_enabled
