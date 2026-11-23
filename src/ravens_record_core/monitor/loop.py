"""
監視ループ（全体の流れ）

AC6 の起動を待ち、画面の見出し（SINGLE / TEAM / CUSTOM MATCH）に応じて、
ランクマッチの記録（この下の Monitor）か、カスタムマッチの記録（custom.py）へ進む。
"""

from __future__ import annotations

import os
import subprocess
import time

from ravens_record_core import __version__
from ravens_record_core.detectors.loading_sequence_detector import capture_frame_with_fallback
from ravens_record_core.detectors.mode_detector import detect_current_screen_mode
from ravens_record_core.detectors.rate_detector import capture_stable_rate
from ravens_record_core.match_session import MatchSession
from ravens_record_core.monitor.base import MonitorContext, is_process_running, log, log_block
from ravens_record_core.monitor.children import (
    LOADING_WATCHER_INTRO_GRACE_SEC,
    LoadingWatcher,
    run_score_detector,
    start_loading_watcher,
    stop_loading_watcher,
    stop_process,
)
from ravens_record_core.monitor.custom import run_custom_match_room, stop_custom_score_detector
from ravens_record_core.monitor.loading import LoadingOutcome, wait_for_loading_complete
from ravens_record_core.monitor.map_detection import wait_for_map_detection
from ravens_record_core.monitor.match_start import wait_for_match_start
from ravens_record_core.monitor.post_match import wait_for_post_match_and_close_session
from ravens_record_core.monitor.rate_reader import log_rate_failure, rate_detail
from ravens_record_core.monitor.screen import screen_watcher, stop_screen_watcher
from ravens_record_core.monitor.team import run_team_lobby
from ravens_record_core.system import aspect_probe, gamewindow

# AC6プロセス起動チェック・モード判定の監視間隔（秒）
CHECK_INTERVAL_SEC = 5.0
# スコアの監視中（試合中）の、監視ループの間隔（秒）
IN_MATCH_INTERVAL_SEC = 0.5
# 記録を進めてよくなるのを待つ間の、監視ループの間隔（秒）
NOT_READY_INTERVAL_SEC = 2.0

# 待機中ログ（生存確認）の出力間隔（秒）
HEARTBEAT_INTERVAL_SEC = 600.0


# ロビーの見出しが読めない時間がこれだけ続いたら、ランクマッチのロビーを離れた（メニュー・ガレージなど）とみなす。
# マッチング中・チャット中などに見出しが少し読めないことがあるため、長めにとる
SCREEN_MODE_LOST_SEC = 30.0

class Monitor:
    """監視ループの状態（進行中の試合・スコアの監視プロセス）と、1 周ごとの処理"""

    def __init__(self, ctx: MonitorContext) -> None:
        self.ctx = ctx
        self.target_name = ctx.config.target_name
        self.match_session: MatchSession | None = None
        self.detector_process: subprocess.Popen | None = None  # スコアの監視（ScoreDetector）
        # 画面の見張り（run で始める）。後始末（stop_everything）は別のスレッドから呼ばれることがあり、
        # そのとき ctx.screen は None になるが、ここでは止めたものを持ち続ける（止めたあとに使っても何もしない）
        self.screen = None
        self.last_heartbeat = time.monotonic()
        self.ready_notified = False  # 初めて記録を進めてよくなったことを、差し込み口に 1 回だけ知らせる
        # 最後に記録した試合の対戦後レート（ロードを確かめられなかった試合で、対戦前レートが読めなかったときの代わりに使う）
        self.last_rate_after: int | None = None
        # いまいる対戦の種類（on_screen_mode で知らせる）と、最後に見出しを読めた時刻
        self.screen_mode: str | None = None
        self.last_header_at = 0.0

    # ---- 全体 ---------------------------------------------------------------

    def run(self, stop_requested=lambda: False) -> None:
        """監視ループを動かし続ける（stop_requested() が True になるか、Ctrl+C か、予期しない例外まで）"""
        log_block([
            "=" * 50,
            f"ravens-record-core {__version__} 監視を開始します",
            f"PID: {os.getpid()}",
            f"監視対象: {self.ctx.trigger_exe}",
            f"チェック間隔: {CHECK_INTERVAL_SEC}秒",
            "=" * 50,
        ])

        # 画面の状態の見張り（別スレッド）。ロビー・マッチング中・カスタムマッチのルームにいる間だけ動かす
        self.ctx.stop_requested = stop_requested
        self.screen = screen_watcher(self.ctx)

        try:
            while not stop_requested():
                wait_sec = self.step()
                if wait_sec > 0:
                    _sleep_unless_stopped(wait_sec, stop_requested)
            self.stop_everything()
        except KeyboardInterrupt:
            log("Ctrl+Cを受信したため監視を終了します。")
            self.stop_everything()
        except Exception as e:
            log(f"[エラー] 予期しない例外が発生しました: {e!r}")
            self.ctx.hooks.on_crash(e)
            self.stop_everything()
            raise

    def step(self) -> float:
        """監視ループの 1 周。戻り値は、次の周までに待つ秒数（0 なら待たない）"""
        ctx = self.ctx
        if not ctx.is_ready():
            # 記録を進めてよくなるまでは、対戦を見つけても記録できないため、監視を進めない
            self._discard_match()
            ctx.set_not_ready_status()
            return NOT_READY_INTERVAL_SEC

        if not self.ready_notified:
            self.ready_notified = True
            ctx.hooks.on_ready()

        now = time.monotonic()
        if not is_process_running(ctx.trigger_exe):
            self._on_game_not_running(now)
            return CHECK_INTERVAL_SEC

        if ctx.current_status() in (None, "waiting_ac6") or ctx.current_status() == _not_ready_state(ctx):
            ctx.set_status("ac6_detected", "AC6を検出しました。ロビー画面を待機中です", "status.ac6Detected")

        # --- Step 1: ModeDetector でランクマッチSINGLEを確認 ---
        # 試合中（スコアの監視が動いている間）は、見出しが無いのが普通なので、文字の読み取りを控えめにする（カクつき対策）
        mode_result = detect_current_screen_mode(ctx.trigger_exe,
                                                 in_match=self.detector_process is not None and self.detector_process.poll() is None)
        current_mode = mode_result.mode

        # 試合の途中でなければ、画面の見張りを動かす（試合中は、ロード監視・マップ判定・スコアが画面を見る）
        idle = self.detector_process is None and (self.match_session is None or self.match_session.is_closed)
        self.screen.set_active(idle)
        self.screen.note_mode(current_mode)
        # 見出しが読めなくても、最後に読めた見出しがランクマッチ（SINGLE）なら、ランクマッチのロビーとして対戦の開始を待つ
        # （マッチング中・キャンセル後・チャット中などは、見出しが読めないことがある）
        context = self.screen.context_mode
        ranked_lobby = current_mode == "SINGLE" or (idle and current_mode is None and context == "SINGLE")
        custom_room = idle and (current_mode == "CUSTOM" or (current_mode is None and context == "CUSTOM"))
        team_lobby = idle and (current_mode == "TEAM" or (current_mode is None and context == "TEAM"))

        self._update_screen_mode(now, current_mode, idle)

        if ranked_lobby:
            self._on_ranked_lobby(mode_result)
        elif team_lobby and ctx.config.record_team:
            self._set_screen_mode("team")
            # チーム戦のロビー（v1.8〜）。ランクマッチ（SINGLE）の記録とは別の流れで記録する（monitor/team.py）
            run_team_lobby(ctx, self.screen)
            ctx.set_status("ac6_detected", "AC6を検出しました。ロビー画面を待機中です", "status.ac6Detected")
        elif current_mode == "TEAM":
            if self.detector_process is not None:
                log("[モード判定] TEAM戦を判定したため、ScoreDetector を停止します。")
            self.stop_score_detector()
            if not ctx.config.record_team:
                ctx.set_status("ac6_detected", "TEAM戦のため記録対象外です", "status.teamNotRecorded")
        elif custom_room and ctx.config.record_custom:
            # カスタムマッチのルーム画面。ランクマッチの記録とは別の流れで記録する（ランクマッチの試合の途中なら、何もしない）
            run_custom_match_room(ctx, self.screen)
            ctx.set_status("ac6_detected", "AC6を検出しました。ロビー画面を待機中です", "status.ac6Detected")
        elif self._on_unknown_screen():
            return 0.0  # 待たずに、すぐ対戦開始の待機に入る（次の周で、最後に読めた見出しから判断する）

        return IN_MATCH_INTERVAL_SEC if self.detector_process is not None else CHECK_INTERVAL_SEC

    # ---- 子プロセス・試合の記録の後始末 -----------------------------------------

    def stop_score_detector(self) -> None:
        """ScoreDetector プロセスが動いていれば停止する（ブロッキング）"""
        if self.detector_process is None:
            return
        if self.detector_process.poll() is None:
            log("[リセット] ScoreDetector を停止します...")
            stop_process(self.detector_process)
        self.detector_process = None

    def stop_everything(self) -> None:
        """子プロセスと画面の見張りを止める（何度呼んでもよい。別のスレッドからも呼べる）"""
        self.stop_score_detector()
        stop_custom_score_detector(self.ctx)
        stop_loading_watcher(self.ctx)
        stop_screen_watcher(self.ctx)

    def _discard_match(self) -> None:
        """スコアの監視を止め、進行中の試合があれば、スコアなしで閉じる"""
        self.stop_score_detector()
        if self.match_session and not self.match_session.is_closed:
            self.match_session.close_without_score()
        self.match_session = None

    def _close_match(self) -> None:
        """試合の結果を確定して保存する（post_match）"""
        saved_path = wait_for_post_match_and_close_session(self.ctx, self.match_session)
        if saved_path and self.match_session.rate_after is not None:
            self.last_rate_after = self.match_session.rate_after
        self.match_session = None

    def _match_in_progress(self) -> bool:
        return bool(self.match_session and not self.match_session.is_closed)

    # ---- 待機中 --------------------------------------------------------------

    def _set_screen_mode(self, mode: str | None) -> None:
        if mode != self.screen_mode:
            self.screen_mode = mode
            try:
                self.ctx.hooks.on_screen_mode(mode)
            except Exception as e:  # 知らせる先の失敗で、監視を止めない
                log(f"[モード判定] 対戦の種類を知らせられませんでした: {e!r}")

    def _update_screen_mode(self, now: float, current_mode: str | None, idle: bool) -> None:
        """いまいる対戦の種類を決める。ロビーの見出しが SINGLE / TEAM ならその種類、カスタムマッチならどちらでもない。
        見出しが読めない時間が SCREEN_MODE_LOST_SEC 続いたら（メニュー・ガレージなどに移った）、どちらでもない。
        試合の途中は変えない"""
        if current_mode is not None:
            self.last_header_at = now
        if not idle:
            return
        if current_mode == "SINGLE":
            self._set_screen_mode("single")
        elif current_mode == "TEAM":
            self._set_screen_mode("team" if self.ctx.config.record_team else None)
        elif current_mode is not None:
            self._set_screen_mode(None)
        elif now - self.last_header_at >= SCREEN_MODE_LOST_SEC:
            self._set_screen_mode(None)

    def _on_game_not_running(self, now: float) -> None:
        """AC6 プロセスが停止している場合はすべてをリセットして待機"""
        self._set_screen_mode(None)
        self.ctx.hooks.on_rate_before(None)  # 次の試合の対戦前レートは分からない
        self.screen.set_active(False)
        self.screen.reset_context()
        self._discard_match()
        self.ctx.set_status("waiting_ac6", "AC6の起動を待機中です", "status.waitingAc6")
        if now - self.last_heartbeat >= HEARTBEAT_INTERVAL_SEC:
            log(f"[待機中] '{self.target_name}' の起動を待機しています...", msg_key="event.waitingAc6", exe=self.target_name)
            # 起動しているのに見つからない場合の原因を調べるため、関連するプロセス・ウィンドウを記録する
            try:
                log(gamewindow.diagnose(self.target_name))
            except Exception as e:
                log(f"[調査用] プロセス・ウィンドウの記録に失敗しました: {e!r}")
            self.last_heartbeat = now

    def _on_unknown_screen(self) -> bool:
        """見出しが読めない画面（試合中・画面遷移前・判定不能）。

        ScoreDetector が終わっていたら、試合の結果を保存する。保存したら True（呼び出し側は、待たずに次の周へ進む）
        """
        if self.detector_process is None or self.detector_process.poll() is None:
            return False
        log(f"[情報] ScoreDetector が終了しました (コード: {self.detector_process.returncode})。")
        self.detector_process = None
        # 試合が終わったら、すぐ画面の見張りを動かす（ロビーに戻ってすぐマッチングが決まっても、
        # 「対戦を開始します」を取りこぼさないため。結果の保存の間も読み続ける）
        self.screen.set_active(True)

        if not self._match_in_progress():
            return False
        self._close_match()
        return True

    # ---- ランクマッチ ----------------------------------------------------------

    def _on_ranked_lobby(self, mode_result) -> None:
        """ランクマッチ（SINGLE）のロビー: 前の試合を閉じ、対戦の開始を待って、次の試合の記録を始める"""
        # 前回の ScoreDetector が動いたままであれば停止・クリーンアップする
        if self.detector_process is not None:
            if self.detector_process.poll() is None:
                log("[情報] SINGLEモード（ロビー画面）へ復帰したため、残っていた ScoreDetector を停止します。")
                self.stop_score_detector()
            else:
                log(f"[情報] ScoreDetector が終了しました (コード: {self.detector_process.returncode})。")
                self.detector_process = None

        if self._match_in_progress():
            self.screen.set_active(True)  # 結果の保存の間も、次の対戦の開始を見張る
            self._close_match()

        if mode_result.mode == "SINGLE":
            log(f"[モード認識] SINGLEモードを検知しました (raw='{mode_result.raw_text}')。"
                "対戦開始を待機します。")
        else:
            log("[モード認識] 見出しは読めませんが、最後に読めた見出しがランクマッチ（SINGLE）のため、対戦開始を待機します。")

        # --- Step 2: 画面の見張り（ScreenStateWatcher）の結果で、対戦開始を待機 ---
        if wait_for_match_start(self.ctx, self.screen):
            self._record_ranked_match()

    def _record_ranked_match(self) -> None:
        """対戦の開始を検出したあと: 対戦前レート → ロード → マップ判定 → スコアの監視の開始"""
        ctx = self.ctx
        # ロード画面の監視プロセスを、すぐに起動する（下のレート取得の間も、画面を見張るため）
        watcher = start_loading_watcher(ctx)
        self.match_session = MatchSession.open()
        ctx.hooks.on_match_started(self.match_session)

        # --- Step 2.5: レート取得（対戦前） ---
        self._read_rate_before()
        ctx.hooks.on_rate_before(self.match_session.rate_before)

        # --- Step 3: LoadingSequenceDetector でシーケンス完了を確認（ロード画面の監視プロセスと同時に） ---
        outcome = self._wait_for_loading(watcher)
        if not outcome.done:
            # ロードを確かめられなかった（記録しない）。対戦前レートが読めなかったときは、最後に記録した試合の対戦後レートで代える
            session = self.match_session
            ctx.hooks.on_match_dropped(session, session.rate_before if session.rate_before is not None else self.last_rate_after)
            session.close_without_score()
            self.match_session = None
            return

        # --- Step 4: MapDetector で試合前シーケンスを検出 ---
        ctx.set_status("map_detection", "マップを判定中です", "status.mapDetection")
        self._detect_map(skip=outcome is LoadingOutcome.MAP_SKIPPED)

        # --- Step 5: ScoreDetector を起動 ---
        self._start_score_detector()

    def _read_rate_before(self) -> None:
        """対戦前のレートを読んで、試合の記録に入れる"""
        session = self.match_session
        trigger = self.ctx.trigger_exe

        def capture():
            return capture_frame_with_fallback(trigger)

        log("[レート取得] 対戦前レートの取得を開始します...")
        # 画面の大きさを記録する（16:9 以外の画面の調査。合わない画面なら、場面の控えも保存）
        probe_frame = capture()
        session.capture_size = aspect_probe.size_text(probe_frame)
        aspect_probe.observe(probe_frame, "match_start")
        rate_trace: dict = {}
        rate_res = capture_stable_rate(capture, trace=rate_trace)
        if rate_res:
            if rate_res.is_s_rank:
                session.rate_before = rate_res.rate
                session.rate_position_before = rate_res.s_rank_position
            else:
                log("[レート取得] Sランクではないためレート取得をスキップします。")
        else:
            log("[レート取得] レートの取得にタイムアウト・失敗しました。")
        if session.rate_before is None:
            session.rate_before_note = rate_trace.get("reason")
            session.rate_before_detail = rate_detail(rate_trace)
            log_rate_failure("対戦前レート", rate_trace)

    def _wait_for_loading(self, watcher: LoadingWatcher | None) -> LoadingOutcome:
        """ロードの終わりを待つ"""
        session = self.match_session
        self.ctx.hooks.on_loading_started(session)
        outcome = LoadingOutcome.ABORTED
        try:
            outcome = wait_for_loading_complete(self.ctx, watcher)
        finally:
            # 完了したときは、ALPHA の紹介の映像の始まりを知らせるまで、少しだけ動かしておく
            stop_loading_watcher(self.ctx, grace_sec=LOADING_WATCHER_INTRO_GRACE_SEC if outcome.done else 0.0)
        if not outcome.done:
            self.ctx.hooks.on_loading_aborted(session)
        return outcome

    def _detect_map(self, skip: bool) -> None:
        """マップを判定して、試合の記録に入れる。skip: ロードの終わりを確かめられなかった（マップは未判定のまま）"""
        session_result = None if skip else wait_for_map_detection(self.ctx)
        result = session_result._result if session_result else None
        if result is not None:
            self.match_session.map_result = result
        self.ctx.hooks.on_map_result(self.match_session, result)

    def _start_score_detector(self) -> None:
        session = self.match_session
        session.mark_match_started()
        logs_dir = self.ctx.config.logs_dir
        logs_dir.mkdir(parents=True, exist_ok=True)
        temp_score_path = str(logs_dir / f"temp_score_{session.session_id}.json")
        session.score_log_path = temp_score_path
        self.detector_process = run_score_detector(self.ctx.config, temp_score_path)
        self.ctx.set_status("in_match", "対戦中です（スコアを監視しています）", "status.inMatch")


def _not_ready_state(ctx: MonitorContext) -> str | None:
    status = ctx.hooks.not_ready_status()
    return status.state if status is not None else None


def _sleep_unless_stopped(seconds: float, stop_requested, step: float = 0.25) -> None:
    """seconds 秒待つ（途中で stop_requested() が True になったら、すぐ戻る）"""
    end = time.monotonic() + seconds
    while not stop_requested():
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(step, left))
