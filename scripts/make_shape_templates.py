"""
決まった言葉のテンプレート（src/ravens_record_core/ocr/shape_templates.py）を作る。

使い方（ac6-app で）:
  .venv/Scripts/python.exe scripts/make_shape_templates.py tests/fixtures/ocr
引数のフォルダの「<対象>/<値>__<なんでも>.png」（scripts/harvest_ocr_samples.py で切り出し、目で確かめたもの）を、
ファイル名の値を正解として、対象ごと・値ごとのテンプレートにする。
似た見本（近さ SAME_SCORE 以上）は 1 つにまとめ、値ごとに MAX_SAMPLES までにする。
画像は git に含めない（tests/fixtures/ocr は .gitignore。作ったテンプレート shape_templates.py だけを含める）。
"""

import sys
import types
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

OUT = Path(__file__).resolve().parent.parent / "src" / "ravens_record_core" / "ocr" / "shape_templates.py"
MAX_SAMPLES = 8
SAME_SCORE = 0.97


def _label(target: str, name: str) -> str:
    label = name.split("__")[0]
    return label.split("_")[0] if target == "result_points" else label  # 「1_ALPHA」→「1」


def _cut(target: str, image: np.ndarray):
    from ravens_record_core.detectors import match_end
    from ravens_record_core.ocr import ocr_shapes
    if target == "banner":
        return ocr_shapes.cut_banner(image, match_end._banner_mask(image))
    if target == "result_points":
        return ocr_shapes.cut_points(image[:, int(image.shape[1] * 0.84):])
    return ocr_shapes.CUTTERS[target](image)


def main(folder: Path) -> None:
    # 作り直すときに、古いテンプレートを読み込まないように
    stub = types.ModuleType("ravens_record_core.ocr.shape_templates")
    stub.TEMPLATES = {}
    sys.modules["ravens_record_core.ocr.shape_templates"] = stub
    from ravens_record_core.ocr import shape_match
    from ravens_record_core.ocr.ocr_shapes import EXPECTED
    out: dict[str, dict[str, list[np.ndarray]]] = {}
    for target_dir in sorted(p for p in folder.iterdir() if p.is_dir()):
        target = target_dir.name
        if target not in EXPECTED:
            print(f"{target}: 読み取りの対象でないため使いません")
            continue
        for path in sorted(target_dir.glob("*.png")):
            image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
            cut = _cut(target, image)
            if cut is None:
                print(f"{target}/{path.name}: 文字を切り出せないため使いません")
                continue
            kept = out.setdefault(target, {}).setdefault(_label(target, path.name), [])
            if any(shape_match.scores(cut, {"x": [k]}).get("x", 0) >= SAME_SCORE for k in kept):
                continue
            kept.append(cut)
    for labels in out.values():
        for label, samples in labels.items():
            if len(samples) > MAX_SAMPLES:
                idx = np.linspace(0, len(samples) - 1, MAX_SAMPLES).astype(int)
                labels[label] = [samples[i] for i in idx]

    lines = [
        '"""',
        "決まった言葉のテンプレート（scripts/make_shape_templates.py で作る。手で直さない）。",
        "対象ごと・値ごとに、実際の画面から切り出した文字を高さ shape_match.TEXT_H にそろえ、明るさを 0〜9 の 10 段階にした 1 行ごとの文字列で持つ。",
        '"""',
        "",
        "import numpy as np",
        "",
        "_RAW = {",
    ]
    for target in sorted(out):
        lines.append(f'    "{target}": {{')
        for label in sorted(out[target]):
            lines.append(f'        "{label}": [')
            for sample in out[target][label]:
                rows = ["".join(str(min(9, int(v * 10))) for v in row) for row in sample]
                lines.append("            (" + ", ".join(f'"{r}"' for r in rows) + "),")
            lines.append("        ],")
        lines.append("    },")
    lines += [
        "}",
        "",
        "TEMPLATES = {",
        "    target: {",
        "        label: [np.array([[int(c) / 9.0 for c in row] for row in sample], dtype=np.float32) for sample in samples]",
        "        for label, samples in labels.items()",
        "    }",
        "    for target, labels in _RAW.items()",
        "}",
        "",
    ]
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"書き出しました: {OUT}")
    for target in sorted(out):
        missing = sorted(EXPECTED.get(target, set()) - set(out[target]))
        note = f"（見本の無い値: {missing}。そろうまで、この対象は使わない）" if missing else ""
        print(f"  {target}: " + "、".join(f"{label} {len(s)}" for label, s in sorted(out[target].items())) + note)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
