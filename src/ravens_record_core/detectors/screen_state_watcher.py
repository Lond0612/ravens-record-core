"""
画面の状態の見張り（本体の中の別スレッド）

AC6 のロビー・マッチング中・カスタムマッチのルームにいる間、左上のモードの見出し（ModeDetector）と、
「マッチングしています / 対戦を開始します」（MatchStatusDetector）を約 1 秒ごとに読み続ける。
本体（monitor パッケージ）は、ここで読んだ結果を使って、対戦の開始を待つ。

■ 以前の作りとの違い
  以前は、本体の待機のループが自分で読んでいて、「RANK MATCH: SINGLE」が 45 秒読めないと待機をやめていた。
  マッチング中・キャンセル後・チャットやメニューを開いている間は見出しが読めないことがあり、
  待機をやめたあとは「対戦を開始します」を誰も見ていないため、そのまま試合に入ると記録されなかった（2026-10）。
  ここでは時間切れを設けない。「対戦を開始します」が出たときに、その時点で**最後に読めた見出し**（context_mode）が
  SINGLE ならランクマッチとみなす。TEAM やカスタムマッチ（CUSTOM）に移れば、その見出しが読めた時点で切り替わる。

■ 止めている間
  試合中（ロード監視・マップ判定・スコア）は set_active(False) で止める（本体が自分で画面を見ているため。CPU の節約）。
"""

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from ravens_record_core import logger

# 読む間隔（秒）
POLL_INTERVAL_SEC = 1.0
# 「対戦を開始します」は数秒表示される。最後に読んでからこの秒数を超えて、また読めたら、別の対戦として知らせる
MATCH_START_REARM_SEC = 3.0
# 知らせてから、この秒数を過ぎても本体が受け取らなければ、古いものとして捨てる（ロード画面が始まってしまうため）
MATCH_START_MAX_AGE_SEC = 20.0
# 確定した見出し（最後に読めた見出しとして覚えるもの）
DEFINITIVE_MODES = ("SINGLE", "TEAM", "CUSTOM")


@dataclass
class MatchStartEvent:
    at: float                   # 読めた時刻（time.monotonic）
    raw_text: str               # 読み取った文字（ログ用）
    context_mode: Optional[str]  # そのときの、最後に読めた見出し（SINGLE / TEAM / CUSTOM / None）


class ScreenStateWatcher:
    def __init__(self, mode_fn: Callable, status_fn: Callable,
                 clock: Callable[[], float] = time.monotonic,
                 interval: float = POLL_INTERVAL_SEC):
        self._mode_fn = mode_fn
        self._status_fn = status_fn
        self._clock = clock
        self._interval = interval
        self._lock = threading.Lock()
        self._active = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_error_log = 0.0

        self.context_mode: Optional[str] = None   # 最後に読めた見出し
        self.context_at: Optional[float] = None
        self.last_mode: Optional[str] = None      # いちばん新しい読み取り結果（読めなければ None）
        self.last_status: Optional[str] = None
        self.last_status_raw = ""
        self._pending: Optional[MatchStartEvent] = None
        self._last_start_seen: Optional[float] = None  # 「対戦を開始します」を最後に読んだ時刻
        self.status_read_count = 0  # 「マッチングしています / 対戦を開始します」を読めた回数（AC6 を閉じても数え直さない）

    # --- スレッド ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="rrc-screen-state")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._active.set()  # 待っているスレッドを起こす

    def set_active(self, active: bool) -> None:
        if active:
            self._active.set()
        else:
            self._active.clear()

    @property
    def active(self) -> bool:
        return self._active.is_set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._active.wait()
            if self._stop.is_set():
                return
            started = self._clock()
            try:
                self.tick()
            except Exception as e:
                now = self._clock()
                if now - self._last_error_log >= 60.0:
                    logger.log(f"[画面の見張り] [警告] 読み取りに失敗しました: {e!r}")
                    self._last_error_log = now
            time.sleep(max(0.05, self._interval - (self._clock() - started)))

    # --- 読み取り 1 回分（テストでは、スレッドを使わずに直接呼ぶ） --------

    def tick(self) -> None:
        mode = self._mode_fn()
        self.note_mode(getattr(mode, "mode", None))
        status = self._status_fn()
        self._note_status(getattr(status, "status", None), getattr(status, "raw_text", "") or "")

    def note_mode(self, mode: Optional[str]) -> None:
        """モードの読み取り結果を反映する（本体が自分で読んだ結果も、ここに渡す）"""
        now = self._clock()
        with self._lock:
            self.last_mode = mode
            if mode in DEFINITIVE_MODES:
                self.context_mode, self.context_at = mode, now

    def _note_status(self, status: Optional[str], raw_text: str) -> None:
        """「対戦を開始します」は、最後に読んでから MATCH_START_REARM_SEC 秒以上たっていれば、新しい対戦として知らせる。

        時刻で決めるため、止めていた間（試合中）も時間が進み、次の対戦の表示を取りこぼさない
        （以前は「消えたのを 3 秒見届ける」まで受け付けず、表示を読んだ直後に止めると、次の対戦の表示を知らせなかった。2026-10-12）。
        """
        now = self._clock()
        with self._lock:
            self.last_status, self.last_status_raw = status, raw_text
            if status is not None:
                self.status_read_count += 1
            if status == "MATCH_START":
                if self._last_start_seen is None or now - self._last_start_seen > MATCH_START_REARM_SEC:
                    self._pending = MatchStartEvent(now, raw_text, self.context_mode)
                self._last_start_seen = now

    # --- 本体から使う ------------------------------------------------------

    def take_match_start(self) -> Optional[MatchStartEvent]:
        """「対戦を開始します」の知らせを 1 つ受け取る（1 回の表示につき 1 回）。古すぎるものは捨てる"""
        with self._lock:
            event, self._pending = self._pending, None
        if event is not None and self._clock() - event.at > MATCH_START_MAX_AGE_SEC:
            return None
        return event

    def clear_match_start(self) -> None:
        with self._lock:
            self._pending = None

    def reset_context(self) -> None:
        """AC6 が終わったとき等。最後に読めた見出しを忘れる"""
        with self._lock:
            self.context_mode = self.context_at = None
            self.last_mode = self.last_status = None
            self._pending = None
            self._last_start_seen = None
