"""ゲームの動作を邪魔しないための設定（コア・子プロセスの起動時に 1 回呼ぶ。v1.5.5）

高いリフレッシュレート（240Hz など）のモニターで遊んでいる人から、画面の監視を止めるとカクつきが無くなるという報告があった（2026-11）。
240fps を出すには 1 フレームを約 4ms で作る必要があり、CPU の全コアを使う処理が割り込むと、フレームが間に合わなくなる。

  - 文字の読み取り（Tesseract）を 1 コアで動かす（OMP_THREAD_LIMIT=1。指定しないと全コアを使う。小さな画像では 1 コアの方が速い）
  - OpenCV の処理を 1 コアで動かす（cv2.setNumThreads(1)）
  - このプロセスの優先度を「通常以下」にする（子プロセスにも引き継がれる。ゲームが CPU を使いたいときは、ゲームが先になる）
"""

import os


def apply() -> None:
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")  # Tesseract（子プロセス）に引き継がれる
    try:
        import cv2
        cv2.setNumThreads(1)
    except Exception:
        pass  # OpenCV を読めなくても、ほかの設定は続ける（読み取りは、それぞれの処理が自分で失敗を扱う）
    try:
        import psutil
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:
        pass  # 優先度を変えられない環境（権限・Windows 以外）でも、そのまま動かす
