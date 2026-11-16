"""対戦後のレートの読み取り（rate_detector.py。ありうる範囲の確認は、読み取りの側で行う）"""

import time

from ravens_record_core.detectors.rate_detector import capture_stable_rate
from ravens_record_core.monitor.base import log

# 1 試合での変動として、ありうる最大の幅。対戦後のレートが対戦前からこれ以上離れていたら、読み誤りとみなして読み直す
# （昇格のボーナスや、低いレートの相手に負けたときなどで 150 を超えることがあるため 400）
RATE_MAX_CHANGE_PER_MATCH = 400
# 読み直す回数（それでも離れていれば、対戦後のレートは空のままにする）
RATE_AFTER_REREADS = 2
# 1 枚も読めなかった（read_error）・画面を取れなかった（no_frame）ときに、もう一度読むまで待つ秒数。
# ロビーに戻った直後は、レートの欄がまだ出ていない・撮影の準備ができていないことがあるため
RATE_AFTER_RETRY_DELAY_SEC = 1.0
RETRY_REASONS = ("read_error", "no_frame")

# 読めなかった理由のうち、試合の記録（rate_*_detail）に残すもの。画像は残さない（読みの結果と数値だけ）
_DETAIL_KEYS = ("reason", "frames", "no_frame", "not_s_rank", "read_error", "values", "rate_paths", "place_paths", "brightness")


def rate_detail(trace: dict, **extra) -> dict | None:
    """読めなかった理由（trace）から、試合の記録に残す分を取り出す。読めた（reason が ok）・空なら None"""
    if not trace or trace.get("reason") in (None, "ok"):
        return None
    detail = {k: trace[k] for k in _DETAIL_KEYS if k in trace}
    detail.update(extra)
    return detail


def log_rate_failure(label: str, trace: dict) -> None:
    """レートを読めなかった理由をログに残す（読めない試合が多い原因の調査用）"""
    log(f"[レート取得] {label}を読めませんでした（理由: {trace.get('reason')}、"
        f"画面 {trace.get('frames', 0)} 枚・取れず {trace.get('no_frame', 0)}・S ランク以外 {trace.get('not_s_rank', 0)}・"
        f"読み不一致 {trace.get('read_error', 0)}・読めた値 {trace.get('values', [])}、"
        f"最後の画面の読み: レート {trace.get('rate_paths')} / PLACE {trace.get('place_paths')}）")


def read_rate_after(rate_before: int | None, capture_func, sleep=time.sleep, trace: dict | None = None,
                    require_s_rank: bool = False):
    """対戦後のレートを読む。対戦前から RATE_MAX_CHANGE_PER_MATCH を超えて離れていたら、読み誤りとみなして読み直す。

    読み直しても離れていれば None（対戦後のレートは空のまま）。
    rate_before が None（対戦前を読めなかった）のときは、比べずに、続けて同じ値が読めたらそれを使う。
    require_s_rank: PLACE 欄も読めたときだけ使う（対戦前に S ランク以外とみなしたとき。S ランク以外の画面の数字を拾わないため）
    trace: 渡すと、読めなかった理由を書き込む（離れすぎは reason = too_far）
    """
    if rate_before is None:
        rate_res = capture_stable_rate(capture_func, interval_sec=0.2, ignore_s_rank=not require_s_rank, trace=trace)
        return rate_res if rate_res and rate_res.is_s_rank and rate_res.rate is not None else None
    for attempt in range(1 + RATE_AFTER_REREADS):
        rate_res = capture_stable_rate(capture_func, interval_sec=0.2, ignore_s_rank=True, trace=trace)
        if not rate_res or abs(rate_res.rate - rate_before) <= RATE_MAX_CHANGE_PER_MATCH:
            return rate_res
        log(f"[レート取得] 対戦後レート {rate_res.rate} が対戦前 {rate_before} から離れすぎているため、"
            f"読み直します（{attempt + 1}/{1 + RATE_AFTER_REREADS}）")
        sleep(1.0)
    log("[レート取得] 対戦後レートが対戦前から離れすぎたままのため、記録しません。")
    if trace is not None:
        trace["reason"] = "too_far"
    return None
