"""
AC6 スコア自動検出・記録 - Phase 0 MVP

要件定義 Phase0 に基づく修正版:
- ポーリング間隔を要件通りに修正（元コードは約30fpsで動作しており、
  CPU使用率・メモリ使用量の要件に違反していました）
- GUIプレビュー(cv2.imshow)を削除（Phase0要件「GUIはなくコンソールにログを残す」）
- 試合終了判定(2,1)/(1,2)/(2,2)を実装
- 試合終了時のJSONログ出力を実装（命名規則: YYYY-MM-DD_HH-MM-SS_自分-相手.json）
- モニタ全体ではなく、AC6のウィンドウ（クライアント領域）を基準にキャプチャするよう変更
- 2026-09-30: マスの点灯判定を「白バーが周囲より明るいか」に変更（背景が明るいと消灯マスを
  点灯と誤判定する問題、2 点目のマスの検出漏れによる 1-1 / 1-0 / 0-1 での途中終了への対応）
- v1.4.1: マスの判定（従来の撮影 mss）はそのまま残し、次を足した
  ・もう一つの撮影（screencap.FrameSource: 手前なら DXGI）でもマスを判定し、点数は両方の大きい方
    （フルスクリーンで mss の画像が止まっても、読み逃さないように）
  ・試合の終わりの帯（YOU WIN / YOU LOSE）とリザルト画面で、最終スコアを決める（match_end.EndWatcher）。
    マスで試合の終わりのスコアに届かないまま試合が終わったときに使う（利用者のログ 3 件: 自分の 2 点目の読み逃し）
  ・撮影の画像が長く変わらないときはログに残し、直近の上部の帯を数枚、端末内に残す（読めなかったときの調査用）
- v1.5.5: もう一つの撮影が DXGI で、画面の更新が届いている間は、mss の撮影を省く（1 回の撮影で済ませる）。
  高いリフレッシュレートのモニターで、フルスクリーンのゲームを 1 秒に何度も 2 通りの方法で撮ると、
  ゲームがカクつく原因になりうるため（2026-11 の報告）。DXGI が使えない・画面が止まっているときは、今までどおり mss でも撮る
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import cv2
import mss
import numpy as np
import win32gui

from ravens_record_core import logger, paths
from ravens_record_core.detectors import match_end
from ravens_record_core.system import gamewindow, screencap

# 対象プロセスの実行ファイル名は Config.trigger_exe（paths.trigger_exe()）。
# ウィンドウタイトルは言語設定等で変わりうるため、より安定するexe名で照合する。

# --- スコアマス（画面上部の左右 2 マスずつ）の判定 ---
# 点灯したマスは、枠の中に「白い横バー」（1080p で約 24×4px）が現れ、消灯時は灰色の塗りだけになる。
# マスは画面中央に近い「内側」から先に点灯し、2 点目で「外側」が点灯する。
#   自分（左）: 外側=枠1 / 内側=枠2    相手（右）: 内側=枠3 / 外側=枠4
#
# 白バーの位置（クライアント領域に対する割合）。2026-09-30 に実スクリーンショット
# （1920x1080: 2-2 / 1-2 / 2-1 / 0-2 の 4 パターン）から実測した。バーは 4 枚とも同じ位置だった。
# (x 開始, x 終了)。縦位置は BAR_SEARCH_Y の範囲で探す（バー本体は約 0.098〜0.102）。
BAR_X_RANGES = [
    (0.2562, 0.2687),  # 枠1: 自分・外側（2 点目）
    (0.2771, 0.2901),  # 枠2: 自分・内側（1 点目）
    (0.7099, 0.7224),  # 枠3: 相手・内側（1 点目）
    (0.7307, 0.7432),  # 枠4: 相手・外側（2 点目）
]
BAR_X_PAD = 0.003                 # 位置ずれの許容（各枠の左右に足す幅の割合）
BAR_SEARCH_Y = (0.086, 0.114)     # バーを探す縦範囲（クライアント領域の高さに対する割合）
BAR_THICKNESS_RATIO = 0.0028      # バーの厚み（高さに対する割合。1080p で約 3px）

# 点灯判定: 「バー候補の白さ」が十分に高く、かつ「周囲の行より明るい」こと。
# 従来の「高輝度ピクセルの割合」方式は、背景が明るい（空・雪など）と消灯マスの枠線まで
# 点灯と誤判定した。周囲との差で見れば背景の明るさに左右されない。
# 実測（4 枚の実スクショ + 0-0 のカウントダウン画面 47 枚）:
#   点灯: 白さ 215 以上 / 差 91 以上、消灯: 差 36 以下。
#   解像度 960x540〜2560x1440、JPEG 圧縮、軽いぼかし、明るさ 0.7 倍でも点灯は差 61 以上・消灯は 36 以下だった。
LIT_MIN_WHITENESS = 150
LIT_MIN_CONTRAST = 50

# 3回連続（0.6秒間）同じ状態が維持された場合に確定。
STABLE_FRAME_COUNT = 3

# 弱い点灯: 背景（空など）が明るいと、点灯したマスでも周囲との差が小さくなり、上の条件（差 50 以上）に届かない
# （2026-10 の録画: 明るい空の前で 2 点目が差 43〜53。解像度が低いほど小さい）。
# 差の条件を緩めると、背景の光の筋・爆発の一瞬を点灯と誤判定する（過去の試合の画像で確認）。
# 誤った点灯は一瞬（録画 2 本で最長 0.4 秒）、本当の点灯は試合の終わりまで続く（最後の点灯から暗転まで 2〜3 秒以上）ため、
# 弱い点灯は WEAK_LIT_MIN_SEC 秒（かつ WEAK_LIT_MIN_FRAMES 回）続いたときだけ点灯とみなす。
WEAK_LIT_MIN_WHITENESS = 200
WEAK_LIT_MIN_CONTRAST = 30
WEAK_LIT_MIN_SEC = 1.2
WEAK_LIT_MIN_FRAMES = 3

# ポーリング間隔
POLL_INTERVAL_SEC = 0.2

def score_debug_dir() -> Path:
    """判定確認用のスコア帯画像の保存先（スコアが変わったときのみ保存する）"""
    return paths.logs_dir() / "score_debug"


def recent_dir() -> Path:
    """直近の上部の帯（スコアのマス）の保存先（試合の終わりのスコアが読めなかったときの調査用）"""
    return score_debug_dir() / "recent"


SCORE_DEBUG_KEEP = 100  # 保存する最大枚数（超えたら古いものから削除）
# 直近の上部の帯を、この間隔で残す（最新 RECENT_KEEP 枚）
RECENT_INTERVAL_SEC = 2.0
RECENT_KEEP = 8
# 撮影の画像が、この秒数以上まったく変わらなければ、ログに残す（撮影が止まっている可能性）
STALE_WARN_SEC = 5.0
# もう一つの撮影（DXGI）の画像がこの秒数変わらなければ、止まっているとみなし、mss でも撮る
DXGI_FRESH_SEC = 1.0
# 帯・リザルト画面で最終スコアを決めたあと、リザルト画面で確かめるまで待つ最長の秒数
END_CONFIRM_WAIT_SEC = 25.0

# 要件3.2: 試合終了とみなすスコアの組み合わせ (自分, 相手)
MATCH_END_COMBINATIONS = {(2, 0), (2, 1), (0, 2), (1, 2), (2, 2)}


# 重複ログ保存防止ウィンドウ（秒）: 前回ログ保存からこの秒数未満の検出は同一試合とみなしてログ保存をスキップする
DUPLICATE_LOG_WINDOW_SEC = 60.0

# 起動確認用: 何もイベントが起きなくてもこの間隔で「生存確認」ログを出す
HEARTBEAT_INTERVAL_SEC = 30.0

# ウィンドウが見つからない/最小化中の状態を知らせる間隔（毎ループ出すとログが埋まるため間引く）
WINDOW_WARNING_INTERVAL_SEC = 5.0


def log(message: str) -> None:
    """ログを出力し、即座にflushする（JSON モードでは JSON Lines。親プロセスと同じ標準出力に書く）。"""
    logger.log(message)


def log_block(lines: list[str]) -> None:
    logger.log_block(lines)


def find_target_window(exe_name: str):
    """指定した実行ファイル名を持つプロセスの、表示中トップレベルウィンドウのhwndを返す。
    見つからない場合はNone。
    複数ウィンドウが該当する場合は、タイトルがあり最も面積が大きいものをメインウィンドウとみなす。
    """
    candidates = []

    def enum_handler(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        if not win32gui.GetWindowText(hwnd):
            return
        # プロセス名で判定（名前を取得できないときは、ウィンドウのタイトルで判定する。gamewindow を参照）
        if not gamewindow.is_game_window(hwnd, exe_name):
            return
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        area = max(0, right - left) * max(0, bottom - top)
        candidates.append((area, hwnd))

    win32gui.EnumWindows(enum_handler, None)
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[0][1]


def get_client_region_on_screen(hwnd):
    """指定ウィンドウのクライアント領域（タイトルバー・枠を含まない実描画領域）の
    スクリーン座標での位置とサイズを返す。 (left, top, width, height)
    """
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return None

    screen_left, screen_top = win32gui.ClientToScreen(hwnd, (left, top))
    return screen_left, screen_top, width, height


def measure_bar(frame, width, height, box_index):
    """枠 box_index の白バーを探し、(白さ, 周囲との差) を返す。

    - 白さ: 3 チャンネルの最小値（純白に近いほど大きい。色付きの明るい背景は低くなる）
    - 差   : バー候補の行の平均から、その上下の行の平均を引いた値（背景が明るくても消灯マスは小さい）
    """
    x0, x1 = BAR_X_RANGES[box_index]
    xa = int((x0 - BAR_X_PAD) * width)
    xb = int((x1 + BAR_X_PAD) * width)
    margin = int((xb - xa) * 0.12)  # 枠の縁を避けるため、左右 12% ずつ内側だけを使う
    xa, xb = xa + margin, xb - margin
    ya, yb = int(BAR_SEARCH_Y[0] * height), int(BAR_SEARCH_Y[1] * height)

    roi = frame[ya:yb, xa:xb]
    if roi.size == 0:
        return 0.0, 0.0

    profile = roi.min(axis=2).astype(np.float32).mean(axis=1)  # 行ごとの白さ
    k = max(2, round(BAR_THICKNESS_RATIO * height))
    if len(profile) < 3 * k + 8:
        return 0.0, 0.0

    bands = np.convolve(profile, np.ones(k, dtype=np.float32) / k, mode="valid")  # bands[y] = 行 y〜y+k-1 の平均
    first, last = k + 3, len(profile) - k - 4  # 上下に比較用の行を確保できる範囲
    y = first + int(np.argmax(bands[first:last + 1]))
    band = float(bands[y])
    above = float(profile[y - k - 3:y - 1].mean())
    below = float(profile[y + k + 1:y + k + 4].mean())
    return band, band - max(above, below)


def is_lit(band: float, contrast: float) -> int:
    return 1 if (band >= LIT_MIN_WHITENESS and contrast >= LIT_MIN_CONTRAST) else 0


def is_weak_lit(band: float, contrast: float) -> int:
    """弱い点灯（明るい背景の前の点灯マス）。続いた長さと合わせて判断する（WeakLitTracker）"""
    return 1 if (band >= WEAK_LIT_MIN_WHITENESS and contrast >= WEAK_LIT_MIN_CONTRAST) else 0


class WeakLitTracker:
    """枠ごとに、点灯（弱い点灯を含む）が続いている時刻と回数を数える"""

    def __init__(self, count: int = 4):
        self.since: list[float | None] = [None] * count
        self.frames = [0] * count

    def update(self, i: int, on: bool, now: float) -> bool:
        """枠 i の観測を 1 回渡す。点灯（弱い点灯を含む）が十分に続いていれば True"""
        if not on:
            self.since[i], self.frames[i] = None, 0
            return False
        if self.since[i] is None:
            self.since[i] = now
        self.frames[i] += 1
        return now - self.since[i] >= WEAK_LIT_MIN_SEC and self.frames[i] >= WEAK_LIT_MIN_FRAMES

    def reset(self) -> None:
        self.since = [None] * len(self.since)
        self.frames = [0] * len(self.frames)


def side_score(outer_lit: int, inner_lit: int) -> int:
    """片側（自分 or 相手）の得点。マスは内側 → 外側の順に点灯するので、
    外側が点灯していれば内側の検出漏れがあっても 2 点とみなす。"""
    if outer_lit:
        return 2
    return 1 if inner_lit else 0


def scores_from_boxes(states) -> tuple[int, int]:
    """4 枠の点灯状態 [枠1, 枠2, 枠3, 枠4] から (自分, 相手) の得点を返す。"""
    return side_score(states[0], states[1]), side_score(states[3], states[2])


def judge_boxes(frame, width, height, pending_states, stable_counters, current_scores,
                weak: WeakLitTracker | None = None, now: float | None = None):
    """各枠の点灯を判定して current_scores を更新し、各枠の (白さ, 差) を返す

    weak・now を渡すと、弱い点灯が WEAK_LIT_MIN_SEC 秒続いた枠も点灯とみなす（続いた長さで確かめ済みのため、すぐに確定する）。
    """
    metrics = []
    for i in range(len(BAR_X_RANGES)):
        band, contrast = measure_bar(frame, width, height, i)
        metrics.append((band, contrast))
        raw_state = is_lit(band, contrast)

        if weak is not None and now is not None:
            lasted = weak.update(i, bool(raw_state or is_weak_lit(band, contrast)), now)
            if lasted and not raw_state:
                raw_state = 1
                pending_states[i] = 1
                stable_counters[i] = max(stable_counters[i], STABLE_FRAME_COUNT - 1)

        # チャタリング防止ロジック
        if raw_state == pending_states[i]:
            stable_counters[i] += 1
        else:
            pending_states[i] = raw_state
            stable_counters[i] = 1

        if stable_counters[i] >= STABLE_FRAME_COUNT and current_scores[i] != raw_state:
            current_scores[i] = raw_state
    return metrics


def format_metrics(metrics) -> str:
    names = ["枠1(自分外)", "枠2(自分内)", "枠3(相手内)", "枠4(相手外)"]
    def state(b, c):
        return "点灯" if is_lit(b, c) else ("弱い点灯" if is_weak_lit(b, c) else "消灯")
    return " / ".join(f"{n}:{state(b, c)}(白さ{b:.0f},差{c:.0f})" for n, (b, c) in zip(names, metrics))


def save_debug_strip(frame, tag: str) -> None:
    """判定の調査用に、スコアマス周辺（画面上部の帯）を小さな画像で保存する。失敗しても判定は止めない。"""
    try:
        h, w = frame.shape[:2]
        strip = frame[int(0.06 * h):int(0.14 * h), int(0.22 * w):int(0.78 * w)]
        debug_dir = str(score_debug_dir())
        os.makedirs(debug_dir, exist_ok=True)
        path = os.path.join(debug_dir, f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}_{tag}.png")
        ok, buf = cv2.imencode(".png", strip)
        if ok:
            buf.tofile(path)  # 日本語パス対応のため imwrite ではなく tofile
        # 古い画像から削除して、保存数が増え続けないようにする（ファイル名が日時順）
        old = sorted(f for f in os.listdir(debug_dir) if f.endswith(".png"))[:-SCORE_DEBUG_KEEP]
        for name in old:
            os.remove(os.path.join(debug_dir, name))
    except Exception as e:
        log(f"[警告] 判定確認用画像の保存に失敗しました: {e}")


def check_image(path: str) -> None:
    """スクリーンショット 1 枚のマスの判定結果を表示する（キャリブレーション・不具合調査用）。
    使い方: python score_detector.py --check-image <画像ファイル>"""
    frame = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        print(f"画像を読み込めませんでした: {path}")
        return
    h, w = frame.shape[:2]
    metrics = [measure_bar(frame, w, h, i) for i in range(len(BAR_X_RANGES))]
    states = [is_lit(b, c) for b, c in metrics]
    mine, enemy = scores_from_boxes(states)
    print(f"{path} ({w}x{h})")
    print(f"  {format_metrics(metrics)}")
    print(f"  判定スコア: {mine}-{enemy}（自分-相手）")


import argparse


def save_match_log(my_score: int, enemy_score: int, output_path: str = None, basis: str = "boxes") -> str:
    """要件3.3: 試合終了時にJSON形式のログファイルをローカルに保存する

    basis: スコアの根拠（boxes: マスの判定 / result_screen: リザルト画面 / end_banner: 試合の終わりの帯）。
    リザルト画面で読めたスコアは、2 点に届いていなくても（切断など）そのとおりなので、本体は「不自然なスコア」として扱わない"""
    now = datetime.now()
    data = {
        "timestamp": now.isoformat(),
        "my_score": my_score,
        "enemy_score": enemy_score,
        "basis": basis,
    }

    if output_path:
        path = output_path
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    else:
        log_dir = str(paths.logs_dir())
        os.makedirs(log_dir, exist_ok=True)
        filename = f"{now.strftime('%Y-%m-%d_%H-%M-%S')}_{my_score}-{enemy_score}.json"
        path = os.path.join(log_dir, filename)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    log(f"[試合終了検出] スコア {my_score}-{enemy_score} をログに保存しました: {path}")
    return path


def save_recent_strip(frame, seq: int) -> None:
    """直近の上部の帯を、recent_dir() に残す（最新 RECENT_KEEP 枚。ファイル名は通し番号と時刻）。失敗しても判定は止めない。"""
    try:
        h, w = frame.shape[:2]
        strip = frame[int(0.06 * h):int(0.14 * h), int(0.22 * w):int(0.78 * w)]
        recent = str(recent_dir())
        os.makedirs(recent, exist_ok=True)
        ok, buf = cv2.imencode(".png", strip)
        if ok:
            buf.tofile(os.path.join(recent, f"{seq:05d}_{datetime.now().strftime('%H%M%S')}.png"))
        old = sorted(f for f in os.listdir(recent) if f.endswith(".png"))[:-RECENT_KEEP]
        for name in old:
            os.remove(os.path.join(recent, name))
    except Exception as e:
        log(f"[警告] 直近の帯の画像を保存できませんでした: {e}")


def clear_recent_strips() -> None:
    try:
        recent = str(recent_dir())
        if os.path.isdir(recent):
            for name in os.listdir(recent):
                os.remove(os.path.join(recent, name))
    except OSError:
        pass


class FreshTracker:
    """画像が直近 DXGI_FRESH_SEC 秒以内に変わったか（撮影が止まっていないか）"""

    def __init__(self):
        self._sig = None
        self._changed_at = None

    def update(self, frame, now: float) -> bool:
        sig = frame[::16, ::16].tobytes()
        if sig != self._sig:
            self._sig, self._changed_at = sig, now
        return self._changed_at is not None and now - self._changed_at < DXGI_FRESH_SEC


class StaleTracker:
    """撮影の画像が長く変わらないことを見つける（同じ状態が続く間は 1 回だけ知らせる）"""

    def __init__(self, label: str):
        self.label = label
        self._sig = None
        self._since = None
        self._warned = False

    def update(self, frame, now: float) -> None:
        sig = frame[::16, ::16].tobytes()
        if sig != self._sig:
            self._sig, self._since, self._warned = sig, now, False
            return
        if not self._warned and self._since is not None and now - self._since >= STALE_WARN_SEC:
            self._warned = True
            log(f"[警告] 撮影（{self.label}）の画像が{STALE_WARN_SEC:.0f}秒以上変わっていません（撮影が止まっている可能性）")


def reset_state(current_scores, pending_states, stable_counters):
    for i in range(4):
        current_scores[i] = 0
        pending_states[i] = 0
        stable_counters[i] = 0


def window_time_path(output_path: str) -> str:
    """監視した時間と、そのうちゲームが最小化されていた時間を書くファイル（スコアの結果のファイルの横）"""
    return f"{output_path}.window.json"


class WindowTimeTracker:
    """監視した時間と、ゲームが最小化されていた時間を数え、少しおきにファイルへ書く。
    本体は、スコアを確定できなかった試合で、最小化が原因かどうかを見分けるのに使う（この処理は止められて終わるため、こまめに書く）"""

    WRITE_INTERVAL_SEC = 5.0

    def __init__(self, path: Optional[str]):
        self.path = path
        self.watched_sec = 0.0
        self.minimized_sec = 0.0
        self._last: Optional[float] = None
        self._last_write = 0.0

    def add(self, now: float, minimized: bool) -> None:
        if self._last is not None:
            dt = min(now - self._last, 60.0)  # 処理が止まっていた間も数える（とびぬけて長いものは切る）
            self.watched_sec += dt
            if minimized:
                self.minimized_sec += dt
        self._last = now
        if self.path and now - self._last_write >= self.WRITE_INTERVAL_SEC:
            self._last_write = now
            try:
                with open(self.path, "w", encoding="utf-8") as f:
                    json.dump({"watched_sec": round(self.watched_sec, 1), "minimized_sec": round(self.minimized_sec, 1)}, f)
            except OSError:
                pass


def main(argv: list[str] | None = None) -> None:
    from ravens_record_core.system import lowload
    lowload.apply()  # 子プロセスとして起動されたとき、自分にも負荷を下げる設定をする
    parser = argparse.ArgumentParser(prog="ravens_record_core --score-detector")
    parser.add_argument("--output", type=str, help="スコア結果のJSONを出力するパス")
    parser.add_argument("--check-image", type=str, help="スクリーンショット 1 枚のマス判定結果を表示して終了する（調査用）")
    args = parser.parse_args(argv)

    if args.check_image:
        check_image(args.check_image)
        return

    target_exe = paths.trigger_exe()

    # --- 起動確認用バナー ---
    log_block([
        "=" * 50,
        "AC6 スコア検出プロセス 起動",
        f"PID: {os.getpid()}",
        f"対象プロセス: {target_exe!r}",
        "=" * 50,
    ])

    current_scores = [0, 0, 0, 0]
    pending_states = [0, 0, 0, 0]
    stable_counters = [0, 0, 0, 0]
    weak_lit = WeakLitTracker()

    # もう一つの撮影（手前なら DXGI）でのマスの判定（従来の mss の判定とは別に持つ）
    source = screencap.FrameSource(log=log, label="[スコア検出]")
    current_scores2 = [0, 0, 0, 0]
    pending_states2 = [0, 0, 0, 0]
    stable_counters2 = [0, 0, 0, 0]
    weak_lit2 = WeakLitTracker()
    # 試合の終わりの帯・リザルト画面
    end_watcher = match_end.EndWatcher()
    end_events_logged = 0
    end_saved = None        # 帯・リザルト画面で決めて保存したスコア
    end_saved_at = 0.0
    stale_mss = StaleTracker("mss")
    dxgi_fresh = FreshTracker()
    recent_seq = 0
    last_recent = 0.0
    clear_recent_strips()

    latched_my_score = 0
    latched_enemy_score = 0
    last_inconsistent = False

    last_heartbeat = time.monotonic()
    last_window_warning = 0.0
    last_log_time = 0.0

    window_time = WindowTimeTracker(window_time_path(args.output) if args.output else None)

    try:
        with mss.MSS() as sct:
            log(f"ポーリング間隔: {POLL_INTERVAL_SEC}秒 / 停止するには Ctrl+C を押してください。")
            log("監視ループを開始します（対象ウィンドウの検出待ち）。")

            while True:
                loop_start = time.monotonic()

                # 毎ループ、対象ウィンドウを探し直す（ウィンドウの移動・リサイズ・
                # 再起動に追従するため。1秒未満の間隔なのでコストは軽微）。
                hwnd = find_target_window(target_exe)

                window_time.add(loop_start, minimized=hwnd is not None and bool(win32gui.IsIconic(hwnd)))

                region = None
                if hwnd is None:
                    if loop_start - last_window_warning >= WINDOW_WARNING_INTERVAL_SEC:
                        log(f"[警告] ウィンドウが見つかりません（対象プロセス: {target_exe!r}）。ゲームが起動しているか確認してください。")
                        last_window_warning = loop_start
                elif win32gui.IsIconic(hwnd):
                    if loop_start - last_window_warning >= WINDOW_WARNING_INTERVAL_SEC:
                        log("[警告] 対象ウィンドウが最小化されています。")
                        last_window_warning = loop_start
                else:
                    region = get_client_region_on_screen(hwnd)
                    if region is None and loop_start - last_window_warning >= WINDOW_WARNING_INTERVAL_SEC:
                        log("[警告] 対象ウィンドウのクライアント領域取得に失敗しました。")
                        last_window_warning = loop_start

                if region is not None:
                    left, top, width, height = region
                    is_in_duplicate_window = (loop_start - last_log_time < DUPLICATE_LOG_WINDOW_SEC)

                    # もう一つの撮影（手前なら DXGI）で、マスを判定する（撮れなかった回は、前の判定のまま）
                    try:
                        frame2 = source.grab(hwnd)
                    except Exception as e:
                        frame2 = None
                        log(f"[警告] もう一つの撮影に失敗しました: {e!r}")
                    metrics2 = None
                    if frame2 is not None:
                        h2, w2 = frame2.shape[:2]
                        metrics2 = judge_boxes(frame2, w2, h2, pending_states2, stable_counters2, current_scores2,
                                               weak=weak_lit2, now=loop_start)

                    # DXGI で画面の更新が届いている間は、mss の撮影を省く（カクつき対策。v1.5.5）
                    skip_mss = frame2 is not None and source.backend == "dxgi" and dxgi_fresh.update(frame2, loop_start)
                    if skip_mss:
                        frame = frame2
                        box_metrics = metrics2
                    else:
                        img = np.array(sct.grab({"left": left, "top": top, "width": width, "height": height}))
                        frame = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
                        box_metrics = judge_boxes(
                            frame, width, height, pending_states, stable_counters, current_scores,
                            weak=weak_lit, now=loop_start,
                        )
                        stale_mss.update(frame, loop_start)

                    # マスは内側 → 外側の順に点灯する。確定した点灯状態から両者の得点を求め、
                    # 一度確定した得点は下がらないようラッチする（max）。2 つの撮影のうち、大きい方を使う
                    cur_my, cur_enemy = scores_from_boxes(current_scores)
                    cur_my2, cur_enemy2 = scores_from_boxes(current_scores2)
                    cur_my, cur_enemy = max(cur_my, cur_my2), max(cur_enemy, cur_enemy2)
                    my_score = max(latched_my_score, cur_my)
                    enemy_score = max(latched_enemy_score, cur_enemy)

                    # 外側だけ点灯して内側が消灯 = 通常ありえない組み合わせ。判定の調査用に記録する
                    judged = current_scores2 if skip_mss else current_scores
                    inconsistent = (judged[0] and not judged[1]) or (judged[3] and not judged[2])
                    if inconsistent and not last_inconsistent:
                        log(f"[警告] 外側のマスだけが点灯しています（内側の検出漏れの可能性）: {format_metrics(box_metrics)}")
                        save_debug_strip(frame, "inconsistent")
                    last_inconsistent = bool(inconsistent)

                    if (my_score, enemy_score) != (latched_my_score, latched_enemy_score):
                        logger.emit("score_updated",
                                    f"[スコア検出] スコア更新: {latched_my_score}-{latched_enemy_score} -> {my_score}-{enemy_score}",
                                    my_score=my_score, enemy_score=enemy_score, msg_key="event.scoreUpdated")
                        log(f"[スコア検出] マス判定: {format_metrics(box_metrics)}")
                        if (cur_my, cur_enemy) != scores_from_boxes(current_scores):
                            log(f"[スコア検出] （この点数は、もう一つの撮影〈{source.backend or '不明'}〉の判定で読めました）")
                        save_debug_strip(frame2 if frame2 is not None else frame, f"{my_score}-{enemy_score}")
                        latched_my_score = my_score
                        latched_enemy_score = enemy_score

                    if (latched_my_score, latched_enemy_score) in MATCH_END_COMBINATIONS:
                        if not is_in_duplicate_window:
                            save_match_log(latched_my_score, latched_enemy_score, args.output)
                            last_log_time = loop_start

                        logger.emit("score_confirmed",
                                    f"[最終スコア確定] 最終スコア: {latched_my_score}-{latched_enemy_score} で試合が終了しました。",
                                    my_score=latched_my_score, enemy_score=latched_enemy_score, msg_key="event.scoreConfirmed")
                        time.sleep(3.0)  # 最終スコアログ確認のための猶予時間
                        log("ScoreDetector 監視プロセスを終了します。")
                        return

                    # --- 試合の終わりの帯・リザルト画面（マスで試合の終わりのスコアに届かないまま終わったときの補い） ---
                    view = frame2 if frame2 is not None else frame
                    if loop_start - last_recent >= RECENT_INTERVAL_SEC:
                        last_recent = loop_start
                        recent_seq += 1
                        save_recent_strip(view, recent_seq)
                    end_watcher.observe(view, loop_start)
                    for event in end_watcher.events[end_events_logged:]:
                        log(f"[試合の終わり] {event}")
                    end_events_logged = len(end_watcher.events)
                    decided = end_watcher.decided_score(latched_my_score, latched_enemy_score)
                    if decided is not None and decided[:2] != end_saved:
                        my_final, enemy_final, basis = decided
                        save_match_log(my_final, enemy_final, args.output,
                                       basis="result_screen" if basis == "リザルト画面" else "end_banner")
                        end_saved, end_saved_at = (my_final, enemy_final), loop_start
                        logger.emit("score_confirmed",
                                    f"[最終スコア確定] {basis}から、最終スコアを {my_final}-{enemy_final} と判断しました"
                                    f"（マスの判定: {latched_my_score}-{latched_enemy_score}）。",
                                    my_score=my_final, enemy_score=enemy_final, msg_key="event.scoreConfirmed")
                    if end_saved is not None and (end_watcher.result is not None
                                                  or loop_start - end_saved_at >= END_CONFIRM_WAIT_SEC):
                        log("ScoreDetector 監視プロセスを終了します。")
                        return

                    if loop_start - last_heartbeat >= HEARTBEAT_INTERVAL_SEC:
                        log(f"[監視中] 現在のスコア {latched_my_score}-{latched_enemy_score} (稼働中, ウィンドウ: {width}x{height})")
                        last_heartbeat = loop_start

                # ポーリング間隔を維持するためのスリープ調整
                elapsed = time.monotonic() - loop_start
                sleep_time = POLL_INTERVAL_SEC - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)
                elif elapsed > POLL_INTERVAL_SEC * 3:
                    # 帯・リザルト画面の文字を読んだ回は、少し長くかかる（それ自体は問題ない）ので、大きく超えたときだけ知らせる
                    log(f"[警告] 1フレームの処理が{elapsed:.3f}秒かかり、ポーリング間隔を超過しました。")

    except KeyboardInterrupt:
        pass
    except Exception as e:
        # 予期しない例外で無言落ちすると「起動したかも分からない」状態に逆戻りするため、
        # 必ずログに残してから再送出する。
        log(f"[エラー] 予期しない例外で終了します: {e!r}")
        raise


if __name__ == "__main__":
    main()
