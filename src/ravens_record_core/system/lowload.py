"""ゲームの動作を邪魔しないための設定（コア・子プロセスの起動時に 1 回呼ぶ。v1.5.5）

高いリフレッシュレート（240Hz など）のモニターで遊んでいる人から、画面の監視を止めるとカクつきが無くなるという報告があった（2026-11）。
240fps を出すには 1 フレームを約 4ms で作る必要があり、CPU の全コアを使う処理が割り込むと、フレームが間に合わなくなる。

  - 文字の読み取り（Tesseract）を 1 コアで動かす（OMP_THREAD_LIMIT=1。指定しないと全コアを使う。小さな画像では 1 コアの方が速い）
  - OpenCV の処理を 1 コアで動かす（cv2.setNumThreads(1)）
  - このプロセスの優先度を「通常以下」にする（子プロセスにも引き継がれる。ゲームが CPU を使いたいときは、ゲームが先になる）

順番待ちの見張り（v2.2〜）:
  ゲームが CPU の全コアを使い切る端末では、「通常以下」の処理は、Windows が数秒に 1 回だけ回す分しか動けなくなる。
  画面の 1 枚の処理に数十秒かかり、試合前の紹介の場面（マップの判定）を撮り逃していた（2026-11 の診断ログ: 撮影 0〜0.8 枚/秒、
  スコアの 1 回の処理に 20〜77 秒）。眠らせた見張りのスレッドが、起きる予定から STARVE_LATE_SEC 秒以上遅れて起きることが
  STARVE_WINDOW_SEC 秒の間に STARVE_HITS 回あったら、順番待ちで止められているとみなし、このプロセスの優先度を「通常」に戻す
  （戻すのは 1 回だけ。プロセスが終わるまで通常のまま。ゲームと同じ優先度で、1 コア分を分け合うだけ）。
  戻したあとに起動する子プロセス（スコアの検出・ロード画面の監視）は、環境変数 PRIORITY_ENV で知らされ、初めから通常のまま動く。
"""

import os
import threading
import time

STARVE_CHECK_SEC = 0.5     # 見張りのスレッドが眠る時間
STARVE_LATE_SEC = 2.0      # 起きる予定からこれ以上遅れたら、順番待ちで止められていた
STARVE_HITS = 2            # STARVE_WINDOW_SEC 秒の間にこの回数遅れたら、優先度を戻す（スリープからの復帰など、1 回だけの遅れは数えない）
STARVE_WINDOW_SEC = 60.0
PRIORITY_ENV = "RRC_PRIORITY_NORMAL"   # 親のプロセスが優先度を通常に戻した（子プロセスへの知らせ）

_applied = False
_state = {"boosted": False, "late_max": 0.0}


def apply() -> None:
    global _applied
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")  # Tesseract（子プロセス）に引き継がれる
    try:
        import cv2
        cv2.setNumThreads(1)
    except Exception:
        pass  # OpenCV を読めなくても、ほかの設定は続ける（読み取りは、それぞれの処理が自分で失敗を扱う）
    if os.environ.get(PRIORITY_ENV) == "1":
        _state["boosted"] = True  # 親が順番待ちで止められていた: 子も初めから通常の優先度のまま
        return
    try:
        import psutil
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:
        return  # 優先度を変えられない環境（権限・Windows 以外）では、見張りもいらない
    if not _applied:
        _applied = True
        threading.Thread(target=_watch_starvation, name="rrc-starvation", daemon=True).start()


def status() -> dict:
    """順番待ちの見張りの様子（調査用）: boosted 優先度を通常に戻したか / late_max 起きるのがいちばん遅れた秒数"""
    return {"boosted": _state["boosted"], "late_max": round(_state["late_max"], 1)}


class StarvationWatch:
    """起きた時刻の遅れを受け取り、順番待ちで止められているかを決める（時計を渡せるように分けた。テスト用）"""

    def __init__(self) -> None:
        self.hits: list[float] = []

    def note(self, now: float, late: float) -> bool:
        """late: 起きる予定からの遅れ（秒）。優先度を戻すべきなら True"""
        _state["late_max"] = max(_state["late_max"], late)
        if late < STARVE_LATE_SEC:
            return False
        self.hits = [t for t in self.hits if now - t <= STARVE_WINDOW_SEC] + [now]
        return len(self.hits) >= STARVE_HITS


def _watch_starvation() -> None:
    watch = StarvationWatch()
    while not _state["boosted"]:
        start = time.monotonic()
        time.sleep(STARVE_CHECK_SEC)
        now = time.monotonic()
        if watch.note(now, now - start - STARVE_CHECK_SEC):
            boost(f"眠らせた見張りが、予定より {_state['late_max']:.1f} 秒遅れて起きることが続きました")


def boost(reason: str) -> None:
    """このプロセスの優先度を「通常」に戻す（1 回だけ）"""
    if _state["boosted"]:
        return
    _state["boosted"] = True
    os.environ[PRIORITY_ENV] = "1"  # このあと起動する子プロセスにも引き継ぐ
    try:
        import psutil
        psutil.Process().nice(psutil.NORMAL_PRIORITY_CLASS)
    except Exception:
        return
    try:
        from ravens_record_core import logger
        logger.log(f"[負荷] ゲームが CPU を使い切っていて、記録の処理が順番待ちで止まっていたため、優先度を通常に戻しました（{reason}）")
    except Exception:
        pass  # ログを出せなくても、優先度は戻したまま
