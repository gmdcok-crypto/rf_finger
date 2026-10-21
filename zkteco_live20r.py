# ZKTeco Live20R / ZKFinger USB 리더용 ctypes 래퍼.
# ZKTeco 공식 ZKFinger SDK 설치 후 libzkfp.dll 이 로드 가능한 경로에 있어야 합니다.
# 환경변수 ZKFP_DLL_DIR: DLL 및 의존 DLL이 있는 폴더 (예: SDK의 x64 디렉터리)

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import (
    POINTER,
    byref,
    cast,
    c_int,
    c_uint,
    c_ubyte,
    c_void_p,
)

# SDK 문서·샘플에서 흔히 쓰는 이미지 크기 파라미터 코드 (실패 시 256x288 기본값)
_PARAM_IMAGE_WIDTH = int(os.environ.get("ZKFP_PARAM_WIDTH_CODE", "1"))
_PARAM_IMAGE_HEIGHT = int(os.environ.get("ZKFP_PARAM_HEIGHT_CODE", "2"))
_DEFAULT_W = 256
_DEFAULT_H = 288
_MAX_TEMPLATE = int(os.environ.get("ZKFP_MAX_TEMPLATE", "4096"))


def _dll_search_dirs():
    root = os.path.dirname(os.path.abspath(__file__))
    env = (os.environ.get("ZKFP_DLL_DIR") or "").strip()
    out = []
    if env:
        out.append(env)
    out.append(root)
    if sys.platform == "win32":
        out.extend(
            [
                r"C:\Program Files (x86)\ZKTeco\ZKFinger SDK\bin",
                r"C:\Program Files\ZKTeco\ZKFinger SDK\bin",
            ]
        )
    return [d for d in out if d and os.path.isdir(d)]


def _load_libzkfp():
    if sys.platform != "win32":
        raise OSError("ZKTeco libzkfp 연동은 Windows에서만 지원합니다.")
    names = ("libzkfp.dll", "zkfp.dll", "zkfp2.dll")
    last_err = None
    for d in _dll_search_dirs():
        for name in names:
            path = os.path.join(d, name)
            if not os.path.isfile(path):
                continue
            try:
                os.add_dll_directory(d)
                return ctypes.WinDLL(path)
            except OSError as e:
                last_err = e
    try:
        return ctypes.WinDLL("libzkfp")
    except OSError as e:
        last_err = e
    raise OSError(
        "libzkfp.dll 을 로드할 수 없습니다. ZKTeco ZKFinger SDK를 설치하고 "
        "ZKFP_DLL_DIR 환경변수에 DLL 폴더를 지정하세요. (%s)" % (last_err,)
    )


def _bind(dll, names, restype, argtypes):
    for n in names:
        if hasattr(dll, n):
            fn = getattr(dll, n)
            fn.restype = restype
            fn.argtypes = argtypes
            return fn, n
    raise AttributeError("DLL에 다음 함수 중 하나가 없습니다: %s" % ", ".join(names))


class ZkFpmSession:
    """ZKFPM_* API (libzkfp.dll) 세션. 컨텍스트 매니저로 사용."""

    RET_NO_FINGER = -8
    # pyzkfp Light() 와 동일 계열 (ZKFinger SDK)
    _LED_WHITE = 101
    _LED_GREEN = 102
    _LED_RED = 103

    def __init__(self):
        self._dll = _load_libzkfp()
        self._init, _ = _bind(self._dll, ("ZKFPM_Init", "ZKFP2_Init"), c_int, [])
        self._terminate, _ = _bind(self._dll, ("ZKFPM_Terminate", "ZKFP2_Terminate"), c_int, [])
        self._get_count, _ = _bind(self._dll, ("ZKFPM_GetDeviceCount", "ZKFP2_GetDeviceCount"), c_int, [])
        self._open, on = _bind(
            self._dll,
            ("ZKFPM_OpenDevice", "ZKFP2_OpenDevice"),
            c_void_p,
            [c_int],
        )
        self._close, cn = _bind(
            self._dll,
            ("ZKFPM_CloseDevice", "ZKFP2_CloseDevice"),
            c_int,
            [c_void_p],
        )
        self._get_params, _ = _bind(
            self._dll,
            ("ZKFPM_GetParameters", "ZKFP2_GetParameters"),
            c_int,
            [c_void_p, c_int, POINTER(c_ubyte), POINTER(c_uint)],
        )
        try:
            self._set_params, _ = _bind(
                self._dll,
                ("ZKFPM_SetParameters", "ZKFP2_SetParameters"),
                c_int,
                [c_void_p, c_int, POINTER(c_ubyte), c_uint],
            )
        except AttributeError:
            self._set_params = None
        self._acquire, an = _bind(
            self._dll,
            ("ZKFPM_AcquireFingerprint", "ZKFP2_AcquireFingerprint"),
            c_int,
            [c_void_p, POINTER(c_ubyte), POINTER(c_ubyte), POINTER(c_uint)],
        )
        self._handle = None
        self._width = _DEFAULT_W
        self._height = _DEFAULT_H
        self._db_handle = None
        self._db_free = None
        self._db_match = None
        try:
            self._db_init, _ = _bind(self._dll, ("ZKFP2_DBInit", "ZKFPM_DBInit"), c_void_p, [])
        except AttributeError:
            self._db_init = None
        try:
            self._db_free, _ = _bind(self._dll, ("ZKFP2_DBFree", "ZKFPM_DBFree"), c_int, [c_void_p])
        except AttributeError:
            self._db_free = None
        try:
            self._db_match, _ = _bind(
                self._dll,
                ("ZKFP2_DBMatch", "ZKFPM_DBMatch"),
                c_int,
                [c_void_p, POINTER(c_ubyte), POINTER(c_ubyte)],
            )
        except AttributeError:
            self._db_match = None

    def __enter__(self):
        r = self._init()
        if r != 0:
            raise RuntimeError("ZKFPM_Init 실패 (코드 %s)" % r)
        n = self._get_count()
        if n <= 0:
            self._terminate()
            raise RuntimeError("연결된 지문 리더가 없습니다. Live20R USB를 확인하세요.")
        self._handle = self._open(0)
        if not self._handle:
            self._terminate()
            raise RuntimeError("OpenDevice(0) 실패")
        w, h = self._read_image_size()
        if w and h and w > 0 and h > 0 and w * h < 512 * 512:
            self._width, self._height = int(w), int(h)
        if self._db_init and self._db_free and self._db_match:
            try:
                self._db_handle = self._db_init()
            except Exception:
                self._db_handle = None
            if not self._db_handle:
                self._db_handle = None
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._db_handle and self._db_free:
            try:
                self._db_free(self._db_handle)
            except Exception:
                pass
            self._db_handle = None
        if self._handle:
            try:
                self.led_scan_idle()
            except Exception:
                pass
            try:
                self._close(self._handle)
            except Exception:
                pass
            self._handle = None
        try:
            self._terminate()
        except Exception:
            pass
        return False

    def _read_image_size(self):
        def read_u32(code):
            buf = (c_ubyte * 4)()
            sz = c_uint(4)
            r = self._get_params(self._handle, code, cast(buf, POINTER(c_ubyte)), byref(sz))
            if r != 0:
                return None
            return int.from_bytes(bytes(buf), "little", signed=False)

        w = read_u32(_PARAM_IMAGE_WIDTH)
        h = read_u32(_PARAM_IMAGE_HEIGHT)
        return w, h

    @property
    def image_size(self):
        return self._width * self._height

    @property
    def width(self):
        return self._width

    @property
    def height(self):
        return self._height

    def led_scan_ready(self):
        """센서에 손가락을 올리라는 뜻으로 LED(녹색, 실패 시 백색). 미지원 DLL이면 무시."""
        if not self._handle or not self._set_params:
            return
        buf_on = (c_ubyte * 4)(1, 0, 0, 0)
        try:
            self._set_params(self._handle, self._LED_GREEN, buf_on, 4)
        except Exception:
            try:
                self._set_params(self._handle, self._LED_WHITE, buf_on, 4)
            except Exception:
                pass

    def led_scan_idle(self):
        """대기 종료 시 LED 끄기(시도)."""
        if not self._handle or not self._set_params:
            return
        buf_off = (c_ubyte * 4)(0, 0, 0, 0)
        for code in (self._LED_GREEN, self._LED_WHITE):
            try:
                self._set_params(self._handle, code, buf_off, 4)
            except Exception:
                pass

    def try_acquire_once(self):
        """한 번 캡처 시도. 손가락 없음(None) 또는 (template_bytes, image_bytes) 반환."""
        if not self._handle:
            raise RuntimeError("디바이스가 열려 있지 않습니다.")
        img_sz = self.image_size
        img_buf = (c_ubyte * img_sz)()
        tpl_buf = (c_ubyte * _MAX_TEMPLATE)()
        sz = c_uint(_MAX_TEMPLATE)
        r = self._acquire(self._handle, img_buf, tpl_buf, byref(sz))
        if r == self.RET_NO_FINGER:
            return None
        if r != 0:
            raise RuntimeError("지문 캡처 실패 (코드 %s)" % r)
        tpl_len = min(int(sz.value), _MAX_TEMPLATE)
        template = bytes(bytearray(tpl_buf)[:tpl_len])
        image = bytes(bytearray(img_buf)[:img_sz])
        return template, image

    def db_match_templates(self, t1: bytes, t2: bytes) -> int:
        """두 템플릿 1:1 유사도(높을수록 동일 지문에 가깝다). DLL 미지원 시 RuntimeError."""
        if not self._db_handle or not self._db_match:
            raise RuntimeError("이 libzkfp 빌드에서 DBMatch/DBInit 을 쓸 수 없습니다.")
        n = min(_MAX_TEMPLATE, 4096)
        buf1 = (c_ubyte * n)()
        buf2 = (c_ubyte * n)()
        b1 = t1[:n]
        b2 = t2[:n]
        for i in range(len(b1)):
            buf1[i] = b1[i]
        for i in range(len(b2)):
            buf2[i] = b2[i]
        r = int(self._db_match(self._db_handle, buf1, buf2))
        return r


def capture_fingerprint_blocking(timeout_sec=60.0, poll_ms=50):
    """
    장치를 열고 손가락이 올라올 때까지 블로킹 캡처.
    Returns: (template: bytes, image: bytes, width: int, height: int)
    """
    import time

    t0 = time.monotonic()
    with ZkFpmSession() as sess:
        while time.monotonic() - t0 < timeout_sec:
            got = sess.try_acquire_once()
            if got:
                t, img = got
                return t, img, sess.width, sess.height
            time.sleep(poll_ms / 1000.0)
    raise TimeoutError("시간 내에 지문을 읽지 못했습니다.")


def is_driver_available() -> bool:
    try:
        _load_libzkfp()
        return True
    except OSError:
        return False
