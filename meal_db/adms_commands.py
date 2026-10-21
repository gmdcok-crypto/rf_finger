"""
ZKTeco ADMS 단말 명령(getrequest 큐).

정식 참조: docs/adms/Security_PUSH_Communication_Protocol_20240712.pdf
  (치트시트: docs/adms/COMMAND_REFERENCE.md)
"""

from __future__ import annotations

import base64
import os
import re
import threading
from collections import deque
from typing import Deque, Dict, List, Literal, Tuple

AdmsCmdProtocol = Literal["security", "legacy"]

# ZKTeco PUSH SDK (getrequest / devicecmd) 자주 나오는 Return 코드
ADMS_RETURN_HINTS: Dict[int, str] = {
    -629: "명령 형식 오류(테이블명) — Security PUSH는 user/templatev10 형식",
    -631: "Base64 길이 오류(지문 템플릿)",
    -1002: "지원하지 않는 명령(USER ADD 등 — DATA UPDATE 사용)",
}


def adms_cmd_protocol() -> AdmsCmdProtocol:
    """MEAL_ADMS_CMD_PROTOCOL: security(기본·20240712) | legacy(구형 Pull PUSH, 비권장)."""
    raw = (os.environ.get("MEAL_ADMS_CMD_PROTOCOL") or "security").strip().lower()
    if raw in ("legacy", "pull", "userinfo", "old"):
        return "legacy"
    return "security"


def adms_protocol_fallback_enabled() -> bool:
    """MEAL_ADMS_CMD_PROTOCOL=auto 일 때만 구형 USERINFO 재시도."""
    raw = (os.environ.get("MEAL_ADMS_CMD_PROTOCOL") or "security").strip().lower()
    return raw == "auto"


def adms_return_message(code: int) -> str:
    try:
        c = int(code)
    except (TypeError, ValueError):
        return "단말 오류 Return=%s" % code
    hint = ADMS_RETURN_HINTS.get(c)
    if hint:
        return "단말 오류 Return=%s (%s)" % (c, hint)
    return "단말 오류 Return=%s" % c


def truncate_adms_name(name: str, max_bytes: int = 23) -> str:
    if not name:
        return ""
    b = name.encode("utf-8", errors="ignore")[:max_bytes]
    return b.decode("utf-8", errors="ignore")


def _rfid_to_card_str(rf: str) -> str:
    """Security PUSH CardNo — RFID UID를 32비트 부호 없는 정수 문자열로."""
    if not rf:
        return ""
    s = str(rf).strip().replace(" ", "")
    try:
        if s.lower().startswith("0x"):
            return str(int(s, 16) & 0xFFFFFFFF)
        return str(int(s) & 0xFFFFFFFF)
    except ValueError:
        try:
            return str(int(s, 16) & 0xFFFFFFFF)
        except ValueError:
            return s


def build_user_update_command(
    pin: str,
    name: str,
    card: str = "",
    *,
    protocol: AdmsCmdProtocol | None = None,
) -> str:
    """사용자 등록 — Security PUSH 20240712 (user) 또는 구형 Pull PUSH(USERINFO, legacy)."""
    proto = protocol or adms_cmd_protocol()
    pin = str(pin or "").strip()
    nm = truncate_adms_name(name or pin)
    card_s = _rfid_to_card_str(card)
    if proto == "legacy":
        return "DATA UPDATE USERINFO PIN=%s\tName=%s\tPrivilege=0\tCard=%s" % (pin, nm, card_s)
    return (
        "DATA UPDATE user CardNo=%s\tPin=%s\tPassword=0000\tName=%s\t"
        "SuperAuthorize=0\tDisable=0\tGroup=1\tStartTime=0\tEndTime=0\tPrivilege=0"
    ) % (card_s, pin, nm)


def default_authorize_timezone_id() -> int:
    try:
        return max(1, int(os.environ.get("MEAL_ADMS_AUTHORIZE_TIMEZONE_ID", "1") or "1"))
    except (TypeError, ValueError):
        return 1


def default_authorize_door_id() -> int:
    try:
        return max(1, int(os.environ.get("MEAL_ADMS_AUTHORIZE_DOOR_ID", "1") or "1"))
    except (TypeError, ValueError):
        return 1


def resolve_authorize_ids(terminal: dict | None) -> Tuple[int, int]:
    """단말 설정 → (AuthorizeTimezoneId, AuthorizeDoorId). 없으면 env 기본값."""
    tz = default_authorize_timezone_id()
    door = default_authorize_door_id()
    if not isinstance(terminal, dict):
        return tz, door
    raw_tz = terminal.get("authorize_timezone_id")
    if raw_tz is not None and str(raw_tz).strip() != "":
        try:
            tz = max(1, int(raw_tz))
        except (TypeError, ValueError):
            pass
    raw_door = terminal.get("authorize_door_id")
    if raw_door is not None and str(raw_door).strip() != "":
        try:
            door = max(1, int(raw_door))
        except (TypeError, ValueError):
            pass
    return tz, door


def build_userauthorize_update_command(
    pin: str,
    timezone_id: int = 1,
    door_id: int = 1,
    *,
    protocol: AdmsCmdProtocol | None = None,
) -> str:
    """사용자 출입 권한 — 시간대·도어 (Security PUSH userauthorize)."""
    proto = protocol or adms_cmd_protocol()
    pin = str(pin or "").strip()
    if proto == "legacy":
        return ""
    tz = max(1, int(timezone_id))
    door = max(1, int(door_id))
    return (
        "DATA UPDATE userauthorize Pin=%s\tAuthorizeTimezoneId=%d\tAuthorizeDoorId=%d"
    ) % (pin, tz, door)


def build_userauthorize_delete_command(pin: str, *, protocol: AdmsCmdProtocol | None = None) -> str:
    pin = str(pin or "").strip()
    proto = protocol or adms_cmd_protocol()
    if proto == "legacy":
        return ""
    return "DATA DELETE userauthorize Pin=%s" % pin


def build_user_delete_command(pin: str, *, protocol: AdmsCmdProtocol | None = None) -> str:
    pin = str(pin or "").strip()
    proto = protocol or adms_cmd_protocol()
    if proto == "legacy":
        return "DATA DELETE USERINFO PIN=%s" % pin
    return "DATA DELETE user Pin=%s" % pin


def build_fingerprint_update_command(
    pin: str,
    fid: int,
    template_bytes: bytes,
    *,
    protocol: AdmsCmdProtocol | None = None,
) -> str:
    pin = str(pin or "").strip()
    fid = max(0, min(9, int(fid)))
    tpl = template_bytes or b""
    tmp_b64 = base64.b64encode(tpl).decode("ascii")
    proto = protocol or adms_cmd_protocol()
    if proto == "legacy":
        size = len(tpl)
        return "DATA UPDATE FINGERTMP PIN=%s\tFID=%d\tSize=%d\tValid=1\tTMP=%s" % (
            pin,
            fid,
            size,
            tmp_b64,
        )
    return "DATA UPDATE templatev10 Pin=%s\tFingerID=%d\tValid=1\tTemplate=%s" % (
        pin,
        fid,
        tmp_b64,
    )


def build_fingerprint_delete_command(
    pin: str,
    fid: int | None = None,
    *,
    protocol: AdmsCmdProtocol | None = None,
) -> str:
    pin = str(pin or "").strip()
    proto = protocol or adms_cmd_protocol()
    if proto == "legacy":
        if fid is None:
            return "DATA DELETE FINGERTMP PIN=%s" % pin
        return "DATA DELETE FINGERTMP PIN=%s\tFID=%d" % (pin, int(fid))
    if fid is None:
        return "DATA DELETE templatev10 Pin=%s" % pin
    return "DATA DELETE templatev10 Pin=%s FingerID=%d" % (pin, int(fid))


class AdmsCommandQueue:
    """단말 SN별 getrequest 명령 큐 + devicecmd 결과 대기."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_id = 1
        self._by_sn: Dict[str, Deque[dict]] = {}
        self._pending: Dict[int, dict] = {}

    def enqueue(self, sn: str, command: str) -> int:
        sn = (sn or "").strip()
        if not sn:
            raise ValueError("SN required")
        with self._lock:
            cmd_id = int(self._next_id)
            self._next_id += 1
            entry = {
                "id": cmd_id,
                "sn": sn,
                "command": command,
                "done": threading.Event(),
                "return_code": None,
                "cmd_echo": "",
            }
            self._pending[cmd_id] = entry
            self._by_sn.setdefault(sn, deque()).append(entry)
            return cmd_id

    def pop_response_text(self, sn: str, max_cmds: int = 1) -> str:
        """getrequest 응답 본문 — 명령 있으면 C:ID:CMD\\n 만 (끝 OK 없음)."""
        sn = (sn or "").strip()
        lines: List[str] = []
        with self._lock:
            q = self._by_sn.get(sn)
            if not q:
                return "OK"
            n = 0
            limit = max(1, int(max_cmds))
            while q and n < limit:
                entry = q.popleft()
                lines.append("C:%d:%s" % (entry["id"], entry["command"]))
                n += 1
        if not lines:
            return "OK"
        return "\n".join(lines) + "\n"

    def complete(self, sn: str, cmd_id: int, return_code: int, cmd_echo: str = "") -> None:
        with self._lock:
            entry = self._pending.get(int(cmd_id))
            if entry is None:
                return
            entry["return_code"] = int(return_code)
            entry["cmd_echo"] = str(cmd_echo or "")
            entry["done"].set()

    def wait(self, cmd_id: int, timeout_sec: float = 45.0) -> Tuple[bool, str]:
        with self._lock:
            entry = self._pending.get(int(cmd_id))
        if entry is None:
            return False, "명령 ID 없음"
        ok = entry["done"].wait(timeout=max(1.0, float(timeout_sec)))
        with self._lock:
            entry = self._pending.pop(int(cmd_id), entry)
        if not ok:
            return False, "단말 명령 시간 초과(getrequest 대기)"
        rc = entry.get("return_code")
        if rc is None:
            return False, "단말 응답 없음"
        if int(rc) == 0:
            return True, "OK"
        return False, adms_return_message(rc)

    def queue_and_wait(
        self,
        sn: str,
        command: str,
        timeout_sec: float = 45.0,
    ) -> Tuple[bool, str]:
        cmd_id = self.enqueue(sn, command)
        return self.wait(cmd_id, timeout_sec)


def parse_devicecmd_body(body: str, query: dict) -> Tuple[int, int, str]:
    """devicecmd POST → (cmd_id, return_code, cmd_echo)."""
    text = (body or "").strip()
    merged: Dict[str, str] = {}
    for k, vals in (query or {}).items():
        if vals:
            merged[k.upper()] = str(vals[0])
    if text:
        if "&" in text or "=" in text:
            for part in re.split(r"[&;\s]+", text):
                if "=" in part:
                    k, _, v = part.partition("=")
                    merged[k.strip().upper()] = v.strip()
        else:
            for line in text.splitlines():
                if "=" in line:
                    k, _, v = line.partition("=")
                    merged[k.strip().upper()] = v.strip()
    try:
        cmd_id = int(merged.get("ID") or merged.get("CMDID") or "0")
    except (TypeError, ValueError):
        cmd_id = 0
    try:
        ret = int(merged.get("RETURN") or merged.get("RETCODE") or "-1")
    except (TypeError, ValueError):
        ret = -1
    cmd_echo = merged.get("CMD") or ""
    return cmd_id, ret, cmd_echo
