"""F22 출퇴근/단말 연동 모드 (환경 변수 MEAL_F22_ATT_MODE)."""

from __future__ import annotations

import os

F22_ADMS_TERMINAL_PUSH_MSG = (
    "ADMS(Security PUSH): 사원 저장 시 getrequest로 user/templatev10 명령 전송. "
    "정식: Security PUSH Communication Protocol 20240712."
)


def f22_att_mode() -> str:
    raw = os.environ.get("MEAL_F22_ATT_MODE")
    if raw is None or not str(raw).strip():
        return "live"
    m = str(raw).strip().lower()
    if m in ("adms", "push", "adms_push", "zk_push", "cloud", "security_push"):
        return "adms"
    if m in ("poll", "pull", "batch", "0", "false", "no", "off"):
        return "poll"
    if m in ("live", "realtime", "event", "1", "true", "yes", "on"):
        return "live"
    return "live"


def f22_att_mode_is_adms() -> bool:
    return f22_att_mode() == "adms"


def f22_pyzk_terminal_push_skipped() -> tuple[bool, str]:
    """ADMS 모드면 pyzk 단말 push 불가 → (True, 안내 메시지)."""
    if f22_att_mode_is_adms():
        return True, F22_ADMS_TERMINAL_PUSH_MSG
    return False, ""
