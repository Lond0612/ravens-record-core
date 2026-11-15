"""
Tesseract の起動のしかたを整える（言語データの場所を、毎回はっきり渡す）

同梱の tesseract.exe（MinGW 版）は、言語データ（tessdata）の場所を、指定が無いと自分の置き場所から探す。
そのとき、置き場所のパスに英数字以外の文字（日本語の Windows のユーザー名など）が入っていると、
「filesystem error: Cannot convert character sequence: Illegal byte sequence」で毎回失敗する
（PyInstaller の 1 ファイル版は %TEMP%\\_MEIxxxx に展開されるので、ユーザー名がそのままパスに入る）。
環境変数 TESSDATA_PREFIX でも同じ失敗になるが、コマンドライン引数の --tessdata-dir なら正しく読める。

configure() / ensure() を呼ぶと、tesseract.exe を探し、pytesseract が起動するときに、必ず --tessdata-dir を付ける。
読み込んだだけでは何もしない（文字を読む直前に ensure() を呼ぶ。runtime.init() でも呼ぶ）。
引数はリストのまま渡す（pytesseract の config に文字列で足すと、Windows では引用符がそのまま残るため）。

また、tesseract.exe の起動の失敗（TesseractError・TesseractNotFoundError）を、呼び出し側に投げない。
一度だけ起動し直し、それでも失敗したら「何も読めなかった」（空の結果）として返す。以前は、1 回の失敗で
監視ループごと止まっていた（終了コード 0xC0000135・0xC0000142 は DLL の読み込み・初期化の失敗で、
PC の資源が足りないときなどに一時的に起きる。%TEMP% の展開先が掃除ソフトなどに消されると TesseractNotFoundError になる）。
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import pytesseract

_original_run = pytesseract.pytesseract.run_tesseract
_tessdata_dir: Optional[str] = None
_inner_run = _original_run  # いま使う起動のしかた（_original_run か、--tessdata-dir を付ける _run_tesseract）

RETRY_DELAY_SEC = 0.3
FAILURE_LOG_INTERVAL_SEC = 60.0
# 起動の失敗の様子（調査用）
failures: dict = {"count": 0, "last_error": None, "missing_exe": False}
_last_failure_log: Optional[float] = None


def tessdata_dir_for(tesseract_cmd: Optional[str]) -> Optional[str]:
    """tesseract.exe の隣の tessdata（無ければ None）"""
    if not tesseract_cmd:
        return None
    folder = Path(tesseract_cmd).resolve().parent / "tessdata"
    return str(folder) if folder.is_dir() else None


def build_args(tesseract_cmd: str, input_filename: str, output_filename_base: str, extension: str,
               lang: Optional[str], config: str, tessdata_dir: Optional[str]) -> list[str]:
    """pytesseract.run_tesseract と同じ並びに、--tessdata-dir を足した引数"""
    args = [tesseract_cmd, input_filename, output_filename_base]
    if lang is not None:
        args += ["-l", lang]
    if tessdata_dir:
        args += ["--tessdata-dir", tessdata_dir]
    if config:
        args += shlex.split(config, posix=not (sys.platform == "win32"))
    for ext in extension.split():
        if ext not in {"box", "osd", "tsv", "xml"}:
            args.append(ext)
    return args


def _run_tesseract(input_filename, output_filename_base, extension, lang, config="", nice=0, timeout=0):
    pt = pytesseract.pytesseract
    args = build_args(pt.tesseract_cmd, input_filename, output_filename_base, extension, lang, config, _tessdata_dir)
    try:
        proc = subprocess.Popen(args, **pt.subprocess_args())
    except OSError as e:
        if e.errno != pt.ENOENT:
            raise
        raise pt.TesseractNotFoundError()
    with pt.timeout_manager(proc, timeout) as error_string:
        if proc.returncode:
            raise pt.TesseractError(proc.returncode, pt.get_errors(error_string))


def _write_empty_outputs(output_filename_base: str, extension: str) -> None:
    """pytesseract が読みに行く出力ファイル（<base>.<拡張子>）を、空で作る（読めなかったとして返すため）"""
    for ext in (extension or "txt").split():
        try:
            Path(f"{output_filename_base}{os.extsep}{ext}").write_bytes(b"")
        except OSError:
            pass


def _note_failure(error: Exception) -> None:
    global _last_failure_log
    failures["count"] += 1
    failures["last_error"] = repr(error)[:200]
    cmd = pytesseract.pytesseract.tesseract_cmd
    failures["missing_exe"] = bool(cmd) and os.path.isabs(cmd) and not os.path.exists(cmd)
    now = time.monotonic()
    if _last_failure_log is not None and now - _last_failure_log < FAILURE_LOG_INTERVAL_SEC:
        return
    _last_failure_log = now
    from ravens_record_core import logger
    extra = "（tesseract.exe が見つかりません。一時フォルダが消された可能性があります）" if failures["missing_exe"] else ""
    logger.log(f"[文字の読み取り] [警告] Tesseract を起動できませんでした。この回は読めなかったとして続けます{extra}: "
               f"{error!r}（これまでに {failures['count']} 回）", level="warn")


def _guarded_run(input_filename, output_filename_base, extension, lang, config="", nice=0, timeout=0):
    """tesseract.exe を起動する。失敗したら 1 回だけ起動し直し、それでも失敗したら空の結果にする（例外を投げない）"""
    pt = pytesseract.pytesseract
    last: Optional[Exception] = None
    for attempt in range(2):
        try:
            return _inner_run(input_filename, output_filename_base, extension, lang, config, nice, timeout)
        except (pt.TesseractError, pt.TesseractNotFoundError) as e:
            last = e
            if attempt == 0:
                time.sleep(RETRY_DELAY_SEC)
    _note_failure(last)
    _write_empty_outputs(output_filename_base, extension)
    return None


def find_tesseract(tesseract_dir: Optional[Path] = None) -> Optional[str]:
    """使う tesseract.exe を探す。指定のフォルダ（Config.tesseract_dir）→ **同梱のもの**
    （ravens_record_core/bin/tesseract）→ PATH → よくあるインストール先の順。

    同梱のものを PATH より先に見るのは、別の用途で入れた Tesseract（日本語・韓国語の言語データが無いことが多い）が
    PATH にあると、そちらが使われるため。Tesseract は、無い言語を飛ばして英語だけで読むので、エラーにならないまま
    「マッチングしています / 対戦を開始します」が読めず、対戦の開始を検出できなくなる。
    """
    package_dir = Path(__file__).resolve().parent.parent
    candidates_first = [package_dir / "bin" / "tesseract" / "tesseract.exe"]
    if tesseract_dir is not None:
        candidates_first.insert(0, Path(tesseract_dir) / "tesseract.exe")
    for p in candidates_first:
        if p.exists():
            return str(p)

    which_path = shutil.which("tesseract")
    if which_path:
        return which_path

    local_appdata = os.environ.get("LOCALAPPDATA", "")
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    candidates = [
        os.path.join(local_appdata, r"Programs\Tesseract-OCR\tesseract.exe"),
        os.path.join(program_files, r"Tesseract-OCR\tesseract.exe"),
        os.path.join(program_files_x86, r"Tesseract-OCR\tesseract.exe"),
    ]
    for drive in ["C", "D", "E", "F"]:
        candidates.extend([
            f"{drive}:\\Tesseract-OCR\\tesseract.exe",
            f"{drive}:\\Program Files\\Tesseract-OCR\\tesseract.exe",
            f"{drive}:\\Program Files (x86)\\Tesseract-OCR\\tesseract.exe",
        ])
    for path_str in candidates:
        if path_str and os.path.exists(path_str):
            return path_str
    return None


def available_languages() -> Optional[list[str]]:
    """いま使う Tesseract で使える言語（tessdata の中身。分からなければ None）"""
    if _tessdata_dir:
        return sorted(p.stem for p in Path(_tessdata_dir).glob("*.traineddata"))
    try:
        return sorted(pytesseract.get_languages(config=""))
    except Exception:
        return None  # Tesseract を起動できないときは、分からない扱い


def pick_languages(wanted: tuple[str, ...], available: Optional[list[str]]) -> tuple[str, list[str]]:
    """wanted のうち使えるものを "+" でつないだ文字と、足りない言語の一覧。
    使える言語が分からないときは、そのまま全部（足りないかは分からない）。1 つも無ければ eng"""
    if available is None:
        return "+".join(wanted), []
    have = [lang for lang in wanted if lang in available]
    missing = [lang for lang in wanted if lang not in available]
    return ("+".join(have) if have else "eng"), missing


def install(tesseract_cmd: Optional[str]) -> None:
    """tesseract_cmd を設定し、言語データの場所を毎回渡すようにする（何度呼んでもよい）"""
    global _tessdata_dir, _inner_run
    pytesseract.pytesseract.run_tesseract = _guarded_run  # 起動の失敗で、呼び出し側（監視ループ）を止めない
    if not tesseract_cmd:
        return
    pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
    _tessdata_dir = tessdata_dir_for(tesseract_cmd)
    _inner_run = _run_tesseract if _tessdata_dir else _original_run


_configured = False
detected_path: Optional[str] = None  # 見つけた tesseract.exe（見つからなければ None）


def configure(tesseract_dir: Optional[Path] = None) -> Optional[str]:
    """tesseract.exe を探して設定する（何度呼んでもよい）。見つけた tesseract.exe を返す"""
    global _configured, detected_path
    detected_path = find_tesseract(tesseract_dir)
    install(detected_path)
    if detected_path is None:
        from ravens_record_core import logger
        logger.log("[文字の読み取り] [警告] Tesseract が見つかりませんでした。文字の読み取りが正しく動かない可能性があります。", level="warn")
    _configured = True
    return detected_path


def ensure() -> None:
    """まだなら、設定（Config.tesseract_dir）に従って Tesseract を設定する。文字を読む直前に呼ぶ"""
    if _configured:
        return
    from ravens_record_core import runtime
    configure(runtime.config().tesseract_dir)
