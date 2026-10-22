"""F22 단말(pyzk) 지문 동기화 — device settings JSON + 공통 로직."""

from __future__ import annotations

import base64
import os
from typing import Any, Dict, List, Tuple

from meal_db.f22_att_mode import f22_att_mode_is_adms


def sync_terminal_name(t: dict) -> str:
    """동기화 결과에 쓸 단말 이름. 번호 · 설치위치 · IP."""
    if not isinstance(t, dict):
        return "단말"
    dno = str(t.get("device_no") or t.get("terminal_no") or "").strip()
    loc = str(t.get("install_location") or "").strip()
    ip = str(t.get("ip") or t.get("host") or "").strip()
    bits = []
    if dno:
        bits.append("%s번" % dno)
    if loc:
        bits.append(loc)
    if ip:
        bits.append(ip)
    return " · ".join(bits) if bits else "단말"


def _terminals_for_sync(
    settings: Dict[str, Any] | None,
    terminals_only: list | None = None,
) -> list:
    if terminals_only is not None:
        return [t for t in terminals_only if isinstance(t, dict)]
    if not isinstance(settings, dict):
        return []
    terminals = settings.get("f22_terminals")
    if not isinstance(terminals, list):
        return []
    return [t for t in terminals if isinstance(t, dict)]


def _template_slot_fids_arg(
    a: int | None, b: int | None, c: int | None, d: int | None
) -> tuple[int | None, int | None, int | None, int | None] | None:
    if a is None and b is None and c is None and d is None:
        return None
    return (a, b, c, d)


def sync_fingerprint_to_f22_terminals(
    settings: Dict[str, Any] | None,
    emp_no: str,
    display_name: str,
    rf_card_uid: str,
    fingerprint_uid: str,
    template_b64: str,
    template_b64_2: str | None = None,
    template_b64_3: str | None = None,
    template_b64_4: str | None = None,
    slot_fid_1: int | None = None,
    slot_fid_2: int | None = None,
    slot_fid_3: int | None = None,
    slot_fid_4: int | None = None,
    terminals_only: list | None = None,
) -> List[Tuple[str, bool, str]]:
    if f22_att_mode_is_adms():
        from meal_db.adms_user_sync import sync_employee_via_adms

        return sync_employee_via_adms(
            settings,
            emp_no,
            display_name,
            rf_card_uid,
            fingerprint_uid,
            template_b64,
            template_b64_2,
            template_b64_3,
            template_b64_4,
            slot_fid_1,
            slot_fid_2,
            slot_fid_3,
            slot_fid_4,
            terminals_only=terminals_only,
        )
    import f22_pyzk

    out: List[Tuple[str, bool, str]] = []
    try:
        template_bytes = base64.b64decode(template_b64)
    except Exception as e:
        return [("—", False, "템플릿 Base64 디코딩 실패: %s" % e)]

    template_bytes_2: bytes | None = None
    if template_b64_2:
        try:
            template_bytes_2 = base64.b64decode(template_b64_2)
        except Exception as e:
            return [("—", False, "템플릿2 Base64 디코딩 실패: %s" % e)]

    template_bytes_3: bytes | None = None
    if template_b64_3:
        try:
            template_bytes_3 = base64.b64decode(template_b64_3)
        except Exception as e:
            return [("—", False, "템플릿3 Base64 디코딩 실패: %s" % e)]

    template_bytes_4: bytes | None = None
    if template_b64_4:
        try:
            template_bytes_4 = base64.b64decode(template_b64_4)
        except Exception as e:
            return [("—", False, "템플릿4 Base64 디코딩 실패: %s" % e)]

    if not isinstance(settings, dict):
        settings = {}
    terminals = _terminals_for_sync(settings, terminals_only)
    if not terminals:
        return out

    try:
        pwd = int(os.environ.get("ZK_DEVICE_COMM_PASSWORD", "0") or "0")
    except ValueError:
        pwd = 0

    for t in terminals:
        if not isinstance(t, dict):
            continue
        ip = (t.get("ip") or t.get("host") or "").strip()
        if not ip:
            continue
        try:
            port = int(t.get("port") or 4370)
        except (TypeError, ValueError):
            port = 4370
        label = sync_terminal_name(t)
        ok, msg = f22_pyzk.push_fingerprint_to_terminal(
            ip,
            port,
            pwd,
            emp_no,
            display_name or emp_no,
            rf_card_uid or "",
            template_bytes,
            fingerprint_uid or "",
            template_bytes_2,
            template_bytes_3,
            template_bytes_4,
            _template_slot_fids_arg(slot_fid_1, slot_fid_2, slot_fid_3, slot_fid_4),
        )
        out.append((label, ok, msg))
    return out


def sync_employee_to_f22_terminals(
    settings: Dict[str, Any] | None,
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
    terminals_only: list | None = None,
) -> List[Tuple[str, bool, str]]:
    """사번·이름·RFID(카드) 및 선택 시 지문 템플릿을 등록된 F22 단말에 반영."""
    if f22_att_mode_is_adms():
        from meal_db.adms_user_sync import sync_employee_via_adms

        return sync_employee_via_adms(
            settings,
            emp_no,
            display_name,
            rf_card_uid,
            fingerprint_uid,
            template_b64,
            template_b64_2,
            template_b64_3,
            template_b64_4,
            slot_fid_1,
            slot_fid_2,
            slot_fid_3,
            slot_fid_4,
            terminals_only=terminals_only,
        )
    import f22_pyzk

    out: List[Tuple[str, bool, str]] = []
    template_bytes: bytes | None = None
    if template_b64:
        try:
            template_bytes = base64.b64decode(template_b64)
        except Exception as e:
            return [("—", False, "템플릿 Base64 디코딩 실패: %s" % e)]

    template_bytes_2: bytes | None = None
    if template_b64_2:
        try:
            template_bytes_2 = base64.b64decode(template_b64_2)
        except Exception as e:
            return [("—", False, "템플릿2 Base64 디코딩 실패: %s" % e)]

    template_bytes_3: bytes | None = None
    if template_b64_3:
        try:
            template_bytes_3 = base64.b64decode(template_b64_3)
        except Exception as e:
            return [("—", False, "템플릿3 Base64 디코딩 실패: %s" % e)]

    template_bytes_4: bytes | None = None
    if template_b64_4:
        try:
            template_bytes_4 = base64.b64decode(template_b64_4)
        except Exception as e:
            return [("—", False, "템플릿4 Base64 디코딩 실패: %s" % e)]

    rf = (rf_card_uid or "").strip()
    has_tpl = bool(template_bytes) or bool(template_bytes_2) or bool(template_bytes_3) or bool(template_bytes_4)
    if not rf and not has_tpl:
        return out

    if not isinstance(settings, dict):
        settings = {}
    terminals = _terminals_for_sync(settings, terminals_only)
    if not terminals:
        return [
            (
                "—",
                False,
                "설정에 등록된 F22 단말이 없습니다. '설정'에서 단말 IP·포트를 추가하세요.",
            )
        ]

    try:
        pwd = int(os.environ.get("ZK_DEVICE_COMM_PASSWORD", "0") or "0")
    except ValueError:
        pwd = 0

    for t in terminals:
        if not isinstance(t, dict):
            continue
        ip = (t.get("ip") or t.get("host") or "").strip()
        if not ip:
            continue
        try:
            port = int(t.get("port") or 4370)
        except (TypeError, ValueError):
            port = 4370
        label = sync_terminal_name(t)
        ok, msg = f22_pyzk.push_fingerprint_to_terminal(
            ip,
            port,
            pwd,
            emp_no,
            display_name or emp_no,
            rf,
            template_bytes if template_bytes else b"",
            fingerprint_uid or "",
            template_bytes_2,
            template_bytes_3,
            template_bytes_4,
            _template_slot_fids_arg(slot_fid_1, slot_fid_2, slot_fid_3, slot_fid_4),
        )
        out.append((label, ok, msg))
    return out


def remove_employee_from_f22_terminals(
    settings: Dict[str, Any] | None,
    emp_no: str,
) -> List[Tuple[str, bool, str]]:
    """등록된 모든 F22 단말에서 해당 사번(user_id) 사용자를 삭제한다."""
    if f22_att_mode_is_adms():
        from meal_db.adms_user_sync import remove_employee_via_adms

        return remove_employee_via_adms(settings, emp_no)
    import f22_pyzk

    out: List[Tuple[str, bool, str]] = []
    eno = str(emp_no or "").strip()
    if not eno:
        return [("—", False, "사번 없음")]

    if not isinstance(settings, dict):
        settings = {}
    terminals = settings.get("f22_terminals")
    if not isinstance(terminals, list) or not terminals:
        return out

    try:
        pwd = int(os.environ.get("ZK_DEVICE_COMM_PASSWORD", "0") or "0")
    except ValueError:
        pwd = 0

    for t in terminals:
        if not isinstance(t, dict):
            continue
        ip = (t.get("ip") or t.get("host") or "").strip()
        if not ip:
            continue
        try:
            port = int(t.get("port") or 4370)
        except (TypeError, ValueError):
            port = 4370
        label = sync_terminal_name(t)
        ok, msg = f22_pyzk.remove_user_from_terminal(ip, port, pwd, eno)
        out.append((label, ok, msg))
    return out
