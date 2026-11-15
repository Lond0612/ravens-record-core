"""
ゲームの画面の撮影（ロード画面の監視プロセス用。フルスクリーン・ボーダーレス・ウィンドウのどれでも撮れるように）

撮影の方式（上から順に試し、撮れない・真っ黒のままなら次へ切り替える）
  1. dxgi        … DXGI Desktop Duplication。画面に表示されている内容を GPU から直接読む。
                   フルスクリーン（排他を含む）でも撮れる。ゲームが手前にあるときだけ使う（隠れていると、手前の別の窓が写るため）。
  2. printwindow … PrintWindow（クライアント領域だけ）。ゲームが裏に回っていても撮れる。
                   フルスクリーンでは真っ黒・古い画面になることがある。
  3. gdi         … 画面のコピー（mss）。ゲームが手前にあるときだけ。

座標は、画面の拡大率（DPI）の影響を受けない実際の画素で扱う（enable_dpi_awareness() を最初に呼ぶ）。
"""

import ctypes
import threading
import time
import uuid
from ctypes import POINTER, WINFUNCTYPE, Structure, byref, c_int64, c_long, c_uint, c_void_p, wintypes
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np
import win32con
import win32gui
import win32ui

user32 = ctypes.WinDLL("user32", use_last_error=True)


def enable_dpi_awareness() -> None:
    """このプロセスを「モニターごとの DPI 対応」にする（座標・撮影の大きさを実際の画素にそろえる）。"""
    try:
        user32.SetProcessDpiAwarenessContext(c_void_p(-4))  # PER_MONITOR_AWARE_V2
        return
    except Exception:
        pass  # 古い Windows には無い API。下の別の方法を試す
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except Exception:
        pass  # どちらも使えなければ、DPI 対応なしで続ける（撮影はできる）


# ---------------------------------------------------------------------------
# ウィンドウの状態（画面モードの判定）
# ---------------------------------------------------------------------------

class _MONITORINFO(Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


@dataclass
class WindowState:
    hwnd: int
    client: tuple          # クライアント領域（画面の座標。left, top, right, bottom）
    monitor: int           # HMONITOR
    monitor_rect: tuple    # モニターの範囲（画面の座標）
    foreground: bool       # 手前にあるか
    minimized: bool
    mode: str              # fullscreen / borderless / windowed

    @property
    def width(self) -> int:
        return self.client[2] - self.client[0]

    @property
    def height(self) -> int:
        return self.client[3] - self.client[1]


def _fullscreen_app_running() -> bool:
    """Windows が「全画面の Direct3D のアプリが動いている」とみなしているか（排他フルスクリーンの目安）"""
    try:
        state = c_uint(0)
        if ctypes.WinDLL("shell32").SHQueryUserNotificationState(byref(state)) == 0:
            return state.value == 3  # QUNS_RUNNING_D3D_FULL_SCREEN
    except Exception:
        pass  # 分からなければ、下の「全画面ではない」とみなす
    return False


def window_state(hwnd) -> Optional[WindowState]:
    try:
        minimized = bool(win32gui.IsIconic(hwnd))
        l, t, r, b = win32gui.GetClientRect(hwnd)
        sl, st = win32gui.ClientToScreen(hwnd, (l, t))
        client = (sl, st, sl + (r - l), st + (b - t))
        hmon = user32.MonitorFromWindow(wintypes.HWND(hwnd), 2)  # MONITOR_DEFAULTTONEAREST
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        user32.GetMonitorInfoW(c_void_p(hmon), byref(info))
        m = info.rcMonitor
        mon_rect = (m.left, m.top, m.right, m.bottom)
        foreground = win32gui.GetForegroundWindow() == hwnd
    except Exception:
        return None  # ウィンドウが閉じた直後などで失敗する（撮れなかった扱い）
    covers = client == mon_rect
    has_caption = bool(win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE) & win32con.WS_CAPTION) and not covers
    if covers and foreground and _fullscreen_app_running():
        mode = "fullscreen"
    elif covers or not has_caption:
        mode = "borderless"
    else:
        mode = "windowed"
    return WindowState(hwnd, client, int(hmon or 0), mon_rect, foreground, minimized, mode)


# ---------------------------------------------------------------------------
# 1. DXGI Desktop Duplication（ctypes で COM を直接呼ぶ）
# ---------------------------------------------------------------------------

class _GUID(Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


def _iid(text: str) -> _GUID:
    return _GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


IID_IDXGIFactory1 = _iid("770aae78-f26f-4dba-a829-253c83d1b387")
IID_IDXGIOutput1 = _iid("00cddea8-939b-4b83-a340-a685226666cc")
IID_ID3D11Texture2D = _iid("6f15aaf2-d208-4e89-9ab4-489535d34f9c")

DXGI_ERROR_NOT_FOUND = 0x887A0002
DXGI_ERROR_ACCESS_LOST = 0x887A0026
DXGI_ERROR_WAIT_TIMEOUT = 0x887A0027
DXGI_FORMAT_B8G8R8A8_UNORM = 87


class _DXGI_OUTPUT_DESC(Structure):
    _fields_ = [("DeviceName", ctypes.c_wchar * 32), ("DesktopCoordinates", wintypes.RECT),
                ("AttachedToDesktop", wintypes.BOOL), ("Rotation", c_uint), ("Monitor", c_void_p)]


class _POINTER_POSITION(Structure):
    _fields_ = [("Position", wintypes.POINT), ("Visible", wintypes.BOOL)]


class _FRAME_INFO(Structure):
    _fields_ = [("LastPresentTime", c_int64), ("LastMouseUpdateTime", c_int64), ("AccumulatedFrames", c_uint),
                ("RectsCoalesced", wintypes.BOOL), ("ProtectedContentMaskedOut", wintypes.BOOL),
                ("PointerPosition", _POINTER_POSITION), ("TotalMetadataBufferSize", c_uint),
                ("PointerShapeBufferSize", c_uint)]


class _TEXTURE2D_DESC(Structure):
    _fields_ = [("Width", c_uint), ("Height", c_uint), ("MipLevels", c_uint), ("ArraySize", c_uint),
                ("Format", c_uint), ("SampleCount", c_uint), ("SampleQuality", c_uint), ("Usage", c_uint),
                ("BindFlags", c_uint), ("CPUAccessFlags", c_uint), ("MiscFlags", c_uint)]


class _BOX(Structure):
    _fields_ = [("left", c_uint), ("top", c_uint), ("front", c_uint), ("right", c_uint), ("bottom", c_uint), ("back", c_uint)]


class _MAPPED(Structure):
    _fields_ = [("pData", c_void_p), ("RowPitch", c_uint), ("DepthPitch", c_uint)]


class DxgiError(Exception):
    def __init__(self, what: str, hr: int):
        super().__init__(f"{what} に失敗しました (0x{hr & 0xFFFFFFFF:08X})")
        self.hr = hr & 0xFFFFFFFF


def _com(ptr, index: int, *argtypes, restype=c_long):
    """COM のメソッド（vtable の index 番目）を呼べる関数にする"""
    vtbl = ctypes.cast(ptr, POINTER(POINTER(c_void_p)))[0]
    fn = WINFUNCTYPE(restype, c_void_p, *argtypes)(vtbl[index])
    return lambda *args: fn(ptr, *args)


def _ok(hr: int, what: str) -> None:
    if hr < 0:
        raise DxgiError(what, hr)


def _release(ptr) -> None:
    if ptr:
        try:
            _com(ptr, 2, restype=c_uint)()
        except Exception:
            pass  # 解放の失敗は、後始末の途中なので無視する


class DesktopDuplication:
    """1 台のモニターの表示内容を、DXGI Desktop Duplication で読む。"""

    def __init__(self, hmonitor: int):
        self.hmonitor = hmonitor
        self.device = self.context = self.dupl = None
        self.staging = None
        self.staging_size = (0, 0)
        self.origin = (0, 0)
        self.size = (0, 0)
        self._last: Optional[np.ndarray] = None
        self._last_rect = None
        self.reopened = 0
        self._open()

    def _open(self) -> None:
        dxgi = ctypes.WinDLL("dxgi")
        d3d11 = ctypes.WinDLL("d3d11")
        factory = c_void_p()
        _ok(dxgi.CreateDXGIFactory1(byref(IID_IDXGIFactory1), byref(factory)), "CreateDXGIFactory1")
        adapter_ptr = output_ptr = None
        try:
            i = 0
            while adapter_ptr is None:
                adapter = c_void_p()
                hr = _com(factory, 12, c_uint, POINTER(c_void_p))(i, byref(adapter))  # EnumAdapters1
                if (hr & 0xFFFFFFFF) == DXGI_ERROR_NOT_FOUND:
                    break
                _ok(hr, "EnumAdapters1")
                j = 0
                while True:
                    output = c_void_p()
                    hr = _com(adapter, 7, c_uint, POINTER(c_void_p))(j, byref(output))  # EnumOutputs
                    if (hr & 0xFFFFFFFF) == DXGI_ERROR_NOT_FOUND:
                        break
                    _ok(hr, "EnumOutputs")
                    desc = _DXGI_OUTPUT_DESC()
                    _ok(_com(output, 7, POINTER(_DXGI_OUTPUT_DESC))(byref(desc)), "IDXGIOutput::GetDesc")
                    if (desc.Monitor or 0) == self.hmonitor:
                        if desc.Rotation not in (0, 1):
                            _release(output)
                            raise DxgiError("回転したモニターの撮影", 0x80004001)
                        r = desc.DesktopCoordinates
                        self.origin = (r.left, r.top)
                        self.size = (r.right - r.left, r.bottom - r.top)
                        adapter_ptr, output_ptr = adapter, output
                        break
                    _release(output)
                    j += 1
                if adapter_ptr is None:
                    _release(adapter)
                i += 1
            if output_ptr is None:
                raise DxgiError("ゲームのモニターの検索", 0x887A0002)

            device, context, level = c_void_p(), c_void_p(), c_uint()
            # D3D11CreateDevice(adapter, D3D_DRIVER_TYPE_UNKNOWN, NULL, flags, NULL, 0, D3D11_SDK_VERSION, ...)
            _ok(d3d11.D3D11CreateDevice(adapter_ptr, 0, None, 0, None, 0, 7, byref(device), byref(level), byref(context)),
                "D3D11CreateDevice")
            self.device, self.context = device, context
            output1 = c_void_p()
            _ok(_com(output_ptr, 0, POINTER(_GUID), POINTER(c_void_p))(byref(IID_IDXGIOutput1), byref(output1)),
                "QueryInterface(IDXGIOutput1)")
            try:
                dupl = c_void_p()
                _ok(_com(output1, 22, c_void_p, POINTER(c_void_p))(device, byref(dupl)), "DuplicateOutput")
                self.dupl = dupl
            finally:
                _release(output1)
        finally:
            _release(output_ptr)
            _release(adapter_ptr)
            _release(factory)

    def close(self) -> None:
        for name in ("staging", "dupl", "context", "device"):
            _release(getattr(self, name))
            setattr(self, name, None)

    def _ensure_staging(self, fmt: int, w: int, h: int) -> None:
        if self.staging is not None and self.staging_size == (w, h):
            return
        _release(self.staging)
        self.staging = None
        desc = _TEXTURE2D_DESC(w, h, 1, 1, fmt, 1, 0, 3, 0, 0x20000, 0)  # USAGE_STAGING, CPU_ACCESS_READ
        tex = c_void_p()
        _ok(_com(self.device, 5, POINTER(_TEXTURE2D_DESC), c_void_p, POINTER(c_void_p))(byref(desc), None, byref(tex)),
            "CreateTexture2D")
        self.staging, self.staging_size = tex, (w, h)

    def grab(self, rect: tuple, timeout_ms: int = 30) -> Optional[np.ndarray]:
        """画面の座標の範囲 rect（left, top, right, bottom）を BGR で返す。画面が変わっていなければ前回の画像。"""
        ox, oy = self.origin
        left, top = max(rect[0] - ox, 0), max(rect[1] - oy, 0)
        right, bottom = min(rect[2] - ox, self.size[0]), min(rect[3] - oy, self.size[1])
        if right - left < 16 or bottom - top < 16:
            return None
        if self._last_rect != (left, top, right, bottom):
            self._last = None
            self._last_rect = (left, top, right, bottom)

        info = _FRAME_INFO()
        resource = c_void_p()
        hr = _com(self.dupl, 8, c_uint, POINTER(_FRAME_INFO), POINTER(c_void_p))(timeout_ms, byref(info), byref(resource))
        code = hr & 0xFFFFFFFF
        if code == DXGI_ERROR_WAIT_TIMEOUT:
            return self._last  # 前回から表示が変わっていない
        if code == DXGI_ERROR_ACCESS_LOST:
            # 画面モードの切り替え・UAC など。作り直す（作り直した直後は、画面が変わるまで画像がない）
            self.close()
            self._open()
            self.reopened += 1
            self._last = None
            return None
        _ok(hr, "AcquireNextFrame")
        if info.LastPresentTime == 0:
            # マウスの動きだけ（画面の中身は更新されていない。作った直後は真っ黒のこともある）。前回の画像のまま
            _release(resource)
            _com(self.dupl, 14)()  # ReleaseFrame
            return self._last
        try:
            tex = c_void_p()
            _ok(_com(resource, 0, POINTER(_GUID), POINTER(c_void_p))(byref(IID_ID3D11Texture2D), byref(tex)),
                "QueryInterface(ID3D11Texture2D)")
            try:
                desc = _TEXTURE2D_DESC()
                _com(tex, 10, POINTER(_TEXTURE2D_DESC), restype=None)(byref(desc))
                if desc.Format != DXGI_FORMAT_B8G8R8A8_UNORM:
                    raise DxgiError(f"画面の形式 {desc.Format} の読み取り", 0x80004001)
                w, h = right - left, bottom - top
                self._ensure_staging(desc.Format, w, h)
                box = _BOX(left, top, 0, right, bottom, 1)
                # CopySubresourceRegion(dst, 0, 0, 0, 0, src, 0, box)
                _com(self.context, 46, c_void_p, c_uint, c_uint, c_uint, c_uint, c_void_p, c_uint, POINTER(_BOX),
                     restype=None)(self.staging, 0, 0, 0, 0, tex, 0, byref(box))
            finally:
                _release(tex)
        finally:
            _release(resource)
            _com(self.dupl, 14)()  # ReleaseFrame

        mapped = _MAPPED()
        _ok(_com(self.context, 14, c_void_p, c_uint, c_uint, c_uint, POINTER(_MAPPED))(self.staging, 0, 1, 0, byref(mapped)),
            "Map")
        try:
            w, h = self.staging_size
            buf = (ctypes.c_ubyte * (mapped.RowPitch * h)).from_address(mapped.pData)
            arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, mapped.RowPitch // 4, 4)[:, :w, :3]
            self._last = np.ascontiguousarray(arr)
        finally:
            _com(self.context, 15, c_void_p, c_uint, restype=None)(self.staging, 0)  # Unmap
        return self._last


# ---------------------------------------------------------------------------
# 2. PrintWindow（クライアント領域だけ） / 3. 画面のコピー
# ---------------------------------------------------------------------------

PW_CLIENTONLY_RENDERFULLCONTENT = 3


def printwindow_client(hwnd) -> Optional[np.ndarray]:
    l, t, r, b = win32gui.GetClientRect(hwnd)
    w, h = r - l, b - t
    if w <= 0 or h <= 0:
        return None
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(mfc_dc, w, h)
    save_dc.SelectObject(bitmap)
    try:
        if ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), PW_CLIENTONLY_RENDERFULLCONTENT) != 1:
            return None
        info = bitmap.GetInfo()
        img = np.frombuffer(bitmap.GetBitmapBits(True), dtype=np.uint8).reshape(info["bmHeight"], info["bmWidth"], 4)
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    finally:
        win32gui.DeleteObject(bitmap.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)


def gdi_grab(rect: tuple) -> Optional[np.ndarray]:
    from mss import mss
    l, t, r, b = rect
    if r - l <= 0 or b - t <= 0:
        return None
    with mss() as sct:
        shot = sct.grab({"left": l, "top": t, "width": r - l, "height": b - t})
    return cv2.cvtColor(np.array(shot), cv2.COLOR_BGRA2BGR)


# ---------------------------------------------------------------------------
# 方式の選択・切り替え
# ---------------------------------------------------------------------------

# この秒数、真っ黒のままなら、別の方式でも撮って比べる（暗転は 1〜2 秒なので、それより長く）
BLACK_PROBE_SEC = 4.0
# 真っ黒とみなす明るさ（最大値）
BLACK_MAX = 8
# 失敗した方式を、もう一度試すまでの秒数
RETRY_SEC = 20.0
# 方式が変わったとログに出すのは、新しい方式がこの回数続いたとき
STABLE_FRAMES = 5
# PrintWindow の画像がこの秒数まったく変わらなければ、撮影が止まっている可能性としてログに残す
STALE_WARN_SEC = 15.0


def _is_black(img: np.ndarray) -> bool:
    return img is None or int(img[::8, ::8].max()) <= BLACK_MAX


class FrameSource:
    """ゲームのクライアント領域を撮る。方式は自動で選び、撮れない・真っ黒のままなら切り替える。"""

    def __init__(self, log=None, label: str = "[ロード画面監視]"):
        self._log = log or (lambda m: None)
        self._label = label
        self._last_sig: Optional[bytes] = None
        self._same_since: Optional[float] = None
        self._stale_warned = False
        self._dxgi: Optional[DesktopDuplication] = None
        self._disabled_until: dict[str, float] = {}
        self._black_since: Optional[float] = None
        self.backend: Optional[str] = None   # いま主に使っている方式
        self.last_state: Optional[WindowState] = None
        self.switches = 0
        self._candidate: Optional[str] = None
        self._streak = 0
        self._dxgi_reopened = 0
        self.last_dxgi_use = 0.0        # 最後に DXGI で撮った時刻（本体の撮影は、使わない時間が続いたら閉じる）

    def close(self) -> None:
        if self._dxgi is not None:
            self._dxgi_reopened += self._dxgi.reopened
            self._dxgi.close()
            self._dxgi = None

    def _disable(self, name: str, reason: str) -> None:
        self._disabled_until[name] = time.monotonic() + RETRY_SEC
        note_capture_failure(name, reason)
        self._log(f"{self._label} 撮影方式 {name} を一時的に使いません: {reason}")
        if name == "dxgi":
            self.close()

    def _order(self, st: WindowState) -> list[str]:
        if st.foreground:
            return ["dxgi", "printwindow", "gdi"]
        # 裏に回っている: 画面に見えているのは別の窓なので、ウィンドウそのものを撮る方式だけ
        return ["printwindow"]

    def _grab_with(self, name: str, st: WindowState) -> Optional[np.ndarray]:
        if name == "dxgi":
            if self._dxgi is None or self._dxgi.hmonitor != st.monitor:
                self.close()
                self._dxgi = DesktopDuplication(st.monitor)
            self.last_dxgi_use = time.monotonic()
            return self._dxgi.grab(st.client)
        if name == "printwindow":
            return printwindow_client(st.hwnd)
        return gdi_grab(st.client)

    def grab(self, hwnd) -> Optional[np.ndarray]:
        st = window_state(hwnd)
        self.last_state = st
        if st is None or st.minimized or st.width <= 0 or st.height <= 0:
            return None
        now = time.monotonic()
        candidates = [n for n in self._order(st) if self._disabled_until.get(n, 0) <= now]
        for name in candidates:
            try:
                img = self._grab_with(name, st)
            except Exception as e:
                self._disable(name, repr(e))
                continue
            if img is None:
                continue  # この回は撮れなかった（DXGI で画面が変わっていない等）。次の方式で撮る
            self._note_backend(name, st)
            self._check_black(name, st, img, candidates)
            self._check_stale(name, st, img)
            return img
        return None

    def _check_stale(self, name: str, st: WindowState, img: np.ndarray) -> None:
        """PrintWindow の画像が長く変わらない（Windows がウィンドウの中身を更新していない）ときに、ログに残す（調査用）。
        AC6 はロビーでも背景が動くため、まったく同じ画像が続くのは、撮影が止まっている可能性が高い"""
        if name != "printwindow":
            self._same_since, self._last_sig = None, None
            return
        sig = img[::16, ::16].tobytes()
        now = time.monotonic()
        if sig != self._last_sig:
            self._last_sig, self._same_since, self._stale_warned = sig, now, False
            return
        if not self._stale_warned and self._same_since is not None and now - self._same_since >= STALE_WARN_SEC:
            self._stale_warned = True
            self._log(f"{self._label} [警告] PrintWindow の画像が{STALE_WARN_SEC:.0f}秒以上変わっていません"
                      f"（撮影が止まっている可能性。画面モード={st.mode}、手前={'はい' if st.foreground else 'いいえ'}）")

    def _note_backend(self, name: str, st: WindowState) -> None:
        """使った方式を記録する。ログは、同じ方式が STABLE_FRAMES 回続いて変わったときだけ（1 回だけの代わりは出さない）"""
        if name == self.backend:
            return
        self._streak = self._streak + 1 if name == self._candidate else 1
        self._candidate = name
        if self.backend is not None and self._streak < STABLE_FRAMES:
            return
        if self.backend is not None:
            self.switches += 1
        self._log(f"{self._label} 撮影方式: {name}（画面モード={st.mode}, {st.width}x{st.height}, "
                  f"手前={'はい' if st.foreground else 'いいえ'}）")
        self.backend = name
        self._black_since = None

    @property
    def dxgi_reopened(self) -> int:
        return self._dxgi_reopened + (self._dxgi.reopened if self._dxgi is not None else 0)

    def _check_black(self, name: str, st: WindowState, img: np.ndarray, candidates: list[str]) -> None:
        """真っ黒が長く続くときは、ほかの方式で撮って比べ、そちらに中身があれば切り替える"""
        now = time.monotonic()
        if not _is_black(img):
            self._black_since = None
            return
        if self._black_since is None:
            self._black_since = now
            return
        if now - self._black_since < BLACK_PROBE_SEC:
            return
        self._black_since = now
        for other in candidates:
            if other == name:
                continue
            try:
                probe = self._grab_with(other, st)
            except Exception:
                continue  # ほかの撮り方が失敗したら、その撮り方は候補から外すだけ
            if not _is_black(probe):
                self._disable(name, f"真っ黒のままでした（{other} では写っています）")
                return


# ---------------------------------------------------------------------------
# 本体（main.py のプロセス）の撮影: モード・対戦状態・ロード監視などの読み取りで共有する
# ---------------------------------------------------------------------------
# 以前は PrintWindow だけで撮っていた。ゲームの描画方式によっては、Windows がウィンドウの中身を更新しなくなり、
# PrintWindow が古い画面を返し続ける（2026-10-12: 試合の開始から終わりまで、ロビーの画面を読み続けて記録されなかった）。
# ゲームが手前にあるときは、画面に表示されている内容を読む DXGI を使う（FrameSource と同じ選び方）。
# 画面の見張りのスレッドと本体のスレッドの両方から呼ばれるため、ロックで 1 つずつ撮る。

_shared_lock = threading.Lock()
_shared_source: Optional[FrameSource] = None
_idle_watch_started = False

# DXGI（Desktop Duplication）を開いている間は、フルスクリーンのゲームの表示が、デスクトップの合成を通されることがある。
# 高いリフレッシュレート（240Hz など）のモニターでカクつく原因になりうるため（2026-11 の報告）、本体の撮影で
# DXGI をこの秒数使わなかったら閉じる（AC6 を閉じた・最小化した・裏に回したあとまで、開いたままにしない）
DXGI_IDLE_CLOSE_SEC = 30.0


def _close_idle_dxgi() -> None:
    while True:
        time.sleep(5.0)
        with _shared_lock:
            src = _shared_source
            if src is not None and src._dxgi is not None and time.monotonic() - src.last_dxgi_use >= DXGI_IDLE_CLOSE_SEC:
                src.close()


# 撮影の失敗の回数と最後の理由（方式ごと。このプロセスの分だけ。調査用）
capture_failures: dict = {}


def note_capture_failure(name: str, reason: str) -> None:
    item = capture_failures.setdefault(name, {"count": 0, "last": None})
    item["count"] += 1
    item["last"] = str(reason)[:200]


def grab_client(hwnd) -> Optional[np.ndarray]:
    """ゲームのクライアント領域を BGR で返す（撮れなければ None）"""
    global _shared_source, _idle_watch_started
    with _shared_lock:
        if _shared_source is None:
            from ravens_record_core import logger
            _shared_source = FrameSource(log=logger.log, label="[撮影]")
        if not _idle_watch_started:
            _idle_watch_started = True
            threading.Thread(target=_close_idle_dxgi, name="rrc-dxgi-idle", daemon=True).start()
        return _shared_source.grab(hwnd)


def monitor_fallback_allowed(hwnd) -> bool:
    """ゲームのウィンドウを撮れなかったとき、モニター全体を撮って代わりにしてよいか。
    ゲームのウィンドウがいちばん手前にあるときだけ（モニターに写っているのがゲームの画面のとき）。
    ゲームが見つからない・裏にある・最小化中にモニターを撮ると、手前の別のアプリ（ブラウザ・音楽アプリなど）を読んでしまう（2026-11）"""
    if hwnd is None:
        return False
    try:
        return (not win32gui.IsIconic(hwnd)) and win32gui.GetForegroundWindow() == hwnd
    except Exception:
        return False  # ウィンドウが閉じた直後などで失敗する（モニターを撮らない側に倒す）


# ---------------------------------------------------------------------------
# 16:9 以外の画面（黒い帯）
# ---------------------------------------------------------------------------
# AC6 の画面は 16:9。16:10 のモニター（1920x1200 など）や、横長のモニターでは、上下・左右に黒い帯が付くことがある。
# 読み取りの範囲（ROI）は 16:9 の画面に対する割合で決めているため、帯が付くと、右上の「マッチングしています」などが
# 範囲から外れる。帯が真っ黒なときだけ、帯を除いた 16:9 の部分を返す（帯が黒くなければ、そのまま）。

GAME_ASPECT = 16 / 9
_BAR_MIN_PX = 4          # これより細い差は、帯とみなさない
_BAR_BRIGHT = 32         # これより明るい画素を、帯の外（ゲームの絵）とみなす
_BAR_BRIGHT_RATIO = 0.002  # 帯の中で、明るい画素がこの割合未満なら、真っ黒の帯


def _is_black(strip: np.ndarray) -> bool:
    return strip.size > 0 and float((strip.max(axis=2) > _BAR_BRIGHT).mean()) < _BAR_BRIGHT_RATIO


def game_area(img: Optional[np.ndarray]) -> tuple[Optional[np.ndarray], bool]:
    """黒い帯を除いたゲームの画面と、除いたかどうか"""
    if img is None or img.ndim != 3 or img.size == 0:
        return img, False
    h, w = img.shape[:2]
    if w <= 0 or h <= 0:
        return img, False
    if w / h < GAME_ASPECT - 0.02:
        content_h = int(round(w / GAME_ASPECT))
        bar = (h - content_h) // 2
        if bar >= _BAR_MIN_PX and _is_black(img[:bar]) and _is_black(img[h - bar:]):
            return img[bar:bar + content_h], True
    elif w / h > GAME_ASPECT + 0.02:
        content_w = int(round(h * GAME_ASPECT))
        bar = (w - content_w) // 2
        if bar >= _BAR_MIN_PX and _is_black(img[:, :bar]) and _is_black(img[:, w - bar:]):
            return img[:, bar:bar + content_w], True
    return img, False


def menu_area(img: Optional[np.ndarray]) -> tuple[Optional[np.ndarray], bool]:
    """ロビー・マッチングの表示・リザルト・紹介の映像を読むための 16:9 の画面と、切り出したかどうか（v1.4.12〜）。
    横長のモニター（21:9 など）でゲームが横いっぱいに描いているとき、これらの UI は中央の 16:9 に収まり、
    左右には景色が広く描かれるだけ（紹介の映像の見本で確かめた）。そのため、帯が黒くなくても中央の 16:9 を使う。
    対戦中の表示（AP・帯など）は、横長で端に寄るかを確かめていないので、ここは使わない（game_area のまま）。"""
    area, trimmed = game_area(img)
    if trimmed or area is None or area.ndim != 3 or area.size == 0:
        return area, trimmed
    h, w = area.shape[:2]
    if w / h > GAME_ASPECT + 0.02:
        content_w = int(round(h * GAME_ASPECT))
        x0 = (w - content_w) // 2
        return area[:, x0:x0 + content_w], True
    return area, False
