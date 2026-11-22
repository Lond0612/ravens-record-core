"""
チーム戦の試合中の表示のテンプレート（src/ravens_record_core/ocr/team_hud_templates.py）を、チーム戦の録画から作る。

使い方（ravens-record-core で）:
  .venv/Scripts/python.exe scripts/make_team_hud_templates.py <チーム戦の録画.mp4> [<録画> ...] [--parts reboot,slot,...]
  --parts を付けると、その種類（own / opp / score / timer / slot / reboot）だけを作り直し、今のテンプレートのほかの種類は残す
録画（1920x1080・16:9）を 0.5 秒ごとに見て、次の表示の見本を集め、形ごとのテンプレートにする。
  - 上部のポイント（自チーム・相手）・自分の SCORE: Tesseract で読み、続けて 3 回以上同じ値だった区間を正解とする
  - 残り時間: Tesseract で読んだ MM:SS のうち、録画の時刻と残り時間の和がそろうものを正解とする
  - 流れる表示の番号（01〜03）: 枠の中は背景が暗いので、Tesseract で読んだ値を正解とする
  - 「MAIN SYSTEM REBOOTING」: Tesseract で読めた画面の、文字の部分
Tesseract は、テンプレートを作るときの正解付けにだけ使う（記録の最中は、テンプレートとの照合だけで読む）。
録画・画像は git に含めない。作ったテンプレートも、ゲームの画面から作るため git に含めない（.gitignore）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import cv2
import numpy as np
import pytesseract

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ravens_record_core.detectors import team_hud  # noqa: E402
from ravens_record_core.system import tesseract_setup  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "src" / "ravens_record_core" / "ocr" / "team_hud_templates.py"
STEP_SEC = 0.5


def _tess(img: np.ndarray, whitelist: str, threshold: int = 185) -> str:
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    g = cv2.resize(g, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    _, b = cv2.threshold(g, threshold, 255, cv2.THRESH_BINARY_INV)
    b = cv2.copyMakeBorder(b, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=255)
    return pytesseract.image_to_string(b, lang="eng", config=f"--psm 7 -c tessedit_char_whitelist={whitelist}").strip()


def _field_label(field: str, text: str) -> str | None:
    """Tesseract の読みから、欄の数字の並び。欄の端の縦棒を「1」と読んだものを除く"""
    if not re.fullmatch(r"\d{1,5}", text):
        return None
    if field == "own":  # 右端に縦棒
        return text[:-1] if len(text) >= 2 and text.endswith("1") else None
    if field == "opp":  # 左端に縦棒
        if len(text) == 4 and text.startswith("1"):
            return text[1:]
        return "0" if text == "10" else (text if len(text) == 3 else None)
    if field == "score":  # 「SCORE:」のコロンを 7 と読むことがある
        if len(text) == 4 and text.startswith("7"):
            return text[1:]
        return text if len(text) in (1, 2, 3) else None
    return None


def harvest(path: str, samples: dict, parts: set[str]) -> None:
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 60
    step = max(1, round(fps * STEP_SEC))
    runs: dict[str, list] = {f: [] for f in ("own", "opp", "score")}
    timer_reads = []
    i = 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if i % step:
            i += 1
            continue
        ok, frame = cap.retrieve()
        i += 1
        if not ok:
            continue
        f = team_hud.to_1080(frame)
        t = i / fps
        # REBOOTING の間は、残り時間などの表示が暗くなるため、残り時間を読む前に見る
        if "reboot" in parts:
            band = team_hud.crop(f, team_hud.REBOOT_BOX)
            if "REBOOTING" in _tess(band, "ABCDEFGHIJKLMNOPQRSTUVWXYZ ", threshold=170).replace(" ", ""):
                samples.setdefault("reboot", []).append(team_hud.reboot_image(f))
        if not parts & {"own", "opp", "score", "timer", "slot"}:
            continue
        timer = team_hud.crop(f, team_hud.TIMER_BOX)
        m = re.fullmatch(r"(\d\d):(\d\d)", _tess(timer, "0123456789:"))
        if not m:
            for field in runs:  # 試合中でない画面: 区間を切る
                runs[field].append((t, None, None))
            continue
        timer_reads.append((t, int(m.group(1)) * 60 + int(m.group(2)), f))
        for field in runs:
            if field in parts:
                box = team_hud.FIELDS[field]["box"]
                runs[field].append((t, _field_label(field, _tess(team_hud.crop(f, box), "0123456789")), f))
        if "slot" in parts:
            for cy, color in team_hud.feed_bars(f):
                token = _tess(team_hud.crop(f, team_hud.feed_slot_box(cy)), "0123", threshold=150)
                if token in ("01", "02", "03"):
                    samples.setdefault(f"slot{token[-1]}", []).append(team_hud.slot_image(f, cy))
    # ポイント・SCORE: 続けて 3 回以上同じ値の区間
    for field, seq in runs.items():
        j = 0
        while j < len(seq):
            v = seq[j][1]
            n = j
            while n < len(seq) and seq[n][1] == v:
                n += 1
            if v is not None and n - j >= 3:
                for _, label, f in seq[j:n]:
                    for d, cell in zip(label, team_hud.field_cells(f, field, len(label))):
                        samples.setdefault(f"{field}:{d}", []).append(team_hud.norm_cell(cell))
            j = max(n, j + 1)
    # 残り時間: 録画の時刻 + 残り時間 がそろうもの（試合ごとに、その中央の値から 1.2 秒以内）
    groups: list[list] = []
    for t, r, f in timer_reads:
        if groups and t - groups[-1][-1][0] < 30:
            groups[-1].append((t, r, f))
        else:
            groups.append([(t, r, f)])
    for g in groups if "timer" in parts else []:
        base = float(np.median([t + r for t, r, _ in g]))
        for t, r, f in g:
            if abs(t + r - base) <= 1.2:
                text = f"{r // 60:02d}{r % 60:02d}"
                for d, cell in zip(text, team_hud.timer_cells(f)):
                    samples.setdefault(f"timer:{d}", []).append(team_hud.norm_cell(cell))
    print(f"{path}: 残り時間 {len(timer_reads)} 回")


def _encode(arr: np.ndarray) -> list[str]:
    q = np.clip(np.round((arr - arr.min()) / max(float(np.ptp(arr)), 1e-6) * 9), 0, 9).astype(int)
    return ["".join(str(v) for v in row) for row in q]


ALL_PARTS = {"own", "opp", "score", "timer", "slot", "reboot"}


def _part(key: str) -> str:
    return "slot" if key.startswith("slot") else key.split(":", 1)[0]


def main(args: list[str]) -> None:
    parts = set(ALL_PARTS)
    if "--parts" in args:
        i = args.index("--parts")
        parts = {x.strip() for x in args[i + 1].split(",") if x.strip()} & ALL_PARTS
        args = args[:i] + args[i + 2:]
    tesseract_setup.ensure()
    samples: dict[str, list[np.ndarray]] = {}
    for p in args:
        harvest(p, samples, parts)
    # 作り直さない種類は、今のテンプレートのものを残す
    kept = {k: v for k, v in team_hud._RAW.items() if _part(k) not in parts}
    lines = ['"""',
             "チーム戦の試合中の表示のテンプレート（scripts/make_team_hud_templates.py で作る。手で直さない。ゲームの画面から作るため git に含めない）。",
             "形ごとに、見本を平均した濃淡（0〜9 の 10 段階の、1 行ごとの文字列）で持つ。",
             '"""', "", "RAW = {"]
    for key in sorted(kept):
        lines.append(f"    {key!r}: {kept[key]!r},")
    for key in sorted(samples):
        items = samples[key]
        if len(items) < 3:
            print(f"{key}: 見本が {len(items)} 個しかないため使いません")
            continue
        mean = np.mean([s.astype(np.float32) for s in items], axis=0)
        lines.append(f"    {key!r}: {_encode(mean)!r},  # 見本 {len(items)}")
        print(f"{key}: 見本 {len(items)}")
    lines.append("}")
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"書き出しました: {OUT}")


if __name__ == "__main__":
    main(sys.argv[1:])
