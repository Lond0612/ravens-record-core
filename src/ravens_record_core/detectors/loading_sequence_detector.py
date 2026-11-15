"""
AC6ランクマッチ戦歴記録サービス - 対戦開始確定シーケンス検出
（暗転 → ロード画面 → 再暗転）

要件定義（Phase 1 詳細定義書 5.3）に基づく実装：
  1. 暗転検出（画面全体の輝度がしきい値以下）
  2. ロード画面検出
  3. 再度の暗転検出
  3が完了した時点で「試合開始」とみなし、呼び出し元（本モジュールの利用者）が
  Phase 0のスコア監視プロセスを起動する。

■ 検出方式について
「対戦開始します」検出後に現れる暗転→（何か）→暗転、という構造そのものが
ロード画面の存在を保証しているため、2.のロード画面検出は
「最初の暗転の後、輝度が一定時間（DEFAULT_CONFIRM_SEC 秒）続けて戻ったこと」のみで判定できる。
画像の中身（背景シーンの見た目）を照合する必要はない。

■ テンプレート画像について（配布物には同梱しない）
ロード画面の背景を記録したテンプレート画像は、検出ロジックには一切使用しない。
あくまで「診断用の副産物」として、実際に検出できたロード画面フレームを
ユーザー環境の書き込み可能なディレクトリ（既定では%LOCALAPPDATA%配下）に
自動的に蓄積するのみ。開発時に採取した画像を配布物に同梱する必要はなく、
各ユーザーの環境で実際に表示されたロード画面から自動収集される。
既知のバリエーションと十分似ている場合は保存をスキップし、
新しいバリエーションが出てきたときだけ増える。

このモジュールは検出のプリミティブと状態機械（LoadingSequenceDetector）のみを提供する。
状態機械へフレームを渡すポーリングループ自体（頻度・呼び出しタイミング・Phase 0との連携）は
呼び出し元で実装する想定。
"""

import time
from ctypes import windll
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import win32gui
import win32ui
from mss import mss
from mss.exception import ScreenShotError

from ravens_record_core import logger, paths
from ravens_record_core.system import gamewindow, screencap

PW_RENDERFULLCONTENT = 2
# 1 = PW_CLIENTONLY。クライアント領域だけを撮る（付けないと、ウィンドウモードでは枠とタイトルバーの分だけ
# 中身が右下にずれ、下端・右端が切れる。2026-10 修正）
PW_CLIENTONLY = 1
DEBUG = False


# 暗転判定：グレースケール平均輝度がこの値未満なら「暗転」とみなす（0〜255）
DEFAULT_DARK_THRESHOLD = 15.0

# 一様な暗い画面も暗転とみなす（試合前の流れだけ。v1.4.1〜）。ゲームの明るさの設定を上げている（または HDR の）環境では、
# 暗転が真っ黒にならず、明るさ 13〜33 程度の一様な灰色になる（2560x1440 の利用者のログ。ほかの環境では 0〜4）。
# 運営者の録画では、本当の暗転は画素のばらつき（標準偏差）がほぼ 0、暗い場面（トンネル・暗いリザルト画面）は 6.8 以上だった
UNIFORM_DARK_MAX_BRIGHTNESS = 40.0
UNIFORM_DARK_MAX_STD = 3.0

# 試合前の流れの、時間での補い（v1.4.1〜。録画: Alpha の紹介を確定してから 1 回目の暗転まで約 6 秒、2 回目の暗転まで約 16 秒）
# ・Alpha の紹介を確定してから、この秒数たっても暗転が来なければ、1 回目の暗転を見逃したとみなし、2 回目の暗転を待つ
PRE_MATCH_DARK1_FALLBACK_SEC = 11.0
# ・Beta の紹介を確定してから（または上の補いから）、この秒数たっても暗転が来なければ、打ち切る（90 秒待たずに、スコアの監視を始める）
PRE_MATCH_DARK2_GIVEUP_SEC = 20.0

# 対戦の画面（上の左の見出しの、陣営の色の縦棒）が、この回数続けて見えたら、暗転を撮れていなくても、カウントダウンに入ったとみなす
# （利用者のログ: 1920x1061 のウィンドウ表示で、2 回目の暗転を撮れないまま 20 秒で打ち切った。紹介の映像には、この見出しは出ない）
PRE_MATCH_HUD_CONFIRM_FRAMES = 2

# 「明るい画面が続いた」とみなす秒数と、その間に必要な最少の枚数（チャタリング防止）。
# 以前は「10 枚連続」だったが、枚数で数えると、撮影の間隔（PC の重さ・解像度で変わる）によって必要な秒数が変わり、
# 遅い PC では 7 秒ほどのロード画面・8 秒ほどの Alpha / Beta の画面の間に 10 枚がそろわなかった（2026-10 の録画で確認）。
# 0.2 秒ごとの 10 枚とほぼ同じ 2 秒にした。
DEFAULT_CONFIRM_SEC = 2.0
DEFAULT_CONFIRM_MIN_FRAMES = 3
# カウントダウンの画面を集める秒数（以前の「10 枚」に相当。0.25 秒ごとで約 2.5 秒）と、最少の枚数
DEFAULT_COUNTDOWN_COLLECT_SEC = 2.5
DEFAULT_COUNTDOWN_MIN_FRAMES = 3

# 収集したロード画面テンプレート同士の重複判定用しきい値（-1.0〜1.0）
# 既存テンプレートとの相関がこの値以上なら「同じバリエーション」とみなし、保存をスキップする
DEFAULT_DEDUP_SIMILARITY_THRESHOLD = 0.98


def _log(message: str) -> None:
    logger.log(message)


def _debug_log(message: str) -> None:
    if DEBUG:
        logger.log(f"[DEBUG] {message}", level="debug")


# ---------------------------------------------------------------------------
# ウィンドウキャプチャ（mode_detector.py / match_status_detector.py と同一方式）
# ---------------------------------------------------------------------------

def find_target_window(exe_name: Optional[str] = None):
    """指定した実行ファイル名を持つプロセスのメインウィンドウhwndを返す"""
    exe_name = exe_name or paths.trigger_exe()
    candidates = []

    def enum_handler(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        # プロセス名で判定（名前を取得できないときは、ウィンドウのタイトルで判定する。gamewindow を参照）
        if not gamewindow.is_game_window(hwnd, exe_name):
            return
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        if max(0, right - left) * max(0, bottom - top) <= 0:
            return
        candidates.append((max(0, right - left) * max(0, bottom - top), hwnd))

    win32gui.EnumWindows(enum_handler, None)
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[0][1]


def capture_window_frame(hwnd) -> Optional[np.ndarray]:
    """PrintWindowでウィンドウのクライアント領域全体をキャプチャし、BGR画像として返す"""
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return None

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()

    save_bitmap = win32ui.CreateBitmap()
    save_bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
    save_dc.SelectObject(save_bitmap)

    try:
        result = windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), PW_CLIENTONLY | PW_RENDERFULLCONTENT)
        if result != 1:
            _debug_log(f"PrintWindow戻り値={result}（1以外は失敗の可能性）")

        bmp_info = save_bitmap.GetInfo()
        bmp_bits = save_bitmap.GetBitmapBits(True)
        img = np.frombuffer(bmp_bits, dtype="uint8").reshape(
            (bmp_info["bmHeight"], bmp_info["bmWidth"], 4)
        )
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    finally:
        win32gui.DeleteObject(save_bitmap.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)


def capture_frame_with_fallback(target_exe: Optional[str] = None, monitor_index: int = 1) -> Optional[np.ndarray]:
    """AC6ウィンドウを直接キャプチャし、失敗時のみモニタ全体にフォールバックする"""
    target_exe = target_exe or paths.trigger_exe()
    hwnd = find_target_window(target_exe)
    if hwnd is not None and win32gui.IsIconic(hwnd):
        # 最小化中（フルスクリーンで別の画面に切り替えたときなど）は、ゲームの画面が無い。
        # モニター全体を撮ると、デスクトップなどを見て判定を誤るので、撮らない（呼び出し側は None を「撮れなかった」として待つ）
        return None
    if hwnd is not None:
        # 手前なら画面から直接（DXGI）、裏なら PrintWindow で撮る（screencap.grab_client。PrintWindow だけだと古い画面が返り続けることがある）
        frame = screencap.grab_client(hwnd)
        if frame is not None:
            return frame
    if not screencap.monitor_fallback_allowed(hwnd):
        # ゲームが見つからない・裏にあるときは、モニターに写っているのはゲームではない（別のアプリを見て判定を誤らない）
        return None

    _debug_log("ウィンドウキャプチャ失敗、モニタ全体にフォールバックします")
    try:
        with mss() as sct:
            monitor = sct.monitors[monitor_index] if monitor_index < len(sct.monitors) else sct.monitors[1]
            shot = sct.grab(monitor)
            return cv2.cvtColor(np.array(shot), cv2.COLOR_BGRA2BGR)
    except ScreenShotError as e:
        # UAC の確認・ロック画面などで、画面を撮れない間（撮れなかったとして、呼び出し側が待つ）
        _debug_log(f"モニタ全体の撮影に失敗: {e!r}")
        return None


# ---------------------------------------------------------------------------
# 暗転検出
# ---------------------------------------------------------------------------

def mean_brightness(img_bgr: np.ndarray) -> float:
    """画像全体のグレースケール平均輝度（0〜255）を返す"""
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    return float(gray.mean())


def is_dark_frame(img_bgr: np.ndarray, threshold: float = DEFAULT_DARK_THRESHOLD) -> bool:
    """画面全体が暗転しているかを判定する"""
    return mean_brightness(img_bgr) < threshold


# ---------------------------------------------------------------------------
# ヒストグラム比較（検出には使わない。収集したテンプレートの重複判定・診断用）
# ---------------------------------------------------------------------------

def compute_hs_histogram(img_bgr: np.ndarray) -> np.ndarray:
    """色相・彩度(H, S)のヒストグラムを計算する（明度Vは含めない）。

    明度を除外するのは、環境（ディスプレイのガンマ設定・HDR設定等）による
    明るさの個体差を吸収するため。
    """
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [50, 60], [0, 180, 0, 256])
    cv2.normalize(hist, hist, alpha=0, beta=1, norm_type=cv2.NORM_MINMAX)
    return hist


def histogram_similarity_multi(img_bgr: np.ndarray, reference_hists: list) -> float:
    """現在のフレームと、複数テンプレートそれぞれとのヒストグラム相関のうち最大値を返す。
    reference_histsが空の場合は-1.0（一致なし）を返す。
    """
    if not reference_hists:
        return -1.0
    current_hist = compute_hs_histogram(img_bgr)
    return max(
        cv2.compareHist(current_hist, ref_hist, cv2.HISTCMP_CORREL)
        for ref_hist in reference_hists
    )


# ---------------------------------------------------------------------------
# ユーザー環境ごとのテンプレート保存先
# ---------------------------------------------------------------------------

def get_default_template_dir() -> Path:
    """テンプレートの既定保存先（data_dir/loading_templates）を返す。

    開発時に採取した画像を配布物に同梱するのではなく、
    各ユーザーの環境で実際に表示されたロード画面から自動収集する方針のため、保存先（data_dir）の下に置く。
    """
    return paths.data_dir() / "loading_templates"


def _load_templates_from_dir(template_dir: Path) -> list:
    """指定ディレクトリ内のtemplate_*.pngを読み込み、ヒストグラムのリストを返す（存在しなくてもよい）"""
    if not template_dir.exists():
        return []
    hists = []
    for path in sorted(template_dir.glob("template_*.png")):
        img = cv2.imread(str(path))
        if img is not None:
            hists.append(compute_hs_histogram(img))
    return hists


# ---------------------------------------------------------------------------
# 対戦開始確定シーケンスの状態機械
# ---------------------------------------------------------------------------

class SequenceState(Enum):
    WAITING_FIRST_DARK = auto()    # 「対戦開始します」検出後、最初の暗転待ち
    WAITING_LOADING = auto()       # 暗転検出後、輝度が戻って安定するのを待つ（＝ロード画面）
    WAITING_SECOND_DARK = auto()   # ロード画面確定後、再度の暗転待ち
    COMPLETE = auto()              # シーケンス完了（試合開始とみなしてよい）


@dataclass
class SequenceStepResult:
    state: SequenceState
    brightness: float
    loading_streak: int
    just_transitioned: bool  # このフレームで状態が変化したか（呼び出し元でのログ制御等に使える）


class LoadingSequenceDetector:
    """「対戦開始します」検出後に呼び出し元が生成し、以後フレームを渡し続けることで
    暗転→ロード画面→再暗転のシーケンスを追跡する状態機械。

    検出そのものは輝度（暗転判定）のみで完結し、テンプレート画像は不要。
    実際に検出できたロード画面フレームは、副産物として自動的に
    テンプレートディレクトリ（既定は %LOCALAPPDATA%\\AC6RankTracker\\loading_templates）
    に蓄積される（診断用途。検出ロジックはこれに依存しない）。

    使い方の想定：
        detector = LoadingSequenceDetector()
        while True:
            frame = capture_frame_with_fallback()
            result = detector.step(frame)
            if result.state == SequenceState.COMPLETE:
                # ここでPhase 0のスコア監視プロセスを呼び出す
                break
            time.sleep(...)  # 高頻度ポーリングを想定
    """

    def __init__(
        self,
        template_dir: Optional[str] = None,
        dark_threshold: float = DEFAULT_DARK_THRESHOLD,
        loading_confirm_sec: float = DEFAULT_CONFIRM_SEC,
        loading_confirm_frames: int = DEFAULT_CONFIRM_MIN_FRAMES,
        auto_collect_templates: bool = True,
        dedup_similarity_threshold: float = DEFAULT_DEDUP_SIMILARITY_THRESHOLD,
    ):
        self.template_dir = Path(template_dir) if template_dir else get_default_template_dir()
        self.dark_threshold = dark_threshold
        self.loading_confirm_sec = loading_confirm_sec
        self.loading_confirm_frames = loading_confirm_frames
        self.auto_collect_templates = auto_collect_templates
        self.dedup_similarity_threshold = dedup_similarity_threshold

        self.reference_hists = _load_templates_from_dir(self.template_dir)
        if self.reference_hists:
            _log(f"ロード画面テンプレートを{len(self.reference_hists)}枚読み込みました"
                 f"（{self.template_dir}）")
        elif self.auto_collect_templates:
            _log("ロード画面テンプレートが未収集です。検出には影響ありません"
                 "（今後の対戦で自動的に収集されます）。")

        self.state = SequenceState.WAITING_FIRST_DARK
        self._loading_streak = 0
        self._loading_since: Optional[float] = None  # 明るい画面が続き始めた時刻
        self._candidate_frame: Optional[np.ndarray] = None

    def reset(self) -> None:
        """シーケンスを最初からやり直す（次の「対戦開始します」検出時に呼び出す想定）"""
        self.state = SequenceState.WAITING_FIRST_DARK
        self._loading_streak = 0
        self._loading_since = None
        self._candidate_frame = None

    def step(self, img_bgr: np.ndarray, now: Optional[float] = None) -> SequenceStepResult:
        """1フレーム分の画像を渡して状態を更新する（now: 撮影した時刻。省略時は time.monotonic()）"""
        now = time.monotonic() if now is None else now
        brightness = mean_brightness(img_bgr)
        previous_state = self.state

        if self.state == SequenceState.WAITING_FIRST_DARK:
            if brightness < self.dark_threshold:
                self.state = SequenceState.WAITING_LOADING
                self._loading_streak = 0
                self._loading_since = None
                self._candidate_frame = None
                _log("暗転を検出しました。ロード画面の確定を待ちます。")

        elif self.state == SequenceState.WAITING_LOADING:
            if brightness >= self.dark_threshold:
                # 暗転から輝度が戻った＝構造上、ロード画面が表示されていると判断できる
                if self._loading_streak == 0:
                    self._loading_since = now
                self._loading_streak += 1
                self._candidate_frame = img_bgr
            else:
                self._loading_streak = 0
                self._loading_since = None

            if (self._loading_streak >= self.loading_confirm_frames and self._loading_since is not None
                    and now - self._loading_since >= self.loading_confirm_sec):
                self.state = SequenceState.WAITING_SECOND_DARK
                _log("ロード画面を確定しました。再度の暗転を待ちます。")

        elif self.state == SequenceState.WAITING_SECOND_DARK:
            if brightness < self.dark_threshold:
                self.state = SequenceState.COMPLETE
                _log("再度の暗転を検出しました。試合開始と判定します。")
                if self.auto_collect_templates and self._candidate_frame is not None:
                    self._maybe_save_template(self._candidate_frame)

        just_transitioned = self.state != previous_state
        return SequenceStepResult(
            state=self.state,
            brightness=brightness,
            loading_streak=self._loading_streak,
            just_transitioned=just_transitioned,
        )

    def _maybe_save_template(self, frame: np.ndarray) -> None:
        """シーケンス完了後、候補フレームを新しいテンプレートとして保存するかどうかを判断する。

        既存テンプレートのいずれかと十分似ている（＝既知のバリエーション）場合は保存しない。
        """
        if self.reference_hists:
            similarity = histogram_similarity_multi(frame, self.reference_hists)
            if similarity >= self.dedup_similarity_threshold:
                _debug_log(f"既知のバリエーションと一致（類似度={similarity:.3f}）のため保存をスキップ")
                return

        self.template_dir.mkdir(parents=True, exist_ok=True)
        existing = list(self.template_dir.glob("template_*.png"))
        next_index = len(existing) + 1
        path = self.template_dir / f"template_{next_index:02d}.png"
        cv2.imwrite(str(path), frame)
        self.reference_hists.append(compute_hs_histogram(frame))
        _log(f"新しいロード画面のバリエーションを自動収集しました: {path}")



# ---------------------------------------------------------------------------
# Phase 2: 試合前シーケンス検出
# （LoadingSequenceDetector.COMPLETE の後に続く、以下のシーケンスを追跡する）
#   Alpha初期ビュー → 暗転 → Beta初期ビュー → 暗転 → カウントダウン → 暗転（試合開始）
# ---------------------------------------------------------------------------

class PreMatchSequenceState(Enum):
    WAITING_INITIAL_ALPHA = auto()   # LoadingSequence.COMPLETE直後。Alpha初期ビューを待機
    WAITING_DARK_1 = auto()          # Alpha初期ビュー確定後。Alpha→Betaへの暗転を待機
    WAITING_INITIAL_BETA = auto()    # 1回目の暗転後。Beta初期ビューを待機
    WAITING_DARK_2 = auto()          # Beta初期ビュー確定後。Beta→カウントダウンへの暗転を待機
    COLLECTING_COUNTDOWN = auto()    # 2回目の暗転後。カウントダウン画面フレームを収集中
    COMPLETE = auto()                # カウントダウン終了（最後の暗転）= 試合開始確定
    TIMEOUT = auto()                 # タイムアウト


@dataclass
class PreMatchSequenceStepResult:
    state: PreMatchSequenceState
    brightness: float
    just_transitioned: bool          # このフレームで状態が変化したか
    countdown_frames_collected: int  # COLLECTING_COUNTDOWN 区間の収集フレーム数


class PreMatchSequenceDetector:
    """LoadingSequenceDetector.COMPLETE 直後に生成し、以後フレームを渡し続けることで
    Alpha初期ビュー → 暗転 → Beta初期ビュー → 暗転 → カウントダウン → 暗転 のシーケンスを追跡する。

    カウントダウン区間のフレームは内部バッファ（collected_countdown_frames）に自動蓄積する。
    COMPLETE 到達後、呼び出し元は collected_countdown_frames をマップ判定に使用する。

    使い方の想定：
        detector = PreMatchSequenceDetector()
        while True:
            frame = capture_frame_with_fallback()
            result = detector.step(frame)
            if result.state == PreMatchSequenceState.COMPLETE:
                frames = detector.collected_countdown_frames
                break
            elif result.state == PreMatchSequenceState.TIMEOUT:
                break
            time.sleep(0.2)
    """

    def __init__(
        self,
        dark_threshold: float = DEFAULT_DARK_THRESHOLD,
        view_confirm_sec: float = DEFAULT_CONFIRM_SEC,
        view_confirm_frames: int = DEFAULT_CONFIRM_MIN_FRAMES,
        countdown_collect_sec: float = DEFAULT_COUNTDOWN_COLLECT_SEC,
        min_countdown_frames: int = DEFAULT_COUNTDOWN_MIN_FRAMES,
        timeout_sec: float = 90.0,
    ):
        """
        Args:
            dark_threshold:        暗転判定の輝度しきい値（LoadingSequenceDetector と共通）
            view_confirm_sec:      初期ビューとして確定するために、明るい画面が続く必要のある秒数
            view_confirm_frames:   その間に必要な最少の枚数
            countdown_collect_sec: カウントダウンの画面をこの秒数集めたら（かつ min_countdown_frames 枚以上）即 COMPLETE に遷移する
                                   （最終暗転の検知に依存しない方式）
            min_countdown_frames:  カウントダウンの画面の最少の枚数
            timeout_sec:           WAITING_INITIAL_ALPHA 開始からのタイムアウト（秒）
        """
        self.dark_threshold = dark_threshold
        self.view_confirm_sec = view_confirm_sec
        self.view_confirm_frames = view_confirm_frames
        self.countdown_collect_sec = countdown_collect_sec
        self.min_countdown_frames = min_countdown_frames
        self.timeout_sec = timeout_sec

        self.state = PreMatchSequenceState.WAITING_INITIAL_ALPHA
        self._streak = 0
        self._streak_since: Optional[float] = None
        self._countdown_since: Optional[float] = None
        self._start_time: Optional[float] = None
        self.collected_countdown_frames: list[np.ndarray] = []
        self._view_confirmed_at: Optional[float] = None  # 直前に Alpha / Beta の紹介を確定した時刻（時間での補いに使う）
        self._hud_streak = 0             # 対戦の画面が続けて見えた回数
        self._wait_frames = 0            # 暗転を待つ間に撮れた枚数（打ち切ったときの調査用）
        self._wait_min_brightness = 255.0
        self._wait_since: Optional[float] = None

    def reset(self) -> None:
        """シーケンスを最初からやり直す"""
        self.state = PreMatchSequenceState.WAITING_INITIAL_ALPHA
        self._streak = 0
        self._streak_since = None
        self._countdown_since = None
        self._start_time = None
        self.collected_countdown_frames = []
        self._view_confirmed_at = None
        self._hud_streak = 0
        self._wait_frames = 0
        self._wait_min_brightness = 255.0
        self._wait_since = None

    def is_dark(self, img_bgr: np.ndarray, brightness: float) -> tuple[bool, bool]:
        """(暗転か, 一様な暗い画面として暗転とみなしたか)"""
        if brightness < self.dark_threshold:
            return True, False
        if brightness >= UNIFORM_DARK_MAX_BRIGHTNESS:
            return False, False
        gray = cv2.cvtColor(img_bgr[::4, ::4], cv2.COLOR_BGR2GRAY)
        uniform = float(gray.std()) < UNIFORM_DARK_MAX_STD
        return uniform, uniform

    def _hud_seen(self, img_bgr: np.ndarray) -> bool:
        """対戦の画面（上の左の見出しの、陣営の色の縦棒）が PRE_MATCH_HUD_CONFIRM_FRAMES 回続けて見えたか"""
        from ravens_record_core.detectors.match_end import read_own_side
        self._hud_streak = self._hud_streak + 1 if read_own_side(img_bgr) else 0
        return self._hud_streak >= PRE_MATCH_HUD_CONFIRM_FRAMES

    def _start_countdown(self, now: float, img_bgr: np.ndarray, message: str) -> None:
        self.state = PreMatchSequenceState.COLLECTING_COUNTDOWN
        self._streak, self._streak_since = 0, None
        self._countdown_since = None
        _log(message)

    def _wait_stats(self, now: float) -> str:
        """暗転を待つ間の様子（打ち切ったときにログに残す。暗転を撮り逃したのか、暗転が来なかったのかの手がかり）"""
        seconds = max(now - (self._wait_since or now), 1e-6)
        return (f"この間の最小の輝度={self._wait_min_brightness:.1f}、撮影 {self._wait_frames} 枚"
                f"（{self._wait_frames / seconds:.1f} 枚/秒）")

    @property
    def waiting_for_dark(self) -> bool:
        """暗転を待っている段階か（呼び出し元は、この間はこまめに画面を見る。暗転は 0.4〜1 秒と短いため）"""
        return self.state in (PreMatchSequenceState.WAITING_DARK_1, PreMatchSequenceState.WAITING_DARK_2)

    def _view_confirmed(self, is_dark: bool, now: float) -> bool:
        """明るい画面が view_confirm_sec 秒（かつ view_confirm_frames 枚）続いたか。暗い画面で数え直す"""
        if is_dark:
            self._streak, self._streak_since = 0, None
            return False
        if self._streak == 0:
            self._streak_since = now
        self._streak += 1
        return self._streak >= self.view_confirm_frames and now - self._streak_since >= self.view_confirm_sec

    def step(self, img_bgr: np.ndarray, now: Optional[float] = None) -> PreMatchSequenceStepResult:
        """1フレーム分の画像を渡して状態を更新する（now: 撮影した時刻。省略時は time.monotonic()）"""
        now = time.monotonic() if now is None else now
        brightness = mean_brightness(img_bgr)
        is_dark, uniform_dark = self.is_dark(img_bgr, brightness)
        dark_note = "（一様な暗い画面として）" if uniform_dark else ""
        previous_state = self.state

        # 暗転を待つ間の様子（Alpha の確定から、カウントダウンに入るまで）
        if self.state in (PreMatchSequenceState.WAITING_DARK_1, PreMatchSequenceState.WAITING_INITIAL_BETA,
                          PreMatchSequenceState.WAITING_DARK_2):
            if self._wait_since is None:
                self._wait_since = now
            self._wait_frames += 1
            self._wait_min_brightness = min(self._wait_min_brightness, brightness)
            # 暗転を撮れていなくても、対戦の画面が出たら、カウントダウンに入ったとみなす
            if not is_dark and self._hud_seen(img_bgr):
                self._start_countdown(now, img_bgr, f"[試合前シーケンス] 暗転を撮れないまま、対戦の画面（上の見出し）が出たため、"
                                                    f"カウントダウンフレームを収集します（状態={self.state.name}、{self._wait_stats(now)}）。")

        # タイムアウト計測開始（最初の step 呼び出し時）
        if self._start_time is None:
            self._start_time = now

        # タイムアウト判定
        if self.state not in (PreMatchSequenceState.COMPLETE, PreMatchSequenceState.TIMEOUT):
            if now - self._start_time >= self.timeout_sec:
                self.state = PreMatchSequenceState.TIMEOUT
                _log("[試合前シーケンス] タイムアウト: 規定時間内にシーケンスが完了しませんでした。")

        if self.state == PreMatchSequenceState.WAITING_INITIAL_ALPHA:
            if self._view_confirmed(is_dark, now):
                self.state = PreMatchSequenceState.WAITING_DARK_1
                self._streak, self._streak_since = 0, None
                self._view_confirmed_at = now
                _log("Alpha初期ビューを確定しました。暗転を待ちます。")

        elif self.state == PreMatchSequenceState.WAITING_DARK_1:
            if is_dark:
                self.state = PreMatchSequenceState.WAITING_INITIAL_BETA
                self._streak, self._streak_since = 0, None
                _log(f"1回目の暗転を検出しました（Alpha→Beta遷移）{dark_note}。Beta初期ビューを待ちます。")
            elif now - self._view_confirmed_at >= PRE_MATCH_DARK1_FALLBACK_SEC:
                # 1 回目の暗転を見逃した（短い・真っ黒にならない）。いまは Beta の紹介が出ているので、2 回目の暗転を待つ
                self.state = PreMatchSequenceState.WAITING_DARK_2
                self._streak, self._streak_since = 0, None
                self._view_confirmed_at = now
                _log(f"Alpha初期ビューの確定から{PRE_MATCH_DARK1_FALLBACK_SEC:.0f}秒たっても暗転が来ないため、1回目の暗転を見逃したとみなし、"
                     "2回目の暗転を待ちます（輝度=" f"{brightness:.1f}）。")

        elif self.state == PreMatchSequenceState.WAITING_INITIAL_BETA:
            if self._view_confirmed(is_dark, now):
                self.state = PreMatchSequenceState.WAITING_DARK_2
                self._streak, self._streak_since = 0, None
                self._view_confirmed_at = now
                _log("Beta初期ビューを確定しました。暗転を待ちます。")

        elif self.state == PreMatchSequenceState.WAITING_DARK_2:
            if is_dark:
                self.state = PreMatchSequenceState.COLLECTING_COUNTDOWN
                self._streak, self._streak_since = 0, None
                self._countdown_since = None
                _log(f"2回目の暗転を検出しました（Beta→カウントダウン遷移）{dark_note}。カウントダウンフレームを収集します。")
            elif now - self._view_confirmed_at >= PRE_MATCH_DARK2_GIVEUP_SEC:
                # 2 回目の暗転も来ない: もう対戦が始まっている（カウントダウンの画面は取れない）。待たずに打ち切る
                self.state = PreMatchSequenceState.TIMEOUT
                _log(f"[試合前シーケンス] {PRE_MATCH_DARK2_GIVEUP_SEC:.0f}秒たっても2回目の暗転が来ないため、打ち切ります"
                     f"（輝度={brightness:.1f}、{self._wait_stats(now)}）。")

        if self.state == PreMatchSequenceState.COLLECTING_COUNTDOWN:
            if not is_dark:
                # 明るいフレームをバッファに追加
                if self._countdown_since is None:
                    self._countdown_since = now
                self.collected_countdown_frames.append(img_bgr.copy())
                # 規定の秒数・枚数を集めた → 最終暗転を待たず即確定
                if (len(self.collected_countdown_frames) >= self.min_countdown_frames
                        and now - self._countdown_since >= self.countdown_collect_sec):
                    self.state = PreMatchSequenceState.COMPLETE
                    _log(f"カウントダウンフレームを{len(self.collected_countdown_frames)}枚収集しました。"
                         f"マップ判定を確定します。")
            else:
                # 暗転でも確定（最終暗転が先に来た場合のフォールバック）
                if self.collected_countdown_frames:
                    self.state = PreMatchSequenceState.COMPLETE
                    _log(f"カウントダウン終了の暗転を検出しました。"
                         f"収集フレーム数: {len(self.collected_countdown_frames)}")

        just_transitioned = self.state != previous_state
        return PreMatchSequenceStepResult(
            state=self.state,
            brightness=brightness,
            just_transitioned=just_transitioned,
            countdown_frames_collected=len(self.collected_countdown_frames),
        )


if __name__ == "__main__":
    # 単体テスト用の簡易デモ：シーケンス検出器を動かし、状態遷移と
    # 自動収集されたテンプレート数をログ表示する。
    #
    # 本来は「対戦開始します」検出後にdetector.step()を呼び始める想定だが、
    # 単体確認用にここでは常時ポーリングし、COMPLETE到達後は自動でreset()する。
    _log("シーケンス検出のデモを開始します（Ctrl+C で停止）")
    detector = LoadingSequenceDetector()

    while True:
        frame = capture_frame_with_fallback()
        if frame is not None:
            result = detector.step(frame)
            if result.just_transitioned:
                _log(f"状態遷移: {result.state.name}（輝度={result.brightness:.1f}）")
            if result.state == SequenceState.COMPLETE:
                detector.reset()
        time.sleep(0.1)
