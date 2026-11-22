"""
差し込み口（Hooks）

使う側のアプリが、監視の途中に自分の処理を差し込むところ。既定はどれも何もしない。
Hooks を継承して、必要なメソッドだけ上書きする。

差し込んだ処理が例外を投げても、監視は止めない（ログに残して続ける。is_ready の例外は「まだ」とみなす）。
画面の画像（frame）は numpy の BGR 配列。監視の側でも使うので、書き換えないこと。

状態とイベントの通知（status / match_saved など）は、ここではなく logger の出力先（logger.add_sink）で受け取る。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, NamedTuple

if TYPE_CHECKING:
    import numpy as np

    from ravens_record_core.detectors.map_detector import MatchMapResult
    from ravens_record_core.match_session import MatchSession


class Status(NamedTuple):
    """監視状態（status イベントの state・ログ用の文・表示用の文言キー）"""

    state: str
    message: str
    msg_key: str | None = None


# 記録を進めてよくなるまでの監視状態（is_ready が False の間）
WAITING_READY = Status("waiting_ready", "記録を始められるようになるまで待っています")

# on_issue の kind（読み取りの問題。どれも、その試合を正しく記録できなかったかもしれない印）
ISSUE_LOADING_TIMEOUT = "loading_timeout"      # ロード画面が現れないまま時間切れ（その対戦は記録しない）
ISSUE_MAP_TIMEOUT = "map_timeout"              # 試合前の映像を追い切れず、マップを判定できなかった
ISSUE_SCORE_MISSING = "score_missing"          # スコアを確定できないまま試合が終わった
ISSUE_INVALID_SCORE = "invalid_score"          # 最終スコアが、ありえない組み合わせ
ISSUE_STATUS_UNREADABLE = "status_unreadable"  # ロビーで長い間「マッチングしています / 対戦を開始します」を読めない

# on_match_settled の verdict（試合の記録を閉じたときの、読み取りの問題の扱い）
VERDICT_GAME_CLOSED = "game_closed"            # 途中でゲームが閉じられた
VERDICT_ABORTED = "aborted"                    # スコアが無く、レートも変わっていない（試合が流れた）
VERDICT_MOSTLY_MINIMIZED = "mostly_minimized"  # 試合のほとんどで、ゲームが最小化されていた
VERDICT_NORMAL = "normal"                      # ふつうに終わった（読み取りの問題があれば、それは本物）

# on_custom_match の kind
CUSTOM_PARTICIPANT = "participant"             # 自分が出た対戦
CUSTOM_SPECTATED = "spectated"                 # 観戦した対戦


class Hooks:
    # ---- 記録を進めてよいか ----------------------------------------------------

    def is_ready(self) -> bool:
        """記録を進めてよいか。False の間は、対戦を見つけても記録しない（進行中の試合は捨てる）"""
        return True

    def not_ready_status(self) -> Status:
        """is_ready が False の間に知らせる監視状態"""
        return WAITING_READY

    def on_ready(self) -> None:
        """初めて記録を進めてよくなったとき（1 回だけ）"""

    def on_crash(self, error: BaseException) -> None:
        """監視が予期しない例外で止まるとき"""

    # ---- 画面の観察 ------------------------------------------------------------

    def on_lobby(self, capture: Callable[[], "np.ndarray | None"]) -> None:
        """ロビーで「マッチングしています / 対戦を開始します」を読めたとき。capture() で、いまの画面を撮れる"""

    def wants_frames(self) -> bool:
        """on_frame で、ロードの終わりから試合前までの画面を受け取るか"""
        return False

    def on_frame(self, frame: "np.ndarray", state: str | None) -> None:
        """ロードの終わりから試合前までの画面（wants_frames が True のときだけ）。state は試合前の映像の段階"""

    def on_intro_start(self, at: float) -> None:
        """試合前の紹介の映像（ALPHA）が始まった時刻（time.time() の値）"""

    # ---- ランクマッチの進行 ------------------------------------------------------

    def on_match_started(self, session: "MatchSession") -> None:
        """対戦の開始を見つけ、試合の記録を開いたとき"""

    def on_rate_before(self, rate: int | None) -> None:
        """対戦前のレートを読んだとき（読めなかった・ゲームが閉じられて分からないときは None）"""

    def on_loading_started(self, session: "MatchSession") -> None:
        """ロードの終わりを待ち始めたとき"""

    def on_loading_aborted(self, session: "MatchSession") -> None:
        """ロードの終わりを確かめられず、その対戦を記録しないとき"""

    def on_map_result(self, session: "MatchSession", result: "MatchMapResult | None") -> None:
        """マップの判定が終わったとき（判定できなかったときは None）"""

    def on_match_dropped(self, session: "MatchSession", rate_before: int | None) -> None:
        """ロードの終わりを確かめられず、試合を記録しないで閉じたとき。
        rate_before: この試合の対戦前レート（読めなければ、ひとつ前に記録した試合の対戦後レート）"""

    def on_issue(self, kind: str, **detail: Any) -> None:
        """読み取りの問題があったとき（kind は ISSUE_*。status_unreadable には started_at と detail が付く）"""

    def on_match_settled(self, session: "MatchSession", verdict: str, window_time: dict | None) -> None:
        """試合の記録を閉じたとき（verdict は VERDICT_*。window_time はゲームを監視した秒数と最小化されていた秒数）"""

    def on_match_saved(self, session: "MatchSession", path: Path) -> None:
        """ランクマッチの試合を保存したとき（path は試合ごとの JSON）"""

    # ---- カスタムマッチ ----------------------------------------------------------

    def on_custom_match(self, kind: str, payload: dict, path: Path | None) -> bool:
        """カスタムマッチの対戦を保存したとき（kind は CUSTOM_*）。False を返すと「記録しました」のイベントを出さない"""
        return True

    def on_custom_match_started(self) -> None:
        """カスタムマッチの対戦の開始の合図（MATCH_START）を見つけたとき（参加者か観戦かは、まだ分からない）"""

    def on_custom_match_ended(self, kind: str | None, payload: dict | None) -> None:
        """カスタムマッチの対戦の監視を終えたとき（必ず呼ぶ）。記録した対戦なら kind（CUSTOM_*）と payload、
        記録しなかった（中止・観戦の結果を読めなかったなど）なら None"""
