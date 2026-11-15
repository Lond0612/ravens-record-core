"""
Tesseract の起動のしかた（tesseract_setup）のテスト

同梱の tesseract.exe は、置き場所のパスに日本語などがあると、言語データを探す段階で失敗する。
--tessdata-dir を毎回渡すことで動くことを、日本語を含むフォルダに置いた tesseract で確かめる
（build\tesseract が無い環境では、そのテストは飛ばす）。
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import importlib  # noqa: E402

import pytesseract  # noqa: E402

ts = importlib.import_module("ravens_record_core.system.tesseract_setup")

BUILD_TESSERACT = Path(__file__).resolve().parent.parent / "build" / "tesseract"


class TesseractSetupTest(unittest.TestCase):
    def setUp(self):
        self._cmd = pytesseract.pytesseract.tesseract_cmd
        self._run = pytesseract.pytesseract.run_tesseract

    def tearDown(self):
        pytesseract.pytesseract.tesseract_cmd = self._cmd
        pytesseract.pytesseract.run_tesseract = self._run

    def test_args_keep_paths_with_spaces_as_one_argument(self):
        args = ts.build_args(r"C:\a b\tesseract.exe", "in.png", "out", "tsv", "jpn+eng", "--psm 7", r"C:\Users\山田 太郎\tessdata")
        self.assertEqual(args, [r"C:\a b\tesseract.exe", "in.png", "out", "-l", "jpn+eng",
                                "--tessdata-dir", r"C:\Users\山田 太郎\tessdata", "--psm", "7"])
        self.assertEqual(ts.build_args("t", "i", "o", "txt", None, "", None)[-1], "txt")

    def test_without_tessdata_keeps_the_original(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / "tesseract.exe"
            exe.write_bytes(b"")
            ts.install(str(exe))
            self.assertIs(ts._inner_run, ts._original_run)
            self.assertIs(pytesseract.pytesseract.run_tesseract, ts._guarded_run)

    def test_failures_become_empty_text_instead_of_exceptions(self):
        # tesseract.exe の起動の失敗（DLL の読み込みの失敗・展開先が消えた）で、呼び出し側を止めない
        img = np.full((40, 120, 3), 255, np.uint8)
        calls = []
        pt = pytesseract.pytesseract
        for error in (pt.TesseractError(3221225781, ""), pt.TesseractNotFoundError()):
            def fail(*args, error=error):
                calls.append(1)
                raise error
            saved = ts._inner_run, ts.failures["count"]
            try:
                ts._inner_run = fail
                pt.run_tesseract = ts._guarded_run
                with mock.patch.object(ts.time, "sleep"), mock.patch("ravens_record_core.logger.log"):
                    self.assertEqual(pytesseract.image_to_string(img, config="--psm 7"), "")
                self.assertEqual(ts.failures["count"], saved[1] + 1)
            finally:
                ts._inner_run = saved[0]
        self.assertEqual(len(calls), 4)  # それぞれ 1 回だけ起動し直す

    def test_retry_success_returns_normally(self):
        state = {"n": 0}
        pt = pytesseract.pytesseract

        def flaky(input_filename, output_filename_base, extension, *rest):
            state["n"] += 1
            if state["n"] == 1:
                raise pt.TesseractError(3221225794, "")
            Path(f"{output_filename_base}.{extension}").write_text("SINGLE", encoding="utf-8")

        saved = ts._inner_run
        try:
            ts._inner_run = flaky
            pt.run_tesseract = ts._guarded_run
            with mock.patch.object(ts.time, "sleep"):
                self.assertEqual(pytesseract.image_to_string(np.full((40, 120, 3), 255, np.uint8)).strip(), "SINGLE")
        finally:
            ts._inner_run = saved

    @unittest.skipUnless((BUILD_TESSERACT / "tesseract.exe").exists(), "build\tesseract が無い")
    def test_reads_when_the_folder_has_japanese(self):
        import cv2
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / "ユーザー 名前" / "tesseract"
            shutil.copytree(BUILD_TESSERACT, folder)
            img = np.full((60, 240, 3), 255, np.uint8)
            cv2.putText(img, "SINGLE", (10, 45), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 0, 0), 3)
            ts.install(str(folder / "tesseract.exe"))
            self.assertIn("SINGLE", pytesseract.image_to_string(img, lang="jpn+eng", config="--psm 7").upper())


if __name__ == "__main__":
    unittest.main()
