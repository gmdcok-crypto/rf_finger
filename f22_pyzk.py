# ZKTeco F22 등 ZK TCP 단말 — pyzk 로 사용자·지문 템플릿 동기화
# pip install pyzk
#
# 출입 반영: (1) 기본 — pc_app 에서 pyzk ZK.live_capture(EF_ATTLOG) 실시간 이벤트
# (2) MEAL_F22_ATT_MODE=poll 일 때 pull_attendance_logs — 전체 로그 읽기(폴링)
#
# 일부 단말은 출입 user_id를 32비트 정수로만 넘겨 10자리 카드번호(>4294967295)가 깨질 수 있다.
# 앞자리 0이 빠진 숫자는 meal_db에서 MEAL_ZK_USER_ID_ZERO_PAD=10 과 emp_no·rf_card_uid 동시 매칭으로 보완.
#
# 폴링 수신 후 DB 반영이 끝나면 단말 출퇴근 버퍼를 비운다(clear_attendance). 끄려면 MEAL_ZK_CLEAR_ATTLOG_AFTER_PULL=0

from __future__ import annotations

import os


def _truncate_zk_name(s: str, max_bytes: int = 23) -> str:
    if not s:
        return ""
    b = s.encode("utf-8", errors="ignore")[:max_bytes]
    return b.decode("utf-8", errors="ignore")


def _rfid_to_card_int(rf: str) -> int:
    """ZK User.card 필드용 정수. 32비트로 맞춤."""
    if not rf:
        return 0
    s = str(rf).strip().replace(" ", "")
    try:
        if s.lower().startswith("0x"):
            return int(s, 16) & 0xFFFFFFFF
        return int(s) & 0xFFFFFFFF
    except ValueError:
        try:
            return int(s, 16) & 0xFFFFFFFF
        except ValueError:
            return 0


def ping_terminal(
    host: str,
    port: int,
    comm_password: int,
    *,
    timeout: int = 10,
) -> tuple[bool, str]:
    """TCP로 단말에 접속해 펌웨어 버전을 읽어 연결 여부를 확인한다."""
    from zk import ZK

    ip = (host or "").strip()
    if not ip:
        return False, "IP가 비었습니다."
    try:
        pwd = int(comm_password)
    except (TypeError, ValueError):
        pwd = 0
    try:
        to = max(3, min(60, int(timeout)))
    except (TypeError, ValueError):
        to = 10
    zk = ZK(ip, port=int(port), password=pwd, timeout=to, ommit_ping=True)
    conn = None
    try:
        conn = zk.connect()
        ver = conn.get_firmware_version()
        return True, (ver or "OK").strip()
    except Exception as e:
        return False, str(e)
    finally:
        if conn is not None:
            try:
                conn.disconnect()
            except Exception:
                pass


def ping_f22_terminals_report(settings: dict | None) -> dict:
    """
    device_settings 의 f22_terminals 기준으로 순차 핑.
    반환: {"all_ok": bool, "short": str, "detail_lines": list[str], "status_rows": list[dict]}
    status_rows: [{"label": "식탁 단말기:", "ok": bool}, ...]  (install_location + " 단말기:")
    """
    out_lines: list[str] = []
    status_rows: list[dict] = []

    def _row_label(t: dict) -> str:
        loc = str(t.get("install_location") or "").strip()
        return ("%s 단말기:" % loc) if loc else "단말기:"

    if not isinstance(settings, dict):
        settings = {}
    terminals = settings.get("f22_terminals")
    if not isinstance(terminals, list) or not terminals:
        return {
            "all_ok": False,
            "short": "단말: 등록 없음",
            "detail_lines": ["등록된 F22 단말기가 없습니다."],
            "status_rows": [{"label": "단말기:", "ok": False}],
        }
    try:
        pwd = int(os.environ.get("ZK_DEVICE_COMM_PASSWORD", "0") or "0")
    except ValueError:
        pwd = 0
    any_ip = False
    all_ok = True
    short_parts: list[str] = []
    for t in terminals:
        if not isinstance(t, dict):
            continue
        ip = (t.get("ip") or t.get("host") or "").strip()
        if not ip:
            continue
        any_ip = True
        try:
            port = int(t.get("port") or 4370)
        except (TypeError, ValueError):
            port = 4370
        dno = str(t.get("device_no") or "").strip()
        label = dno if dno else ip
        ok, msg = ping_terminal(ip, port, pwd)
        all_ok = all_ok and ok
        if ok:
            line = "✓ [%s] %s:%s — 연결 (%s)" % (label, ip, port, msg)
            short_parts.append("%s:%s ✓" % (ip, port))
        else:
            line = "✗ [%s] %s:%s — 실패: %s" % (label, ip, port, msg)
            short_parts.append("%s:%s ✗" % (ip, port))
        status_rows.append({
            "label": _row_label(t),
            "ok": ok,
            "detail": line,
            "device_no": dno,
            "ip": ip,
        })
        out_lines.append(line)
    if not any_ip:
        return {
            "all_ok": False,
            "short": "단말: IP 없음",
            "detail_lines": ["단말 목록에 IP가 없습니다."],
            "status_rows": [{"label": "단말기:", "ok": False}],
        }
    return {
        "all_ok": all_ok,
        "short": "단말 | " + " · ".join(short_parts),
        "detail_lines": out_lines,
        "status_rows": status_rows,
    }


def clear_terminal_attlog(host: str, port: int, comm_password: int) -> tuple[bool, str]:
    """단말에 저장된 출퇴근(Attendance) 로그 전체 삭제. pull 반영 후 호출용."""
    from zk import ZK

    ip = (host or "").strip()
    if not ip:
        return False, "IP가 비었습니다."
    try:
        pwd = int(comm_password)
    except (TypeError, ValueError):
        pwd = 0
    try:
        timeout = int(os.environ.get("ZK_ATT_PULL_TIMEOUT", "45") or "45")
    except ValueError:
        timeout = 45
    timeout = max(10, min(120, timeout))
    conn = None
    try:
        zk = ZK(ip, port=int(port), password=pwd, timeout=timeout, ommit_ping=True)
        conn = zk.connect()
        try:
            conn.disable_device()
        except Exception:
            pass
        conn.clear_attendance()
        return True, "OK"
    except Exception as e:
        return False, str(e)
    finally:
        if conn is not None:
            try:
                conn.enable_device()
            except Exception:
                pass
            try:
                conn.disconnect()
            except Exception:
                pass


def pull_attendance_logs(settings: dict | None) -> tuple[list[dict], list[str], list[dict]]:
    """
    등록된 F22(ZK TCP) 단말에서 attendance 전체를 읽는다.
    반환: (records, err_lines, pull_ok_devices)
    pull_ok_devices: get_attendance까지 성공한 단말 목록
      [{"ip","port","label"}, ...] — DB 반영 후 clear_attendance 용.
    각 record dict: device (ip:port), label, user_id(str), timestamp(datetime naive local),
    status, punch, zk_uid
    """
    from datetime import datetime

    from zk import ZK

    records: list[dict] = []
    err_lines: list[str] = []
    pull_ok_devices: list[dict] = []
    if not isinstance(settings, dict):
        settings = {}
    terminals = settings.get("f22_terminals")
    if not isinstance(terminals, list) or not terminals:
        return records, err_lines, pull_ok_devices
    try:
        pwd = int(os.environ.get("ZK_DEVICE_COMM_PASSWORD", "0") or "0")
    except ValueError:
        pwd = 0
    try:
        timeout = int(os.environ.get("ZK_ATT_PULL_TIMEOUT", "45") or "45")
    except ValueError:
        timeout = 45
    timeout = max(10, min(120, timeout))

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
        dno = str(t.get("device_no") or "").strip()
        label = dno if dno else ip
        device_key = "%s:%s" % (ip, port)
        conn = None
        try:
            zk = ZK(ip, port=int(port), password=pwd, timeout=timeout, ommit_ping=True)
            conn = zk.connect()
            try:
                conn.disable_device()
            except Exception:
                pass
            logs = conn.get_attendance()
            for a in logs or []:
                ts = a.timestamp
                if isinstance(ts, datetime):
                    if ts.tzinfo is not None:
                        ts = ts.astimezone().replace(tzinfo=None)
                    else:
                        ts = ts.replace(tzinfo=None)
                else:
                    continue
                try:
                    zuid = int(a.uid)
                except (TypeError, ValueError):
                    zuid = 0
                records.append(
                    {
                        "device": device_key,
                        "label": label,
                        "user_id": str(a.user_id).strip(),
                        "timestamp": ts,
                        "status": int(a.status) if a.status is not None else 0,
                        "punch": int(a.punch) if a.punch is not None else 0,
                        "zk_uid": zuid,
                    }
                )
            pull_ok_devices.append({"ip": ip, "port": port, "label": label})
        except Exception as e:
            err_lines.append("[%s] %s — 실패: %s" % (label, device_key, e))
        finally:
            if conn is not None:
                try:
                    conn.enable_device()
                except Exception:
                    pass
                try:
                    conn.disconnect()
                except Exception:
                    pass
    return records, err_lines, pull_ok_devices


def push_fingerprint_to_terminal(
    host: str,
    port: int,
    comm_password: int,
    emp_no: str,
    display_name: str,
    rf_card_uid: str,
    template_bytes: bytes,
    fingerprint_uid: str,
    template_bytes_2: bytes | None = None,
    template_bytes_3: bytes | None = None,
    template_bytes_4: bytes | None = None,
    template_slot_fids: tuple[int | None, int | None, int | None, int | None] | None = None,
) -> tuple[bool, str]:
    """
    단말에 사번(user_id)·이름·카드(RFID)→card, 지문 템플릿 최대 4개 저장.
    template_slot_fids: 각 템플릿이 저장될 단말 fid(0~9). None 이면 순서대로 0,1,2,3.
    지문 UID는 단말에 전용 필드가 없어 기본은 서버 원장만; 이름에 붙이려면
    환경변수 ZK_NAME_APPEND_FP_UID=1

    주의: 다른 장비(예: PC USB Live20R)에서 만든 템플릿 바이트는 ZK TCP로
    올려도 명령이 성공할 수 있으나, 단말 내장 센서·지문 알고리즘과 형식이
    맞지 않으면 출입 시 지문 인증이 실패할 수 있다. 카드만 되고 지문이 안 되면
    해당 단말에서 직접 지문 등록을 하거나, 단말과 호환되는 등록기/SDK를 쓴다.
    """
    from zk import ZK
    from zk.finger import Finger

    eno = str(emp_no).strip()
    if not eno:
        return False, "사번이 비었습니다."
    tpl = template_bytes if template_bytes else None
    if tpl is not None and len(tpl) == 0:
        tpl = None
    tpl2 = template_bytes_2 if template_bytes_2 else None
    if tpl2 is not None and len(tpl2) == 0:
        tpl2 = None
    tpl3 = template_bytes_3 if template_bytes_3 else None
    if tpl3 is not None and len(tpl3) == 0:
        tpl3 = None
    tpl4 = template_bytes_4 if template_bytes_4 else None
    if tpl4 is not None and len(tpl4) == 0:
        tpl4 = None
    # RFID만 전송 테스트: tpl 없이 카드·사용자만 등록 가능
    if tpl is None and tpl2 is None and tpl3 is None and tpl4 is None and not (rf_card_uid or "").strip():
        return False, "RFID(카드) UID 또는 지문 템플릿 중 하나는 필요합니다."

    dname = _truncate_zk_name(display_name or eno, 23)
    if os.environ.get("ZK_NAME_APPEND_FP_UID", "").lower() in ("1", "true", "yes"):
        tail = (fingerprint_uid or "")[:12]
        dname = _truncate_zk_name("%s|%s" % (display_name or eno, tail), 23)

    zk = ZK(host, port=int(port), password=int(comm_password), timeout=25, ommit_ping=True)
    conn = None
    try:
        conn = zk.connect()
    except Exception as e:
        return False, str(e)

    try:
        try:
            conn.disable_device()
        except Exception:
            pass

        users = conn.get_users()
        u = None
        for x in users:
            if str(x.user_id).strip() == eno:
                u = x
                break

        card_i = _rfid_to_card_int(rf_card_uid)

        if u is None:
            conn.set_user(name=dname, user_id=eno, card=card_i, privilege=0, password="")
        else:
            conn.set_user(
                uid=u.uid,
                name=dname,
                privilege=u.privilege,
                password=u.password or "",
                group_id=u.group_id or "",
                user_id=eno,
                card=card_i,
            )

        users = conn.get_users()
        u = None
        for x in users:
            if str(x.user_id).strip() == eno:
                u = x
                break
        if u is None:
            return False, "단말 사용자 조회 실패"

        tpls = (tpl, tpl2, tpl3, tpl4)
        fids_plan = template_slot_fids if template_slot_fids else (None, None, None, None)
        fingers_to_save = []
        for i, tb in enumerate(tpls):
            if tb is None:
                continue
            if fids_plan[i] is not None:
                dev_fid = max(0, min(9, int(fids_plan[i])))
            else:
                dev_fid = i
            fingers_to_save.append(Finger(u.uid, dev_fid, 1, tb))
        if fingers_to_save:
            conn.save_user_template(u, fingers_to_save)

        try:
            conn.enable_device()
        except Exception:
            pass
        if fingers_to_save:
            parts = []
            for i, tb in enumerate(tpls):
                if tb is None:
                    continue
                if fids_plan[i] is not None:
                    dev_fid = max(0, min(9, int(fids_plan[i])))
                else:
                    dev_fid = i
                parts.append("%d:%d" % (dev_fid, len(tb)))
            return True, "OK (지문 %d개; %s)" % (len(parts), "+".join(parts))
        return True, "OK (카드·사용자만 저장)"
    except Exception as e:
        try:
            conn.enable_device()
        except Exception:
            pass
        return False, str(e)
    finally:
        if conn is not None:
            try:
                conn.disconnect()
            except Exception:
                pass


def remove_user_from_terminal(
    host: str,
    port: int,
    comm_password: int,
    emp_no: str,
) -> tuple[bool, str]:
    """단말에서 user_id(사번)에 해당하는 사용자를 삭제한다. 카드·지문은 사용자 삭제와 함께 제거되는 것이 일반적이다."""
    from zk import ZK

    eno = str(emp_no or "").strip()
    if not eno:
        return False, "사번이 비었습니다."
    ip = (host or "").strip()
    if not ip:
        return False, "IP가 비었습니다."

    zk = ZK(ip, port=int(port), password=int(comm_password), timeout=25, ommit_ping=True)
    conn = None
    try:
        conn = zk.connect()
    except Exception as e:
        return False, str(e)

    try:
        try:
            conn.disable_device()
        except Exception:
            pass

        users = conn.get_users() or []
        u = next((x for x in users if str(x.user_id).strip() == eno), None)
        if u is None:
            try:
                conn.enable_device()
            except Exception:
                pass
            return True, "단말에 사용자 없음"

        conn.delete_user(uid=u.uid)
        try:
            conn.enable_device()
        except Exception:
            pass
        return True, "단말에서 사용자 삭제됨"
    except Exception as e:
        try:
            conn.enable_device()
        except Exception:
            pass
        return False, str(e)
    finally:
        if conn is not None:
            try:
                conn.disconnect()
            except Exception:
                pass


def prune_users_not_in(
    host: str,
    port: int,
    comm_password: int,
    keep_user_ids: set,
) -> tuple[bool, str, int, int]:
    """keep 에 없는 일반 사용자(privilege 0)를 삭제한다. 반환: ok, 메시지, 삭제 수, 남긴 관리자 수."""
    from zk import ZK

    ip = (host or "").strip()
    if not ip:
        return False, "IP가 비었습니다.", 0, 0
    keep = {str(x).strip() for x in (keep_user_ids or set()) if str(x).strip()}
    zk = ZK(ip, port=int(port), password=int(comm_password), timeout=25, ommit_ping=True)
    conn = None
    try:
        conn = zk.connect()
    except Exception as e:
        return False, str(e), 0, 0
    deleted = 0
    kept_admin = 0
    try:
        try:
            conn.disable_device()
        except Exception:
            pass
        users = conn.get_users() or []
        for u in users:
            uid_s = str(getattr(u, "user_id", "") or "").strip()
            if not uid_s or uid_s in keep:
                continue
            try:
                priv = int(getattr(u, "privilege", 0) or 0)
            except (TypeError, ValueError):
                priv = 0
            if priv != 0:
                kept_admin += 1
                continue
            conn.delete_user(uid=u.uid)
            deleted += 1
        try:
            conn.enable_device()
        except Exception:
            pass
        return True, "OK", deleted, kept_admin
    except Exception as e:
        try:
            conn.enable_device()
        except Exception:
            pass
        return False, str(e), deleted, kept_admin
    finally:
        if conn is not None:
            try:
                conn.disconnect()
            except Exception:
                pass


def pick_f22_terminal_for_import(settings: dict | None) -> dict | None:
    """
    지문 템플릿 가져오기 등에 쓸 단말 1대 선택.
    단말기 번호(device_no) 가 "1" 인 항목(IP 있음)을 마스터로 우선 사용,
    없으면 master_enrollment 가 True 인 항목, 없으면 IP 있는 첫 항목.
    항목에는 enroll_fid0~3(0~9) 로 등록기 손가락 슬롯을 둘 수 있음.
    (F22 UI: 손등 위, 왼손 새끼=0 … 오른손 새끼=9.)
    """
    if not isinstance(settings, dict):
        return None
    terminals = settings.get("f22_terminals")
    if not isinstance(terminals, list):
        return None

    def _has_ip(t: dict) -> bool:
        return bool(str(t.get("ip") or t.get("host") or "").strip())

    for t in terminals:
        if not isinstance(t, dict) or not _has_ip(t):
            continue
        if str(t.get("device_no") or "").strip() == "1":
            return t
    for t in terminals:
        if not isinstance(t, dict):
            continue
        if not _has_ip(t):
            continue
        if bool(t.get("master_enrollment") or t.get("is_master_enrollment")):
            return t
    for t in terminals:
        if isinstance(t, dict) and _has_ip(t):
            return t
    return None
