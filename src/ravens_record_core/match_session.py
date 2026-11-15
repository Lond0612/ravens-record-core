"""
ランクマッチの試合の記録（MatchSession）

1試合分のデータを保持・管理する MatchSession クラスを提供する。

  - 対戦前: レート(before) / 昇格戦情報(before) を集める
  - 進行中: score_detector.py がスコアを追跡、マップ判定結果を受け取る
  - 対戦後: スコアを受け取り確定・保存し、セッションをクローズ

close() が完了するまで次の MatchSession を作ってはならない（呼び出し側の責務）。

試合ごとの JSON（logs/YYYY-MM-DD_HH-MM-SS_自分-相手_session.json）の中身（to_log_dict）:
  log_schema            この JSON の形の版（1）
  kind                  "rank"
  ended_at              試合の記録を閉じた日時（ISO 8601）
  score_basis           スコアの根拠（boxes: 試合中のマス / end_banner: 勝敗の表示 / result_screen: リザルト画面 / null）
  core_version          ravens-record-core のバージョン
  app_version           使う側のアプリのバージョン（Config.app_version。無ければ null）
  matched_at            試合日時（ISO 8601）
  map                   マップ名 / null
  map_confidence        判定信頼度 0.0〜1.0（1位と2位のマージン） / null
  map_status            判定ステータス文字列（AUTO_CONFIRMED 等。ローカル保存のみ）
  score                 スコア文字列 「自分-相手」
  result                WIN / LOSE / DRAW / null
  own_side              ALPHA / BETA / null（自他識別結果）
  own_rank              自分のランク / null
  opponent_rank         相手のランク / null
  rate_before           対戦前レート / null
  rate_position_before  対戦前の S ランクの順位 / null
  rate_after            対戦後レート / null
  rate_position_after   対戦後の S ランクの順位 / null
  rate_before_note      対戦前レートを読めなかった理由 / null
  rate_after_note       対戦後レートを読めなかった理由 / null
  capture_size          ゲームの画面の大きさ（幅x高さ） / null
  promotion_*           昇格戦の情報（未実装。いつも null）
  session_id            セッション識別子（matched_at ベース）
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Optional

from ravens_record_core import __version__, logger, paths

# 試合ごとの JSON の形の版（to_log_dict の log_schema）。項目を足すだけなら上げない（消す・意味を変えるときに上げる）
LOG_SCHEMA_VERSION = 1


class SessionState(Enum):
    """MatchSession のライフサイクル状態"""
    OPEN     = auto()  # セッション開始直後（対戦前データ収集中）
    IN_MATCH = auto()  # ScoreDetector 起動済み（試合進行中）
    CLOSED   = auto()  # 確定・保存済み（クローズ済み）


@dataclass
class MatchSession:
    """1試合分のデータを保持・管理する状態オブジェクト。

    設計書 3.2節に基づき、試合開始〜終了の明示的なライフサイクルを持つ。
    外部から直接フィールドに代入してデータを集積し、
    close() を呼ぶことで確定・保存・クローズを行う。

    使い方の想定:
        session = MatchSession.open()
        session.home_frame   = home_frame   # マッチング中に取得
        session.map_result   = map_result   # MapDetector から
        session.rank_result  = rank_result  # RankDetector から
        session.id_result    = id_result    # RankDetector から
        session.mark_match_started()        # ScoreDetector 起動と同時に
        session.score_my     = 2
        session.score_enemy  = 1
        session.close()                     # 確定・保存・クローズ
    """

    # --- ライフサイクル ---
    state:      SessionState = field(default=SessionState.OPEN, init=False)
    session_id: str          = field(default="", init=False)
    matched_at: Optional[datetime] = field(default=None, init=False)

    # --- 試合中データ（MapDetector）---
    map_result:  Optional[object] = field(default=None, init=False)  # MatchMapResult

    # --- 試合後データ（ScoreDetector）---
    score_my:       Optional[int] = field(default=None, init=False)
    score_enemy:    Optional[int] = field(default=None, init=False)
    score_log_path: Optional[str] = field(default=None, init=False)

    # --- 未実装フィールド（Phase 3: レート・昇格戦）---
    rate_before:             Optional[int]  = field(default=None, init=False)
    rate_after:              Optional[int]  = field(default=None, init=False)
    rate_position_before:    Optional[int]  = field(default=None, init=False)
    rate_position_after:     Optional[int]  = field(default=None, init=False)
    # レートを読めなかった理由（RateDetector.failure_reason の reason。読めたら None）。調査用
    rate_before_note:        Optional[str]  = field(default=None, init=False)
    rate_after_note:         Optional[str]  = field(default=None, init=False)
    # 撮った画面の大きさ（"1920x1080" など。16:9 以外の利用者の調査用）
    capture_size:            Optional[str]  = field(default=None, init=False)
    promotion_active:        Optional[bool] = field(default=None, init=False)
    promotion_wins_before:   Optional[int]  = field(default=None, init=False)
    promotion_matches_before:Optional[int]  = field(default=None, init=False)
    promotion_wins_after:    Optional[int]  = field(default=None, init=False)
    promotion_matches_after: Optional[int]  = field(default=None, init=False)

    # --- 保存先パス（close() 完了後に設定される）---
    saved_path: Optional[str] = field(default=None, init=False)

    # --- 試合ごとの JSON にだけ書く項目 ---
    ended_at: Optional[datetime] = field(default=None, init=False)     # close() の日時
    score_basis: Optional[str] = field(default=None, init=False)       # スコアの根拠（ScoreDetector の basis）
    app_version: Optional[str] = field(default=None, init=False)       # 使う側のアプリのバージョン

    # -------------------------------------------------------------------------
    # クラスメソッド
    # -------------------------------------------------------------------------

    @classmethod
    def open(cls) -> "MatchSession":
        """新しいセッションを開始して返す。

        matched_at（セッション開始日時）と session_id を自動生成する。
        「マッチングしています」を検出してホームカードを取得し始める
        タイミングで呼び出す。
        """
        session = cls()
        session.matched_at = datetime.now()
        session.session_id = session.matched_at.strftime("%Y%m%d_%H%M%S")
        _log_info(f"[MatchSession] セッション {session.session_id} を開始しました。")
        return session

    # -------------------------------------------------------------------------
    # ライフサイクルメソッド
    # -------------------------------------------------------------------------

    def mark_match_started(self) -> None:
        """ScoreDetector 起動と同時に呼び出す。状態を IN_MATCH に進める。

        OPEN 以外の状態で呼ばれた場合は警告ログを出すが続行する
        （誤呼び出しが検出できても処理は止めない）。
        """
        if self.state != SessionState.OPEN:
            _log_warn(
                f"[MatchSession] mark_match_started() を OPEN 以外の状態で呼び出しました: {self.state}"
            )
        self.state = SessionState.IN_MATCH
        _log_info(f"[MatchSession] セッション {self.session_id}: 試合開始 (IN_MATCH)。")

    def close(self) -> Optional[str]:
        """試合データを確定・保存し、セッションをクローズする。

        OPEN / IN_MATCH のどちらからでも呼び出せる
        （タイムアウトや異常終了時にも安全にクローズできるように）。

        Returns:
            保存先のファイルパス（保存成功時）、または None（スキップ時）
        """
        if self.state == SessionState.CLOSED:
            _log_warn(
                "[MatchSession] 既にクローズ済みのセッションに対して close() が呼ばれました。無視します。"
            )
            return self.saved_path

        self.state = SessionState.CLOSED
        self.ended_at = datetime.now()

        if self.score_my is None or self.score_enemy is None:
            _log_warn(
                f"[MatchSession] セッション {self.session_id}: "
                "スコアが未取得ですが、ファイル保存・連携を実行します。"
            )

        path = self._save()
        self.saved_path = path
        return path

    def close_without_score(self) -> None:
        """スコア取得なしで強制クローズする（通信切断・AC6 終了等の異常系）。

        状態を CLOSED にするだけで保存は行わない。
        呼び出し元は次のセッションを作成してよい。
        """
        if self.state == SessionState.CLOSED:
            return
        _log_warn(
            f"[MatchSession] セッション {self.session_id}: 強制クローズ（スコアなし）。"
            " MatchSession を破棄します。"
        )
        self.state = SessionState.CLOSED

    # -------------------------------------------------------------------------
    # プロパティ・内部ユーティリティ
    # -------------------------------------------------------------------------

    @property
    def is_closed(self) -> bool:
        return self.state == SessionState.CLOSED

    @property
    def result(self) -> str:
        """勝敗文字列を返す。

        スコアが取得できている場合はスコアで判定。
        スコア未取得の場合、レートや順位の変動から推測を試みる。
        それでも不明な場合は、仮として 'DRAW' を返す（いつも値があるように）。
        """
        if self.score_my is not None and self.score_enemy is not None:
            if self.score_my > self.score_enemy:
                return "WIN"
            if self.score_enemy > self.score_my:
                return "LOSE"
            return "DRAW"

        # レート変動で推測
        if self.rate_before is not None and self.rate_after is not None:
            if self.rate_after > self.rate_before:
                return "WIN"
            if self.rate_after < self.rate_before:
                return "LOSE"
            # レートが変わっていない: 試合が流れた（切断など）。順位は、ほかの人の試合でも動くので、勝敗の手がかりにしない
            # （以前は順位の 302 → 303 から LOSE としていた）
            return "DRAW"

        # 順位変動で推測（レートを読めなかったときだけ。Sランク: 順位の数値が小さいほうが上）
        if self.rate_position_before is not None and self.rate_position_after is not None:
            if self.rate_position_after < self.rate_position_before:
                return "WIN"
            if self.rate_position_after > self.rate_position_before:
                return "LOSE"

        return "DRAW"

    def _save(self) -> str:
        """試合データを統合 JSON としてローカルに保存する。

        保存先: logs/YYYY-MM-DD_HH-MM-SS_自分-相手_session.json
        score_detector.py の個別ログと同じディレクトリに出力する。
        """
        log_dir = str(paths.logs_dir())
        os.makedirs(log_dir, exist_ok=True)

        assert self.matched_at is not None

        my_str = str(self.score_my) if self.score_my is not None else "X"
        enemy_str = str(self.score_enemy) if self.score_enemy is not None else "X"

        filename = (
            f"{self.matched_at.strftime('%Y-%m-%d_%H-%M-%S')}"
            f"_{my_str}-{enemy_str}"
            f"_session.json"
        )
        path = os.path.join(log_dir, filename)

        # 途中で止まっても壊れたファイルを残さないよう、一時ファイルに書いてから置き換える
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_log_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

        _log_info(f"[MatchSession] セッション {self.session_id} を保存しました: {path}")
        return path

    def to_dict(self) -> dict:
        """セッションデータを JSON シリアライズ可能な dict に変換する。

        MapDetector / RankDetector の結果オブジェクトは型を直接 import せず
        getattr() で安全にフィールドを取り出す（循環 import を回避するため）。
        """
        # --- MapDetector の MatchMapResult からフィールドを取得 ---
        map_id: Optional[str] = None
        map_confidence: Optional[float] = None
        map_status: Optional[str] = None
        if self.map_result is not None:
            map_id = getattr(self.map_result, "auto_detected_map_id", None)
            # ユーザーが修正済みの場合は修正値を優先
            user_corrected = getattr(self.map_result, "user_corrected_map_id", None)
            if user_corrected:
                map_id = user_corrected
            raw_confidence = getattr(self.map_result, "map_confidence", None)
            if raw_confidence is not None:
                map_confidence = round(float(raw_confidence), 4)
            status_obj = getattr(self.map_result, "status", None)
            map_status = status_obj.value if status_obj is not None else None

        own_side: Optional[str] = None
        own_rank: Optional[str] = None
        opponent_rank: Optional[str] = None

        score_str: Optional[str] = (
            f"{self.score_my}-{self.score_enemy}"
            if self.score_my is not None and self.score_enemy is not None
            else None
        )

        return {
            "session_id":              self.session_id,
            "matched_at":              self.matched_at.isoformat() if self.matched_at else None,
            "map":                     map_id,
            "map_confidence":          map_confidence,
            "map_status":              map_status,
            "score":                   score_str,
            "result":                  self.result,
            "own_side":                own_side,
            "own_rank":                own_rank,
            "opponent_rank":           opponent_rank,
            "rate_before":             self.rate_before,
            "rate_position_before":    self.rate_position_before,
            "rate_after":              self.rate_after,
            "rate_position_after":     self.rate_position_after,
            "rate_before_note":        self.rate_before_note,
            "rate_after_note":         self.rate_after_note,
            "capture_size":            self.capture_size,
            "promotion_active":        self.promotion_active,
            "promotion_wins_before":   self.promotion_wins_before,
            "promotion_matches_before":self.promotion_matches_before,
            "promotion_wins_after":    self.promotion_wins_after,
            "promotion_matches_after": self.promotion_matches_after,
            "score_log_path":          self.score_log_path,
        }

    def to_log_dict(self) -> dict:
        """試合ごとの JSON に書く dict（to_dict に、形の版・終わった日時・スコアの根拠・バージョンを足したもの）"""
        return {
            **self.to_dict(),
            "log_schema":   LOG_SCHEMA_VERSION,
            "kind":         "rank",
            "ended_at":     self.ended_at.isoformat() if self.ended_at else None,
            "score_basis":  self.score_basis,
            "core_version": __version__,
            "app_version":  self.app_version,
        }

    def map_feature(self) -> Optional[dict]:
        """マップ判定に使った特徴量（約 1000 個の数値。大きいので、試合ごとの JSON には含めない。判定できなかったときは None）"""
        observations = getattr(self.map_result, "observation_results", None) or []
        feature = getattr(observations[0], "feature", None) if observations else None
        if feature is None:
            return None
        raw_confidence = getattr(self.map_result, "map_confidence", None)
        return {
            "version": getattr(observations[0], "feature_version", None),
            "values": [round(float(x), 6) for x in feature],
            # 修正前の自動判定結果（ユーザー修正値ではない）
            "detected_map": getattr(self.map_result, "auto_detected_map_id", None),
            "map_confidence": round(float(raw_confidence), 4) if raw_confidence is not None else None,
        }

    def summary(self) -> str:
        """ログ出力用のサマリー文字列を返す。"""
        score_str = (
            f"{self.score_my}-{self.score_enemy}"
            if self.score_my is not None and self.score_enemy is not None
            else "スコア未取得"
        )
        map_str = getattr(self.map_result, "auto_detected_map_id", None) or "マップ未判定"
        result_str = self.result or "-"
        return (
            f"session={self.session_id}, "
            f"state={self.state.name}, "
            f"score={score_str}, "
            f"result={result_str}, "
            f"map={map_str}"
        )


# ---------------------------------------------------------------------------
# ロギングヘルパー
# ---------------------------------------------------------------------------

def _log_info(message: str) -> None:
    logger.log(message)


def _log_warn(message: str) -> None:
    logger.log(f"[警告] {message}", level="warn")
