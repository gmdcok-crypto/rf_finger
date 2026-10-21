"""ADMS getrequest 명령으로 단말에 사용자·지문 등록.

정식 프로토콜: Security PUSH Communication Protocol 20240712 (HTTP /iclock/*).
pyzk(4370)는 ADMS 모드에서 사용하지 않음.
"""

from __future__ import annotations

import base64
import os
from typing import Any, Dict, List, Optional, Tuple

from meal_db.adms_commands import (
    AdmsCmdProtocol,
    adms_cmd_protocol,
    adms_protocol_fallback_enabled,
    build_fingerprint_update_command,
    build_user_delete_command,
    build_user_update_command,
    build_userauthorize_delete_command,
    build_userauthorize_update_command,
    resolve_authorize_ids,
)
from meal_db.adms_push_server import (
    _fallback_match_registry,
    _match_terminal_registry,
    _terminal_config_sn,
    get_runtime_adms_server,
)


def _terminal_label(t: dict) -> str:
    dno = str(t.get("device_no") or "").strip()
    if dno:
        return dno
    ip = str(t.get("ip") or t.get("host") or "").strip()
    sn = _terminal_config_sn(t)
    return ip or sn or "단말"


def resolve_terminal_adms_sn(t: dict, registry: Dict[str, dict]) -> str:
    cfg = _terminal_config_sn(t)
    if cfg:
        return cfg
    matched, _ = _match_terminal_registry(t, registry)
    if matched:
        return matched
    configured = [t]
    matched, _ = _fallback_match_registry(t, registry, configured)
    return matched


def _decode_tpl(b64: Optional[str]) -> Optional[bytes]:
    if not b64:
        return None
    try:
        raw = base64.b64decode(b64)
        return raw if raw else None
    except Exception:
        return None


def _command_timeout_sec() -> float:
    try:
        return float(os.environ.get("MEAL_ADMS_CMD_TIMEOUT_SEC", "45") or "45")
    except ValueError:
        return 45.0


def _queue_with_protocol_fallback(
    srv,
    sn: str,
    build_fn,
    timeout: float,
    *args,
    **kwargs,
) -> Tuple[bool, str, AdmsCmdProtocol]:
    """Security PUSH 20240712 기준. MEAL_ADMS_CMD_PROTOCOL=auto 일 때만 legacy 재시도."""
    primary = adms_cmd_protocol()
    protocols: List[AdmsCmdProtocol] = [primary]
    if adms_protocol_fallback_enabled():
        secondary: AdmsCmdProtocol = "legacy" if primary == "security" else "security"
        if secondary not in protocols:
            protocols.append(secondary)
    last_msg = ""
    for proto in protocols:
        cmd = build_fn(*args, protocol=proto, **kwargs)
        ok, msg = srv.queue_user_command(sn, cmd, timeout_sec=timeout)
        last_msg = msg
        if ok:
            return True, msg, proto
        if "-629" not in str(msg):
            return False, msg, proto
    return False, last_msg, primary


def _queue_userauthorize(
    srv,
    sn: str,
    pin: str,
    terminal: dict,
    timeout: float,
) -> Tuple[bool, str]:
    tz_id, door_id = resolve_authorize_ids(terminal)
    cmd = build_userauthorize_update_command(pin, tz_id, door_id, protocol="security")
    if not cmd:
        return True, "userauthorize 생략(legacy)"
    ok, msg = srv.queue_user_command(sn, cmd, timeout_sec=timeout)
    if ok:
        return True, "userauthorize tz=%d door=%d" % (tz_id, door_id)
    return False, msg


def _terminals_for_sync(
    settings: dict | None,
    terminals_only: Optional[List[dict]] = None,
) -> List[dict]:
    if terminals_only is not None:
        return [t for t in terminals_only if isinstance(t, dict)]
    if not isinstance(settings, dict):
        return []
    terminals = settings.get("f22_terminals")
    if not isinstance(terminals, list):
        return []
    return [t for t in terminals if isinstance(t, dict)]


def sync_employee_via_adms(
    settings: dict | None,
    emp_no: str,
    display_name: str,
    rf_card_uid: str,
    fingerprint_uid: str = "",
    template_b64: str | None = None,
    template_b64_2: str | None = None,
    template_b64_3: str | None = None,
    template_b64_4: str | None = None,
    slot_fid_1: int | None = None,
    slot_fid_2: int | None = None,
    slot_fid_3: int | None = None,
    slot_fid_4: int | None = None,
    terminals_only: Optional[List[dict]] = None,
) -> List[Tuple[str, bool, str]]:
    """등록된 단말(SN)에 Security PUSH user / userauthorize / templatev10 명령 큐."""
    srv = get_runtime_adms_server()
    if srv is None or not srv.is_running():
        return [
            (
                "—",
                False,
                "ADMS 연결 서버가 실행 중이 아닙니다. 앱을 재시작하거나 단말 연결을 확인하세요.",
            )
        ]

    eno = str(emp_no or "").strip()
    if not eno:
        return [("—", False, "사번이 비었습니다.")]

    tpls = [
        _decode_tpl(template_b64),
        _decode_tpl(template_b64_2),
        _decode_tpl(template_b64_3),
        _decode_tpl(template_b64_4),
    ]
    rf = str(rf_card_uid or "").strip()
    has_tpl = any(tpls)
    if not rf and not has_tpl:
        return []

    terminals = _terminals_for_sync(settings, terminals_only)
    if not terminals:
        return [
            (
                "—",
                False,
                "설정에 등록된 F22 단말이 없습니다. '설정'에서 단말 IP·SN을 추가하세요.",
            )
        ]

    registry = srv.get_registry_snapshot()
    timeout = _command_timeout_sec()
    fids = [
        slot_fid_1 if slot_fid_1 is not None else 0,
        slot_fid_2 if slot_fid_2 is not None else 1,
        slot_fid_3 if slot_fid_3 is not None else 2,
        slot_fid_4 if slot_fid_4 is not None else 3,
    ]
    out: List[Tuple[str, bool, str]] = []

    for t in terminals:
        if not isinstance(t, dict):
            continue
        ip = str(t.get("ip") or t.get("host") or "").strip()
        if not ip and not _terminal_config_sn(t):
            continue
        label = _terminal_label(t)
        sn = resolve_terminal_adms_sn(t, registry)
        if not sn:
            out.append(
                (
                    label,
                    False,
                    "단말 SN 미확인 — 단말이 ADMS로 PC에 접속했는지 확인 후 재시도",
                )
            )
            continue

        ok, msg, proto = _queue_with_protocol_fallback(
            srv,
            sn,
            build_user_update_command,
            timeout,
            eno,
            display_name or eno,
            rf,
        )
        if not ok:
            out.append((label, False, msg))
            continue

        auth_ok, auth_msg = _queue_userauthorize(srv, sn, eno, t, timeout)
        if not auth_ok:
            out.append((label, False, "사용자 OK, 출입권한 실패 — %s" % auth_msg))
            continue

        fp_errors: List[str] = []
        for idx, tpl in enumerate(tpls):
            if not tpl:
                continue
            try:
                fid = max(0, min(9, int(fids[idx])))
            except (TypeError, ValueError):
                fid = idx
            fp_ok, fp_msg, _ = _queue_with_protocol_fallback(
                srv,
                sn,
                build_fingerprint_update_command,
                timeout,
                eno,
                fid,
                tpl,
            )
            if not fp_ok:
                fp_errors.append("fid%d: %s" % (fid, fp_msg))

        if fp_errors:
            out.append((label, False, "사용자 OK, 지문 실패 — %s" % "; ".join(fp_errors[:2])))
        else:
            detail = "ADMS 등록 SN=%s PIN=%s (%s)" % (sn, eno, proto)
            detail += " +출입권한"
            if has_tpl:
                detail += " +지문"
            if rf:
                detail += " +카드"
            out.append((label, True, detail))

    return out


def remove_employee_via_adms(settings: dict | None, emp_no: str) -> List[Tuple[str, bool, str]]:
    srv = get_runtime_adms_server()
    if srv is None or not srv.is_running():
        return []

    eno = str(emp_no or "").strip()
    if not eno:
        return [("—", False, "사번 없음")]

    if not isinstance(settings, dict):
        settings = {}
    terminals = settings.get("f22_terminals")
    if not isinstance(terminals, list) or not terminals:
        return []

    registry = srv.get_registry_snapshot()
    timeout = _command_timeout_sec()
    out: List[Tuple[str, bool, str]] = []

    for t in terminals:
        if not isinstance(t, dict):
            continue
        sn = resolve_terminal_adms_sn(t, registry)
        if not sn:
            continue
        label = _terminal_label(t)
        auth_del = build_userauthorize_delete_command(eno, protocol="security")
        if auth_del:
            srv.queue_user_command(sn, auth_del, timeout_sec=timeout)
        ok, msg, _ = _queue_with_protocol_fallback(
            srv,
            sn,
            build_user_delete_command,
            timeout,
            eno,
        )
        out.append((label, ok, msg if ok else msg))
    return out
