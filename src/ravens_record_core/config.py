"""
設定（Config）

監視対象のゲーム・保存先・Tesseract の場所など、実行のしかたを 1 か所にまとめる。
ライブラリの中では環境変数を直接読まない（読むのは Config.from_env だけ）。

子プロセス（スコアの監視・ロード画面の監視）には、to_env() の環境変数で同じ設定を渡す。
子プロセスは Config.from_env() で受け取るので、親と子で監視対象や保存先がずれない。

  RRC_TRIGGER_EXE    監視するゲームの実行ファイル名（テストで、実際に動いているゲームに触れないよう差し替える）
  RRC_DATA_DIR       保存先（ログ・試合ごとの JSON・判定の控え）
  RRC_TESSERACT_DIR  tesseract.exe のあるフォルダ
  RRC_JSON_LOGS      1 なら、標準出力のログを JSON Lines にする
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

DEFAULT_TRIGGER_EXE = "armoredcore6.exe"
DEFAULT_DATA_DIR_NAME = "RavensRecordCore"


def default_data_dir() -> Path:
    """既定の保存先（%LOCALAPPDATA%\\RavensRecordCore）"""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / DEFAULT_DATA_DIR_NAME


@dataclass(frozen=True)
class Config:
    trigger_exe: str = DEFAULT_TRIGGER_EXE
    data_dir: Path = field(default_factory=default_data_dir)
    tesseract_dir: Path | None = None   # None なら、同梱 → PATH → よくあるインストール先の順に探す
    json_logs: bool = False             # 標準出力のログを JSON Lines にする
    record_custom: bool = True          # カスタムマッチも記録する
    app_version: str | None = None      # 試合ごとの JSON に書く、使う側のアプリのバージョン（無ければライブラリのバージョン）

    @property
    def logs_dir(self) -> Path:
        """ログ・試合ごとの JSON の保存先"""
        return self.data_dir / "logs"

    @property
    def target_name(self) -> str:
        """監視するゲームの実行ファイル名（フルパスを渡されたときも、名前だけ）"""
        return os.path.basename(self.trigger_exe)

    @classmethod
    def from_env(cls, environ: dict | None = None, **overrides) -> "Config":
        env = os.environ if environ is None else environ
        values: dict = {}
        if env.get("RRC_TRIGGER_EXE"):
            values["trigger_exe"] = env["RRC_TRIGGER_EXE"]
        if env.get("RRC_DATA_DIR"):
            values["data_dir"] = Path(env["RRC_DATA_DIR"])
        if env.get("RRC_TESSERACT_DIR"):
            values["tesseract_dir"] = Path(env["RRC_TESSERACT_DIR"])
        if env.get("RRC_JSON_LOGS") == "1":
            values["json_logs"] = True
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)

    def to_env(self) -> dict[str, str]:
        """子プロセスへ渡す環境変数（from_env で同じ設定に戻る）"""
        env = {"RRC_TRIGGER_EXE": self.trigger_exe, "RRC_DATA_DIR": str(self.data_dir)}
        if self.tesseract_dir is not None:
            env["RRC_TESSERACT_DIR"] = str(self.tesseract_dir)
        if self.json_logs:
            env["RRC_JSON_LOGS"] = "1"
        return env

    def with_changes(self, **changes) -> "Config":
        return replace(self, **changes)


def is_frozen() -> bool:
    """PyInstaller などで 1 つの実行ファイルにまとめられているか"""
    return bool(getattr(sys, "frozen", False))
