"""
ログの書き出し（試合ごとの JSON・JSON Lines のイベントログ）

試合ごとの JSON（logs_dir に保存）:
  ランクマッチ        YYYY-MM-DD_HH-MM-SS_自分-相手_session.json        （MatchSession.to_log_dict。match_session.py を参照）
  カスタムマッチ      YYYY-MM-DD_HH-MM-SS_custom_自分-相手.json          （custom_match.build_payload ＋ 下の項目）
  観戦したカスタム    YYYY-MM-DD_HH-MM-SS_custom_spectated_ALPHA-BETA.json（custom_match.build_spectated_payload ＋ 下の項目）
  読めなかったスコアは X。カスタムマッチの JSON には、payload に加えて log_schema・kind・ended_at・core_version を書く。

JSON Lines（JsonlFileSink）: logger のイベントを 1 行 1 件で追記する。
  {"ts": "...", "level": "info", "event": "status", "message": "...", "state": "loading", ...}
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

from ravens_record_core import __version__, logger
from ravens_record_core.hooks import CUSTOM_SPECTATED

# カスタムマッチの JSON の形の版（項目を足すだけなら上げない）
CUSTOM_LOG_SCHEMA_VERSION = 1


def write_json_atomic(path: Path, data: dict) -> None:
    """途中で止まっても壊れたファイルを残さないよう、一時ファイルに書いてから置き換える"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _score_text(a, b) -> str:
    return f"{a if a is not None else 'X'}-{b if b is not None else 'X'}"


def write_custom_match(logs_dir: Path, kind: str, payload: dict, ended_at: datetime) -> Path | None:
    """カスタムマッチの対戦 1 回を JSON に保存する（保存できなければ None。記録の流れは止めない）"""
    spectated = kind == CUSTOM_SPECTATED
    started = payload.get("started_at") if spectated else payload.get("played_at")
    try:
        stamp = datetime.fromisoformat(started).strftime("%Y-%m-%d_%H-%M-%S") if started else ended_at.strftime("%Y-%m-%d_%H-%M-%S")
    except ValueError:
        stamp = ended_at.strftime("%Y-%m-%d_%H-%M-%S")
    if spectated:
        name = f"{stamp}_custom_spectated_{_score_text(payload.get('alpha_wins'), payload.get('beta_wins'))}.json"
    else:
        name = f"{stamp}_custom_{_score_text(payload.get('own_wins'), payload.get('opp_wins'))}.json"
    data = {
        **payload,
        "log_schema": CUSTOM_LOG_SCHEMA_VERSION,
        "kind": "custom_spectated" if spectated else "custom",
        "ended_at": payload.get("ended_at") or ended_at.astimezone().isoformat(timespec="seconds"),
        "core_version": __version__,
    }
    path = Path(logs_dir) / name
    try:
        write_json_atomic(path, data)
    except OSError as e:
        logger.log(f"[カスタム] [警告] 試合の JSON を保存できませんでした: {e!r}", level="warn")
        return None
    logger.log(f"[カスタム] 試合の JSON を保存しました: {path}")
    return path


class JsonlFileSink:
    """logger のイベントを JSON Lines でファイルに追記する出力先（logger.add_sink に渡す）。

    include_logs=False なら、ふつうのログ行（event = log）は書かず、状態の変化や試合の記録などのイベントだけ書く。
    """

    def __init__(self, path: Path, include_logs: bool = True):
        self.path = Path(path)
        self.include_logs = include_logs
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, obj: dict) -> None:
        if not self.include_logs and obj.get("event") == "log":
            return
        line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
