"""
レート・順位の数字のテンプレート（src/ravens_record_core/ocr/rate_digit_templates.py）を作る。

使い方（ac6-app で）:
  .venv/Scripts/python.exe scripts/make_rate_digit_templates.py tests/fixtures/rate
引数のフォルダの「<欄>_<数字>.png」（例: rating_1606.png）を、ファイル名の数字を正解として 1 文字ずつに分け、
数字ごとのテンプレートにする。数字を足したいときは、画像をフォルダに足して作り直す。
解像度・撮影の方式の違いに強くするため、元の大きさのほかに、縮小・拡大・ぼかした画像からも作る（VARIANTS）。
画像は git に含めない（tests/fixtures/rate は .gitignore。作ったテンプレート rate_digit_templates.py だけを含める）。
"""

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

GLYPH_W = 20
GLYPH_H = 28
OUT = Path(__file__).resolve().parent.parent / "src" / "ravens_record_core" / "ocr" / "rate_digit_templates.py"
# 元の画像のほかに作る見え方（720p〜4K の画面・撮影のぼけ）。拡大・縮小の倍率と、ぼかすか
VARIANTS = [(1.0, False), (1.0, True), (0.67, False), (0.8, False), (1.33, False), (2.0, False), (0.67, True), (1.33, True)]


def _variant(image: np.ndarray, scale: float, blur: bool) -> np.ndarray:
    if scale != 1.0:
        image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
    return cv2.GaussianBlur(image, (3, 3), 0) if blur else image


def main(folder: Path) -> None:
    # テンプレートの無い状態でも、文字の切り分けと大きさのそろえ方は rate_digits と同じものを使う
    import types
    stub = types.ModuleType("ravens_record_core.ocr.rate_digit_templates")
    stub.GLYPH_W, stub.GLYPH_H, stub.TEMPLATES = GLYPH_W, GLYPH_H, {}
    sys.modules["ravens_record_core.ocr.rate_digit_templates"] = stub
    from ravens_record_core.ocr.rate_digits import normalize_glyph, split_glyphs

    samples: dict[str, list[np.ndarray]] = {}
    for path in sorted(folder.glob("*_*.png")):
        label = path.stem.split("_")[-1]
        original = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
        for scale, blur in VARIANTS:
            glyphs = split_glyphs(_variant(original, scale, blur))
            if len(glyphs) != len(label):
                print(f"{path.name}（倍率 {scale}・ぼかし {blur}）: 文字の数が合わないため使いません（{len(glyphs)} 文字）")
                continue
            for digit, glyph in zip(label, glyphs):
                samples.setdefault(digit, []).append(normalize_glyph(glyph))
    missing = [d for d in "0123456789" if d not in samples]
    if missing:
        raise SystemExit(f"テンプレートの無い数字があります: {missing}")

    lines = [
        '"""',
        "レート・順位の数字のテンプレート（scripts/make_rate_digit_templates.py で作る。手で直さない）。",
        "数字ごとに、実際の画面から切り出した文字を GLYPH_W × GLYPH_H にそろえ、明るさを 0〜9 の 10 段階にした 1 行ごとの文字列で持つ。",
        '"""',
        "",
        "import numpy as np",
        "",
        f"GLYPH_W = {GLYPH_W}",
        f"GLYPH_H = {GLYPH_H}",
        "",
        "_RAW = {",
    ]
    for digit in "0123456789":
        lines.append(f'    "{digit}": [')
        for sample in samples[digit]:
            rows = ["".join(str(min(9, int(v * 10))) for v in row) for row in sample]
            lines.append("        (" + ", ".join(f'"{r}"' for r in rows) + "),")
        lines.append("    ],")
    lines += [
        "}",
        "",
        "TEMPLATES = {",
        "    digit: [np.array([[int(c) / 9.0 for c in row] for row in sample], dtype=np.float32) for sample in samples]",
        "    for digit, samples in _RAW.items()",
        "}",
        "",
    ]
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"書き出しました: {OUT}（" + "、".join(f"{d}: {len(samples[d])}" for d in "0123456789") + "）")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
