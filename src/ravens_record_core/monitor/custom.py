"""カスタムマッチ（custom_match.py）: ルームで設定を読み、対戦（参加・観戦）を記録する"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from ravens_record_core import logger, matchlog
from ravens_record_core.detectors import custom_match
from ravens_record_core.detectors.loading_sequence_detector import capture_frame_with_fallback
from ravens_record_core.detectors.mode_detector import detect_current_screen_mode
from ravens_record_core.detectors.screen_state_watcher import ScreenStateWatcher
from ravens_record_core.hooks import CUSTOM_PARTICIPANT, CUSTOM_SPECTATED
from ravens_record_core.monitor.base import WATCH_POLL_INTERVAL_SEC, MonitorContext, is_process_running, log
from ravens_record_core.monitor.children import run_score_detector, stop_process
from ravens_record_core.monitor.screen import screen_watcher

# ルームを離れたかは、ほかの見出し（SINGLE / TEAM）が読めたかで決める（以前の「30 秒見えなければ」の時間切れはなくした）
# ルーム画面で設定を読み直す間隔（秒）
CUSTOM_SETTINGS_READ_INTERVAL_SEC = 3.0
# 対戦の開始の合図のあと、ルーム画面の見出しが見えても「ルームに戻った」と判断しない秒数（合図はルーム画面の上に出るため）
CUSTOM_START_GRACE_SEC = 20.0
# 対戦の開始の合図のあと、左下の AP の表示（参加者の画面）か、観戦のカメラのマークを待つ最長の秒数
CUSTOM_HUD_WAIT_SEC = 300.0
# 観戦と判断するのに、カメラのマークが続けて見えている必要のある秒数と回数（一瞬の誤検出で観戦にしない）
CUSTOM_CAMERA_MIN_SEC = 2.0
CUSTOM_CAMERA_MIN_FRAMES = 3
# 対戦中（スコアの監視中）の最長の秒数。超えたら、結果不明として記録を閉じる
CUSTOM_MATCH_MAX_SEC = 2400.0


def stop_custom_score_detector(ctx: MonitorContext) -> None:
    """カスタムマッチ用の ScoreDetector が動いていれば停止する。"""
    proc, ctx.custom_detector = ctx.custom_detector, None
    stop_process(proc)


def _wait_custom_room_return(ctx: MonitorContext, started: float, timeout_sec: float) -> bool:
    """ルーム画面（CUSTOM MATCH）に戻るまで待つ。戻ったら True、AC6 の終了・タイムアウト・ランクマッチのロビーなら False。"""
    while True:
        if not is_process_running(ctx.trigger_exe):
            return False
        if time.monotonic() - started > timeout_sec:
            return False
        mode = detect_current_screen_mode(ctx.trigger_exe).mode
        if mode == "CUSTOM" and time.monotonic() - started >= CUSTOM_START_GRACE_SEC:
            return True
        if mode in ("SINGLE", "TEAM"):
            return False
        time.sleep(2.0)


def _wait_custom_role(ctx: MonitorContext, started: float) -> str | None:
    """対戦の画面が、参加者（"participant"）か観戦（"spectator"）かを待って決める。

    左下の AP の表示が見えたら参加者。カメラのマークが続けて見えたら観戦。どちらも見えないまま
    ルームに戻った・AC6 が終了した・ランクマッチの画面に移った・時間切れなら None（記録しない）。
    """
    camera_since: float | None = None
    camera_frames = 0
    last_mode_check = 0.0
    while time.monotonic() - started < CUSTOM_HUD_WAIT_SEC:
        if not is_process_running(ctx.trigger_exe):
            log("[カスタム] AC6プロセスが終了したため記録を中断します。")
            return None
        frame = capture_frame_with_fallback(ctx.trigger_exe)
        now = time.monotonic()
        if frame is not None:
            # カメラのマークは小さな範囲の照合だけで軽いので、先に見る（AP の表示は OCR）
            if custom_match.has_spectator_camera(frame):
                camera_since = camera_since if camera_since is not None else now
                camera_frames += 1
                if now - camera_since >= CUSTOM_CAMERA_MIN_SEC and camera_frames >= CUSTOM_CAMERA_MIN_FRAMES:
                    return "spectator"
            else:
                camera_since, camera_frames = None, 0
                if custom_match.has_ap_hud(frame):
                    return "participant"
        if now - last_mode_check >= 2.0:
            last_mode_check = now
            mode = detect_current_screen_mode(ctx.trigger_exe).mode
            if mode == "CUSTOM" and now - started >= CUSTOM_START_GRACE_SEC:
                log("[カスタム] 対戦の画面にならないままルームに戻りました（中止など）。記録しません。")
                return None
            if mode in ("SINGLE", "TEAM"):
                log("[カスタム] ランクマッチの画面に移ったため、記録を中断します。")
                return None
        time.sleep(1.0 if camera_since is None else 0.5)

    log("[カスタム] 参加者の画面（AP の表示）も、観戦の画面（カメラのマーク）も確認できませんでした。記録しません。")
    _wait_custom_room_return(ctx, started, CUSTOM_MATCH_MAX_SEC)
    return None


def _read_custom_score(score_path: Path) -> tuple[int | None, int | None]:
    """ScoreDetector が書いた結果（左, 右）を読み、ファイルを消す。無ければ (None, None)。"""
    left = right = None
    if score_path.exists():
        try:
            with open(score_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            left, right = data.get("my_score"), data.get("enemy_score")
        except Exception as e:
            log(f"[カスタム] スコアの読み込みに失敗しました: {e}")
        try:
            score_path.unlink()
        except OSError:
            pass
    return left, right


def run_custom_spectated_match(ctx: MonitorContext, settings, started_at: datetime, started: float) -> None:
    """観戦の 1 試合を記録する。観戦の画面（カメラのマーク）を確認した直後に呼ぶ。

    観戦の画面は常に左が ALPHA・右が BETA なので、ScoreDetector の左右のスコアを、そのまま ALPHA・BETA のラウンド数にする。
    試合の終わりのスコア（2-0・2-1 など）まで読めた試合だけを記録する（切断などで終わりまで読めなければ、記録しない）。
    プレイヤー名などは読まない（読むのはスコアのマスだけ）。
    """
    logs_dir = ctx.config.logs_dir
    logs_dir.mkdir(parents=True, exist_ok=True)
    score_path = logs_dir / f"temp_custom_spectated_score_{started_at.strftime('%Y%m%d_%H%M%S')}.json"
    ctx.custom_detector = run_score_detector(ctx.config, str(score_path))
    ctx.set_status("in_match", "カスタムマッチを観戦中です（スコアを監視しています）", "status.customSpectating")
    log("[カスタム] 観戦の画面を確認しました。スコアの監視を始めます。")

    returned = _wait_custom_room_return(ctx, started, CUSTOM_MATCH_MAX_SEC)
    ended_at = datetime.now().astimezone()
    ctx.set_status("post_match", "観戦した対戦が終了しました。結果を保存しています", "status.customPostMatch")
    if not returned:
        log("[カスタム] ルーム画面への復帰を確認できないまま、観戦の監視を終了します。")
    stop_custom_score_detector(ctx)

    alpha_wins, beta_wins = _read_custom_score(score_path)
    if not custom_match.is_final_score(alpha_wins, beta_wins):
        logger.emit("custom_spectated_skipped",
                    f"[カスタム] 観戦した対戦の結果を最後まで読み取れなかったため、記録しませんでした（{alpha_wins}-{beta_wins}）",
                    msg_key="event.customSpectatedSkipped")
        return

    payload = custom_match.build_spectated_payload(settings, started_at, ended_at, alpha_wins, beta_wins, _app_version(ctx))
    path = matchlog.write_custom_match(ctx.config.logs_dir, CUSTOM_SPECTATED, payload, ended_at)
    if ctx.hooks.on_custom_match(CUSTOM_SPECTATED, payload, path):
        logger.emit("custom_spectated_saved",
                    f"観戦したカスタムマッチを記録しました: ALPHA {alpha_wins}-{beta_wins} BETA / {settings.summary()}",
                    alpha_wins=alpha_wins, beta_wins=beta_wins, msg_key="event.customSpectatedSaved")


def run_custom_match(ctx: MonitorContext, settings) -> None:
    """カスタムマッチ（シングル）の対戦 1 回を記録する。対戦の開始の合図（MATCH_START）を検出した直後に呼ぶ。

    参加者の画面（左下に AP の表示）になったら ScoreDetector を起動し、ルーム画面に戻ったら結果を確定して記録する。
    観戦の画面（カメラのマーク）になったら、観戦の記録として別に扱う（run_custom_spectated_match）。
    どちらにもならないままルームに戻った（中止など）なら、記録しない。
    """
    played_at = datetime.now().astimezone()
    started = time.monotonic()
    ctx.set_status("loading", "カスタムマッチの対戦を検出しました。開始を待っています", "status.customLoading")
    logger.emit("custom_match_detected", f"[カスタム] 対戦の開始を検出しました（{settings.summary()}）",
                msg_key="event.customDetected", map=settings.map_match)

    # --- 参加者の画面（左下の AP の表示）か、観戦の画面（カメラのマーク）かを待つ ---
    role = _wait_custom_role(ctx, started)
    if role is None:
        return
    if role == "spectator":
        run_custom_spectated_match(ctx, settings, played_at, started)
        return

    # --- 対戦中: ScoreDetector でラウンドの勝敗を監視する（ランクマッチと同じ判定） ---
    logs_dir = ctx.config.logs_dir
    logs_dir.mkdir(parents=True, exist_ok=True)
    score_path = logs_dir / f"temp_custom_score_{played_at.strftime('%Y%m%d_%H%M%S')}.json"
    ctx.custom_detector = run_score_detector(ctx.config, str(score_path))
    ctx.set_status("in_match", "カスタムマッチの対戦中です（スコアを監視しています）", "status.customInMatch")
    log("[カスタム] 参加者の画面を確認しました。スコアの監視を始めます。")

    returned = _wait_custom_room_return(ctx, started, CUSTOM_MATCH_MAX_SEC)
    ctx.set_status("post_match", "カスタムマッチの対戦が終了しました。結果を保存しています", "status.customPostMatch")
    if not returned:
        log("[カスタム] ルーム画面への復帰を確認できないまま、監視を終了します。")
    stop_custom_score_detector(ctx)

    own_wins, opp_wins = _read_custom_score(score_path)
    if own_wins is None or opp_wins is None:
        own_wins = opp_wins = None
        logger.emit("score_missing", "[カスタム] [警告] スコアを確定できないまま対戦が終了しました（結果は不明として記録します）", level="warn",
                    msg_key="event.customScoreMissing")

    payload = custom_match.build_payload(settings, played_at, own_wins, opp_wins, _app_version(ctx))
    path = matchlog.write_custom_match(ctx.config.logs_dir, CUSTOM_PARTICIPANT, payload, datetime.now().astimezone())
    if ctx.hooks.on_custom_match(CUSTOM_PARTICIPANT, payload, path):
        score_text = f"{own_wins}-{opp_wins}" if own_wins is not None else "結果不明"
        logger.emit("custom_match_saved", f"カスタムマッチを記録しました: {score_text} / {settings.summary()}",
                    own_wins=own_wins, opp_wins=opp_wins,
                    msg_key="event.customSaved" if own_wins is not None else "event.customSavedUnknown")


def run_custom_match_room(ctx: MonitorContext, watcher: ScreenStateWatcher | None = None) -> None:
    """カスタムマッチのルーム画面にいる間の処理。設定を読み続け、対戦の開始の合図で、その対戦を記録する。

    ランクマッチの記録の流れとは独立している（ルームを離れたら戻る）。
    画面の見出しと対戦の開始の合図は、見張り（ScreenStateWatcher）が並行して読む。
    ルームを離れたかは、ほかの見出し（SINGLE / TEAM）が読めたかで決める（時間切れは無い）。
    """
    watcher = watcher or screen_watcher(ctx)
    watcher.set_active(True)
    ctx.set_status("matchmaking", "カスタムマッチのルーム。対戦の開始を待機中です", "status.customRoom")
    log("[カスタム] カスタムマッチのルーム画面を検出しました。設定を読み取り、対戦の開始を待機します。")
    last_settings = None
    last_summary = ""
    last_read = 0.0

    while True:
        if ctx.stop_requested() or not is_process_running(ctx.trigger_exe) or not ctx.is_ready():
            return
        now = time.monotonic()
        if watcher.context_mode in ("SINGLE", "TEAM"):
            log("[カスタム] カスタムマッチのルームを離れました。")
            return
        mode = watcher.last_mode

        # 設定は数秒ごとに読み直す（対戦の前に変更されうるため。対戦の開始の直前に読んだ値を使う）
        if mode == "CUSTOM" and now - last_read >= CUSTOM_SETTINGS_READ_INTERVAL_SEC:
            last_read = now
            frame = capture_frame_with_fallback(ctx.trigger_exe)
            if frame is not None:
                settings = custom_match.read_room_settings(frame)
                if settings.format_match or settings.map_raw:
                    last_settings = settings
                    if settings.summary() != last_summary:
                        last_summary = settings.summary()
                        log(f"[カスタム] ルームの設定: {last_summary}")

        event = watcher.take_match_start()
        if event is not None:
            frame = capture_frame_with_fallback(ctx.trigger_exe)
            fresh = custom_match.read_room_settings(frame) if frame is not None else None
            settings = fresh if (fresh is not None and fresh.format_match) else last_settings
            watcher.set_active(False)  # 対戦の間は、下の処理が画面を見る
            try:
                if settings is None or not settings.is_single():
                    reason = "チーム形式の" if (settings is not None and settings.is_team()) else "対戦形式を読み取れない"
                    log(f"[カスタム] 対戦の開始を検出しましたが、{reason}ため記録しません。")
                    _wait_custom_room_return(ctx, time.monotonic(), CUSTOM_MATCH_MAX_SEC)
                else:
                    run_custom_match(ctx, settings)
            finally:
                watcher.clear_match_start()
                watcher.set_active(True)
            ctx.set_status("matchmaking", "カスタムマッチのルーム。対戦の開始を待機中です", "status.customRoom")
            last_read = 0.0
            continue

        time.sleep(WATCH_POLL_INTERVAL_SEC)


def _app_version(ctx: MonitorContext) -> str:
    """payload の app_version（使う側のアプリのバージョン。無ければライブラリのバージョン）"""
    from ravens_record_core import __version__
    return ctx.config.app_version or __version__
