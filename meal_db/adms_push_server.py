"""
ZKTeco ADMS / Security PUSH (HTTP /iclock/*) 최소 수신 서버.

단말(ADMS 전용 펌웨어)이 PC로 HTTP PUSH → 출퇴근(ATTLOG·rtlog) 파싱 → 콜백.
암호화 exchange 단계는 미구현(일부 기기는 비암호 PUSH로 동작).

환경 변수:
  MEAL_ADMS_PUSH_HOST  (기본 0.0.0.0)
  MEAL_ADMS_PUSH_PORT  (기본 8088)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from meal_db.adms_commands import AdmsCommandQueue, parse_devicecmd_body

_runtime_adms_server: Optional["AdmsPushServer"] = None
_runtime_lock = threading.Lock()


def set_runtime_adms_server(srv: Optional["AdmsPushServer"]) -> None:
    global _runtime_adms_server
    with _runtime_lock:
        _runtime_adms_server = srv


def get_runtime_adms_server() -> Optional["AdmsPushServer"]:
    with _runtime_lock:
        return _runtime_adms_server


OnAttendanceFn = Callable[[Dict[str, Any]], None]
OnAuthFailureFn = Callable[[Dict[str, Any]], None]
OnStatusFn = Callable[[str], None]
ResolveDeviceFn = Callable[[str, Optional[str]], Tuple[str, str]]

# Appendix 3 — 카드 관련 인증 방식
_CARD_VERIFY_MODES = frozenset({0, 4, 6, 7, 10, 11, 12, 14, 18, 23, 24, 26})

# Appendix 2 — 실패·거부 이벤트 (일부)
_FAILURE_EVENT_LABELS = {
    20: "인증 간격 짧음",
    21: "비유효 시간대",
    22: "비유효 시간대",
    23: "무권한 출입",
    24: "APB",
    25: "인터락",
    27: "카드 미등록",
    29: "권한 만료",
    30: "비밀번호 오류",
    31: "인증 간격 짧음",
    34: "지문 미등록",
    35: "비유효 시간대",
    38: "분실 카드",
    39: "블랙리스트",
    40: "다중 사용자 인증 실패",
    44: "원격 인증 실패",
    48: "다중 사용자 인증 실패",
    73: "잘못된 QR",
}

_SUCCESS_ACCESS_EVENTS = frozenset({0, 1, 2, 14, 17, 18, 19})


def _parse_push_datetime(raw: str) -> Optional[datetime]:
    s = (raw or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


_KV_PAIR_RE = re.compile(
    r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<val>.*?)(?=\s+[A-Za-z_][A-Za-z0-9_]*=|$)"
)


def _parse_kv_line(line: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    s = (line or "").strip()
    if not s:
        return out
    if "\t" in s:
        cols = s.split("\t")
        if len(cols) >= 2 and not cols[0].startswith("pin") and "=" not in cols[0]:
            out["pin"] = cols[0].strip()
            out["datetime"] = cols[1].strip()
            if len(cols) >= 3:
                out["status"] = cols[2].strip()
            if len(cols) >= 4:
                out["verifytype"] = cols[3].strip()
            if len(cols) >= 5:
                out["workcode"] = cols[4].strip()
            return out
        # Security PUSH rtlog: time=YYYY-MM-DD HH:MM:SS\tpin=...\t... (탭만 구분, 값 안 공백 유지)
        for part in cols:
            part = part.strip()
            if not part or "=" not in part:
                continue
            k, _, v = part.partition("=")
            out[k.strip().lower()] = v.strip()
        if out:
            return out
    if "=" in s:
        for m in _KV_PAIR_RE.finditer(s):
            out[m.group("key").strip().lower()] = m.group("val").strip()
        if out:
            return out
        if " " not in s:
            k, _, v = s.partition("=")
            out[k.strip().lower()] = v.strip()
    return out


def _iter_rtlog_kv_lines(body: str):
    """rtlog/attlog 본문 → (원본 라인, kv dict) 제너레이터."""
    for line in (body or "").splitlines():
        line = line.strip()
        if not line or line.startswith("OPERLOG"):
            continue
        if line.upper().startswith("ATTLOG"):
            line = line[6:].strip()
        for prefix in ("attlog ", "rtlog ", "remoteatt "):
            if line.lower().startswith(prefix):
                line = line[len(prefix) :].strip()
                break
        kv = _parse_kv_line(line)
        if kv:
            yield line, kv


def _verifytype_involves_card(vt_raw: str) -> bool:
    s = (vt_raw or "").strip()
    if not s:
        return False
    if s.isdigit():
        try:
            return int(s) in _CARD_VERIFY_MODES
        except ValueError:
            return False
    return False


def _event_label(code: int) -> str:
    if code in _FAILURE_EVENT_LABELS:
        return _FAILURE_EVENT_LABELS[code]
    if 5000 <= code < 6000:
        return "비정상 이벤트(%d)" % code
    if 6000 <= code < 7000:
        return "경고 이벤트(%d)" % code
    if code:
        return "이벤트(%d)" % code
    return "인증 실패"


def _is_failed_rf_card_auth(kv: Dict[str, str]) -> bool:
    """RF 카드 인증 실패 rtlog 여부."""
    pin = (kv.get("pin") or kv.get("userid") or kv.get("user_id") or "").strip()
    cardno = (kv.get("cardno") or "").strip()
    try:
        event = int(kv.get("event") or 0)
    except (TypeError, ValueError):
        event = 0
    has_card_no = bool(cardno) and cardno != "0"
    card_verify = _verifytype_involves_card(kv.get("verifytype") or "")
    if not (has_card_no or card_verify):
        return False
    if pin and pin != "0" and event in _SUCCESS_ACCESS_EVENTS:
        return False
    if pin in ("", "0"):
        return True
    if event in _FAILURE_EVENT_LABELS or event >= 5000:
        return True
    return False


def parse_attendance_lines(body: str) -> List[Dict[str, Any]]:
    """ATTLOG / rtlog 본문 → ingest_terminal_attendance_record 형식 dict 목록."""
    rows: List[Dict[str, Any]] = []
    for _line, kv in _iter_rtlog_kv_lines(body):
        pin = (kv.get("pin") or kv.get("userid") or kv.get("user_id") or "").strip()
        ts_raw = (
            kv.get("datetime")
            or kv.get("time")
            or kv.get("checktime")
            or kv.get("date")
            or ""
        ).strip()
        if not pin or pin == "0":
            continue
        ts = _parse_push_datetime(ts_raw)
        if ts is None:
            continue
        try:
            status = int(kv.get("status") or kv.get("state") or 0)
        except (TypeError, ValueError):
            status = 0
        try:
            punch = int(kv.get("punch") or kv.get("inoutstatus") or status)
        except (TypeError, ValueError):
            punch = status
        rows.append(
            {
                "user_id": str(pin),
                "timestamp": ts.replace(microsecond=0),
                "status": status,
                "punch": punch,
                "zk_uid": 0,
            }
        )
    return rows


def parse_failed_rf_card_lines(body: str) -> List[Dict[str, Any]]:
    """rtlog 본문 → RF 카드 인증 실패 dict 목록."""
    rows: List[Dict[str, Any]] = []
    for _line, kv in _iter_rtlog_kv_lines(body):
        if not _is_failed_rf_card_auth(kv):
            continue
        ts_raw = (
            kv.get("datetime")
            or kv.get("time")
            or kv.get("checktime")
            or kv.get("date")
            or ""
        ).strip()
        ts = _parse_push_datetime(ts_raw)
        if ts is None:
            continue
        pin = (kv.get("pin") or kv.get("userid") or kv.get("user_id") or "").strip()
        cardno = (kv.get("cardno") or "").strip()
        try:
            event = int(kv.get("event") or 0)
        except (TypeError, ValueError):
            event = 0
        try:
            index = int(kv.get("index") or 0)
        except (TypeError, ValueError):
            index = 0
        rows.append(
            {
                "timestamp": ts.replace(microsecond=0),
                "card_no": cardno if cardno and cardno != "0" else "",
                "pin_raw": pin if pin and pin != "0" else "",
                "event_code": event,
                "event_label": _event_label(event),
                "verify_type": (kv.get("verifytype") or "").strip(),
                "adms_index": index,
            }
        )
    return rows


class _AdmsRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_sn: Dict[str, Dict[str, str]] = {}

    def _get_or_create(self, sn: str) -> Dict[str, str]:
        row = self._by_sn.get(sn)
        if row is None:
            row = {
                "registry_code": hashlib.md5((sn + secrets.token_hex(8)).encode()).hexdigest()[:16],
                "session_id": secrets.token_hex(16).upper(),
                "ip": "",
                "last_seen": "",
            }
            self._by_sn[sn] = row
        return row

    def ensure(self, sn: str) -> Dict[str, str]:
        sn = (sn or "").strip()
        with self._lock:
            return dict(self._get_or_create(sn))

    def touch(self, sn: str, ip: str = "") -> None:
        sn = (sn or "").strip()
        if not sn:
            return
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            row = self._get_or_create(sn)
            if ip:
                row["ip"] = ip.strip()
            row["last_seen"] = now

    def set_ip(self, sn: str, ip: str) -> None:
        self.touch(sn, ip)

    def snapshot(self) -> Dict[str, Dict[str, str]]:
        with self._lock:
            return {sn: dict(row) for sn, row in self._by_sn.items()}


class AdmsPushServer:
    """백그라운드 스레드에서 HTTP PUSH 수신."""

    def __init__(
        self,
        host: str,
        port: int,
        on_attendance: OnAttendanceFn,
        resolve_device: Optional[ResolveDeviceFn] = None,
        on_status: Optional[OnStatusFn] = None,
        on_auth_failure: Optional[OnAuthFailureFn] = None,
    ):
        self._host = (host or "0.0.0.0").strip()
        self._port = int(port)
        self._on_attendance = on_attendance
        self._on_auth_failure = on_auth_failure
        self._resolve_device = resolve_device
        self._on_status = on_status
        self._registry = _AdmsRegistry()
        self._cmd_queue = AdmsCommandQueue()
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def listen_addr(self) -> str:
        return "%s:%s" % (self._host, self._port)

    def is_running(self) -> bool:
        return self._httpd is not None

    def get_registry_snapshot(self) -> Dict[str, Dict[str, str]]:
        return self._registry.snapshot()

    def queue_user_command(
        self,
        sn: str,
        command: str,
        timeout_sec: float = 45.0,
    ) -> Tuple[bool, str]:
        """getrequest 큐에 명령 넣고 단말 devicecmd 응답까지 대기."""
        return self._cmd_queue.queue_and_wait(sn, command, timeout_sec)

    def _log(self, msg: str) -> None:
        if self._on_status:
            try:
                self._on_status(msg)
            except Exception:
                pass

    def _config_text(self, sn: str) -> str:
        reg = self._registry.ensure(sn)
        return "\r\n".join(
            [
                "registry=ok",
                "RegistryCode=%s" % reg["registry_code"],
                "ServerVersion=3.0.1",
                "ServerName=MealRF",
                "PushProtVer=3.0.1",
                "ErrorDelay=30",
                "RequestDelay=2",
                "TransTimes=00:00;14:05",
                "TransInterval=1",
                "TransTables=RemoteAtt\tAttLog",
                "Realtime=1",
                "SessionID=%s" % reg["session_id"],
                "TimeoutSec=10",
            ]
        )

    def _resolve(self, sn: str, ip_hint: Optional[str]) -> Tuple[str, str]:
        if self._resolve_device:
            try:
                return self._resolve_device(sn, ip_hint)
            except Exception:
                pass
        ip = (ip_hint or "").strip()
        if ip:
            return "%s:4370" % ip, sn or ip
        return "adms:%s" % ((sn or "unknown").strip()), sn or "ADMS"

    def _ingest_body(self, sn: str, table: str, body: str, ip_hint: Optional[str]) -> None:
        table_l = (table or "").strip().lower()
        if table_l not in ("attlog", "rtlog", "transaction", "remoteatt"):
            return
        device_key, label = self._resolve(sn, ip_hint)
        is_live = table_l == "rtlog"
        rows = parse_attendance_lines(body)
        fail_rows = parse_failed_rf_card_lines(body) if table_l == "rtlog" else []
        if (body or "").strip() and not rows and not fail_rows:
            preview = (body or "").replace("\n", " | ").strip()[:120]
            self._log("ADMS %s 파싱 0건 (본문 미리보기: %s)" % (table_l, preview))
        for rec in rows:
            rec["device"] = device_key
            rec["label"] = label
            rec["adms_sn"] = sn
            rec["live_event"] = is_live
            rec["adms_table"] = table_l
            try:
                self._on_attendance(rec)
            except Exception as e:
                self._log("ADMS 기록 처리 오류: %s" % str(e)[:120])
        if self._on_auth_failure:
            for rec in fail_rows:
                rec["device"] = device_key
                rec["label"] = label
                rec["adms_sn"] = sn
                rec["live_event"] = is_live
                rec["adms_table"] = table_l
                try:
                    self._on_auth_failure(rec)
                except Exception as e:
                    self._log("ADMS 인증실패 처리 오류: %s" % str(e)[:120])

    def _extract_ip_from_body(self, body: str) -> str:
        m = re.search(r"IPAddress=([0-9.]+)", body or "", re.I)
        if m:
            return m.group(1).strip()
        m = re.search(r"IPAddress=([^\s,]+)", body or "", re.I)
        return m.group(1).strip() if m else ""

    def start(self) -> None:
        if self._httpd is not None:
            return
        server_ref = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                return

            def _read_body(self) -> bytes:
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    n = 0
                if n <= 0:
                    return b""
                return self.rfile.read(n)

            def _query(self) -> Dict[str, List[str]]:
                return parse_qs(urlparse(self.path).query)

            def _sn(self) -> str:
                q = self._query()
                vals = q.get("SN") or q.get("sn") or [""]
                return str(vals[0] or "").strip()

            def _send(self, code: int, body: str, content_type: str = "text/plain") -> None:
                data = (body or "").encode("utf-8", errors="replace")
                self.send_response(code)
                self.send_header("Content-Type", "%s; charset=UTF-8" % content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Date", datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT"))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)

            def _peer_ip(self) -> str:
                try:
                    return str(self.client_address[0] or "").strip()
                except Exception:
                    return ""

            def _ip_for_registry(self, body: str) -> str:
                ip = server_ref._extract_ip_from_body(body)
                if ip:
                    return ip
                return self._peer_ip()

            def _touch_sn(self, sn: str, body: str = "") -> str:
                ip = self._ip_for_registry(body)
                if sn:
                    server_ref._registry.touch(sn, ip)
                return ip

            def do_GET(self):
                path = urlparse(self.path).path or ""
                sn = self._sn()
                q = self._query()
                if path.endswith("/getrequest"):
                    ip = self._touch_sn(sn)
                    resp = server_ref._cmd_queue.pop_response_text(sn)
                    if resp != "OK":
                        preview = resp.replace("\n", " | ").strip()[:160]
                        server_ref._log(
                            "ADMS getrequest SN=%s ip=%s → %s"
                            % (sn, ip or "-", preview)
                        )
                    else:
                        server_ref._log("ADMS getrequest SN=%s ip=%s" % (sn, ip or "-"))
                    self._send(200, resp)
                    return
                if path.endswith("/cdata") and (q.get("options") or [""])[0].lower() == "all":
                    ip = self._touch_sn(sn)
                    server_ref._log("ADMS options SN=%s ip=%s" % (sn, ip or "-"))
                    self._send(200, server_ref._config_text(sn))
                    return
                if path.endswith("/cdata"):
                    self._send(200, "OK")
                    return
                if path.endswith("/ping"):
                    self._send(200, "OK")
                    return
                self._send(404, "Not Found")

            def do_POST(self):
                path = urlparse(self.path).path or ""
                sn = self._sn()
                q = self._query()
                body_b = self._read_body()
                body = body_b.decode("utf-8", errors="replace")
                table = (q.get("table") or [""])[0]
                ip_hint = self._ip_for_registry(body)
                if sn:
                    server_ref._registry.touch(sn, ip_hint)

                if path.endswith("/registry"):
                    server_ref._log("ADMS registry SN=%s ip=%s" % (sn, ip_hint or "-"))
                    reg = server_ref._registry.ensure(sn)
                    self._send(200, "RegistryCode=%s\r\n" % reg["registry_code"])
                    return
                if path.endswith("/push"):
                    server_ref._log("ADMS push SN=%s ip=%s" % (sn, ip_hint or "-"))
                    self._send(200, server_ref._config_text(sn))
                    return
                if path.endswith("/exchange"):
                    server_ref._log("ADMS exchange SN=%s (암호화 handshake — OK만 응답)" % sn)
                    self._send(200, "OK")
                    return
                if path.endswith("/devicecmd"):
                    cmd_id, ret, cmd_echo = parse_devicecmd_body(body, q)
                    if cmd_id:
                        server_ref._cmd_queue.complete(sn, cmd_id, ret, cmd_echo)
                        server_ref._log(
                            "ADMS devicecmd SN=%s ID=%s Return=%s CMD=%s"
                            % (sn, cmd_id, ret, cmd_echo or "-")
                        )
                    self._send(200, "OK")
                    return
                if path.endswith("/cdata"):
                    ingest_table = (table or "").strip()
                    if ingest_table.lower() == "tabledata":
                        ingest_table = str((q.get("tablename") or [""])[0] or "").strip()
                    if ingest_table:
                        preview = (body or "").replace("\n", " | ").strip()[:80]
                        server_ref._log(
                            "ADMS cdata SN=%s table=%s body=%s"
                            % (sn, ingest_table, preview or "(empty)")
                        )
                        server_ref._ingest_body(sn, ingest_table, body, ip_hint)
                    elif (body or "").strip():
                        server_ref._log(
                            "ADMS cdata SN=%s (table 없음) body=%s"
                            % (sn, (body or "").replace("\n", " | ").strip()[:80])
                        )
                    self._send(200, "OK")
                    return
                self._send(404, "Not Found")

        self._httpd = ThreadingHTTPServer((self._host, self._port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        self._log("ADMS PUSH 수신 시작 %s" % self.listen_addr)

    def stop(self) -> None:
        httpd = self._httpd
        self._httpd = None
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass
        th = self._thread
        self._thread = None
        if th is not None:
            try:
                th.join(timeout=3.0)
            except Exception:
                pass
        self._log("ADMS PUSH 수신 중지")


def default_resolve_device_from_settings(
    get_settings: Callable[[], dict],
) -> ResolveDeviceFn:
    """device_settings.f22_terminals 에서 SN·IP로 device_key·label 매칭."""

    def _resolve(sn: str, ip_hint: Optional[str]) -> Tuple[str, str]:
        sn = (sn or "").strip()
        ip_hint = (ip_hint or "").strip()
        data = get_settings() if get_settings else {}
        if not isinstance(data, dict):
            data = {}
        terminals = data.get("f22_terminals")
        if not isinstance(terminals, list):
            terminals = []
        for t in terminals:
            if not isinstance(t, dict):
                continue
            t_sn = str(t.get("serial_no") or t.get("adms_sn") or t.get("device_sn") or "").strip()
            ip = str(t.get("ip") or t.get("host") or "").strip()
            try:
                port = int(t.get("port") or 4370)
            except (TypeError, ValueError):
                port = 4370
            dno = str(t.get("device_no") or "").strip()
            label = dno if dno else (ip or sn)
            if sn and t_sn and t_sn == sn:
                return "%s:%s" % (ip or "adms", port), label
            if ip_hint and ip and ip == ip_hint:
                return "%s:%s" % (ip, port), label
        if ip_hint:
            return "%s:4370" % ip_hint, ip_hint
        return "adms:%s" % (sn or "unknown"), sn or "ADMS"

    return _resolve


def push_server_endpoint_from_env() -> Tuple[str, int]:
    host = (os.environ.get("MEAL_ADMS_PUSH_HOST") or "0.0.0.0").strip()
    try:
        port = int(os.environ.get("MEAL_ADMS_PUSH_PORT", "8088") or "8088")
    except ValueError:
        port = 8088
    port = max(1, min(65535, port))
    return host, port


def _terminal_row_label(t: dict) -> str:
    loc = str(t.get("install_location") or t.get("location") or "").strip()
    return ("%s 단말기:" % loc) if loc else "단말기:"


def _terminal_config_sn(t: dict) -> str:
    return str(t.get("serial_no") or t.get("adms_sn") or t.get("device_sn") or "").strip()


def _configured_terminals(settings: dict) -> list:
    terminals = settings.get("f22_terminals") if isinstance(settings, dict) else None
    if not isinstance(terminals, list):
        return []
    out = []
    for t in terminals:
        if not isinstance(t, dict):
            continue
        ip = (t.get("ip") or t.get("host") or "").strip()
        cfg_sn = _terminal_config_sn(t)
        if ip or cfg_sn:
            out.append(t)
    return out


def _match_terminal_registry(t: dict, registry: Dict[str, Dict[str, str]]) -> Tuple[str, str]:
    """설정 단말 ↔ ADMS registry 매칭 → (sn, ip)."""
    ip = (t.get("ip") or t.get("host") or "").strip()
    cfg_sn = _terminal_config_sn(t)
    for reg_sn, reg in registry.items():
        reg_ip = str(reg.get("ip") or "").strip()
        if cfg_sn and cfg_sn == reg_sn:
            return reg_sn, reg_ip
        if ip and reg_ip and ip == reg_ip:
            return reg_sn, reg_ip
        if ip and reg_sn and ip == reg_sn:
            return reg_sn, reg_ip
    return "", ""


def _fallback_match_registry(t: dict, registry: Dict[str, Dict[str, str]], configured: list) -> Tuple[str, str]:
    """단말 1대·registry 1건이면 IP 없어도 최근 접속을 연결로 본다."""
    if len(configured) != 1 or not registry:
        return "", ""
    if _match_terminal_registry(t, registry)[0]:
        return "", ""
    best_sn = ""
    best_seen = ""
    for reg_sn, reg in registry.items():
        seen = str(reg.get("last_seen") or "").strip()
        if seen >= best_seen:
            best_seen = seen
            best_sn = reg_sn
    if not best_sn:
        return "", ""
    reg_ip = str((registry.get(best_sn) or {}).get("ip") or "").strip()
    return best_sn, reg_ip


def adms_terminals_report(
    settings: dict | None,
    registry: Optional[Dict[str, Dict[str, str]]] = None,
    *,
    push_running: bool = False,
    log_receive_active: bool = False,
    receive_active: bool = False,
    adms_url: str = "",
) -> dict:
    """ADMS PUSH 기준 단말 연결 상태. 연결 ON=서버+registry, 로그 수신=log_receive_active."""
    if receive_active and not log_receive_active:
        log_receive_active = receive_active
    out_lines: list[str] = []
    status_rows: list[dict] = []
    registry = registry or {}

    if adms_url:
        out_lines.append("단말 ADMS 서버 주소: %s" % adms_url)
    if push_running and log_receive_active:
        out_lines.append("PC: 연결 서버 실행 + 로그 수신 중")
    elif push_running:
        out_lines.append("PC: 연결 서버 실행 — 단말 접속·사이드바 ON 확인")
    else:
        out_lines.append("PC: 연결 서버 중지")

    if not isinstance(settings, dict):
        settings = {}
    terminals = settings.get("f22_terminals")
    if not isinstance(terminals, list) or not terminals:
        return {
            "all_ok": False,
            "short": "단말: 등록 없음",
            "detail_lines": out_lines + ["등록된 F22 단말기가 없습니다."],
            "status_rows": [{"label": "단말기:", "ok": False}],
        }

    configured = _configured_terminals(settings)
    all_ok = True
    short_parts: list[str] = []
    any_row = False
    for t in configured:
        any_row = True
        ip = (t.get("ip") or t.get("host") or "").strip()
        cfg_sn = _terminal_config_sn(t)
        dno = str(t.get("device_no") or "").strip()
        label = dno if dno else (ip or cfg_sn)

        matched_sn, matched_ip = _match_terminal_registry(t, registry)
        if not matched_sn:
            matched_sn, matched_ip = _fallback_match_registry(t, registry, configured)

        if push_running and matched_sn:
            ok = True
            seen = str((registry.get(matched_sn) or {}).get("last_seen") or "").strip()
            detail = "ADMS 연결 SN=%s" % matched_sn
            if matched_ip:
                detail += " ip=%s" % matched_ip
            if seen:
                detail += " (%s)" % seen
            if log_receive_active:
                line = "✓ [%s] %s · 로그 수신 중" % (label, detail)
            else:
                line = "✓ [%s] %s · 연결됨 (로그 수신 대기)" % (label, detail)
            short_parts.append("%s ✓" % (matched_sn or ip))
        elif push_running:
            ok = False
            hint = ip or cfg_sn or label
            line = "✗ [%s] ADMS 미연결 — 단말 서버 URL·SN·IP 확인 (%s)" % (label, hint)
            short_parts.append("%s ✗" % hint)
        else:
            ok = False
            line = "✗ [%s] 연결 서버 중지" % label
            short_parts.append("%s 중지" % label)

        all_ok = all_ok and ok
        status_rows.append({
            "label": _terminal_row_label(t),
            "ok": ok,
            "detail": line,
            "device_no": dno,
            "ip": ip,
        })
        out_lines.append(line)

    if not any_row:
        return {
            "all_ok": False,
            "short": "단말: IP/SN 없음",
            "detail_lines": out_lines + ["등록된 단말에 IP 또는 단말 SN이 없습니다."],
            "status_rows": [{"label": "단말기:", "ok": False}],
        }

    short = "단말: " + (", ".join(short_parts) if short_parts else "확인")
    return {
        "all_ok": all_ok,
        "short": short[:120],
        "detail_lines": out_lines,
        "status_rows": status_rows,
    }
