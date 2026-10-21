# -*- coding: utf-8 -*-
"""
Q라이트(Q-Light) ST45L-ETN / ST56(M)EL, ST80(M)EL-ETN 시리즈 네트워크 경광등 제어.
- 프로토콜: "Data format of Socket Program_V01(ENG).pdf" 기준.
- TCP 포트: 20000 (Ethernet Tower Lamp Library 매뉴얼).
- 10바이트: W(0x57) + Type + R,Y,G,B,W(0=Off,1=On,2=Blink) + Sound(0~5) + Spare 2바이트.
※ Library 매뉴얼은 1=Blink 2=ON 이라 다름. 동작 안 하면 LAMP_ON/LAMP_BLINK 값 맞춰볼 것.
※ meal_manage 와 동일: trigger_ok(녹색+사운드1), trigger_alert(빨강+사운드2). pc_app 은 blink=False.
"""
import socket
import os
import ctypes
import time
import threading
from pathlib import Path
from typing import Optional

# 매뉴얼 기준 포트
QLIGHT_PORT = 20000
DEFAULT_TIMEOUT = 3.0
_ROOT_DIR = Path(__file__).resolve().parent

# 램프 제어값 (Socket Program 문서)
LAMP_OFF = 0
LAMP_ON = 1
LAMP_BLINK = 2

# 일부 모델/설정에서 ON/BLINK 값이 반대로 동작할 수 있어 토글 지원
_SWAP_ON_BLINK = os.environ.get("QLIGHT_SWAP_ON_BLINK", "").strip().lower() in ("1", "true", "yes", "y", "on")
if _SWAP_ON_BLINK:
    LAMP_ON, LAMP_BLINK = LAMP_BLINK, LAMP_ON

# 사운드: 0=Off, 1~5=Sound 선택 (톤이 다름)
SOUND_OFF = 0
SOUND_OK = 1      # 녹색용
SOUND_ALERT = 2   # 빨강용
_BUZZER_PULSE_SEC = float(os.environ.get("QLIGHT_BUZZER_PULSE_SEC", "0.5"))
try:
    _WRITE_RETRIES = max(1, min(5, int(os.environ.get("QLIGHT_WRITE_RETRIES", "3") or "3")))
except (TypeError, ValueError):
    _WRITE_RETRIES = 3
_INTER_CMD_GAP_SEC = max(0.0, float(os.environ.get("QLIGHT_INTER_CMD_GAP_SEC", "0.08")))
_QLIGHT_LOCK = threading.Lock()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def default_ok_sound() -> int:
    """성공(녹색) 부저 — meal_manage 기본 1."""
    return max(0, min(5, _env_int("QLIGHT_OK_SOUND", SOUND_OK)))


def default_alert_sound() -> int:
    """실패(빨강) 부저 — meal_manage 기본 2 (녹색과 다른 소리)."""
    return max(0, min(5, _env_int("QLIGHT_ALERT_SOUND", SOUND_ALERT)))

# 제어 방식: auto(기본)=DLL 우선 후 RAW TCP fallback, dll, raw
_MODE = os.environ.get("QLIGHT_MODE", "auto").strip().lower()
# DLL 우선 탐색 경로(환경변수로 추가 가능: 세미콜론 구분)
_DLL_CANDIDATES = [
    _ROOT_DIR / "Ex64_dllc.dll",
    _ROOT_DIR / "Qtvc_dll.dll",
    _ROOT_DIR / "2. Sample Program(x64)" / "4.Library" / "Dll_x64" / "VC10_and_VC#10" / "Ex64_dllc.dll",
    _ROOT_DIR / "2. Sample Program(x64)" / "4.Library" / "Dll_x86" / "VC10_and_VC#10" / "Qtvc_dll.dll",
]
_EXTRA_DLLS = [p.strip() for p in os.environ.get("QLIGHT_DLL_PATHS", "").split(";") if p.strip()]
for _p in _EXTRA_DLLS:
    _DLL_CANDIDATES.insert(0, Path(_p))


def _load_dll_function():
    """Tcp_Qu_RW export를 가진 DLL 함수 로드(없으면 None)."""
    for dll_path in _DLL_CANDIDATES:
        try:
            if not dll_path.exists():
                continue
            lib = ctypes.WinDLL(str(dll_path))
            fn = lib.Tcp_Qu_RW
            fn.argtypes = [
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_ubyte),
                ctypes.POINTER(ctypes.c_ubyte),
            ]
            fn.restype = ctypes.c_bool
            print(f"[QLIGHT] DLL loaded: {dll_path}")
            return fn
        except Exception as e:
            print(f"[QLIGHT] DLL load failed: {dll_path} error={e}")
    return None


_TCP_QU_RW = _load_dll_function() if _MODE in ("auto", "dll") else None


def _ip_to_4bytes(host: str):
    parts = (host or "").strip().split(".")
    if len(parts) != 4:
        raise ValueError(f"invalid ip: {host}")
    out = []
    for p in parts:
        v = int(p)
        if v < 0 or v > 255:
            raise ValueError(f"invalid ip octet: {p}")
        out.append(v)
    return out


def trigger_ok(host: str, port: int = QLIGHT_PORT, blink: bool = True, sound: Optional[int] = None, timeout: float = DEFAULT_TIMEOUT) -> bool:
    """정상/완료: 녹색 + 부저(기본 1번)."""
    if sound is None:
        sound = default_ok_sound()
    g = LAMP_BLINK if blink else LAMP_ON
    ok = write_lamp(host, port, LAMP_OFF, LAMP_OFF, g, LAMP_OFF, LAMP_OFF, sound, timeout)
    if ok:
        write_lamp(host, port, LAMP_OFF, LAMP_OFF, g, LAMP_OFF, LAMP_OFF, sound, timeout)
    if ok and _BUZZER_PULSE_SEC > 0:
        time.sleep(_BUZZER_PULSE_SEC)
        write_lamp(host, port, LAMP_OFF, LAMP_OFF, LAMP_OFF, LAMP_OFF, LAMP_OFF, SOUND_OFF, timeout)
    return ok


def trigger_alert(host: str, port: int = QLIGHT_PORT, blink: bool = True, sound: Optional[int] = None, timeout: float = DEFAULT_TIMEOUT) -> bool:
    """알림/에러: 빨강 + 부저(기본 2번, 녹색과 다름)."""
    if sound is None:
        sound = default_alert_sound()
    r = LAMP_BLINK if blink else LAMP_ON
    ok = write_lamp(host, port, r, LAMP_OFF, LAMP_OFF, LAMP_OFF, LAMP_OFF, sound, timeout)
    if ok:
        write_lamp(host, port, r, LAMP_OFF, LAMP_OFF, LAMP_OFF, LAMP_OFF, sound, timeout)
    if ok and _BUZZER_PULSE_SEC > 0:
        time.sleep(_BUZZER_PULSE_SEC)
        write_lamp(host, port, LAMP_OFF, LAMP_OFF, LAMP_OFF, LAMP_OFF, LAMP_OFF, SOUND_OFF, timeout)
    return ok


def _make_write_packet(
    red: int = LAMP_OFF,
    yellow: int = LAMP_OFF,
    green: int = LAMP_OFF,
    blue: int = LAMP_OFF,
    white: int = LAMP_OFF,
    sound: int = SOUND_OFF,
    sound_type: int = 0,
) -> bytes:
    """
    10바이트 Write 패킷 생성.
    sound_type: 0=5 sound model, 0~4=25 sound model 그룹.
    """
    return bytes([
        0x57,  # 'W' Write
        sound_type & 0xFF,
        red & 0xFF,
        yellow & 0xFF,
        green & 0xFF,
        blue & 0xFF,
        white & 0xFF,
        sound & 0xFF,
        0x00,
        0x00,
    ])


def _make_dll_write_data(
    red: int = LAMP_OFF,
    yellow: int = LAMP_OFF,
    green: int = LAMP_OFF,
    blue: int = LAMP_OFF,
    white: int = LAMP_OFF,
    sound: int = SOUND_OFF,
    sound_type: int = 0,
) -> bytes:
    """
    Tcp_Qu_RW용 pbData(10바이트) 생성.
    pbData[0]=1(write), pbData[1]=type, pbData[2..6]=lamp, pbData[7]=sound.
    """
    return bytes([
        0x01,  # write
        sound_type & 0xFF,
        red & 0xFF,
        yellow & 0xFF,
        green & 0xFF,
        blue & 0xFF,
        white & 0xFF,
        sound & 0xFF,
        0x00,
        0x00,
    ])


def _write_lamp_tcp(host: str, port: int, payload: bytes, timeout: float) -> bool:
    """RAW TCP 전송 — 재시도·순차 잠금으로 간헐 실패 완화."""
    last_err = None
    for attempt in range(_WRITE_RETRIES):
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect((host, port))
            s.sendall(payload)
            time.sleep(0.03)
            try:
                s.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            time.sleep(0.02)
            return True
        except Exception as e:
            last_err = e
            if attempt + 1 < _WRITE_RETRIES:
                time.sleep(0.12 * (attempt + 1))
        finally:
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
            if attempt + 1 < _WRITE_RETRIES:
                time.sleep(_INTER_CMD_GAP_SEC)
    if last_err is not None:
        print(f"[QLIGHT] write_lamp failed host={host} port={port} error={last_err}")
    return False


def write_lamp(
    host: str,
    port: int = QLIGHT_PORT,
    red: int = LAMP_OFF,
    yellow: int = LAMP_OFF,
    green: int = LAMP_OFF,
    blue: int = LAMP_OFF,
    white: int = LAMP_OFF,
    sound: int = SOUND_OFF,
    timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """
    경광등 램프/사운드 제어 (10바이트 TCP 전송).
    램프: 0=Off, 1=On, 2=Blink. sound: 0=Off, 1~5=Sound 선택.
    """
    payload = _make_write_packet(red, yellow, green, blue, white, sound)

    with _QLIGHT_LOCK:
        # DLL 우선 (auto/dll) — meal_manage 와 동일
        if _TCP_QU_RW is not None and _MODE in ("auto", "dll"):
            try:
                ip4 = _ip_to_4bytes(host)
                ip_buf = (ctypes.c_ubyte * 4)(*ip4)
                dll_payload = _make_dll_write_data(red, yellow, green, blue, white, sound)
                data_buf = (ctypes.c_ubyte * 10)(*dll_payload)
                ok = bool(_TCP_QU_RW(int(port), ip_buf, data_buf))
                if ok:
                    time.sleep(_INTER_CMD_GAP_SEC)
                    return True
                print(f"[QLIGHT] DLL call returned False host={host} port={port}")
                if _MODE == "dll":
                    return False
            except Exception as e:
                print(f"[QLIGHT] DLL call failed host={host} port={port} error={e}")
                if _MODE == "dll":
                    return False

        if _MODE == "dll":
            return False

        ok = _write_lamp_tcp(host, port, payload, timeout)
        if ok:
            time.sleep(_INTER_CMD_GAP_SEC)
        return ok


def trigger(
    host: str,
    port: int = QLIGHT_PORT,
    lamp: str = "green",
    blink: bool = True,
    sound: int = 0,
    timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """
    식사 인증 등 알림용 트리거: 지정 램프를 켜거나 깜빡임.
    lamp: 'red','green'. blink=True면 Blink, False면 On.
    """
    r = g = LAMP_OFF
    v = LAMP_BLINK if blink else LAMP_ON
    if lamp == "red":
        r = v
    else:
        g = v
    return write_lamp(host, port, r, LAMP_OFF, g, LAMP_OFF, LAMP_OFF, sound, timeout)


def trigger_off(
    host: str,
    port: int = QLIGHT_PORT,
    timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """모든 램프·사운드 끄기."""
    return write_lamp(host, port, LAMP_OFF, LAMP_OFF, LAMP_OFF, LAMP_OFF, LAMP_OFF, SOUND_OFF, timeout)
