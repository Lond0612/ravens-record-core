"""
カスタムマッチ（シングル）の記録

- ルーム画面（左上の見出し「CUSTOM MATCH」）の設定 4 項目（対戦形式・制限時間・メンバー交代方式・マップ）を OCR で読む。
  プレイヤーの欄（名前・ランク）は読まない（読む範囲は、設定の欄だけ）。
- ゲームの言語（日本語・英語・韓国語）は、対戦形式の欄をどの言語の表記で読めたかで決め、ほかの欄はその言語で読む。
  マップは、どの言語で読んでも、照合の結果は日本語の表記にそろえる。
- 参加者か観戦かは、対戦中の画面で決める。左下に「AP」の表示があれば参加者、プレイヤーの欄のエンブレムの横に
  カメラのマーク（観戦で、カメラが追っているプレイヤーの印）があれば観戦（v1.3.0〜。どちらも無ければ記録しない）。
- 観戦の記録は、左右（観戦の画面は常に左が ALPHA・右が BETA）のラウンド数だけを持つ。プレイヤー名・エンブレム・画像は
  読まない・保存しない。試合の終わりのスコアまで読めた観戦だけを記録する（仮の値は入れない）。
- 記録（build_payload / build_spectated_payload）には、読んだ文字をそのまま入れる（照合の結果は参考として添える）。
  どのマップか・何秒かの正規化は、記録を使う側で行う。

ランクマッチの検出・記録の流れには関わらない（monitor/custom.py からだけ使う）。
"""

import difflib
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import cv2
import numpy as np
import pytesseract

from ravens_record_core import logger
from ravens_record_core.ocr import ocr_shapes
from ravens_record_core.system import screencap, tesseract_setup

SCHEMA_VERSION = 1
SPECTATED_SCHEMA_VERSION = 1

# --- ルーム画面の読み取り範囲（ウィンドウのクライアント領域に対する割合。1920x1080 の画面で実測） ---
# いずれも画面中央の設定の欄だけ。左の PLAYER の欄（名前・ランク）は含めない
ROOM_SETTING_ROIS = {
    "format":     (0.318, 0.355, 0.300, 0.042),  # 対戦形式（例: シングル）
    "time_limit": (0.318, 0.438, 0.300, 0.042),  # 制限時間（例: 2分）
    "swap_mode":  (0.318, 0.520, 0.300, 0.042),  # メンバー交代方式（例: 固定）
    "map":        (0.318, 0.600, 0.300, 0.034),  # マップ名（例: 汚染市街 - A）。下のプレビュー画像にかからない高さ
}

# 対戦中の画面の左下「AP」の表示（観戦の画面には無い）
AP_ROI = (0.060, 0.790, 0.195, 0.045)

# 観戦の画面のカメラのマーク（カメラが追っているプレイヤーの欄の、エンブレムの横に出る。参加者の画面には無い）。
# 左の ALPHA の欄ならエンブレムの左、右の BETA の欄ならエンブレムの右。運営者の観戦の録画（1920x1080・3 試合）で実測
CAMERA_ROIS = (
    (0.0443, 0.537, 0.0286, 0.037),  # 左（ALPHA）の欄
    (0.9297, 0.319, 0.0260, 0.037),  # 右（BETA）の欄
)
# マークとの近さ（正規化相互相関）。録画では、対戦中は 0.97 前後、ルーム・ローディング・紹介の映像では最大 0.65
CAMERA_MATCH_MIN = 0.85

# 対戦形式の選択肢（ゲームの言語ごとの表記 → 種類と言語）。シングルだけを記録する。
# 英語・韓国語の表記は、運営者がゲームの画面から確認したもの（2026-10-10）
FORMAT_OPTIONS = {
    "シングル": ("single", "ja"), "チーム": ("team", "ja"),
    "Single": ("single", "en"), "Team": ("team", "en"),
    "싱글": ("single", "ko"), "팀": ("team", "ko"),
}

# ゲームの言語 → Tesseract の言語データ
OCR_LANGS = {"ja": "jpn", "en": "eng", "ko": "kor"}

# カスタムマッチで選べるマップ（ルーム画面の表記そのまま。運営者提供・2026-10-07）
CUSTOM_MAP_OPTIONS = [
    "ボナ・デア砂丘 - A", "ボナ・デア砂丘 - B",
    "「壁」 - A", "「壁」 - B",
    "ヨルゲン燃料基地",
    "バートラム旧宇宙港",
    "グリッド086 - A", "グリッド086 - B",
    "グリッド012 - A", "グリッド012 - B",
    "ウォッチポイント・デルタ - A", "ウォッチポイント・デルタ - B",
    "洋上都市ザイレム",
    "汚染市街 - A", "汚染市街 - B",
    "LOCステーション31",
    "ランダム",
]

# 英語・韓国語のゲームのルーム画面の表記 → 日本語の表記（運営者確認・2026-10-10）
CUSTOM_MAP_TRANSLATIONS = {
    "汚染市街 - A": ("Contaminated City – A", "오염된 시가지 A"),
    "汚染市街 - B": ("Contaminated City – B", "오염된 시가지 B"),
    "ボナ・デア砂丘 - A": ("Bona Dea Dunes – A", "보나 데아 사구 A"),
    "ボナ・デア砂丘 - B": ("Bona Dea Dunes – B", "보나 데아 사구 B"),
    "「壁」 - A": ("The Wall – A", "「벽」 A"),
    "「壁」 - B": ("The Wall – B", "「벽」 B"),
    "ヨルゲン燃料基地": ("Jorgen Refueling Base", "요르겐 연료 기지"),
    "バートラム旧宇宙港": ("Old Bertram Spaceport", "버트럼 구 우주 공항"),
    "グリッド086 - A": ("Grid 086 – A", "그리드 086 A"),
    "グリッド086 - B": ("Grid 086 – B", "그리드 086 B"),
    "グリッド012 - A": ("Grid 012 – A", "그리드 012 A"),
    "グリッド012 - B": ("Grid 012 – B", "그리드 012 B"),
    "ウォッチポイント・デルタ - A": ("Watchpoint Delta – A", "워치 포인트: 델타 A"),
    "ウォッチポイント・デルタ - B": ("Watchpoint Delta – B", "워치 포인트: 델타 B"),
    "洋上都市ザイレム": ("Xylem, the Floating City", "해상 도시 자일렘"),
    "LOCステーション31": ("LOC Station 31", "LOC 스테이션 31"),
    "ランダム": ("Random", "무작위"),
}

# 言語ごとの、ルーム画面のマップの表記 → 日本語の表記
CUSTOM_MAP_NAMES = {
    "ja": {name: name for name in CUSTOM_MAP_OPTIONS},
    "en": {en: ja for ja, (en, _) in CUSTOM_MAP_TRANSLATIONS.items()},
    "ko": {ko: ja for ja, (_, ko) in CUSTOM_MAP_TRANSLATIONS.items()},
}

# 直前に読めたゲームの言語（次からは、この言語で先に読む。OCR の回数を減らすため）
_last_game_lang = "ja"


def _log(message: str) -> None:
    logger.log(message)


# ---------------------------------------------------------------------------
# 読み取り
# ---------------------------------------------------------------------------

def _crop(frame: np.ndarray, roi: tuple) -> np.ndarray:
    frame, _ = screencap.game_area(frame)  # 黒い帯の内側に対する割合で切る
    h, w = frame.shape[:2]
    left, top, width, height = roi
    return frame[int(top * h):int((top + height) * h), int(left * w):int((left + width) * w)]


def _ocr(img_bgr: np.ndarray, threshold: int, lang: str) -> str:
    if img_bgr is None or img_bgr.size == 0:
        return ""
    resized = cv2.resize(img_bgr, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY)
    try:
        tesseract_setup.ensure()
        return pytesseract.image_to_string(cv2.bitwise_not(binary), lang=lang, config="--psm 7").strip()
    except Exception as e:
        _log(f"[カスタム] OCR に失敗しました: {e}")
        return ""


def clean_text(text: str) -> str:
    """OCR の文字の前後の余分な記号・空白を落とす（プレビュー画像の縁が「ーー」「_」として読まれることがある）。"""
    # 3 つ以上の空白の後ろは、別の要素（縁・飾り）とみなして捨てる
    text = re.split(r"\s{3,}", (text or "").replace("\n", " ").strip())[0].strip()
    if re.search(r"[-‐–—―－ー一]\s*[ABＡＢ]$", text):
        return text  # 「 - A」「 - B」で終わるマップ名は、そのまま
    return re.sub(r"[\s_|.ー\-–—―－]+$", "", text)


def _normalize_for_match(text: str) -> str:
    """照合のときだけ使う正規化（空白・区切りのハイフン・かぎ括弧の揺れをそろえる）。保存は読んだ文字のまま。"""
    t = (text or "").replace("Ａ", "A").replace("Ｂ", "B")
    t = re.sub(r"\s*[-‐–—―－ー一]\s*([AB])$", r"-\1", t.strip())
    t = re.sub(r"[「『\[｢]", "「", t)
    t = re.sub(r"[」』\]｣]", "」", t)
    t = re.sub(r"[:：,.]", "", t)  # 英語・韓国語の表記の「:」「,」（워치 포인트: 델타、Xylem, the Floating City）の読み落とし
    return _decompose_hangul(re.sub(r"\s+", "", t).lower())


# ハングルの音節を、字母（初声・中声・終声）に分ける。OCR はよく似た字母を取り違える（무→므、작→자）ので、
# 音節ごとではなく字母ごとに比べたほうが、正しい選択肢に近くなる
_CHOSEONG = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
_JUNGSEONG = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"
_JONGSEONG = ["", *"ㄱㄲㄳㄴㄵㄶㄷㄹㄺㄻㄼㄽㄾㄿㅀㅁㅂㅄㅅㅆㅇㅈㅊㅋㅌㅍㅎ"]


def _decompose_hangul(text: str) -> str:
    out = []
    for ch in text:
        code = ord(ch) - 0xAC00
        if 0 <= code < 11172:
            out.append(_CHOSEONG[code // 588] + _JUNGSEONG[(code % 588) // 28] + _JONGSEONG[code % 28])
        else:
            out.append(ch)
    return "".join(out)


def best_match(text: str, options: list[str]) -> tuple[Optional[str], float]:
    """既知の選択肢のうち、いちばん近いものと、その近さ（0〜1）を返す。"""
    key = _normalize_for_match(text)
    if not key:
        return None, 0.0
    best, score = None, 0.0
    for option in options:
        r = difflib.SequenceMatcher(None, key, _normalize_for_match(option)).ratio()
        if r > score:
            best, score = option, r
    return best, round(score, 3)


@dataclass
class RoomSettings:
    format_raw: str = ""
    time_limit_raw: str = ""
    swap_mode_raw: str = ""
    map_raw: str = ""
    format_match: Optional[str] = None   # 照合した対戦形式（ゲームの言語の表記のまま。例: シングル / Single / 싱글）
    map_match: Optional[str] = None      # 照合したマップ（ゲームの言語によらず、日本語の表記）
    read_confidence: dict = field(default_factory=dict)
    game_lang: Optional[str] = None      # 読み取りに使ったゲームの言語（ja / en / ko）

    def _format_kind(self) -> Optional[str]:
        return FORMAT_OPTIONS.get(self.format_match or "", (None, None))[0]

    def is_single(self) -> bool:
        return self._format_kind() == "single" and self.read_confidence.get("format", 0.0) >= 0.6

    def is_team(self) -> bool:
        return self._format_kind() == "team" and self.read_confidence.get("format", 0.0) >= 0.6

    def summary(self) -> str:
        return f"形式={self.format_raw or '?'} / 時間={self.time_limit_raw or '?'} / 交代={self.swap_mode_raw or '?'} / マップ={self.map_raw or '?'}"


def _read_field(frame: np.ndarray, key: str, options: Optional[list[str]], lang: str = "jpn") -> tuple[str, Optional[str], float]:
    """1 項目を読む。選択肢が決まっている項目は、しきい値を変えて読み直し、近い方を採る。"""
    roi = _crop(frame, ROOM_SETTING_ROIS[key])
    best_raw, best_opt, best_score = "", None, 0.0
    for threshold in (170, 190, 150):
        raw = clean_text(_ocr(roi, threshold, lang))
        if options is None:
            if raw:
                return raw, None, 0.0
            continue
        opt, score = best_match(raw, options)
        if score > best_score:
            best_raw, best_opt, best_score = raw, opt, score
        if score >= 0.95:
            break
    return best_raw, best_opt, best_score


# A/B のあるマップ（日本語の表記が「 - A」「 - B」で終わるもの）
_AB_SUFFIX = re.compile(r" - ([AB])$")


def _read_ab_suffix(frame: np.ndarray) -> Optional[str]:
    """マップ名の最後の 1 文字（A / B）だけを読む。

    韓国語のゲームでは、ハングルの後ろの英字 1 文字を、韓国語の言語データでは読み落とす・別の記号に読む
    （実機の画面で「오염된 시가지 A」が「오염된 시가지 &」になった）。そこで、文字の並びの右端の 1 文字を切り出して、
    英字（A / B）だけに絞って読む。読めなければ None。
    """
    roi = _crop(frame, ROOM_SETTING_ROIS["map"])
    if roi is None or roi.size == 0:
        return None
    resized = cv2.resize(roi, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
    for threshold in (170, 190, 150):
        _, binary = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY)
        # 右端の 1 文字 = いちばん右にある、文字の高さほどの大きさのかたまり（A・B は、どちらもひとつながりの形）
        count, _, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
        parts = [stats[i] for i in range(1, count) if stats[i][cv2.CC_STAT_HEIGHT] >= binary.shape[0] * 0.3]
        if not parts:
            continue
        x, y, w, h = max(parts, key=lambda st: st[cv2.CC_STAT_LEFT] + st[cv2.CC_STAT_WIDTH])[:4]
        glyph = binary[max(0, y - 4):y + h + 4, max(0, x - 4):x + w + 4]
        padded = cv2.copyMakeBorder(cv2.bitwise_not(glyph), 12, 12, 12, 12, cv2.BORDER_CONSTANT, value=255)
        try:
            tesseract_setup.ensure()
            text = pytesseract.image_to_string(padded, lang="eng",
                                               config="--psm 10 -c tessedit_char_whitelist=AB").strip().upper()
        except Exception as e:
            _log(f"[カスタム] OCR に失敗しました: {e}")
            return None
        if text in ("A", "B"):
            return text
    return None


def _choose_ab_variant(frame: np.ndarray, map_match: Optional[str], map_raw: str) -> Optional[str]:
    """A/B のあるマップは、最後の 1 文字を読み直して、A か B かを決める（読めなければ、照合の結果のまま）。"""
    m = _AB_SUFFIX.search(map_match or "")
    if not m:
        return map_match
    suffix = _read_ab_suffix(frame)
    if suffix is None:
        # 読み直せなかったときは、生の文字の最後の 1 文字で決める（よくある読み違い: A → & / 4、B → 8 / R）
        last = (map_raw or "").strip()[-1:].upper()
        suffix = "A" if last in ("A", "&", "4", "Ａ") else "B" if last in ("B", "8", "R", "Ｂ") else None
    if suffix is None or suffix == m.group(1):
        return map_match
    return map_match[:m.start(1)] + suffix


def _read_format(frame: np.ndarray) -> tuple[str, Optional[str], float, str]:
    """対戦形式を読み、ゲームの言語を決める。直前の言語から順に試し、よく一致したら、ほかの言語は試さない。"""
    global _last_game_lang
    order = [_last_game_lang] + [lang for lang in OCR_LANGS if lang != _last_game_lang]
    best = ("", None, 0.0, _last_game_lang)
    for lang in order:
        raw, opt, score = _read_field(frame, "format", list(FORMAT_OPTIONS), OCR_LANGS[lang])
        if opt is not None:
            lang = FORMAT_OPTIONS[opt][1]  # 言語は、読めた表記から決める（英語の表記は、日本語の言語データでも読めるため）
        if score > best[2]:
            best = (raw, opt, score, lang)
        if score >= 0.95:
            break
    if best[1] is not None and best[2] >= 0.6:
        _last_game_lang = best[3]
    return best


def read_room_settings(frame: np.ndarray) -> RoomSettings:
    """ルーム画面の設定 4 項目を読む。"""
    fmt_raw, fmt_opt, fmt_score, game_lang = _read_format(frame)
    ocr_lang = OCR_LANGS[game_lang]
    time_raw, _, _ = _read_field(frame, "time_limit", None, ocr_lang)
    swap_raw, _, _ = _read_field(frame, "swap_mode", None, ocr_lang)
    map_names = CUSTOM_MAP_NAMES[game_lang]
    map_raw, map_shown, map_score = _read_field(frame, "map", list(map_names), ocr_lang)

    # 制限時間: 2分 / 2 minutes / 2분
    time_conf = 1.0 if re.fullmatch(r"\d+\s*(分|분|min(ute)?s?)", time_raw or "", re.IGNORECASE) else (0.3 if time_raw else 0.0)
    swap_conf = 0.8 if swap_raw else 0.0
    return RoomSettings(
        format_raw=fmt_raw,
        time_limit_raw=time_raw,
        swap_mode_raw=swap_raw,
        map_raw=map_raw,
        format_match=fmt_opt,
        map_match=_choose_ab_variant(frame, map_names.get(map_shown), map_raw) if map_shown else None,
        read_confidence={"format": fmt_score, "time_limit": time_conf, "swap_mode": swap_conf, "map": map_score},
        game_lang=game_lang,
    )


def has_ap_hud(frame: np.ndarray) -> bool:
    """対戦中の画面の左下に「AP」の表示があるか（参加者の画面）。観戦の画面には無い。"""
    roi = _crop(frame, AP_ROI)
    if ocr_shapes.read("ap", roi) == "AP":
        return True
    for threshold in (180, 150):
        text = _ocr(roi, threshold, "eng").upper().replace(" ", "")
        if text.startswith("AP") or re.search(r"AP\d", text):
            return True
    return False


def _camera_template() -> np.ndarray:
    """カメラのマーク（1080p で、白い四角 11x9 と、その右の左向きの小さな台形）を描いた照合用の画像。"""
    t = np.full((21, 26), 80, np.uint8)
    cv2.rectangle(t, (5, 6), (15, 14), 215, -1)
    cv2.fillConvexPoly(t, np.array([[17, 9], [19, 7], [19, 14], [17, 12]], np.int32), 205)
    return t


_CAMERA_TEMPLATE = _camera_template()


def camera_icon_score(frame: np.ndarray) -> float:
    """観戦のカメラのマークとの近さ（0〜1。左右の欄のうち高いほう）。読むのは、マークの出る小さな範囲だけ。"""
    if frame is None or frame.size == 0:
        return 0.0
    h = frame.shape[0]
    best = 0.0
    for roi in CAMERA_ROIS:
        img = _crop(frame, roi)
        if img.size == 0:
            continue
        if h != 1080:
            img = cv2.resize(img, None, fx=1080 / h, fy=1080 / h, interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        if gray.shape[0] < _CAMERA_TEMPLATE.shape[0] or gray.shape[1] < _CAMERA_TEMPLATE.shape[1]:
            continue
        best = max(best, float(cv2.matchTemplate(gray, _CAMERA_TEMPLATE, cv2.TM_CCOEFF_NORMED).max()))
    return best


def has_spectator_camera(frame: np.ndarray) -> bool:
    """観戦の画面か（カメラのマークがあるか）。参加者の画面には無い。"""
    return camera_icon_score(frame) >= CAMERA_MATCH_MIN


# 観戦で記録する、試合の終わりのスコア（ALPHA, BETA）。ScoreDetector の試合の終了と同じ組み合わせ
SPECTATED_FINAL_SCORES = {(2, 0), (2, 1), (1, 2), (0, 2), (2, 2)}


def is_final_score(alpha_wins: Optional[int], beta_wins: Optional[int]) -> bool:
    return (alpha_wins, beta_wins) in SPECTATED_FINAL_SCORES


# ---------------------------------------------------------------------------
# 記録（試合ごとの JSON の中身。使う側のアプリへも、この形で渡す）
# ---------------------------------------------------------------------------

def build_payload(settings: RoomSettings, played_at: datetime, own_wins: Optional[int], opp_wins: Optional[int],
                  app_version: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "client_match_id": str(uuid.uuid4()),
        "app_version": app_version,
        "played_at": played_at.astimezone().isoformat(timespec="seconds"),
        "format_raw": settings.format_raw,
        "time_limit_raw": settings.time_limit_raw,
        "swap_mode_raw": settings.swap_mode_raw,
        "map_raw": settings.map_raw,
        # 照合の結果（参考）。正規化は、記録を使う側で、生の文字から行う
        # lang: 読み取ったゲームの言語（生の文字の言語。正規化するときの手がかり）
        "map_hint": {"match": settings.map_match, "score": settings.read_confidence.get("map", 0.0), "lang": settings.game_lang}
                    if settings.map_match else None,
        "read_confidence": settings.read_confidence,
        "own_side": None,
        "own_wins": own_wins,
        "opp_wins": opp_wins,
    }


def build_spectated_payload(settings: RoomSettings, started_at: datetime, ended_at: datetime,
                            alpha_wins: int, beta_wins: int, app_version: str) -> dict:
    """観戦の記録。左右（ALPHA / BETA）のラウンド数だけを持つ（自分の勝敗・プレイヤーの情報は無い）。"""
    return {
        "schema_version": SPECTATED_SCHEMA_VERSION,
        "client_match_id": str(uuid.uuid4()),
        "app_version": app_version,
        "started_at": started_at.astimezone().isoformat(timespec="seconds"),
        "ended_at": ended_at.astimezone().isoformat(timespec="seconds"),
        "format_raw": settings.format_raw,
        "time_limit_raw": settings.time_limit_raw,
        "swap_mode_raw": settings.swap_mode_raw,
        "map_raw": settings.map_raw,
        "map_hint": {"match": settings.map_match, "score": settings.read_confidence.get("map", 0.0), "lang": settings.game_lang}
                    if settings.map_match else None,
        "read_confidence": settings.read_confidence,
        "alpha_wins": alpha_wins,
        "beta_wins": beta_wins,
    }

