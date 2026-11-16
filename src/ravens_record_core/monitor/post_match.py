"""試合が終わったあと: ロビーへの復帰を待ち、スコアと対戦後のレートを確定して、試合を保存する"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from ravens_record_core import logger
from ravens_record_core.detectors.loading_sequence_detector import capture_frame_with_fallback
from ravens_record_core.detectors.match_end import is_valid_score
from ravens_record_core.detectors.mode_detector import detect_current_screen_mode
from ravens_record_core.hooks import (
    ISSUE_INVALID_SCORE,
    ISSUE_SCORE_MISSING,
    VERDICT_ABORTED,
    VERDICT_GAME_CLOSED,
    VERDICT_MOSTLY_MINIMIZED,
    VERDICT_NORMAL,
)
from ravens_record_core.match_session import MatchSession
from ravens_record_core.monitor.base import MonitorContext, is_process_running, log
from ravens_record_core.monitor.rate_reader import (
    RATE_AFTER_RETRY_DELAY_SEC,
    RETRY_REASONS,
    log_rate_failure,
    rate_detail,
    read_rate_after,
)
from ravens_record_core.system import aspect_probe


def match_aborted(session) -> bool:
    """スコアが無く、対戦の前後でレートが変わっていない（試合が流れた）か"""
    no_score = session.score_my is None or session.score_enemy is None
    return no_score and session.rate_before is not None and session.rate_before == session.rate_after


MISSED_STRIPS_KEEP = 5  # スコアを読めなかった試合の、直近の帯の画像を残す試合の数


def keep_missed_score_strips(session_id: str) -> None:
    """スコアを読めなかった試合の、最後の数枚の上部の帯（ScoreDetector が残す）を、試合ごとのフォルダに移す（調査用）。"""
    from ravens_record_core.detectors.score_detector import recent_dir, score_debug_dir
    recent, debug = str(recent_dir()), str(score_debug_dir())
    try:
        names = sorted(f for f in os.listdir(recent) if f.endswith(".png")) if os.path.isdir(recent) else []
        if not names:
            return
        target = os.path.join(debug, f"missed_{session_id}")
        os.makedirs(target, exist_ok=True)
        for name in names:
            shutil.move(os.path.join(recent, name), os.path.join(target, name))
        log(f"[調査用] スコアを読めなかった試合の、最後の画面（スコアの帯）を {len(names)} 枚残しました: {target}")
        missed = sorted(d for d in os.listdir(debug) if d.startswith("missed_"))
        for old in missed[:-MISSED_STRIPS_KEEP]:
            shutil.rmtree(os.path.join(debug, old), ignore_errors=True)
    except Exception as e:
        log(f"[警告] スコアの帯の画像を残せませんでした: {e}")


LOBBY_RETURN_TIMEOUT_SEC = 60.0  # 試合のあと、SINGLE ロビーへの復帰を待つ最長の秒数


def wait_for_post_match_and_close_session(ctx: MonitorContext, match_session: MatchSession):
    """試合終了後、SINGLEロビーへの復帰を高速で監視し、スコアとレートを確定してセッションを終了する。

    Returns: 保存した記録のファイル（保存しなかったときは None）
    """
    ctx.set_status("post_match", "試合終了。結果を保存しています", "status.postMatch")
    log("[待機] 試合終了（または中断）を検知しました。リザルト画面通過後のSINGLEロビー復帰を0.2秒間隔で監視します...")
    game_closed = _wait_for_lobby_return(ctx)  # AC6 を閉じた（利用者の操作。読み取りの問題にしない）

    match_session.score_basis = _load_score(match_session)
    # 試合中に、ゲームが最小化されていた時間（ScoreDetector が数える）。大半が最小化なら、読めなかったのはそのため
    window_time = _read_window_time(match_session.score_log_path)
    mostly_minimized = _is_mostly_minimized(window_time)
    _report_score_problem(ctx, match_session, match_session.score_basis, mostly_minimized, game_closed)

    _read_rate_after(ctx, match_session)
    _log_rate_change(match_session)

    match_session.app_version = ctx.config.app_version
    saved_path = match_session.close()
    _settle(ctx, match_session, window_time, mostly_minimized, game_closed)
    _remove_window_time(match_session.score_log_path)
    if saved_path:
        _publish_saved_match(ctx, match_session, saved_path)
    return saved_path


def _wait_for_lobby_return(ctx: MonitorContext) -> bool:
    """SINGLE ロビーに戻る（または時間切れ）まで待つ。待つ間に AC6 が閉じられたら True"""
    start_time = time.monotonic()
    while True:
        if not is_process_running(ctx.trigger_exe):
            log("[待機] AC6プロセスが終了したため監視を中断します。")
            return True

        if time.monotonic() - start_time > LOBBY_RETURN_TIMEOUT_SEC:
            log("[待機] タイムアウト: 60秒以内にSINGLEロビーへの復帰が検出されませんでした。")
            # 読み取りの問題にしない（リザルトのあとにガレージなどへ行っただけのことが多い）
            return False

        mode_result = detect_current_screen_mode(ctx.trigger_exe)
        if mode_result.mode == "SINGLE":
            log(f"[モード認識] SINGLEロビーへの復帰を検知しました (raw='{mode_result.raw_text}')。")
            return False

        time.sleep(0.2)


def _load_score(match_session: MatchSession) -> str | None:
    """ScoreDetector が書いたスコアを、試合の記録に入れる。

    Returns: スコアの根拠（result_screen ならリザルト画面で読めたもの。ファイルが無い・読めないときは None）
    """
    if not (match_session.score_log_path and os.path.exists(match_session.score_log_path)):
        return None
    try:
        with open(match_session.score_log_path, "r", encoding="utf-8") as f:
            score_data = json.load(f)
            match_session.score_my = score_data.get("my_score")
            match_session.score_enemy = score_data.get("enemy_score")
            return score_data.get("basis")
    except Exception as e:
        log(f"[エラー] スコアログの読み込みに失敗しました: {e}")
        return None


def _report_score_problem(ctx: MonitorContext, match_session: MatchSession, score_basis: str | None,
                          mostly_minimized: bool, game_closed: bool) -> None:
    """スコアが無い・不自然なときに、知らせと読み取りの問題の印を残す"""
    if match_session.score_my is None or match_session.score_enemy is None:
        if mostly_minimized:
            logger.emit("score_missing", "[警告] ゲームが最小化されていたため、スコアを読めませんでした"
                        "（フルスクリーンでは、別の画面に切り替えると最小化されます。ボーダーレスウィンドウなら読み取れます）",
                        level="warn", msg_key="event.scoreMissingMinimized")
        else:
            logger.emit("score_missing", "[警告] スコアを確定できないまま試合が終了しました（記録は不明として保存されます）", level="warn",
                        msg_key="event.scoreMissing")
        if not game_closed:
            ctx.hooks.on_issue(ISSUE_SCORE_MISSING)
            keep_missed_score_strips(match_session.session_id)
    elif score_basis == "result_screen" and not is_valid_score(match_session.score_my, match_session.score_enemy):
        # リザルト画面のとおり（2 点に届く前に、切断などで終わった試合）。読み取りの問題ではない
        log(f"[情報] 2 点に届く前に終わった試合です（リザルト画面: {match_session.score_my}-{match_session.score_enemy}）")
    elif not is_valid_score(match_session.score_my, match_session.score_enemy) and not game_closed:
        log(f"[警告] 最終スコアが不自然です: {match_session.score_my}-{match_session.score_enemy}")
        ctx.hooks.on_issue(ISSUE_INVALID_SCORE)


def _read_rate_after(ctx: MonitorContext, match_session: MatchSession) -> None:
    """対戦後のレートを読んで、試合の記録に入れる。

    対戦前を読めなかったときも読む（比べる相手が無いので、続けて同じ値が読めたものだけ。
    対戦前に S ランク以外とみなしたときは、PLACE 欄も読めたときだけ使う）
    """
    if match_session.rate_before is not None:
        log("[レート取得] 対戦後レートの取得を開始します...")
    else:
        log("[レート取得] 対戦前レートは読めていませんが、対戦後レートの取得を試みます...")

    def capture():
        return capture_frame_with_fallback(ctx.trigger_exe)

    aspect_probe.observe(capture(), "after_match")
    require_s_rank = match_session.rate_before_note == "not_s_rank"

    def attempt(trace: dict):
        # 0.2秒間隔で最大15回（約3秒）試行
        return read_rate_after(match_session.rate_before, capture, trace=trace, require_s_rank=require_s_rank)

    trace: dict = {}
    rate_res = attempt(trace)
    first_reason = None
    if not rate_res and trace.get("reason") in RETRY_REASONS and not ctx.stop_requested():
        # 1 枚も読めなかった: レートの欄がまだ出ていない・撮影の準備ができていないことがあるので、少し待ってもう一度
        first_reason = trace.get("reason")
        log(f"[レート取得] 対戦後レートを読めなかったため（理由: {first_reason}）、もう一度読みます...")
        time.sleep(RATE_AFTER_RETRY_DELAY_SEC)
        trace = {}
        rate_res = attempt(trace)
    if rate_res:
        match_session.rate_after = rate_res.rate
        match_session.rate_position_after = rate_res.s_rank_position
        if first_reason:
            log("[レート取得] もう一度読んだところ、対戦後レートを読めました。")
            match_session.rate_after_detail = {"reason": "ok", "first_reason": first_reason, "retried": True}
    else:
        log("[レート取得] レートの取得にタイムアウト・失敗しました。")
        match_session.rate_after_note = trace.get("reason")
        match_session.rate_after_detail = rate_detail(trace, **({"first_reason": first_reason, "retried": True} if first_reason else {}))
        log_rate_failure("対戦後レート", trace)


def _log_rate_change(match_session: MatchSession) -> None:
    """レート・順位変動ログ"""
    if match_session.rate_before is None and match_session.rate_after is None:
        return
    before_str = f"順位: {match_session.rate_position_before} / レート: {match_session.rate_before}" if match_session.rate_before is not None else "取得なし"
    after_str = f"順位: {match_session.rate_position_after} / レート: {match_session.rate_after}" if match_session.rate_after is not None else "取得なし"
    diff = match_session.rate_after - match_session.rate_before if (match_session.rate_before is not None and match_session.rate_after is not None) else 0
    log(f"[レート情報] 試合後レート・順位\n  対戦前: {before_str}\n  対戦後: {after_str}\n  変動: {diff:+}")


def settle_verdict(match_session: MatchSession, mostly_minimized: bool, game_closed: bool) -> str:
    """この試合で見つけた読み取りの問題を、本物として扱うか（VERDICT_*）"""
    if game_closed:
        return VERDICT_GAME_CLOSED
    if match_aborted(match_session):
        return VERDICT_ABORTED
    if mostly_minimized:
        return VERDICT_MOSTLY_MINIMIZED
    return VERDICT_NORMAL


def _settle(ctx: MonitorContext, match_session: MatchSession, window_time: dict | None, mostly_minimized: bool, game_closed: bool) -> None:
    verdict = settle_verdict(match_session, mostly_minimized, game_closed)
    if verdict == VERDICT_ABORTED:
        # スコアが無く、レートも変わっていない: 試合が流れた（ロード中・試合の途中の切断など）。読み取りの問題ではない
        log("[情報] スコアが無く、対戦の前後でレートが変わっていないため、試合が流れたとみなします。")
    elif verdict == VERDICT_MOSTLY_MINIMIZED:
        # ゲームの画面が映っていなかった。理由は上のイベントで知らせている
        log("[情報] 試合のほとんどでゲームが最小化されていました。")
    ctx.hooks.on_match_settled(match_session, verdict, window_time)


def _publish_saved_match(ctx: MonitorContext, match_session: MatchSession, saved_path) -> None:
    """保存した試合を、イベントと差し込み口（on_match_saved）へ渡す"""
    logger.emit("match_saved", f"試合を記録しました: {match_session.summary()}",
                result=match_session.result, path=str(saved_path), msg_key="event.matchSaved")
    ctx.hooks.on_match_saved(match_session, Path(saved_path))


MOSTLY_MINIMIZED_RATIO = 0.8  # スコアの監視のうち、この割合以上が最小化なら「最小化のため読めなかった」とみなす


def _read_window_time(score_log_path: str | None) -> dict | None:
    """ScoreDetector が書いた、監視した時間と最小化されていた時間（無ければ None）"""
    from ravens_record_core.detectors.score_detector import window_time_path
    if not score_log_path:
        return None
    try:
        with open(window_time_path(score_log_path), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _is_mostly_minimized(window_time: dict | None) -> bool:
    if not window_time:
        return False
    watched = float(window_time.get("watched_sec") or 0)
    return watched > 0 and float(window_time.get("minimized_sec") or 0) / watched >= MOSTLY_MINIMIZED_RATIO


def _remove_window_time(score_log_path: str | None) -> None:
    from ravens_record_core.detectors.score_detector import window_time_path
    if score_log_path:
        try:
            os.remove(window_time_path(score_log_path))
        except OSError:
            pass
