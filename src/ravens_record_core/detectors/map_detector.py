"""
AC6 マップ自動判定モジュール（Phase 2 v2）

PreMatchSequenceDetector（loading_sequence_detector.py に実装）が収集した
カウントダウン画面フレームから、HSV カラーヒストグラム＋最近傍探索（カイ二乗距離）で
マップを自動判定する。

■ アーキテクチャ（本モジュール内）
  MapReferencePool      … labels_pool.json の読み込みと管理
  FeatureExtractor      … ROI 切り出し + HSV ヒストグラム抽出
  MapCandidateScorer    … プールへの最近傍探索（カイ二乗距離）
  ObservationAggregator … 複数フレームのヒストグラム平均化 + 信頼度判定
  DetectionLogger       … 判定結果のログ出力
  MapDetectionSession   … 上記を束ねた 1 試合単位のセッション管理

■ 監視ループ（monitor/map_detection.py）との接続
  LoadingSequenceDetector.COMPLETE 後に MapDetectionSession を生成し、
  以後フレームを step() で渡し続ける。
  PreMatchSequenceState.COMPLETE に達した時点で finalize() を呼び
  MatchMapResult を確定する。
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from ravens_record_core import logger, paths
from ravens_record_core.system import screencap
from ravens_record_core.detectors.loading_sequence_detector import (
    DEFAULT_DARK_THRESHOLD,
    PreMatchSequenceDetector,
    PreMatchSequenceState,
)

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

MAP_NAMES = [
    "汚染市街A",
    "汚染市街B",
    "LOCステーション",
    "ウォッチポイント・アルファ",
    "ボナデア砂漠",
]

# HSVヒストグラム抽出用 ROI（相対座標）: MapLabeling.py と共通
ROI: tuple[float, float, float, float] = (0.0, 0.05, 1.0, 0.55)

# 特徴量の方式バージョン。ROI・ビン数・色空間など FeatureExtractor の仕様を変えたら必ず上げる。
# 追加の見本（template_cache_path）は同じバージョンのものだけを使う（方式が違う特徴量は比較できないため）
FEATURE_VERSION: str = "hsv_h30s32_roi_v1"

# 同梱データ（labels_pool.json）は paths.bundled_data_dir()、実行時に書き込むデータは paths.data_dir() の下。

def template_cache_path() -> Path:
    """追加のマップ見本（data_dir/map_templates_cache.json）。あれば、同梱の labels_pool.json より優先する。
    形: {"feature_version": FEATURE_VERSION, "shared": {マップ名: [特徴量, ...]}, "own": {...}}。
    使う側のアプリが、集めた見本をここに書く（ライブラリは読むだけ）"""
    return paths.data_dir() / "map_templates_cache.json"


def map_frame_dir() -> Path:
    """試合ごとの判定画面（手元に残す調査用）"""
    return paths.logs_dir() / "map_frames"


def ambiguous_sample_dir() -> Path:
    """A/B の区別がつきにくかった画面（手元に残す調査用）"""
    return paths.logs_dir() / "ambiguous_ab"


MAP_FRAME_WIDTH: int = 960

# 信頼度判定の閾値（マージンベースの map_confidence 0.0〜1.0）
# カイ二乗距離の絶対値は画面の明るさ等で変動するため、1位と2位の相対的な差で信頼度を測る
CONFIDENCE_HIGH: float = 0.6   # >= 0.6       → 高（AUTO_CONFIRMED）。将来のテンプレート学習候補の目安
CONFIDENCE_LOW: float  = 0.3   # 0.3 〜 0.6   → 中 / < 0.3 → 低（どちらも AUTO_LOW_CONFIDENCE）
                               # 低信頼度でも最有力候補は必ず保存する（null にしない）

# 汚染市街A/B の取り違え対策（二次判定）
AMBIGUOUS_PAIR: frozenset[str] = frozenset({"汚染市街A", "汚染市街B"})
AMBIGUOUS_THRESHOLD: float = 0.15  # 要チューニング。上位2つが A/B かつこれ未満で二次判定を発動

# LOCステーション / 汚染市街B の取り違え対策（二次判定）
# どちらも色の薄い灰色の景色で、色合い・鮮やかさのヒストグラム（明るさを含まない）では見分けにくい
# （2026-10: LOC の試合を汚染市街B と判定した）。空の明るさがまったく違うため、空の暗い画素の割合で決め直す:
#   LOC = 黒い宇宙の空（0.40〜0.89） / 汚染市街B = 明るい灰色の曇り空（0.01〜0.03）。見本・過去の判定画面・録画で計測
LOC_B_PAIR: frozenset[str] = frozenset({"LOCステーション", "汚染市街B"})
SKY_ROI_Y: tuple[float, float] = (0.05, 0.25)   # 空の範囲（画面の上から。横は全体）
SKY_DARK_V: int = 50                            # 暗い画素とみなす明るさ（HSV の V、0〜255）
SKY_DARK_RATIO_THRESHOLD: float = 0.2           # 暗い画素の割合がこれ以上なら LOC

# 二次判定の発動時にキャリブレーション用フレームを保存するディレクトリは ambiguous_sample_dir()

# MapDetectionSession のポーリング間隔・タイムアウト
MAP_DETECTION_POLL_INTERVAL_SEC: float = 0.2
MAP_DETECTION_TIMEOUT_SEC: float       = 90.0


def _log(message: str) -> None:
    logger.log(message)


# ---------------------------------------------------------------------------
# データモデル
# ---------------------------------------------------------------------------

class DetectionStatus(Enum):
    AUTO_CONFIRMED      = "AUTO_CONFIRMED"       # 高信頼度で自動確定
    AUTO_LOW_CONFIDENCE = "AUTO_LOW_CONFIDENCE"  # 中信頼度、ユーザー確認を推奨
    UNKNOWN             = "UNKNOWN"              # 判定不能（プール不足 or 未知マップ）
    USER_CORRECTED      = "USER_CORRECTED"       # ユーザーが修正済み


@dataclass
class ObservationResult:
    """1 観測区間（カウントダウン等）ごとの判定詳細"""
    observation_type: str           # "COUNTDOWN"（将来的に ALPHA / BETA も追加予定）
    frames_captured: int            # 収集フレーム数
    frames_analyzed: int            # ヒストグラム計算に使用したフレーム数
    top_map: Optional[str]          # 最近傍マップ名
    top_distance: float             # 最小カイ二乗距離
    second_map: Optional[str]       # 2 番目に近いマップ名
    second_distance: float          # 2 番目のカイ二乗距離
    margin: float                   # second_distance - top_distance
    candidates: list[tuple[str, float]] = field(default_factory=list)  # 全マップの最小距離（昇順）
    feature: Optional[np.ndarray] = None       # 判定に使った平均ヒストグラム（見本を増やすときの材料）
    feature_version: str = FEATURE_VERSION


@dataclass
class MatchMapResult:
    """1 試合分のマップ判定結果"""
    auto_detected_map_id: Optional[str]
    confidence: str                                  # "高" / "中" / "低" / "不明"
    status: DetectionStatus
    map_confidence: Optional[float] = None           # 0.0〜1.0（matches.map_confidence に格納）
    user_corrected_map_id: Optional[str] = None
    observation_results: list[ObservationResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# FeatureExtractor
# ---------------------------------------------------------------------------

class FeatureExtractor:
    """ROI 切り出し + HSV カラーヒストグラム抽出。
    MapLabeling.py の extract_feature() と同一方式。"""

    def __init__(self, roi: tuple[float, float, float, float] = ROI):
        self.roi = roi

    def extract(self, image: np.ndarray) -> np.ndarray:
        image, _ = screencap.game_area(image)  # 黒い帯の内側に対する割合で切る
        h, w = image.shape[:2]
        x0, y0, x1, y1 = self.roi
        roi_img = image[int(h * y0):int(h * y1), int(w * x0):int(w * x1)]
        hsv = cv2.cvtColor(roi_img, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [30, 32], [0, 180, 0, 256])
        cv2.normalize(hist, hist, 0, 1, cv2.NORM_MINMAX)
        return hist.flatten()


# ---------------------------------------------------------------------------
# MapReferencePool
# ---------------------------------------------------------------------------

class MapReferencePool:
    """参照プールを読み込んで管理する。

    優先順位:
      1. 追加の見本（template_cache_path() の shared。あれば同梱データより優先する）
      2. 1 が空・未取得なら同梱の labels_pool.json
      + いずれの場合も、追加の見本の own（利用者が自分で直した判定）を足す
    cache_path に None を渡すと、追加の見本を使わない（同梱データだけ）。
    """

    _DEFAULT_CACHE = object()

    def __init__(self, pool_path: Optional[Path] = None, cache_path: "Optional[Path] | object" = _DEFAULT_CACHE):
        if pool_path is None:
            pool_path = paths.bundled_data_dir() / "labels_pool.json"
        self.pool_path = pool_path
        self.cache_path = template_cache_path() if cache_path is MapReferencePool._DEFAULT_CACHE else cache_path
        self.pool: dict[str, list[list[float]]] = self._load_with_remote()

    def _load_with_remote(self) -> dict[str, list[list[float]]]:
        shared, own = self._load_remote_cache()
        if shared:
            pool = shared
            total = sum(len(v) for v in pool.values())
            _log(f"[マップ判定] 追加の見本を読み込みました: {len(pool)} マップ / 合計 {total} サンプル")
        else:
            pool = self._load()

        own_total = 0
        for map_name, vectors in own.items():
            pool.setdefault(map_name, []).extend(vectors)
            own_total += len(vectors)
        if own_total:
            _log(f"[マップ判定] 自分で直した判定の見本を追加: {own_total} サンプル")
        return pool

    def _load_remote_cache(self) -> tuple[dict[str, list[list[float]]], dict[str, list[list[float]]]]:
        """キャッシュから (shared, own) を返す。無い・壊れている・方式バージョン違いなら空。"""
        if self.cache_path is None or not self.cache_path.exists():
            return {}, {}
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception as e:
            _log(f"[マップ判定] 追加の見本を読めませんでした（同梱データを使用）: {e}")
            return {}, {}
        if data.get("feature_version") != FEATURE_VERSION:
            return {}, {}
        return data.get("shared") or {}, data.get("own") or {}

    def _load(self) -> dict[str, list[list[float]]]:
        if self.pool_path.exists():
            data: dict[str, list[list[float]]] = json.loads(
                self.pool_path.read_text(encoding="utf-8")
            )
            total = sum(len(v) for v in data.values())
            _log(f"[マップ判定] 参照プール読み込み完了: "
                 f"{len(data)} マップ / 合計 {total} サンプル ({self.pool_path.name})")
            return data
        _log(f"[マップ判定] 参照プールが見つかりません: {self.pool_path}"
             " （判定結果は UNKNOWN になります）")
        return {}

    @property
    def is_empty(self) -> bool:
        return not any(v for v in self.pool.values())


# ---------------------------------------------------------------------------
# MapCandidateScorer
# ---------------------------------------------------------------------------

class MapCandidateScorer:
    """プールに対して最近傍探索（カイ二乗距離）を行い、マップ名ごとの最小距離を返す。"""

    def score(
        self,
        feature: np.ndarray,
        pool: dict[str, list[list[float]]],
    ) -> list[tuple[str, float]]:
        """各マップの最小カイ二乗距離を距離昇順で返す。

        Returns:
            [(map_name, min_distance), ...] 距離昇順
        """
        feat = feature.astype(np.float32)
        map_min: dict[str, float] = {}
        for map_name, vectors in pool.items():
            if not vectors:
                continue
            dists = [
                cv2.compareHist(feat, np.array(v, dtype=np.float32), cv2.HISTCMP_CHISQR)
                for v in vectors
            ]
            map_min[map_name] = min(dists)
        return sorted(map_min.items(), key=lambda x: x[1])


# ---------------------------------------------------------------------------
# ObservationAggregator
# ---------------------------------------------------------------------------

class ObservationAggregator:
    """複数フレームのヒストグラムを平均化して 1 特徴量に集約し、
    最近傍探索で ObservationResult を返す。"""

    def __init__(self) -> None:
        self.extractor = FeatureExtractor()
        self.scorer = MapCandidateScorer()

    def aggregate(
        self,
        frames: list[np.ndarray],
        pool: dict[str, list[list[float]]],
        observation_type: str = "COUNTDOWN",
    ) -> ObservationResult:
        frames_captured = len(frames)

        if not frames or not any(v for v in pool.values()):
            return ObservationResult(
                observation_type=observation_type,
                frames_captured=frames_captured,
                frames_analyzed=0,
                top_map=None,
                top_distance=float("inf"),
                second_map=None,
                second_distance=float("inf"),
                margin=0.0,
            )

        # 全フレームのヒストグラムを平均化して 1 特徴量に集約
        features = [self.extractor.extract(f) for f in frames]
        mean_feature = np.mean(features, axis=0).astype(np.float32)

        candidates = self.scorer.score(mean_feature, pool)

        top_map,    top_dist    = candidates[0] if len(candidates) >= 1 else (None, float("inf"))
        second_map, second_dist = candidates[1] if len(candidates) >= 2 else (None, float("inf"))
        margin = (second_dist - top_dist) if second_dist != float("inf") else 0.0

        return ObservationResult(
            observation_type=observation_type,
            frames_captured=frames_captured,
            frames_analyzed=len(features),
            top_map=top_map,
            top_distance=top_dist,
            second_map=second_map,
            second_distance=second_dist,
            margin=margin,
            candidates=candidates,
            feature=mean_feature,
        )


# ---------------------------------------------------------------------------
# 信頼度算出・汚染市街A/B 二次判定
# ---------------------------------------------------------------------------

def compute_map_confidence(distances: dict[str, float]) -> tuple[Optional[str], float]:
    """1位と2位の距離の差（マージン）から信頼度を算出する。

    Args:
        distances: {マップ名: カイ二乗距離}（全テンプレートとの最小距離）

    Returns:
        (最有力マップ名, confidence 0.0〜1.0)。候補が無ければ (None, 0.0)
    """
    sorted_maps = sorted(distances.items(), key=lambda x: x[1])
    if not sorted_maps:
        return None, 0.0
    best_name, best_dist = sorted_maps[0]
    if len(sorted_maps) < 2:
        # 比較対象が無いため確信度は測れない
        return best_name, 0.0
    second_dist = sorted_maps[1][1]

    # マージン = 2位との差を2位の距離で正規化（0〜1 にクリップ）
    if not np.isfinite(second_dist) or second_dist <= 0:
        return best_name, 0.0
    margin = (second_dist - best_dist) / second_dist
    return best_name, float(max(0.0, min(1.0, margin)))


def resolve_map(
    distances: dict[str, float],
    frames: list[np.ndarray],
) -> tuple[Optional[str], float]:
    """マージンベースで最有力マップを決め、汚染市街A/B が僅差のときと、LOC / 汚染市街B が上位 2 つのときに二次判定を行う。"""
    best_name, confidence = compute_map_confidence(distances)

    top2_names = {name for name, _ in sorted(distances.items(), key=lambda x: x[1])[:2]}
    if top2_names == LOC_B_PAIR:
        # 信頼度に関係なく行う（ヒストグラムでは高い信頼度のまま取り違えることがあるため）
        resolved = disambiguate_loc_b(frames)
        if resolved is not None:
            resolved_name, sub_confidence, ratio = resolved
            if resolved_name != best_name:
                _log(f"[マップ判定] LOC / 汚染市街B の二次判定: 空の暗い画素の割合={ratio:.2f} → {resolved_name} に決め直します"
                     f"（ヒストグラムでは {best_name}、confidence={confidence:.3f}）")
                return resolved_name, sub_confidence
            _log(f"[マップ判定] LOC / 汚染市街B の二次判定: 空の暗い画素の割合={ratio:.2f} → {resolved_name}（ヒストグラムと一致）")
            return resolved_name, max(confidence, sub_confidence)

    if top2_names == AMBIGUOUS_PAIR and confidence < AMBIGUOUS_THRESHOLD:
        _log(f"[マップ判定] 汚染市街A/B が僅差のため二次判定を実行します（confidence={confidence:.3f}）")
        _save_ambiguous_sample(frames, best_name, confidence)
        resolved = disambiguate_a_b(frames)
        if resolved is not None:
            resolved_name, sub_confidence = resolved
            # 二次判定が成功した場合は confidence を底上げ
            return resolved_name, max(confidence, sub_confidence)
        _log("[マップ判定] 二次判定は未確定のため、ヒストグラムの最有力候補を採用します")

    return best_name, confidence


def sky_dark_ratio(frame: np.ndarray) -> float:
    """空の範囲（SKY_ROI_Y）のうち、暗い画素（V < SKY_DARK_V）の割合（0〜1）"""
    frame, _ = screencap.game_area(frame)
    h = frame.shape[0]
    sky = frame[int(h * SKY_ROI_Y[0]):int(h * SKY_ROI_Y[1])]
    if sky.size == 0:
        return 0.0
    v = cv2.cvtColor(sky, cv2.COLOR_BGR2HSV)[:, :, 2]
    return float((v < SKY_DARK_V).mean())


def disambiguate_loc_b(frames: list[np.ndarray]) -> Optional[tuple[str, float, float]]:
    """LOCステーション / 汚染市街B を、空の暗い画素の割合で決める。

    カウントダウンの始めは暗転から明るくなる途中で、どのマップも暗く写るため、集めた画面の後半だけを使う（中央値）。

    Returns:
        (マップ名, confidence, 暗い画素の割合) / 画面が無ければ None
    """
    usable = frames[len(frames) // 2:]
    if not usable:
        return None
    ratio = float(np.median([sky_dark_ratio(f) for f in usable]))
    name = "LOCステーション" if ratio >= SKY_DARK_RATIO_THRESHOLD else "汚染市街B"
    # しきい値から離れているほど確か（実測では LOC 0.40 以上 / 汚染市街B 0.03 以下）
    confidence = float(min(1.0, abs(ratio - SKY_DARK_RATIO_THRESHOLD) / SKY_DARK_RATIO_THRESHOLD))
    return name, confidence, ratio


def disambiguate_a_b(frames: list[np.ndarray]) -> Optional[tuple[str, float]]:
    """汚染市街A/B をサブROI の簡易判定で確定させる。

    Returns:
        (マップ名, confidence) / 判定できない場合は None（呼び出し側で元の結果を採用）

    TODO: ROI 座標とピクセル判定の閾値は、実際に A/B が僅差になった画面
          （ambiguous_sample_dir() に保存される）を見てキャリブレーションしてから実装する。
          候補: 空/山エリアの白〜グレー比率（A）、アンテナ付近の赤色ピクセル比率（B）など。
    """
    return None


def _save_map_frame(frames: list[np.ndarray], map_name: Optional[str], confidence: Optional[float]) -> None:
    """判定に使った画面（中央フレーム）を縮小してローカルに保存する。
    判定を直すか迷ったときに見返す用。失敗しても判定は止めない。"""
    if not frames:
        return
    try:
        frame_dir = map_frame_dir()
        frame_dir.mkdir(parents=True, exist_ok=True)
        frame = frames[len(frames) // 2]
        h, w = frame.shape[:2]
        if w > MAP_FRAME_WIDTH:
            frame = cv2.resize(frame, (MAP_FRAME_WIDTH, int(h * MAP_FRAME_WIDTH / w)), interpolation=cv2.INTER_AREA)
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        conf = f"{confidence:.3f}" if confidence is not None else "na"
        path = frame_dir / f"{ts}_{map_name or '不明'}_{conf}.jpg"
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if ok:
            buf.tofile(str(path))  # 日本語パス対応のため imwrite ではなく tofile
    except Exception as e:
        _log(f"[マップ判定] 判定画面の保存に失敗しました: {e}")


def _save_ambiguous_sample(frames: list[np.ndarray], best_name: Optional[str], confidence: float) -> None:
    """二次判定のキャリブレーション用に、代表フレーム（中央）を保存する。失敗しても判定は止めない。"""
    if not frames:
        return
    try:
        sample_dir = ambiguous_sample_dir()
        sample_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = sample_dir / f"{ts}_{best_name}_{confidence:.3f}.png"
        ok, buf = cv2.imencode(".png", frames[len(frames) // 2])
        if ok:
            buf.tofile(str(path))  # 日本語パス対応のため imwrite ではなく tofile
            _log(f"[マップ判定] 二次判定用サンプルを保存しました: {path.name}")
    except Exception as e:
        _log(f"[マップ判定] 二次判定用サンプルの保存に失敗しました: {e}")


# ---------------------------------------------------------------------------
# DetectionLogger
# ---------------------------------------------------------------------------

class DetectionLogger:
    """判定結果を 監視ループと同じログ形式で出力する。"""

    def log_result(self, result: MatchMapResult) -> None:
        map_name   = result.auto_detected_map_id or "不明"
        confidence = result.confidence

        obs = result.observation_results[0] if result.observation_results else None
        if obs and obs.top_distance != float("inf"):
            dist_part   = f"distance={obs.top_distance:.1f}"
            margin_part = f"2位との差={obs.margin:.1f}" if obs.second_map else ""
            detail = f"、{dist_part}、{margin_part}" if margin_part else f"、{dist_part}"
        else:
            detail = ""

        if result.map_confidence is not None:
            confidence = f"{confidence} {result.map_confidence:.3f}"

        _log(f"[マップ判定] 判定結果: {map_name}（信頼度：{confidence}{detail}）")

        if result.status == DetectionStatus.AUTO_LOW_CONFIDENCE:
            _log("[マップ判定] 信頼度が低めのため、ユーザー確認を推奨します")
        elif result.status == DetectionStatus.UNKNOWN:
            _log("[マップ判定] マップを判定できませんでした（プール不足 or 未知マップ）")


# ---------------------------------------------------------------------------
# MapDetectionSession
# ---------------------------------------------------------------------------

class MapDetectionSession:
    """1 試合単位のマップ判定セッション。

    LoadingSequenceDetector.COMPLETE 直後に生成し、以後 step(frame) でフレームを渡し続ける。
    PreMatchSequenceState.COMPLETE に達した時点で finalize() を呼んで MatchMapResult を確定する。

    使い方の想定（monitor/map_detection.py から）:
        session = MapDetectionSession()
        while True:
            frame = capture_frame_with_fallback()
            result = session.step(frame)
            if result.state == PreMatchSequenceState.COMPLETE:
                map_result = session.finalize()
                break
            elif result.state == PreMatchSequenceState.TIMEOUT:
                break
            time.sleep(0.2)
    """

    def __init__(
        self,
        pool: Optional[MapReferencePool] = None,
        dark_threshold: float = DEFAULT_DARK_THRESHOLD,
        timeout_sec: float = MAP_DETECTION_TIMEOUT_SEC,
    ):
        self.pool = pool or MapReferencePool()
        self._pre_match_detector = PreMatchSequenceDetector(
            dark_threshold=dark_threshold,
            timeout_sec=timeout_sec,
        )
        self._aggregator = ObservationAggregator()
        self._logger     = DetectionLogger()
        self._result: Optional[MatchMapResult] = None

    @property
    def state(self) -> PreMatchSequenceState:
        return self._pre_match_detector.state

    def step(self, frame: np.ndarray, now: Optional[float] = None):
        """フレームを 1 枚渡して状態を更新する。PreMatchSequenceDetector に委譲（now: 撮影した時刻。省略時は現在）。"""
        return self._pre_match_detector.step(frame, now=now)

    @property
    def waiting_for_dark(self) -> bool:
        """暗転を待っている段階か（呼び出し元は、この間はこまめに画面を見る）"""
        return self._pre_match_detector.waiting_for_dark

    def finalize(self) -> MatchMapResult:
        """PreMatchSequenceState.COMPLETE 到達後に呼び出す。
        収集フレームからマップを判定して MatchMapResult を返す。"""
        frames = self._pre_match_detector.collected_countdown_frames
        obs = self._aggregator.aggregate(frames, self.pool.pool, "COUNTDOWN")

        # 信頼度・status の決定
        # 推測値が出せる限りは低信頼度でも最有力候補を保存し、精度の低さは map_confidence で示す
        map_confidence: Optional[float] = None
        if obs.top_map is None or self.pool.is_empty:
            auto_map, confidence, status = None, "不明", DetectionStatus.UNKNOWN
        else:
            auto_map, map_confidence = resolve_map(dict(obs.candidates), frames)
            if map_confidence >= CONFIDENCE_HIGH:
                confidence, status = "高", DetectionStatus.AUTO_CONFIRMED
            elif map_confidence >= CONFIDENCE_LOW:
                confidence, status = "中", DetectionStatus.AUTO_LOW_CONFIDENCE
            else:
                confidence, status = "低", DetectionStatus.AUTO_LOW_CONFIDENCE

        result = MatchMapResult(
            auto_detected_map_id=auto_map,
            confidence=confidence,
            status=status,
            map_confidence=map_confidence,
            observation_results=[obs],
        )
        self._logger.log_result(result)
        _save_map_frame(frames, auto_map, map_confidence)
        self._result = result
        return result
