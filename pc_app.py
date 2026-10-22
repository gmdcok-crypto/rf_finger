from __future__ import annotations

import sys
import warnings
# Suppress PyQt5 sip deprecation warnings
warnings.filterwarnings("ignore", category=DeprecationWarning, message=".*sipPyTypeDict.*")
import json
import os
import queue
import threading
from pathlib import Path

try:
    from meal_db.connection import load_meal_db_dotenv

    load_meal_db_dotenv()
except Exception:
    pass

import bixolon_print

from fp_register_ui import FingerprintRegisterDialog, dark_information, dark_question, dark_warning

from meal_db.local_client import terminal_attendance_stable_key, _db_time_as_time
from datetime import datetime, date, timedelta
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QStackedWidget, QTableWidget, QTableWidgetItem,
    QLineEdit, QComboBox, QFrame, QHeaderView, QGraphicsDropShadowEffect,
    QAbstractItemView, QDialog, QFormLayout, QMessageBox, QInputDialog, QStyledItemDelegate, QTimeEdit, QDateEdit, QCalendarWidget, QLayout, QPlainTextEdit, QSizePolicy,     QCheckBox, QSpinBox, QGroupBox, QProgressBar, QRadioButton, QButtonGroup, QListWidget, QListWidgetItem
)
from PyQt5.QtCore import Qt, QSize, pyqtSignal, QTimer, QThread, QTime, QDate, QByteArray, QObject, QEvent
from PyQt5.QtGui import QFont, QColor, QIcon, QPixmap, QResizeEvent, QImage, QPainter

class CaptionPushButton(QPushButton):
    """본문 바로 아래에 더 작은 안내 글자를 그리는 버튼."""

    def __init__(self, title: str, caption: str, parent=None):
        super().__init__("", parent)
        self._title = title
        self._caption = caption

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.TextAntialiasing)
        color = QColor("#f8fafc" if self.isEnabled() else "#64748b")
        rect = self.rect().adjusted(6, 4, -6, -4)
        title_font = QFont("Malgun Gothic")
        title_font.setPixelSize(19)
        title_font.setBold(True)
        cap_font = QFont("Malgun Gothic")
        cap_font.setPixelSize(12)
        cap_font.setBold(True)
        painter.setFont(title_font)
        title_h = painter.fontMetrics().height()
        painter.setFont(cap_font)
        cap_h = painter.fontMetrics().height()
        gap = 0
        total = title_h + gap + cap_h
        y = rect.top() + max(0, (rect.height() - total) // 2)
        painter.setPen(color)
        painter.setFont(title_font)
        painter.drawText(
            rect.left(), y, rect.width(), title_h,
            Qt.AlignHCenter | Qt.AlignVCenter, self._title,
        )
        painter.setFont(cap_font)
        painter.drawText(
            rect.left(), y + title_h + gap, rect.width(), cap_h,
            Qt.AlignHCenter | Qt.AlignVCenter, self._caption,
        )
        painter.end()


# --- Worker Thread for Async Data Loading ---
class DataLoader(QThread):
    finished = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, func, *args, **kwargs):
        super().__init__()
        self.func = func
        self.args = args
        self.kwargs = kwargs

    def run(self):
        try:
            result = self.func(*self.args, **self.kwargs)
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))


def _meal_policy_attlog_trigger_datetime(
    today: date,
    end_time_raw,
    minutes_after: int,
) -> datetime:
    """정책 end_time(당일) + N분 → 단말 로그 클리어 트리거 시각. 야식은 심야/새벽 정책으로 분리해 자정 넘김 구간은 두지 않음."""
    e = _db_time_as_time(end_time_raw)
    base = datetime.combine(today, e)
    return base + timedelta(minutes=int(minutes_after))


def _device_ping_task(api):
    import f22_pyzk

    return f22_pyzk.ping_f22_terminals_report(api.get_device_settings())


def _build_adms_status_report(main_win, api, log_receive_active=None) -> dict:
    from meal_db.adms_push_server import adms_terminals_report, push_server_endpoint_from_env

    srv = getattr(main_win, "_adms_push_server", None) if main_win is not None else None
    registry = srv.get_registry_snapshot() if srv is not None else {}
    push_running = bool(srv is not None and srv.is_running())
    if log_receive_active is None:
        log_receive = bool(getattr(main_win, "_f22_receive_active", False)) if main_win is not None else False
    else:
        log_receive = bool(log_receive_active)
    _, port = push_server_endpoint_from_env()
    ip_hint = main_win._local_lan_ip_hint() if main_win is not None else "PC_IP"
    return adms_terminals_report(
        api.get_device_settings(),
        registry,
        push_running=push_running,
        log_receive_active=log_receive,
        adms_url="http://%s:%s/iclock/" % (ip_hint, port),
    )


def _device_status_task(api, main_win=None):
    """단말 연결 상태: ADMS=PUSH registry, 그 외=pyzk 4370 핑."""
    if _f22_att_mode_is_adms():
        return _build_adms_status_report(main_win, api)
    return _device_ping_task(api)


def _tcp_port_open(host: str, port: int, timeout: float = 1.2) -> bool:
    """프린터·경광등 IP:포트에 TCP 접속이 되는지만 확인한다. 제어 명령은 보내지 않는다."""
    import socket

    ip = (host or "").strip()
    if not ip:
        return False
    try:
        port_i = int(port)
    except (TypeError, ValueError):
        return False
    if not (1 <= port_i <= 65535):
        return False
    try:
        with socket.create_connection((ip, port_i), timeout=timeout):
            return True
    except Exception:
        return False


def _probe_printer_qlight(printers, qlights) -> dict:
    """{"ip:port": bool} — 같은 주소는 한 번만 확인."""
    out = {}
    for row in list(printers or []) + list(qlights or []):
        if not isinstance(row, dict):
            continue
        ip = str(row.get("ip") or "").strip()
        try:
            port = int(row.get("port") or 0)
        except (TypeError, ValueError):
            continue
        key = "%s:%s" % (ip, port)
        if not ip or key in out:
            continue
        out[key] = _tcp_port_open(ip, port)
    return out


def render_terminal_status_rows(layout, rep, conn_icon=None, disconn_icon=None, *, compact=False):
    """사이드바·설정 탭 공통: 단말별 ON/OFF + conn/disconn 아이콘."""
    if layout is None:
        return
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.deleteLater()
    rows = rep.get("status_rows") if isinstance(rep, dict) else None
    if not isinstance(rows, list) or not rows:
        ok = bool(rep.get("all_ok", False)) if isinstance(rep, dict) else False
        rows = [{"label": "단말기:", "ok": ok, "detail": ""}]
    font_px = 12 if compact else 14
    icon_box = 28 if compact else 52
    icon_px = 24 if compact else 44
    row_margins = (2, 0, 2, 0) if compact else (4, 2, 4, 2)
    for row in rows:
        if not isinstance(row, dict):
            continue
        label_txt = (str(row.get("label") or "단말기:")).strip() or "단말기:"
        one_ok = bool(row.get("ok", False))
        detail = str(row.get("detail") or "").strip()
        row_w = QWidget()
        h = QHBoxLayout(row_w)
        h.setContentsMargins(*row_margins)
        h.setSpacing(4 if compact else 6)
        lab = QLabel(label_txt)
        lab.setStyleSheet(
            "color: #e2e8f0; font-size: %dpx; font-weight: bold; font-family: 'Malgun Gothic';"
            % font_px
        )
        lab.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        if detail:
            lab.setToolTip(detail)
        onoff = QLabel("ON" if one_ok else "OFF")
        onoff.setStyleSheet(
            "color: %s; font-size: %dpx; font-weight: bold; font-family: 'Malgun Gothic';"
            % (("#4ade80" if one_ok else "#f87171"), font_px)
        )
        onoff.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        if detail:
            onoff.setToolTip(detail)
        ic = QLabel()
        ic.setFixedSize(icon_box, icon_box)
        ic.setAlignment(Qt.AlignCenter)
        src = conn_icon if one_ok else disconn_icon
        if src is not None and not src.isNull():
            ic.setPixmap(src.scaled(icon_px, icon_px, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            ic.setText("—")
            ic.setStyleSheet(
                "color: #64748b; font-weight: bold; font-size: %dpx;" % (12 if compact else 16)
            )
        if detail:
            ic.setToolTip(detail)
        h.addWidget(lab, 0)
        h.addWidget(onoff, 0)
        h.addStretch(1)
        h.addWidget(ic, 0, Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(row_w)


def _f22_att_mode() -> str:
    """live=pull 4370 pyzk, poll=주기 pull, adms=HTTP PUSH 수신(MEAL_F22_ATT_MODE=adms)."""
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


def _f22_att_mode_is_live() -> bool:
    return _f22_att_mode() == "live"


def _f22_att_mode_is_adms() -> bool:
    return _f22_att_mode() == "adms"


def _attendance_stream_is_live(rec: dict) -> bool:
    """ADMS rtlog / pyzk live_capture 여부 (브로드캐스트 테이블·플래그)."""
    if "live_event" in rec:
        return bool(rec.get("live_event"))
    return not _f22_att_mode_is_adms()


def _live_device_max_age_sec() -> float:
    """프린터·경광등: 출입 시각이 지금과 이 초 이내일 때만 동작 (기본 90초).

    단말이 저장 후 재접속 시 rtlog로 밀어내는 과거 건은 live_event=True여도
    시각이 오래되어 장치 출력을 건너뛴다. MEAL_LIVE_DEVICE_MAX_AGE_SEC.
    """
    try:
        return float(os.environ.get("MEAL_LIVE_DEVICE_MAX_AGE_SEC", "90") or "90")
    except (TypeError, ValueError):
        return 90.0


def _attendance_is_live_for_ticket(rec: dict) -> bool:
    """프린터·경광등 대상: 실시간 스트림 + 출입 시각이 최근인 경우만.

    - attlog 백필 / live_event=False → False
    - 앱 기동 시 rtlog로 밀려 온 과거 저장 기록 → False (시각 차이)
    - 방금 카드/지문 인증 → True
    """
    if not _attendance_stream_is_live(rec):
        return False
    ts = rec.get("timestamp")
    if not isinstance(ts, datetime):
        return False
    if ts.tzinfo is not None:
        ts = ts.astimezone().replace(tzinfo=None)
    else:
        ts = ts.replace(tzinfo=None)
    age = (datetime.now() - ts).total_seconds()
    max_age = max(5.0, _live_device_max_age_sec())
    # 단말 시계가 약간 앞선 경우는 허용(최대 30초)
    if age < -30.0:
        return False
    return age <= max_age


class AdmsPushBridge(QObject):
    """ADMS HTTP 스레드 → Qt 메인 스레드."""

    attendance = pyqtSignal(dict)
    auth_failure = pyqtSignal(dict)
    status = pyqtSignal(str)


class F22LiveAttendanceThread(QThread):
    """단말이 출입 시 TCP로 보내는 EF_ATTLOG 이벤트를 수신 (pyzk live_capture)."""

    attendance = pyqtSignal(dict)
    thread_failed = pyqtSignal(str)

    def __init__(self, ip: str, port: int, pwd: int, device_key: str, label: str):
        super().__init__()
        self._ip = (ip or "").strip()
        self._port = int(port)
        self._pwd = int(pwd)
        self._device_key = device_key
        self._label = label or self._ip
        self._conn = None
        try:
            self._recv_to = int(os.environ.get("MEAL_F22_LIVE_RECV_TIMEOUT", "10"))
        except ValueError:
            self._recv_to = 10
        self._recv_to = max(3, min(120, self._recv_to))

    def stop_listener(self):
        self.requestInterruption()
        c = self._conn
        if c is not None:
            try:
                c.end_live_capture = True
            except Exception:
                pass

    def run(self):
        from zk import ZK

        if not self._ip:
            self.thread_failed.emit("IP 없음")
            return
        zk = ZK(
            self._ip,
            port=self._port,
            password=self._pwd,
            timeout=max(30, self._recv_to + 20),
            ommit_ping=True,
        )
        conn = None
        try:
            conn = zk.connect()
            self._conn = conn
        except Exception as e:
            self.thread_failed.emit("[%s] 실시간 연결 실패: %s" % (self._label, e))
            return
        try:
            for att in conn.live_capture(new_timeout=self._recv_to):
                if self.isInterruptionRequested():
                    conn.end_live_capture = True
                    break
                if att is None:
                    continue
                ts = att.timestamp
                if isinstance(ts, datetime) and ts.tzinfo is not None:
                    ts = ts.astimezone().replace(tzinfo=None)
                elif isinstance(ts, datetime):
                    ts = ts.replace(tzinfo=None)
                else:
                    continue
                try:
                    zuid = int(att.uid)
                except (TypeError, ValueError):
                    zuid = 0
                self.attendance.emit(
                    {
                        "device": self._device_key,
                        "label": self._label,
                        "user_id": str(att.user_id).strip(),
                        "timestamp": ts,
                        "status": int(att.status) if att.status is not None else 0,
                        "punch": int(att.punch) if att.punch is not None else 0,
                        "zk_uid": zuid,
                        "live_event": True,
                    }
                )
        except Exception as e:
            self.thread_failed.emit("[%s] 실시간 수신 오류: %s" % (self._label, e))
        finally:
            self._conn = None
            if conn is not None:
                try:
                    conn.end_live_capture = True
                except Exception:
                    pass
                try:
                    conn.disconnect()
                except Exception:
                    pass


def _f22_employee_sync_task(
    api,
    emp_no,
    name,
    rf,
    fp_uid,
    template_b64=None,
    template_b64_2=None,
    template_b64_3=None,
    template_b64_4=None,
    slot_fid_1=None,
    slot_fid_2=None,
    slot_fid_3=None,
    slot_fid_4=None,
):
    return api.sync_employee_to_f22_terminals(
        str(emp_no or ""),
        str(name or ""),
        str(rf or ""),
        str(fp_uid or ""),
        template_b64,
        template_b64_2,
        template_b64_3,
        template_b64_4,
        slot_fid_1,
        slot_fid_2,
        slot_fid_3,
        slot_fid_4,
    )


def _f22_import_from_terminal_task(api, emp_no: str):
    from meal_db.f22_import_from_terminal import import_fingerprint_templates_from_f22_terminal

    return import_fingerprint_templates_from_f22_terminal(emp_no)


def _sync_result_terminal_name(settings, key) -> str:
    """결과의 IP·단말기번호만 있어도 설정에서 번호·설치위치·IP로 풀어 준다."""
    from meal_db.f22_sync import sync_terminal_name

    raw = str(key or "").strip()
    terminals = settings.get("f22_terminals") if isinstance(settings, dict) else None
    if isinstance(terminals, list):
        for t in terminals:
            if not isinstance(t, dict):
                continue
            ip = str(t.get("ip") or t.get("host") or "").strip()
            dno = str(t.get("device_no") or t.get("terminal_no") or "").strip()
            if raw and raw in (ip, dno):
                return sync_terminal_name(t)
    return raw or "단말"


def _f22_sync_result_lines(api, results) -> tuple:
    """(실패 목록, 성공 목록). 각 항목은 (단말이름, 메시지)."""
    try:
        settings = api.get_device_settings()
    except Exception:
        settings = {}
    fails = []
    oks = []
    for row in results or []:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        name = _sync_result_terminal_name(settings, row[0])
        msg = str(row[2]).strip() if len(row) > 2 and row[2] is not None else ""
        if row[1]:
            oks.append((name, msg))
        else:
            fails.append((name, msg))
    return fails, oks


def _f22_fingerprint_sync_dialog_text(api, results) -> str:
    """등록·수정·지문 저장 후 단말 동기화 결과."""
    fails, oks = _f22_sync_result_lines(api, results)
    if not fails and not oks:
        return "DB에는 저장되었습니다.\n\n등록된 F22 단말이 없어 단말 전송은 하지 않았습니다."
    lines = []
    if fails:
        lines.append("DB에는 저장되었습니다. 아래 단말에는 전송되지 않았습니다.")
        lines.append("실패 %d대 · 성공 %d대" % (len(fails), len(oks)))
        lines.append("")
        lines.append("동기화되지 않은 단말")
        for name, msg in fails:
            lines.append("· %s" % name)
            if msg:
                lines.append("  %s" % msg)
        if oks:
            lines.append("")
            lines.append("동기화된 단말")
            for name, _msg in oks:
                lines.append("· %s" % name)
    else:
        lines.append("DB 저장 완료. 단말 %d대 동기화됨" % len(oks))
        for name, _msg in oks:
            lines.append("· %s" % name)
    return "\n".join(lines)


def _f22_sync_after_terminal_import_task(api, emp_row: dict):
    tpl0 = emp_row.get("fingerprint_template_b64")
    if not tpl0:
        return []
    return api.sync_fingerprint_to_f22_terminals(
        str(emp_row.get("emp_no") or ""),
        str(emp_row.get("name") or ""),
        str(emp_row.get("rf_card_uid") or emp_row.get("card_uid") or ""),
        "",
        tpl0,
        emp_row.get("fingerprint_template_b64_2"),
        emp_row.get("fingerprint_template_b64_3"),
        emp_row.get("fingerprint_template_b64_4"),
        emp_row.get("fingerprint_slot_fid_1"),
        emp_row.get("fingerprint_slot_fid_2"),
        emp_row.get("fingerprint_slot_fid_3"),
        emp_row.get("fingerprint_slot_fid_4"),
    )


class F22BulkResyncThread(QThread):
    progress = pyqtSignal(int, int, str, int, int, int)
    finished_summary = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, api, terminals_only=None, parent=None):
        super().__init__(parent)
        self.api = api
        self.terminals_only = terminals_only
        self._cancel = False

    def request_cancel(self):
        self._cancel = True

    def run(self):
        try:

            def cancel_check():
                return self._cancel

            def on_progress(cur, total, msg, ok, fail, skip):
                self.progress.emit(cur, total, msg, ok, fail, skip)

            summary = self.api.bulk_resync_active_employees_to_f22(
                self.terminals_only,
                cancel_check=cancel_check,
                on_progress=on_progress,
            )
            self.finished_summary.emit(summary)
        except Exception as e:
            self.error.emit(str(e))


class F22BulkResyncProgressDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("단말 재동기화")
        self.setModal(True)
        self.setMinimumWidth(500)
        lay = QVBoxLayout(self)
        self.status_label = QLabel("준비 중…")
        self.status_label.setWordWrap(True)
        lay.addWidget(self.status_label)
        self.counts_label = QLabel("")
        self.counts_label.setStyleSheet("color: #94a3b8; font-size: 12px;")
        lay.addWidget(self.counts_label)
        self.bar = QProgressBar()
        self.bar.setTextVisible(True)
        lay.addWidget(self.bar)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.cancel_btn = QPushButton("취소")
        self.cancel_btn.clicked.connect(self._on_cancel)
        btn_row.addWidget(self.cancel_btn)
        lay.addLayout(btn_row)
        self._cancelled = False

    def _on_cancel(self):
        self._cancelled = True
        self.cancel_btn.setEnabled(False)
        self.status_label.setText("취소 요청 중… 남은 사원은 처리하지 않습니다.")

    def was_cancelled(self):
        return self._cancelled

    def update_progress(self, cur, total, msg, ok, fail, skip):
        self.status_label.setText(msg or "")
        self.counts_label.setText("성공 %d · 실패 %d · 건너뜀 %d" % (ok, fail, skip))
        if total > 0:
            self.bar.setMaximum(total)
            self.bar.setValue(min(cur, total))
            self.bar.setFormat("%v / %m")


def _f22_bulk_resync_dialog_text(summary: dict) -> str:
    if not isinstance(summary, dict):
        return "결과를 표시할 수 없습니다."
    total = int(summary.get("total") or 0)
    ok = int(summary.get("ok_emp") or 0)
    fail = int(summary.get("fail_emp") or 0)
    skip = int(summary.get("skip_emp") or 0)
    lines = [
        "처리 대상 %d명 — 성공 %d · 실패 %d · 건너뜀 %d" % (total, ok, fail, skip),
        "단말에서 삭제 %d명" % int(summary.get("deleted_emp") or 0),
    ]
    kept_admin = int(summary.get("kept_admin") or 0)
    if kept_admin:
        lines.append("단말 관리자 계정 %d명은 삭제하지 않았습니다." % kept_admin)
    if summary.get("cancelled"):
        lines.append("")
        lines.append("취소되어 남은 사원은 처리하지 않았습니다.")
    failures = summary.get("per_emp_failures") or []
    if failures:
        lines.append("")
        lines.append("— 실패 상세 —")
        show = failures[:40]
        lines.extend(show)
        if len(failures) > 40:
            lines.append("… 외 %d건" % (len(failures) - 40))
    return "\n".join(lines)


class StartupOverlay(QWidget):
    """앱 기동 시 단계별 진행 상황을 메인 화면 위에 표시."""

    _STATE_STYLE = {
        "pending": ("○", "#64748b"),
        "active": ("▶", "#60a5fa"),
        "done": ("✓", "#4ade80"),
        "error": ("✗", "#f87171"),
    }

    def __init__(self, parent, step_labels: list):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet("background-color: rgba(15, 23, 42, 0.94);")
        outer = QVBoxLayout(self)
        outer.setAlignment(Qt.AlignCenter)
        panel = QFrame()
        panel.setObjectName("StartupPanel")
        panel.setStyleSheet(
            "QFrame#StartupPanel { background-color: #1e293b; border: 1px solid #475569; "
            "border-radius: 14px; }"
        )
        panel.setMinimumWidth(460)
        panel.setMaximumWidth(560)
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(28, 24, 28, 24)
        pl.setSpacing(14)
        title = QLabel("Meal Admin")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            "color: #f8fafc; font-size: 28px; font-weight: bold; font-family: 'Malgun Gothic';"
        )
        pl.addWidget(title)
        self._subtitle = QLabel("시작 중…")
        self._subtitle.setAlignment(Qt.AlignCenter)
        self._subtitle.setStyleSheet(
            "color: #94a3b8; font-size: 15px; font-weight: bold; font-family: 'Malgun Gothic';"
        )
        pl.addWidget(self._subtitle)
        self._row_labels: list[QLabel] = []
        for txt in step_labels:
            row = QLabel("○  %s" % txt)
            row.setStyleSheet(
                "color: #64748b; font-size: 16px; font-weight: bold; font-family: 'Malgun Gothic'; "
                "padding: 2px 0;"
            )
            self._row_labels.append(row)
            pl.addWidget(row)
        outer.addWidget(panel)

    def set_step_state(self, index: int, state: str):
        if index < 0 or index >= len(self._row_labels):
            return
        prefix, color = self._STATE_STYLE.get(state, self._STATE_STYLE["pending"])
        text = self._row_labels[index].text()
        if "  " in text:
            text = text.split("  ", 1)[1]
        self._row_labels[index].setText("%s  %s" % (prefix, text))
        self._row_labels[index].setStyleSheet(
            "color: %s; font-size: 16px; font-weight: bold; font-family: 'Malgun Gothic'; "
            "padding: 2px 0;" % color
        )
        if state == "active":
            self._subtitle.setText(text)

    def set_status(self, text: str):
        self._subtitle.setText(str(text or ""))

    def mark_all_done(self):
        for i in range(len(self._row_labels)):
            self.set_step_state(i, "done")
        self._subtitle.setText("시작 완료")

    def dismiss(self):
        self.hide()
        self.deleteLater()


def _f22_terminal_pick_label(t: dict, adms: bool = False) -> str:
    if not isinstance(t, dict):
        return "단말"
    parts = []
    dno = str(t.get("device_no") or t.get("terminal_no") or "").strip()
    if dno:
        parts.append(dno)
    ip = str(t.get("ip") or t.get("host") or "").strip()
    if ip:
        parts.append(ip)
    if not adms:
        port = str(t.get("port") or "").strip()
        if port:
            parts.append("포트 %s" % port)
    loc = str(t.get("install_location") or "").strip()
    if loc:
        parts.append(loc)
    return " · ".join(parts) if parts else "단말"


class F22TerminalPickDialog(QDialog):
    """단말 재동기화 — 대상 단말 1대 선택."""

    def __init__(self, terminals: list, parent=None, *, adms: bool = False):
        super().__init__(parent)
        self.setWindowTitle("단말 선택")
        self.setModal(True)
        self.setMinimumSize(500, 320)
        self._terminals = [t for t in (terminals or []) if isinstance(t, dict)]
        self._selected = None
        lay = QVBoxLayout(self)
        lay.setSpacing(12)
        hint = QLabel("재동기화할 단말을 선택하세요.")
        hint.setStyleSheet(
            "color: #e2e8f0; font-size: 16px; font-weight: bold; font-family: 'Malgun Gothic';"
        )
        lay.addWidget(hint)
        self.list_w = QListWidget()
        self.list_w.setStyleSheet(
            "QListWidget { background-color: #1e293b; color: #f1f5f9; border: 1px solid #475569; "
            "border-radius: 8px; font-size: 16px; font-weight: bold; font-family: 'Malgun Gothic'; }"
            "QListWidget::item { padding: 10px 12px; }"
            "QListWidget::item:selected { background-color: #3b82f6; color: #ffffff; }"
        )
        for i, t in enumerate(self._terminals):
            item = QListWidgetItem(_f22_terminal_pick_label(t, adms))
            item.setData(Qt.UserRole, i)
            self.list_w.addItem(item)
        if self.list_w.count() > 0:
            self.list_w.setCurrentRow(0)
        self.list_w.itemDoubleClicked.connect(lambda _it: self._accept_current())
        lay.addWidget(self.list_w, 1)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("취소")
        cancel_btn.setObjectName("SecondaryBtn")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = QPushButton("선택")
        ok_btn.setObjectName("PrimaryBtn")
        ok_btn.clicked.connect(self._accept_current)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(ok_btn)
        lay.addLayout(btn_row)

    def _accept_current(self):
        row = self.list_w.currentRow()
        if row < 0 or row >= len(self._terminals):
            QMessageBox.warning(self, "단말 선택", "목록에서 단말을 선택하세요.")
            return
        self._selected = self._terminals[row]
        self.accept()

    def selected_terminal(self):
        return self._selected


# --- Premium QSS Styling ---
QSS = """
QMainWindow { background-color: #0f172a; font-family: 'Malgun Gothic', 'Segoe UI', sans-serif; font-weight: bold; }
QWidget#Sidebar { background-color: #1e293b; border-right: 1px solid #334155; min-width: 260px; }
QWidget#SidebarTerminalHost { border-top: 1px solid #334155; margin-top: 6px; padding-top: 4px; }
QLabel#SidebarTerminalTitle { color: #94a3b8; font-size: 13px; font-weight: bold; font-family: 'Malgun Gothic'; }
QLabel#Logo { color: #f8fafc; font-size: 28px; font-weight: bold; font-family: 'Malgun Gothic'; padding: 25px; }
QPushButton#MenuBtn {
    background-color: transparent; color: #94a3b8; border: none; text-align: left;
    padding: 15px 25px; font-size: 20px; font-weight: bold; font-family: 'Malgun Gothic'; border-radius: 8px; margin: 4px 12px;
}
QPushButton#MenuBtn:hover { background-color: #334155; color: #f8fafc; }
QPushButton#MenuBtn:pressed { background-color: #1e293b; color: #f8fafc; padding: 16px 25px 14px 27px; }
QPushButton#MenuBtn[active="true"] { background-color: #6366f1; color: #ffffff; }
QWidget#ContentArea { background-color: #0f172a; }
QLabel#HeaderTitle { color: #f8fafc; font-size: 36px; font-weight: bold; font-family: 'Malgun Gothic'; }
QFrame#StatCard { background-color: #1e293b; border-radius: 12px; border: 1px solid #334155; padding: 15px; }
QLabel#StatValue { color: #f8fafc; font-size: 52px; font-weight: bold; font-family: 'Malgun Gothic'; }
QLabel#StatLabel { color: #94a3b8; font-size: 22px; font-weight: bold; font-family: 'Malgun Gothic'; }
QTableWidget { background-color: #1e293b; color: #f1f5f9; gridline-color: #334155; border: none; border-radius: 12px; alternate-background-color: #1a2333; font-size: 17px; selection-background-color: #3b82f6; outline: none; font-weight: bold; }
QTableWidget::item { padding: 0px; border-bottom: 1px solid #334155; }
QTableWidget::item:selected { background-color: #3b82f6; color: #ffffff; font-weight: bold; }
QHeaderView::section { background-color: #111b2d; color: #94a3b8; padding: 12px; border: none; font-weight: bold; font-size: 19px; font-family: 'Malgun Gothic'; border-bottom: 2px solid #334155; }
QLineEdit, QTimeEdit { background-color: #1e293b; border: 1px solid #475569; border-radius: 8px; color: #f8fafc; padding: 10px 15px; font-size: 19px; height: 40px; font-weight: bold; font-family: 'Malgun Gothic'; }
QDateEdit { background-color: #1e293b; border: 1px solid #475569; border-radius: 8px; color: #f8fafc; padding-left: 10px; padding-right: 30px; font-size: 19px; height: 40px; font-weight: bold; font-family: 'Malgun Gothic'; }
QDateEdit::up-button { subcontrol-origin: border; subcontrol-position: top right; width: 20px; height: 16px; border-left: 1px solid #475569; border-top-right-radius: 8px; background-color: #334155; }
QDateEdit::down-button { subcontrol-origin: border; subcontrol-position: bottom right; width: 20px; height: 16px; border-left: 1px solid #475569; border-top: 1px solid #475569; border-bottom-right-radius: 8px; background-color: #334155; }
QDateEdit::drop-down { subcontrol-origin: border; subcontrol-position: top right; width: 26px; border-left: 1px solid #475569; border-top-right-radius: 8px; border-bottom-right-radius: 8px; background-color: #334155; }
QDateEdit::down-arrow { image: url("data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxMCIgaGVpZ2h0PSIxMCIgdmlld0JveD0iMCAwIDEwIDEwIj48cGF0aCBkPSJNMSAzIEg5IEw1IDggWiIgZmlsbD0iI2Y4ZmFmYyIvPjwvc3ZnPg=="); width: 12px; height: 12px; }
QCalendarWidget QWidget { background-color: #1e293b; color: #f8fafc; font-family: 'Malgun Gothic'; }
QCalendarWidget QAbstractItemView:enabled { background-color: #1e293b; color: #f8fafc; selection-background-color: #6366f1; selection-color: white; }
QCalendarWidget QToolButton { color: #f8fafc; background-color: #334155; border-radius: 4px; font-weight: bold; }
QCalendarWidget QMenu { background-color: #1e293b; color: #f8fafc; }
QCalendarWidget QSpinBox { color: #f8fafc; background-color: #1e293b; selection-background-color: #6366f1; }
QPushButton#PrimaryBtn { background-color: #3b82f6; color: white; border-radius: 8px; padding: 10px 20px; font-weight: bold; font-size: 19px; min-height: 40px; min-width: 100px; font-family: 'Malgun Gothic'; border: none; }
QPushButton#PrimaryBtn:hover { background-color: #2563eb; }
QPushButton#PrimaryBtn:pressed { background-color: #1d4ed8; padding-top: 11px; padding-bottom: 9px; padding-left: 21px; padding-right: 19px; }
QPushButton#PrimaryBtn:disabled { background-color: #334155; color: #64748b; }
QPushButton#SecondaryBtn { background-color: #64748b; color: #f8fafc; border-radius: 8px; padding: 10px 20px; font-weight: bold; font-size: 19px; min-height: 40px; min-width: 100px; font-family: 'Malgun Gothic'; border: none; }
QPushButton#SecondaryBtn:hover { background-color: #475569; }
QPushButton#SecondaryBtn:pressed { background-color: #334155; padding-top: 11px; padding-bottom: 9px; padding-left: 21px; padding-right: 19px; }
QPushButton#SecondaryBtn:disabled { background-color: #334155; color: #64748b; }
QPushButton#DashboardReceiveBtn {
    background-color: #64748b; color: #f8fafc; border-radius: 8px; padding: 8px 18px; font-weight: bold; font-size: 17px;
    min-height: 40px; min-width: 120px; font-family: 'Malgun Gothic'; border: 1px solid #475569;
}
QPushButton#DashboardReceiveBtn:hover { background-color: #475569; }
QPushButton#DashboardReceiveBtn:pressed { background-color: #334155; padding-top: 9px; padding-bottom: 7px; }
QPushButton#DashboardReceiveBtn:checked { background-color: #16a34a; color: #ffffff; border: 1px solid #15803d; }
QPushButton#DashboardReceiveBtn:checked:hover { background-color: #15803d; }
QPushButton#DashboardReceiveBtn:checked:pressed { background-color: #14532d; padding-top: 9px; padding-bottom: 7px; }
QPushButton#DangerBtn { background-color: #ef4444; color: white; border-radius: 8px; padding: 10px 20px; font-weight: bold; font-size: 19px; min-height: 40px; min-width: 100px; font-family: 'Malgun Gothic'; border: none; }
QPushButton#DangerBtn:hover { background-color: #dc2626; }
QPushButton#DangerBtn:pressed { background-color: #b91c1c; padding-top: 11px; padding-bottom: 9px; padding-left: 21px; padding-right: 19px; }
QPushButton#DangerBtn:disabled { background-color: #7f1d1d; color: #fecaca; }
QPushButton#SettingsCompactPrimaryBtn { background-color: #3b82f6; color: white; border-radius: 8px; padding: 6px 14px; font-weight: bold; font-size: 15px; min-height: 34px; min-width: 72px; font-family: 'Malgun Gothic'; border: none; }
QPushButton#SettingsCompactPrimaryBtn:hover { background-color: #2563eb; }
QPushButton#SettingsCompactPrimaryBtn:pressed { background-color: #1d4ed8; }
QPushButton#SettingsCompactSecondaryBtn { background-color: #64748b; color: #f8fafc; border-radius: 8px; padding: 6px 14px; font-weight: bold; font-size: 15px; min-height: 34px; min-width: 72px; font-family: 'Malgun Gothic'; border: none; }
QPushButton#SettingsCompactSecondaryBtn:hover { background-color: #475569; }
QPushButton#SettingsCompactSecondaryBtn:pressed { background-color: #334155; }
QPushButton#SettingsCompactDangerBtn { background-color: #ef4444; color: white; border-radius: 8px; padding: 6px 14px; font-weight: bold; font-size: 15px; min-height: 34px; min-width: 72px; font-family: 'Malgun Gothic'; border: none; }
QPushButton#SettingsCompactDangerBtn:hover { background-color: #dc2626; }
QPushButton#SettingsCompactDangerBtn:pressed { background-color: #b91c1c; }
QPushButton#SettingsCompactPrimaryBtn:disabled { background-color: #334155; color: #64748b; }
QPushButton#SettingsCompactSecondaryBtn:disabled { background-color: #334155; color: #64748b; }
QPushButton#SettingsCompactDangerBtn:disabled { background-color: #7f1d1d; color: #fecaca; }
QLabel#SettingsPageTitle { color: #f8fafc; font-size: 28px; font-weight: bold; font-family: 'Malgun Gothic'; }
QPushButton#SettingsSaveBtn { background-color: #3b82f6; color: white; border-radius: 8px; padding: 8px 16px; font-weight: bold; font-size: 15px; min-height: 36px; font-family: 'Malgun Gothic'; border: none; }
QPushButton#SettingsSaveBtn:hover { background-color: #2563eb; }
QPushButton#SettingsSaveBtn:pressed { background-color: #1d4ed8; }
QPushButton#SettingsSaveBtn:disabled { background-color: #334155; color: #64748b; }
QPushButton#SettingsActionBtn { background-color: #64748b; color: #f8fafc; border-radius: 8px; padding: 8px 16px; font-weight: bold; font-size: 15px; min-height: 36px; font-family: 'Malgun Gothic'; border: none; }
QPushButton#SettingsActionBtn:hover { background-color: #475569; }
QPushButton#SettingsActionBtn:pressed { background-color: #334155; }
QWidget#SettingsScreen QLineEdit { background-color: #1e293b; border: 1px solid #475569; border-radius: 8px; color: #f8fafc; padding: 6px 10px; font-size: 15px; min-height: 34px; font-weight: bold; font-family: 'Malgun Gothic'; }
QWidget#SettingsScreen QComboBox { background-color: #1e293b; border: 1px solid #475569; border-radius: 8px; color: #f8fafc; padding: 6px 10px; font-size: 15px; min-height: 34px; font-weight: bold; font-family: 'Malgun Gothic'; }
QWidget#SettingsScreen QCheckBox { color: #e2e8f0; font-size: 15px; font-weight: bold; font-family: 'Malgun Gothic'; }
QWidget#SettingsScreen QRadioButton { color: #f8fafc; font-size: 15px; font-weight: bold; font-family: 'Malgun Gothic'; }
QWidget#SettingsScreen QTableWidget { font-size: 15px; font-weight: bold; border-radius: 8px; }
QWidget#SettingsScreen QHeaderView::section { font-size: 14px; padding: 8px 8px; }
QWidget#SettingsScreen QGroupBox { font-size: 18px; }
QWidget#SettingsScreen QLabel#SettingsFormLabel {
    color: #f8fafc; font-size: 14px; font-weight: bold; font-family: 'Malgun Gothic';
    background: transparent;
}
QComboBox { background-color: #1e293b; border: 1px solid #475569; border-radius: 8px; color: #f8fafc; padding: 5px 15px; font-size: 21px; height: 40px; font-weight: bold; font-family: 'Malgun Gothic'; }
QComboBox QAbstractItemView { background-color: #1e293b; color: #f8fafc; selection-background-color: #3b82f6; border: 1px solid #334155; outline: none; }
QComboBox QAbstractItemView::item { min-height: 35px; padding: 2px 10px; }
QLabel#InputLabel { color: #ffffff; font-weight: bold; font-family: 'Malgun Gothic'; font-size: 18px; }
QTableWidget#EmployeeTable {
    border: 1px solid #334155;
    border-radius: 12px;
    background-color: #1e293b;
    gridline-color: #334155;
}
QTableWidget#EmployeeTable QHeaderView::section {
    background-color: #111b2d;
    color: #94a3b8;
    padding: 12px;
    font-weight: bold;
    font-size: 19px;
    font-family: 'Malgun Gothic';
    border: none;
    border-top: 1px solid #334155;
    border-bottom: 1px solid #334155;
    border-right: 1px solid #334155;
}
QTableWidget#EmployeeTable QHeaderView::section:first {
    border-left: 1px solid #334155;
}
QTableWidget#EmployeeTable QHeaderView::section:last {
    border-right: 1px solid #334155;
}
"""

class EmployeeSearchDialog(QDialog):
    def __init__(self, api, main_win, parent=None):
        super().__init__(parent)
        self.api = api
        self.main_win = main_win
        self.selected_employee = None
        self.setWindowTitle("사원 검색")
        self.setMinimumSize(760, 500)
        self.setStyleSheet(QSS)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)
        
        # Search area
        search_layout = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("검색할 성명 입력...")
        self.search_input.returnPressed.connect(self.on_search)
        
        self.search_btn = QPushButton("조회")
        self.search_btn.setObjectName("PrimaryBtn")
        self.search_btn.setFixedWidth(80)
        self.search_btn.clicked.connect(self.on_search)
        
        search_layout.addWidget(self.search_input)
        search_layout.addWidget(self.search_btn)
        layout.addLayout(search_layout)
        
        # Table area
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["사번", "성명", "부서", "회사", "카드 UID"])
        setup_standard_table(self.table)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.doubleClicked.connect(self.on_select)
        layout.addWidget(self.table)
        
        # Bottom buttons
        btn_layout = QHBoxLayout()
        self.select_btn = QPushButton("선택")
        self.select_btn.setObjectName("PrimaryBtn")
        self.select_btn.clicked.connect(self.on_select)
        
        self.cancel_btn = QPushButton("취소")
        self.cancel_btn.setObjectName("SecondaryBtn")
        self.cancel_btn.clicked.connect(self.reject)
        
        btn_layout.addStretch()
        btn_layout.addWidget(self.select_btn)
        btn_layout.addWidget(self.cancel_btn)
        layout.addLayout(btn_layout)

    def on_search(self):
        name = self.search_input.text().strip()
        if not name:
            return
        self.search_btn.setEnabled(False)
        self.loader = DataLoader(self.api.get_employees, name)
        self.loader.finished.connect(self.display_results)
        self.loader.error.connect(self.on_search_error)
        self.loader.start()

    def on_search_error(self, err_msg):
        self.search_btn.setEnabled(True)
        QMessageBox.warning(self, "조회 실패", f"사원 검색 중 오류가 발생했습니다:\n{err_msg}")

    def display_results(self, data):
        self.search_btn.setEnabled(True)
        if not isinstance(data, list):
            return
        self.results = data
        self.table.setRowCount(len(data))
        
        comp_map = {c["code"]: c["name"] for c in self.main_win.companies_data if isinstance(c, dict)}
        dept_map = {d["code"]: d["name"] for d in self.main_win.departments_data if isinstance(d, dict)}
        
        for i, row in enumerate(data):
            comp_name = comp_map.get(row.get("company_code"), "Unknown")
            dept_name = dept_map.get(row.get("department_code"), "Unknown")
            
            item_no = QTableWidgetItem(str(row.get("emp_no", "")))
            item_no.setData(Qt.UserRole, i) # Original index in self.results
            
            self.table.setItem(i, 0, item_no)
            self.table.setItem(i, 1, QTableWidgetItem(str(row.get("name", ""))))
            self.table.setItem(i, 2, QTableWidgetItem(dept_name))
            self.table.setItem(i, 3, QTableWidgetItem(comp_name))
            self.table.setItem(i, 4, QTableWidgetItem(str(row.get("rf_card_uid") or row.get("card_uid") or "")))
            
    def on_select(self):
        selected = self.table.selectedItems()
        if selected:
            row = selected[0].row()
            item = self.table.item(row, 0)
            if item is not None:
                original_idx = item.data(Qt.UserRole)
                if original_idx is not None and 0 <= original_idx < len(self.results):
                    self.selected_employee = self.results[original_idx]
                    self.accept()
                    return
            QMessageBox.warning(self, "경고", "선택한 행의 데이터를 찾을 수 없습니다.")
        else:
            QMessageBox.warning(self, "경고", "사원을 선택해 주세요.")

def setup_standard_table(table):
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(38)
    table.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
    table.setAlternatingRowColors(True)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    table.setShowGrid(True)
    table.setSortingEnabled(True)


def setup_settings_device_table(table, width: int = 420):
    """설정 탭 장치 테이블 — 시인성 있게 글자·행 높이 확대."""
    setup_standard_table(table)
    table.verticalHeader().setDefaultSectionSize(32)
    table.setFixedWidth(width)
    table.setMinimumHeight(96)
    table.setMaximumHeight(150)
    table.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
    table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

def _employee_fp_cell_text(row: dict) -> str:
    """사원 테이블 지문 열: 단말 fid 번호(저장된 fingerprint_slot_fid_*), 없으면 0~3."""
    if not isinstance(row, dict):
        return ""
    parts = []
    for i in range(4):
        if not bool(row.get("fingerprint_has_slot%d" % i)):
            continue
        sf = row.get("fingerprint_slot_fid_%d" % (i + 1))
        if sf is not None and sf != "":
            try:
                parts.append(str(int(sf)))
            except (TypeError, ValueError):
                parts.append(str(i))
        else:
            parts.append(str(i))
    if not parts:
        return "—"
    return "·".join(parts)


def _employee_status_is_active(raw) -> bool:
    """employees.status 가 재직인지 (DB에 소문자·공백이 섞여도 표시·버튼 활성화와 맞춤)."""
    return str(raw or "").strip().upper() == "ACTIVE"


def _meal_log_uid(row: dict) -> str:
    """대시보드·로그 행에서 표시용 UID 문자열 (해당 필드가 없으면 빈 문자열)."""
    if not isinstance(row, dict):
        return ""
    u = row.get("user") or {}
    r = (row.get("rf_card_uid") or u.get("rf_card_uid") or u.get("card_uid") or "").strip()
    emp_no = (u.get("emp_no") or row.get("emp_no") or "").strip()
    auth = str(row.get("auth_uid") or "").strip()
    # 단말 반영 시 auth_uid는 내부 중복 방지용 해시(zk:…)라 사용자에게는 카드 UID·사번을 보여준다
    if auth.startswith("zk:"):
        if r:
            return r
        if emp_no:
            return emp_no
        return auth
    v = auth or row.get("scan_uid") or row.get("card_uid")
    if v:
        return str(v)
    return str(r or "")


class StatCard(QFrame):
    def __init__(self, title, icon_text, color):
        super().__init__()
        self.setObjectName("StatCard")
        layout = QVBoxLayout(self)
        self.val_label = QLabel("0")
        self.val_label.setObjectName("StatValue")
        self.title_label = QLabel(title)
        self.title_label.setObjectName("StatLabel")
        layout.addWidget(self.val_label)
        layout.addWidget(self.title_label)
    def set_value(self, val):
        self.val_label.setText(str(val))

class DashboardScreen(QWidget):
    def __init__(self, main_win):
        super().__init__()
        self.main_win = main_win
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(20)
        header_h_layout = QHBoxLayout()
        header = QLabel("오늘의 현황")
        header.setObjectName("HeaderTitle")

        self.receive_btn = QPushButton("수신시작")
        self.receive_btn.setCheckable(True)
        self.receive_btn.setObjectName("DashboardReceiveBtn")
        self.receive_btn.setToolTip(
            "ADMS: 앱 시작 시 연결 서버 자동 기동, 단말 연결 ON 후 로그 수신 자동 시작. "
            "수신중지=로그만 중지(연결 서버 유지). 단말 http://PC_IP:8088/iclock/ "
            "pyzk live/poll: MEAL_F22_ATT_MODE, poll 간격 MEAL_F22_ATT_GAP_MS."
        )
        self.receive_btn.toggled.connect(main_win.on_dashboard_receive_toggled)

        # Digital Clock
        self.clock_label = QLabel()
        self.clock_label.setStyleSheet("color: #6366f1; font-size: 26px; font-weight: bold; font-family: 'Malgun Gothic'; margin-left: 20px; margin-top: 10px;")

        header_h_layout.addWidget(header)
        header_h_layout.addWidget(self.receive_btn)
        header_h_layout.addWidget(self.clock_label)
        header_h_layout.addStretch()
        layout.addLayout(header_h_layout)

        # Update clock every second
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_clock)
        self.timer.start(1000)
        self.update_clock()

        # Stats layout (Dynamic cards)
        self.stats_layout = QHBoxLayout()
        layout.addLayout(self.stats_layout)

        self.cards = {}  # Store cards by title

        info_frame = QFrame()
        info_frame.setObjectName("StatCard")
        info_layout = QVBoxLayout(info_frame)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(8)

        meal_title = QLabel("오늘 식사 기록")
        meal_title.setStyleSheet(
            "font-size: 16px; font-weight: bold; color: #94a3b8; padding: 4px 8px 0 8px; font-family: 'Malgun Gothic';"
        )
        info_layout.addWidget(meal_title)

        self.recent_table = QTableWidget(0, 5)
        self.recent_table.setHorizontalHeaderLabels(["시간", "이름", "부서", "식사종류", "UID"])
        setup_standard_table(self.recent_table)
        self.recent_table.setMinimumHeight(200)
        self.recent_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.recent_table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        info_layout.addWidget(self.recent_table, 2)

        fail_title = QLabel("RF 카드 인증 실패")
        fail_title.setStyleSheet(
            "font-size: 16px; font-weight: bold; color: #f87171; padding: 8px 8px 0 8px; font-family: 'Malgun Gothic';"
        )
        info_layout.addWidget(fail_title)

        self.fail_recent_table = QTableWidget(0, 4)
        self.fail_recent_table.setHorizontalHeaderLabels(["시간", "카드번호", "사유", "단말"])
        setup_standard_table(self.fail_recent_table)
        self.fail_recent_table.setMinimumHeight(120)
        self.fail_recent_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        info_layout.addWidget(self.fail_recent_table, 1)

        layout.addWidget(info_frame, 1)

    def update_clock(self):
        # OS에 설정된 로컬 타임존(한국 PC면 KST와 동일)
        now = datetime.now().astimezone()
        days = ["월", "화", "수", "목", "금", "토", "일"]
        day_str = days[now.weekday()]
        time_str = now.strftime(f"%m월 %d일({day_str}) %H:%M:%S")
        self.clock_label.setText(time_str)

    def update_stats(self, stats):
        if not stats or not isinstance(stats, dict): return
        
        # Define card data to build
        # Order: Total -> Meal Summaries (Policies) -> Exception
        card_data = []
        card_data.append({"title": "오늘 총 식수", "val": stats.get("total_count", 0), "color": "#6366f1", "icon": "👥"})
        
        # Add meal summaries (Policies)
        colors = ["#8b5cf6", "#10b981", "#f59e0b", "#06b6d4"] # Shared palette
        for i, s in enumerate(stats.get("meal_summaries", [])):
            color = colors[i % len(colors)]
            card_data.append({"title": s["meal_type"], "val": s["count"], "color": color, "icon": "🍱"})
            
        # policy_id 없음(번외 건수) — 식사종류 '번외' 카드와 중복되지 않게 요약에서는 제외, 여기만 표시
        card_data.append({"title": "기타", "val": stats.get("exception_count", 0), "color": "#ef4444", "icon": "⚠️"})
        
        # Refresh cards: same count + same title order → update values only; else rebuild layout
        want_titles = [x["title"] for x in card_data]
        have_titles = list(self.cards.keys())
        if len(self.cards) == len(card_data) and have_titles == want_titles:
            for item in card_data:
                self.cards[item["title"]].set_value(item["val"])
        else:
            self._rebuild_cards(card_data)

    def _rebuild_cards(self, card_data):
        # Clear existing
        while self.stats_layout.count():
            child = self.stats_layout.takeAt(0)
            if child.widget(): child.widget().deleteLater()
        self.cards = {}
        
        # Create new
        for item in card_data:
            card = StatCard(item["title"], item["icon"], item["color"])
            card.set_value(item["val"])
            self.stats_layout.addWidget(card)
            self.cards[item["title"]] = card

    def update_recent(self, data):
        if not isinstance(data, list): return
        self.recent_table.setSortingEnabled(False)
        self.recent_table.setUpdatesEnabled(False)
        self.recent_table.setRowCount(len(data))
        local_tz = datetime.now().astimezone().tzinfo
        for i, row in enumerate(data):
            if not isinstance(row, dict): continue
            user = row.get("user") or {}
            policy = row.get("policy") or {}
            ts = row.get("created_at", "")
            # 로컬 DB naive DATETIME = 이 PC 달력·시각. UTC(Z) 등이 있으면 OS 로컬로 변환해 표시. (대시보드: 시·분·초)
            display_ts = ""
            try:
                if isinstance(ts, str):
                    s = ts.replace("Z", "+00:00").strip().replace(" ", "T")
                    dt = datetime.fromisoformat(s)
                    if dt.tzinfo is None:
                        display_ts = dt.strftime("%H:%M:%S")
                    else:
                        display_ts = dt.astimezone(local_tz).strftime("%H:%M:%S")
                elif hasattr(ts, "astimezone"):
                    if ts.tzinfo is None:
                        display_ts = ts.strftime("%H:%M:%S")
                    else:
                        display_ts = ts.astimezone(local_tz).strftime("%H:%M:%S")
                else:
                    display_ts = str(ts)[:8] if ts else ""
            except Exception:
                try:
                    if isinstance(ts, str) and "T" in ts:
                        display_ts = ts.split("T")[-1].split(".")[0][:8]
                    elif isinstance(ts, str) and " " in ts:
                        display_ts = ts.split(" ")[-1].split(".")[0][:8]
                    else:
                        display_ts = str(ts)[:8] if ts else ""
                except Exception:
                    display_ts = str(ts)[:8] if ts else ""
            self.recent_table.setItem(i, 0, QTableWidgetItem(display_ts[:8] if len(display_ts) >= 8 else display_ts))
            self.recent_table.setItem(i, 1, QTableWidgetItem(str(user.get("name", ""))))
            self.recent_table.setItem(i, 2, QTableWidgetItem(str(user.get("department_name", ""))))
            self.recent_table.setItem(i, 3, QTableWidgetItem(str(policy.get("meal_type", ""))))
            self.recent_table.setItem(i, 4, QTableWidgetItem(_meal_log_uid(row)))
        self.recent_table.setUpdatesEnabled(True)
        self.recent_table.setSortingEnabled(True)

    def update_recent_failures(self, data):
        if not isinstance(data, list):
            return
        self.fail_recent_table.setSortingEnabled(False)
        self.fail_recent_table.setUpdatesEnabled(False)
        self.fail_recent_table.setRowCount(len(data))
        for i, row in enumerate(data):
            if not isinstance(row, dict):
                continue
            created_at = str(row.get("created_at") or "")
            time_part = created_at.split("T")[-1][:8] if "T" in created_at else created_at.split(" ")[-1][:8]
            self.fail_recent_table.setItem(i, 0, QTableWidgetItem(time_part))
            card = str(row.get("card_no") or "").strip() or "-"
            self.fail_recent_table.setItem(i, 1, QTableWidgetItem(card))
            self.fail_recent_table.setItem(i, 2, QTableWidgetItem(str(row.get("event_label") or "")))
            term = str(row.get("terminal_label") or row.get("terminal_device") or "")
            self.fail_recent_table.setItem(i, 3, QTableWidgetItem(term))
        self.fail_recent_table.setUpdatesEnabled(True)
        self.fail_recent_table.setSortingEnabled(True)


class CompanyScreen(QWidget):
    def __init__(self, api, main_win):
        super().__init__()
        self.api = api
        self.main_win = main_win
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        
        header_layout = QHBoxLayout()
        header = QLabel("회사 관리")
        header.setObjectName("HeaderTitle")
        
        reg_frame = QFrame()
        reg_frame.setObjectName("StatCard")
        reg_layout = QHBoxLayout(reg_frame)
        self.code_input = QLineEdit()
        self.code_input.setPlaceholderText("등록 시 자동 부여 (순번)")
        self.code_input.setReadOnly(True)
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("회사명")
        self.add_btn = QPushButton("등록")
        self.add_btn.setObjectName("PrimaryBtn")
        self.add_btn.setFixedWidth(100)
        self.add_btn.clicked.connect(self.on_add)
        
        self.edit_btn = QPushButton("수정")
        self.edit_btn.setObjectName("SecondaryBtn")
        self.edit_btn.setFixedWidth(100)
        self.edit_btn.clicked.connect(self.on_edit)
        
        self.del_btn = QPushButton("삭제")
        self.del_btn.setObjectName("DangerBtn")
        self.del_btn.setFixedWidth(100)
        self.del_btn.clicked.connect(self.on_delete)
        
        reg_layout.addWidget(self.code_input)
        reg_layout.addWidget(self.name_input)
        reg_layout.addWidget(self.add_btn)
        reg_layout.addWidget(self.edit_btn)
        reg_layout.addWidget(self.del_btn)
        
        header_layout.addWidget(header)
        header_layout.addStretch()
        header_layout.addWidget(reg_frame)
        layout.addLayout(header_layout)
        
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["회사코드", "회사명"])
        setup_standard_table(self.table)
        self.table.itemSelectionChanged.connect(self.on_selection_changed)
        layout.addWidget(self.table)

    def on_selection_changed(self):
        selected = self.table.selectedItems()
        if selected:
            row = selected[0].row()
            item0 = self.table.item(row, 0)
            item1 = self.table.item(row, 1)
            self.code_input.setText(item0.text() if item0 else "")
            self.name_input.setText(item1.text() if item1 else "")
            cid = item0.data(Qt.UserRole) if item0 else None
            self.edit_btn.setEnabled(cid is not None)
            self.del_btn.setEnabled(cid is not None)
            self.code_input.setReadOnly(False)
            self.name_input.setFocus()
            self.name_input.selectAll()
        else:
            self.code_input.clear()
            self.name_input.clear()
            self.code_input.setReadOnly(True)
            self.edit_btn.setEnabled(False)
            self.del_btn.setEnabled(False)
        
    def load_data(self):
        QApplication.setOverrideCursor(Qt.WaitCursor)
        self.loader = DataLoader(self.api.get_companies)
        self.loader.finished.connect(self.display_data)
        self.loader.error.connect(self._on_list_load_error)
        self.loader.start()

    def _on_list_load_error(self, err_msg: str):
        QApplication.restoreOverrideCursor()
        QMessageBox.warning(self, "회사 목록", "불러오기 실패:\n%s" % err_msg)

    def display_data(self, data):
        QApplication.restoreOverrideCursor()
        if not isinstance(data, list): return
        self.table.setSortingEnabled(False)
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(data))
        for i, row in enumerate(data):
            if not isinstance(row, dict): continue
            code = row.get("code", "")
            name = row.get("name", "")
            item_code = QTableWidgetItem(str(code))
            item_code.setData(Qt.UserRole, row.get("code"))
            self.table.setItem(i, 0, item_code)
            self.table.setItem(i, 1, QTableWidgetItem(str(name)))
        self.table.setUpdatesEnabled(True)
        self.table.setSortingEnabled(True)

    def clear_inputs(self):
        self.code_input.clear()
        self.name_input.clear()
        self.code_input.setReadOnly(True)
        self.table.clearSelection()
        self.edit_btn.setEnabled(False)
        self.del_btn.setEnabled(False)

    def on_edit(self):
        selected = self.table.selectedItems()
        if not selected:
            QMessageBox.warning(self, "경고", "수정할 회사를 선택하세요.")
            return

        row = selected[0].row()
        item0 = self.table.item(row, 0)
        cid = item0.data(Qt.UserRole) if item0 else None
        if cid is None:
            QMessageBox.warning(self, "경고", "선택한 항목을 수정할 수 없습니다.")
            return
        code = self.code_input.text()
        name = self.name_input.text()
        
        self.edit_btn.setEnabled(False)
        self.main_win.statusBar().showMessage("수정 중...", 2000)
        self.edit_loader = DataLoader(self.api.update_company, cid, code, name)
        self.edit_loader.finished.connect(self.on_edit_finished)
        self.edit_loader.error.connect(self.on_edit_error)
        self.edit_loader.start()

    def on_edit_error(self, err_msg):
        self.edit_btn.setEnabled(True)
        QMessageBox.warning(self, "오류", f"수정 중 오류가 발생했습니다:\n{err_msg}")

    def on_edit_finished(self, data):
        self.edit_btn.setEnabled(True)
        if data:
            self.main_win.statusBar().showMessage("수정되었습니다.", 3000)
            selected = self.table.selectedItems()
            if selected:
                row = selected[0].row()
                item0 = self.table.item(row, 0)
                if item0 is not None:
                    self.table.setItem(row, 0, QTableWidgetItem(data.get("code", "")))
                    self.table.item(row, 0).setData(Qt.UserRole, data.get("code"))
                self.table.setItem(row, 1, QTableWidgetItem(str(data.get("name", ""))))
            self.main_win.on_company_changed()
            QMessageBox.information(self, "성공", "수정되었습니다.")
            self.clear_inputs()
        else:
            QMessageBox.warning(self, "오류", "수정에 실패했습니다.")

    def on_delete(self):
        selected = self.table.selectedItems()
        if not selected:
            QMessageBox.warning(self, "경고", "삭제할 회사를 선택하세요.")
            return

        row = selected[0].row()
        item0 = self.table.item(row, 0)
        item1 = self.table.item(row, 1)
        cid = item0.data(Qt.UserRole) if item0 else None
        if cid is None:
            QMessageBox.warning(self, "경고", "선택한 항목을 삭제할 수 없습니다.")
            return
        name = item1.text() if item1 else "(선택 행)"
        
        reply = QMessageBox.question(self, "삭제 확인", f"'{name}' 회사를 정말 삭제하시겠습니까?\n관련 부서 및 사원 데이터에 영향을 줄 수 있습니다.",
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply == QMessageBox.Yes:
            self.del_btn.setEnabled(False)
            self.main_win.statusBar().showMessage("삭제 중...", 2000)
            self.del_loader = DataLoader(self.api.delete_company, cid)
            self.del_loader.finished.connect(self.on_delete_finished)
            self.del_loader.error.connect(self.on_del_error)
            self.del_loader.start()

    def on_del_error(self, err_msg):
        self.del_btn.setEnabled(True)
        QMessageBox.warning(self, "오류", f"삭제 중 오류가 발생했습니다:\n{err_msg}")

    def on_delete_finished(self, success):
        self.del_btn.setEnabled(True)
        if success:
            self.main_win.statusBar().showMessage("삭제되었습니다.", 3000)
            selected = self.table.selectedItems()
            if selected:
                row = selected[0].row()
                self.table.removeRow(row) # UI에서 즉시 제거
                self.main_win.on_company_changed()
            QMessageBox.information(self, "성공", "삭제되었습니다.")
            self.clear_inputs()
            self.load_data()  # DB와 동기화
        else:
            QMessageBox.warning(self, "오류", "삭제에 실패했습니다.")
            
    def on_add(self):
        name = self.name_input.text().strip()
        if not name:
            QMessageBox.warning(self, "경고", "회사명을 입력하세요.")
            return
        
        # Optimistic UI update: Insert row immediately
        pos = self.table.rowCount()
        self.table.insertRow(pos)
        item_code = QTableWidgetItem("자동…")
        item_code.setData(Qt.UserRole, None) # Pending ID
        self.table.setItem(pos, 0, item_code)
        self.table.setItem(pos, 1, QTableWidgetItem(name))
        
        self.main_win.statusBar().showMessage(f"'{name}' 등록 중...", 2000)
        self.add_btn.setEnabled(False)
        self.add_loader = DataLoader(self.api.create_company, "", name)
        self.add_loader.finished.connect(self.on_add_finished)
        self.add_loader.error.connect(self.on_add_error)
        self.add_loader.start()

    def on_add_error(self, e):
        self.add_btn.setEnabled(True)
        self.load_data() # Rollback optimistic change by reloading
        QMessageBox.critical(self, "치명적 오류", f"스레드 오류: {e}")

    def on_add_finished(self, result):
        self.add_btn.setEnabled(True)
        success, data = result if isinstance(result, tuple) else (False, str(result))
        if success and isinstance(data, dict):
            self.main_win.statusBar().showMessage("회사가 등록되었습니다.", 3000)
            for i in range(self.table.rowCount()):
                item0 = self.table.item(i, 0)
                if item0 is not None and item0.data(Qt.UserRole) is None:
                    item0.setText(str(data.get("code") or ""))
                    item0.setData(Qt.UserRole, data.get("code"))
                    break
            self.main_win.on_company_changed()
            QMessageBox.information(self, "성공", "등록되었습니다.")
            self.clear_inputs()
        else:
            self.load_data()
            msg = data if isinstance(data, str) else "등록 실패"
            QMessageBox.warning(self, "오류", f"등록에 실패했습니다: {msg}")

class DepartmentScreen(QWidget):
    def __init__(self, api, main_win):
        super().__init__()
        self.api = api
        self.main_win = main_win
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        
        header_layout = QHBoxLayout()
        header = QLabel("부서 관리")
        header.setObjectName("HeaderTitle")
        
        reg_frame = QFrame()
        reg_frame.setObjectName("StatCard")
        reg_layout = QHBoxLayout(reg_frame)
        self.code_input = QLineEdit()
        self.code_input.setPlaceholderText("등록 시 자동 부여 (순번)")
        self.code_input.setReadOnly(True)
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("부서명")
        self.add_btn = QPushButton("등록")
        self.add_btn.setObjectName("PrimaryBtn")
        self.add_btn.setFixedWidth(100)
        self.add_btn.clicked.connect(self.on_add)
        
        self.edit_btn = QPushButton("수정")
        self.edit_btn.setObjectName("SecondaryBtn")
        self.edit_btn.setFixedWidth(100)
        self.edit_btn.clicked.connect(self.on_edit)
        
        self.del_btn = QPushButton("삭제")
        self.del_btn.setObjectName("DangerBtn")
        self.del_btn.setFixedWidth(100)
        self.del_btn.clicked.connect(self.on_delete)
        
        reg_layout.addWidget(self.code_input)
        reg_layout.addWidget(self.name_input)
        reg_layout.addWidget(self.add_btn)
        reg_layout.addWidget(self.edit_btn)
        reg_layout.addWidget(self.del_btn)
        
        header_layout.addWidget(header)
        header_layout.addStretch()
        header_layout.addWidget(reg_frame)
        layout.addLayout(header_layout)
        
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["부서코드", "부서명"])
        setup_standard_table(self.table)
        self.table.itemSelectionChanged.connect(self.on_selection_changed)
        layout.addWidget(self.table)

    def on_selection_changed(self):
        selected = self.table.selectedItems()
        if selected:
            row = selected[0].row()
            item0 = self.table.item(row, 0)
            item1 = self.table.item(row, 1)
            dept_id = item0.data(Qt.UserRole) if item0 else None
            self.code_input.setText(item0.text() if item0 else "")
            self.name_input.setText(item1.text() if item1 else "")
            self.code_input.setReadOnly(False)
            self.edit_btn.setEnabled(dept_id is not None)
            self.del_btn.setEnabled(dept_id is not None)
            self.name_input.setFocus()
            self.name_input.selectAll()
        else:
            self.code_input.clear()
            self.name_input.clear()
            self.code_input.setReadOnly(True)
            self.edit_btn.setEnabled(False)
            self.del_btn.setEnabled(False)
    def clear_inputs(self):
        self.code_input.clear()
        self.name_input.clear()
        self.code_input.setReadOnly(True)
        self.table.clearSelection()
        self.edit_btn.setEnabled(False)
        self.del_btn.setEnabled(False)

    def update_company_combo(self, companies):
        pass  # 부서 관리: 회사 1개만 사용, 콤보 없음

    def load_data(self):
        dept_list = [d for d in self.main_win.departments_data if isinstance(d, dict)]
        dept_list.sort(key=lambda x: (str(x.get("company_code") or ""), str(x.get("code") or "")))
        self.display_data(dept_list)

    def display_data(self, data):
        if not isinstance(data, list):
            return
        if getattr(self, "_filling_table", False):
            return
        self._filling_table = True
        self.table.setSortingEnabled(False)
        self.table.setUpdatesEnabled(False)
        self.table.blockSignals(True)
        self.table.setRowCount(len(data))

        for i, row in enumerate(data):
            if not isinstance(row, dict):
                continue
            item_code = QTableWidgetItem(str(row.get("code") or ""))
            item_code.setData(Qt.UserRole, row.get("code"))
            self.table.setItem(i, 0, item_code)
            self.table.setItem(i, 1, QTableWidgetItem(str(row.get("name") or "")))
        self.table.blockSignals(False)
        self.table.setUpdatesEnabled(True)
        self.table.setSortingEnabled(True)
        self._filling_table = False

    def on_edit(self):
        selected = self.table.selectedItems()
        if not selected:
            QMessageBox.warning(self, "경고", "수정할 부서를 선택하세요.")
            return
        
        row = selected[0].row()
        did = self.table.item(row, 0).data(Qt.UserRole)
        if did is None:
            QMessageBox.warning(self, "경고", "선택한 항목을 수정할 수 없습니다.")
            return
        code = self.code_input.text()
        name = self.name_input.text()
        
        self.edit_btn.setEnabled(False)
        self.main_win.statusBar().showMessage("부서 수정 중...", 2000)
        self.edit_loader = DataLoader(self.api.update_department, did, code, name)
        self.edit_loader.finished.connect(self.on_edit_finished)
        self.edit_loader.error.connect(self.on_edit_error)
        self.edit_loader.start()

    def on_edit_error(self, err_msg):
        self.edit_btn.setEnabled(True)
        QMessageBox.warning(self, "오류", f"수정 중 오류가 발생했습니다:\n{err_msg}")

    def on_edit_finished(self, data):
        self.edit_btn.setEnabled(True)
        if data:
            self.main_win.statusBar().showMessage("수정되었습니다.", 3000)
            # Update local cache in main window
            old_code = None
            selected = self.table.selectedItems()
            if selected:
                item0 = self.table.item(selected[0].row(), 0)
                if item0 is not None:
                    old_code = item0.data(Qt.UserRole)
            for i, d in enumerate(self.main_win.departments_data):
                if isinstance(d, dict) and d.get("code") == old_code:
                    self.main_win.departments_data[i] = data
                    break
            
            QMessageBox.information(self, "성공", "수정되었습니다.")
            self.clear_inputs()
            self.load_data()
            self.main_win.employees.update_dept_combo()
            self.main_win.reports._refresh_dept_sub_combo()
        else:
            QMessageBox.warning(self, "오류", "수정에 실패했습니다.")

    def on_delete(self):
        selected = self.table.selectedItems()
        if not selected:
            QMessageBox.warning(self, "경고", "삭제할 부서를 선택하세요.")
            return
            
        row = selected[0].row()
        did = self.table.item(row, 0).data(Qt.UserRole)
        if did is None:
            QMessageBox.warning(self, "경고", "선택한 항목을 삭제할 수 없습니다.")
            return
        name_item = self.table.item(row, 1)
        name = name_item.text() if name_item else "(선택 행)"
        
        reply = QMessageBox.question(self, "삭제 확인", f"'{name}' 부서를 정말 삭제하시겠습니까?",
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply == QMessageBox.Yes:
            self.del_btn.setEnabled(False)
            self.main_win.statusBar().showMessage("부서 삭제 중...", 2000)
            self.del_loader = DataLoader(self.api.delete_department, did)
            self.del_loader.finished.connect(self.on_delete_finished)
            self.del_loader.error.connect(self.on_del_error)
            self.del_loader.start()

    def on_del_error(self, err_msg):
        self.del_btn.setEnabled(True)
        QMessageBox.warning(self, "오류", f"삭제 중 오류가 발생했습니다:\n{err_msg}")

    def on_delete_finished(self, success):
        self.del_btn.setEnabled(True)
        if success:
            self.main_win.statusBar().showMessage("삭제되었습니다.", 3000)
            selected = self.table.selectedItems()
            if selected:
                row = selected[0].row()
                target_did = self.table.item(row, 0).data(Qt.UserRole)
                self.main_win.departments_data = [d for d in self.main_win.departments_data if d.get("code") != target_did]
                self.table.removeRow(row)
            self.main_win.employees.update_dept_combo()
            self.main_win.reports._refresh_dept_sub_combo()
            self.main_win.sync_departments()
            QMessageBox.information(self, "성공", "삭제되었습니다.")
            self.clear_inputs()
            self.load_data()
        else:
            QMessageBox.warning(self, "오류", "삭제에 실패했습니다.")

    def on_add(self):
        companies = getattr(self.main_win, "companies_data", []) or []
        cid = companies[0].get("code") if companies and isinstance(companies[0], dict) else None
        name = self.name_input.text().strip()
        if not cid or not name:
            QMessageBox.warning(self, "경고", "부서명을 입력하세요.")
            return

        # Optimistic UI update
        pos = self.table.rowCount()
        self.table.insertRow(pos)
        item_code = QTableWidgetItem("자동…")
        item_code.setData(Qt.UserRole, None)
        self.table.setItem(pos, 0, item_code)
        self.table.setItem(pos, 1, QTableWidgetItem(name))

        self.add_btn.setEnabled(False)
        self.add_loader = DataLoader(self.api.create_department, cid, "", name)
        self.add_loader.finished.connect(self.on_add_finished)
        self.add_loader.error.connect(self.on_add_error)
        self.add_loader.start()

    def on_add_error(self, e):
        self.add_btn.setEnabled(True)
        self.load_data()
        QMessageBox.critical(self, "치명적 오류", f"스레드 오류: {e}")

    def on_add_finished(self, result):
        self.add_btn.setEnabled(True)
        success, data = result if isinstance(result, tuple) else (False, str(result))
        if success and isinstance(data, dict):
            self.main_win.statusBar().showMessage("부서가 등록되었습니다.", 3000)
            self.main_win.departments_data.append(data)
            self.load_data()
            self.main_win.employees.update_dept_combo()
            self.main_win.reports._refresh_dept_sub_combo()
            QMessageBox.information(self, "성공", "등록되었습니다.")
            self.clear_inputs()
        else:
            self.main_win.sync_departments()
            msg = data if isinstance(data, str) else "등록 실패"
            QMessageBox.warning(self, "오류", f"등록에 실패했습니다: {msg}")

class EmployeeScreen(QWidget):
    def __init__(self, api, main_win):
        super().__init__()
        self.api = api
        self.main_win = main_win
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(20)

        header_layout = QHBoxLayout()
        header = QLabel("사원 관리")
        header.setObjectName("HeaderTitle")
        
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("사번 또는 이름 검색...")
        self.search_input.setFixedWidth(250)
        
        self.refresh_btn = QPushButton("새로고침")
        self.refresh_btn.setFixedWidth(80)
        self.refresh_btn.clicked.connect(self.load_data)
        
        header_layout.addWidget(header)
        header_layout.addStretch()
        header_layout.addWidget(self.refresh_btn)
        header_layout.addWidget(self.search_input)
        layout.addLayout(header_layout)

        self.dept_combo = QComboBox()
        self.dept_combo.setItemDelegate(QStyledItemDelegate())
        self.dept_combo.setPlaceholderText("부서")
        self.dept_combo.setMinimumHeight(40)

        self.emp_no_input = QLineEdit()
        self.emp_no_input.setPlaceholderText("사번")
        self.emp_no_input.setMinimumHeight(40)

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("이름")
        self.name_input.setMinimumHeight(40)

        self.rf_card_uid_input = QLineEdit()
        self.rf_card_uid_input.setPlaceholderText("RF 카드 UID")
        self.rf_card_uid_input.setMinimumHeight(40)
        self.rf_card_uid_input.setToolTip("F22에서 읽거나 수동 입력. 로컬 DB 연동 시 저장됩니다.")

        self.add_btn = QPushButton("등록")
        self.add_btn.setObjectName("PrimaryBtn")
        self.add_btn.setMinimumHeight(40)
        self.add_btn.clicked.connect(self.on_add)

        self.edit_btn = CaptionPushButton("수정", "(단말기정보전송)")
        self.edit_btn.setObjectName("SecondaryBtn")
        self.edit_btn.setMinimumHeight(64)
        self.edit_btn.setEnabled(False)
        self.edit_btn.setToolTip(
            "카드나 지문이 있으면 저장 후 설정에 등록된 단말 전체에 다시 전송합니다. "
            "네트워크 오류로 DB에만 저장됐을 때 이 버튼으로 단말에 보냅니다."
        )
        self.edit_btn.clicked.connect(self.on_edit)

        self.del_btn = QPushButton("삭제(퇴사)")
        self.del_btn.setObjectName("DangerBtn")
        self.del_btn.setMinimumHeight(40)
        self.del_btn.setEnabled(False)
        self.del_btn.clicked.connect(self.on_delete)

        self.cancel_resign_btn = QPushButton("퇴사취소")
        self.cancel_resign_btn.setObjectName("SecondaryBtn")
        self.cancel_resign_btn.setMinimumHeight(40)
        self.cancel_resign_btn.setEnabled(False)
        self.cancel_resign_btn.setToolTip("선택한 퇴사자를 재직으로 복구합니다.")
        self.cancel_resign_btn.clicked.connect(self.on_cancel_resign)

        self.fp_register_edit_btn = QPushButton("DB_지문등록 (수정)")
        self.fp_register_edit_btn.setObjectName("SecondaryBtn")
        self.fp_register_edit_btn.setMinimumHeight(40)
        self.fp_register_edit_btn.setEnabled(False)
        self.fp_register_edit_btn.setToolTip(
            "ZKTeco Live20R USB 지문 등록기: 손가락 2개 × 2회 캡처 → DB 저장 → F22 단말 동기화."
            + (" (ADMS: getrequest user/templatev10)" if _f22_att_mode_is_adms() else "")
        )
        self.fp_register_edit_btn.clicked.connect(self.on_register_fingerprint)

        self.permanent_del_btn = QPushButton("완전 삭제")
        self.permanent_del_btn.setObjectName("DangerBtn")
        self.permanent_del_btn.setMinimumHeight(40)
        self.permanent_del_btn.setEnabled(False)
        self.permanent_del_btn.setToolTip("DB에서 제거하여 같은 사번으로 재등록 가능")
        self.permanent_del_btn.clicked.connect(self.on_permanent_delete)

        self.import_btn = QPushButton("엑셀 일괄등록")
        self.import_btn.setObjectName("SecondaryBtn")
        self.import_btn.setMinimumHeight(40)
        self.import_btn.clicked.connect(self.on_import_excel)

        self.export_btn = QPushButton("엑셀 내보내기")
        self.export_btn.setObjectName("SecondaryBtn")
        self.export_btn.setMinimumHeight(40)
        self.export_btn.setToolTip("화면에 보이는 사원 목록을 엑셀 파일로 저장합니다.")
        self.export_btn.clicked.connect(self.on_export_excel)

        # 좌측: 좁은 테이블 / 우측: 입력·버튼 전용 프레임
        main_row = QHBoxLayout()
        main_row.setSpacing(20)

        table_wrap = QWidget()
        table_wrap.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        table_v = QVBoxLayout(table_wrap)
        table_v.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, 7)
        self.table.setObjectName("EmployeeTable")
        self.table.setHorizontalHeaderLabels(["No", "사번", "이름", "부서", "RF카드 UID", "지문", "상태"])
        setup_standard_table(self.table)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Fixed)
        hdr.setSectionResizeMode(1, QHeaderView.Fixed)
        hdr.setSectionResizeMode(2, QHeaderView.Fixed)
        hdr.setSectionResizeMode(3, QHeaderView.Fixed)
        hdr.setSectionResizeMode(4, QHeaderView.Fixed)
        hdr.setSectionResizeMode(5, QHeaderView.Fixed)
        hdr.setSectionResizeMode(6, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 72)
        self.table.setColumnWidth(1, 150)
        self.table.setColumnWidth(2, 120)
        self.table.setColumnWidth(3, 160)
        self.table.setColumnWidth(4, 160)
        self.table.setColumnWidth(5, 110)
        self.table.setColumnWidth(6, 72)
        self._applying_col_widths = False
        self.table.installEventFilter(self)
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.table.itemSelectionChanged.connect(self.on_selection_changed)
        table_v.addWidget(self.table)
        main_row.addWidget(table_wrap, 1)

        self._selected_emp_id = None
        self._emp_mutate_loader = None

        reg_frame = QFrame()
        reg_frame.setObjectName("StatCard")
        reg_frame.setMinimumWidth(340)
        reg_frame.setMaximumWidth(400)
        reg_frame.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        reg_outer = QVBoxLayout(reg_frame)
        reg_outer.setContentsMargins(18, 18, 18, 18)
        reg_outer.setSpacing(16)

        panel_title = QLabel("사원 등록 · 수정")
        panel_title.setStyleSheet("font-size: 20px; font-weight: bold; color: #6366f1; font-family: 'Malgun Gothic';")
        reg_outer.addWidget(panel_title)
        reg_outer.addSpacing(4)

        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(14)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        for lbl_text, w in [
            ("부서", self.dept_combo),
            ("사번", self.emp_no_input),
            ("이름", self.name_input),
            ("RF카드 UID", self.rf_card_uid_input),
        ]:
            lab = QLabel(lbl_text)
            lab.setObjectName("InputLabel")
            form.addRow(lab, w)
        reg_outer.addLayout(form)

        # 입력 영역과 버튼 사이를 늘리고, 프레임 세로를 테이블과 맞춤(남는 높이를 이 구간이 흡수)
        reg_outer.addStretch(1)

        row_ae = QHBoxLayout()
        row_ae.setSpacing(10)
        row_ae.addWidget(self.add_btn, 1)
        row_ae.addWidget(self.edit_btn, 1)
        reg_outer.addLayout(row_ae)

        reg_outer.addSpacing(10)
        row_fp = QHBoxLayout()
        row_fp.setSpacing(10)
        row_fp.addWidget(self.fp_register_edit_btn, 1)
        reg_outer.addLayout(row_fp)

        reg_outer.addSpacing(10)
        row_del = QHBoxLayout()
        row_del.setSpacing(10)
        row_del.addWidget(self.del_btn, 1)
        row_del.addWidget(self.cancel_resign_btn, 1)
        reg_outer.addLayout(row_del)

        reg_outer.addSpacing(10)
        row_rest = QHBoxLayout()
        row_rest.setSpacing(10)
        row_rest.addWidget(self.permanent_del_btn, 1)
        reg_outer.addLayout(row_rest)

        reg_outer.addSpacing(12)
        self.replace_employees_check = QCheckBox("기존 데이터 삭제후 등록 (미체크시 추가등록)")
        self.replace_employees_check.setStyleSheet(
            "QCheckBox { color: #e2e8f0; font-size: 13px; font-weight: bold; font-family: 'Malgun Gothic'; }"
            "QCheckBox::indicator { width: 18px; height: 18px; }"
        )
        self.replace_employees_check.setToolTip(
            "체크하면 이 회사 사원·지문·식사 기록을 지운 뒤 엑셀만 등록합니다. "
            "체크하지 않으면 없는 사번만 추가 등록합니다."
        )
        reg_outer.addWidget(self.replace_employees_check)
        row_excel = QHBoxLayout()
        row_excel.setSpacing(10)
        row_excel.addWidget(self.import_btn, 1)
        row_excel.addWidget(self.export_btn, 1)
        reg_outer.addLayout(row_excel)
        main_row.addWidget(reg_frame, 0)

        layout.addLayout(main_row, 1)

        # Connections — 글자마다 1000명을 다시 읽지 않도록 잠깐 모아서 조회
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self.load_data)
        self.search_input.textChanged.connect(self._on_employee_search_changed)

    def eventFilter(self, obj, event):
        if obj is getattr(self, "table", None) and event.type() == QEvent.Resize:
            self._apply_employee_column_widths(event.size().width())
        return super().eventFilter(obj, event)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._apply_employee_column_widths)

    def _employee_table_inner_width(self, widget_width=None) -> int:
        table = self.table
        if widget_width is None:
            widget_width = table.width()
        frame = table.frameWidth() * 2
        sb = table.verticalScrollBar()
        sb_w = sb.width() if sb is not None and sb.isVisible() else 0
        return max(0, int(widget_width) - frame - sb_w)

    def _apply_employee_column_widths(self, widget_width=None):
        """RF카드 UID는 남는 폭의 절반만 쓰고, 줄인 폭은 No·이름·부서에 균등 분배."""
        if getattr(self, "_applying_col_widths", False):
            return
        table = self.table
        vp = self._employee_table_inner_width(widget_width)
        if vp <= 0:
            return
        keep = 150 + 110 + 72
        base_no, base_name, base_dept = 72, 120, 160
        rf_now = vp - keep - (base_no + base_name + base_dept)
        if rf_now < 2:
            return
        freed = rf_now // 2
        rf_w = rf_now - freed
        share, rem = divmod(freed, 3)
        self._applying_col_widths = True
        try:
            hdr = table.horizontalHeader()
            hdr.setStretchLastSection(False)
            for col in range(7):
                hdr.setSectionResizeMode(col, QHeaderView.Fixed)
            table.setColumnWidth(0, base_no + share + (1 if rem > 0 else 0))
            table.setColumnWidth(1, 150)
            table.setColumnWidth(2, base_name + share + (1 if rem > 1 else 0))
            table.setColumnWidth(3, base_dept + share)
            table.setColumnWidth(4, rf_w)
            table.setColumnWidth(5, 110)
            table.setColumnWidth(6, 72)
        finally:
            self._applying_col_widths = False

    def _on_employee_search_changed(self, _text: str):
        self._search_timer.start()

    def _employee_id_for_row_actions(self):
        """표 선택이 버튼으로 포커스가 옮겨가며 selectedItems()가 비는 경우에도 id 복구."""
        eid = getattr(self, "_selected_emp_id", None)
        if eid is not None:
            return eid
        sm = self.table.selectionModel()
        if sm is not None:
            for ix in sm.selectedRows():
                it = self.table.item(ix.row(), 1)
                if it is not None and it.data(Qt.UserRole) is not None:
                    return it.data(Qt.UserRole)
        r = self.table.currentRow()
        if r >= 0:
            it = self.table.item(r, 1)
            if it is not None and it.data(Qt.UserRole) is not None:
                return it.data(Qt.UserRole)
        for it in self.table.selectedItems():
            if it.column() == 1 and it.data(Qt.UserRole) is not None:
                return it.data(Qt.UserRole)
        return None

    def _employee_row_index_for_id(self, eid) -> int:
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 1)
            if it is not None and it.data(Qt.UserRole) == eid:
                return r
        return -1

    def update_company_combo(self, companies):
        """회사 1개만 사용: 부서 콤보만 갱신."""
        self.update_dept_combo()

    def update_dept_combo(self):
        """회사 1개 전제: 콤보에는 부서명만 표시."""
        self.dept_combo.clear()
        self.dept_combo.addItem("** 선택 **", None)
        depts = [d for d in self.main_win.departments_data if isinstance(d, dict)]
        depts.sort(key=lambda x: (str(x.get("company_code") or ""), str(x.get("code") or "")))
        for d in depts:
            label = str(d.get("name") or "").strip() or "(이름 없음)"
            self.dept_combo.addItem(label, d.get("code"))

    def on_selection_changed(self):
        self._selected_emp_id = None
        self.edit_btn.setEnabled(False)
        self.del_btn.setEnabled(False)
        self.cancel_resign_btn.setEnabled(False)
        self.permanent_del_btn.setEnabled(False)
        self.fp_register_edit_btn.setEnabled(False)
        selected = self.table.selectedItems()
        if selected:
            row = selected[0].row()
            item_emp = self.table.item(row, 1)
            item_name = self.table.item(row, 2)
            item_dept = self.table.item(row, 3)
            item_rf = self.table.item(row, 4)
            item_status = self.table.item(row, 6)
            emp_no = item_emp.text() if item_emp else ""
            name = item_name.text() if item_name else ""
            status_text = (item_status.text() if item_status else "").strip()

            self.emp_no_input.setText(emp_no)
            self.name_input.setText(name)
            self.rf_card_uid_input.setText(item_rf.text() if item_rf else "")

            did = item_dept.data(Qt.UserRole) if item_dept else None
            if did is None:
                self.dept_combo.setCurrentIndex(0)
            else:
                d_idx = self.dept_combo.findData(did)
                if d_idx >= 0:
                    self.dept_combo.setCurrentIndex(d_idx)
                else:
                    self.dept_combo.setCurrentIndex(0)

            if item_emp and item_emp.data(Qt.UserRole) is not None:
                self._selected_emp_id = item_emp.data(Qt.UserRole)
                self.edit_btn.setEnabled(True)
                self.permanent_del_btn.setEnabled(True)
                self.fp_register_edit_btn.setEnabled(True)
                if status_text == "재직":
                    self.del_btn.setEnabled(True)
                    self.cancel_resign_btn.setEnabled(False)
                else:
                    self.del_btn.setEnabled(False)
                    self.cancel_resign_btn.setEnabled(True)

    def clear_inputs(self):
        self._selected_emp_id = None
        self.emp_no_input.clear()
        self.name_input.clear()
        self.rf_card_uid_input.clear()
        self.table.clearSelection()
        self.edit_btn.setEnabled(False)
        self.del_btn.setEnabled(False)
        self.cancel_resign_btn.setEnabled(False)
        self.permanent_del_btn.setEnabled(False)
        self.fp_register_edit_btn.setEnabled(False)

    def on_add(self):
        companies = getattr(self.main_win, "companies_data", []) or []
        did = self.dept_combo.currentData()
        cid = None
        if did is not None:
            for d in self.main_win.departments_data or []:
                if isinstance(d, dict) and d.get("code") == did:
                    cid = d.get("company_code")
                    break
        if cid is None and companies and isinstance(companies[0], dict):
            cid = companies[0].get("code")
        emp_no = self.emp_no_input.text().strip()
        name = self.name_input.text().strip()

        if not cid or not emp_no or not name:
            QMessageBox.warning(self, "경고", "부서를 선택하고 사번·이름을 입력하세요.")
            return

        payload = {
            "company_code": cid,
            "department_code": did,
            "emp_no": emp_no,
            "name": name,
            "status": "ACTIVE",
        }
        rf_uid = self.rf_card_uid_input.text().strip()
        if rf_uid:
            payload["rf_card_uid"] = rf_uid

        self.main_win.statusBar().showMessage("사원 등록 중...", 5000)
        if self._emp_mutate_loader is not None and self._emp_mutate_loader.isRunning():
            QMessageBox.warning(self, "알림", "다른 작업이 진행 중입니다. 잠시 후 다시 시도하세요.")
            return
        self.add_btn.setEnabled(False)
        self._emp_mutate_loader = DataLoader(self.api.create_employee, payload)
        self._emp_mutate_loader.finished.connect(self.on_action_finished)
        self._emp_mutate_loader.error.connect(self.on_action_error)
        self._emp_mutate_loader.start()

    def on_edit(self):
        eid = self._employee_id_for_row_actions()
        if eid is None:
            QMessageBox.warning(self, "경고", "수정할 사원을 표에서 선택하세요.")
            return

        did = self.dept_combo.currentData()
        cid = None
        if did is not None:
            for d in self.main_win.departments_data or []:
                if isinstance(d, dict) and d.get("code") == did:
                    cid = d.get("company_code")
                    break
        companies = getattr(self.main_win, "companies_data", []) or []
        if cid is None and companies and isinstance(companies[0], dict):
            cid = companies[0].get("code")
        emp_no = self.emp_no_input.text().strip()
        name = self.name_input.text().strip()

        payload = {
            "company_code": cid,
            "department_code": did,
            "emp_no": emp_no,
            "name": name,
            "rf_card_uid": self.rf_card_uid_input.text().strip() or None,
        }

        if self._emp_mutate_loader is not None and self._emp_mutate_loader.isRunning():
            QMessageBox.warning(self, "알림", "다른 작업이 진행 중입니다. 잠시 후 다시 시도하세요.")
            return
        self._emp_mutate_loader = DataLoader(self.api.update_employee, eid, payload)
        self._emp_mutate_loader.finished.connect(self.on_action_finished)
        self._emp_mutate_loader.error.connect(self.on_action_error)
        self._emp_mutate_loader.start()

    def on_export_excel(self):
        """화면에 보이는 사원 테이블을 엑셀로 저장한 뒤 연다."""
        import os
        import subprocess
        import sys

        try:
            import openpyxl
            from openpyxl.styles import Font, Alignment
        except ImportError:
            QMessageBox.warning(self, "오류", "엑셀 저장을 위해 openpyxl이 필요합니다.\npip install openpyxl")
            return
        if self.table.rowCount() <= 0:
            QMessageBox.information(self, "엑셀 내보내기", "내보낼 사원이 없습니다.")
            return
        from PyQt5.QtWidgets import QFileDialog

        file_path, _ = QFileDialog.getSaveFileName(
            self, "사원 목록 저장", "사원목록.xlsx", "Excel Files (*.xlsx)"
        )
        if not file_path:
            return
        if not file_path.lower().endswith(".xlsx"):
            file_path += ".xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "사원목록"
        col_count = self.table.columnCount()
        if col_count > 1:
            ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=col_count)
        ws.cell(row=1, column=1, value="사원 목록").font = Font(bold=True, size=16)
        ws.cell(row=1, column=1).alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 28
        for c in range(col_count):
            h = self.table.horizontalHeaderItem(c)
            ws.cell(row=2, column=c + 1, value=h.text() if h else "").font = Font(bold=True)
        for r in range(self.table.rowCount()):
            for c in range(col_count):
                item = self.table.item(r, c)
                val = item.text() if item else ""
                ws.cell(row=r + 3, column=c + 1, value=val)
        widths = (8, 16, 14, 18, 22, 14, 10)
        for i in range(1, col_count + 1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = (
                widths[i - 1] if i - 1 < len(widths) else 14
            )
        try:
            wb.save(file_path)
        except Exception as e:
            QMessageBox.warning(self, "오류", "엑셀 파일 저장 실패:\n%s" % e)
            return
        try:
            if sys.platform == "win32":
                os.startfile(file_path)
            elif sys.platform == "darwin":
                subprocess.run(["open", file_path], check=True)
            else:
                subprocess.run(["xdg-open", file_path], check=True)
        except Exception:
            pass

    def on_import_excel(self):
        companies = getattr(self.main_win, "companies_data", []) or []
        cid = companies[0].get("code") if companies and isinstance(companies[0], dict) else None
        if not cid:
            QMessageBox.warning(self, "경고", "회사 데이터가 없습니다.")
            return
            
        from PyQt5.QtWidgets import QFileDialog
        file_path, _ = QFileDialog.getOpenFileName(self, "사원 엑셀 파일 선택", "", "Excel Files (*.xlsx *.xls)")
        if not file_path: return

        replace = self.replace_employees_check.isChecked()
        if replace:
            if QMessageBox.question(
                self,
                "확인",
                "기존 사원을 모두 삭제한 뒤 엑셀 내용으로 다시 등록합니다.\n"
                "지문과 식사 기록도 함께 삭제됩니다.\n"
                "계속하시겠습니까?",
            ) != QMessageBox.Yes:
                return
        
        try:
            with open(file_path, "rb") as f:
                file_content = f.read()
            
            self.import_btn.setEnabled(False)
            self.replace_employees_check.setEnabled(False)
            self.main_win.statusBar().showMessage(
                "기존 사원 삭제 후 엑셀 등록 중..." if replace else "엑셀 데이터 추가 등록 중...",
                5000,
            )
            self.import_loader = DataLoader(self.api.import_employees_excel, cid, file_content, replace)
            self.import_loader.finished.connect(self.on_import_finished)
            self.import_loader.error.connect(self._on_import_error)
            self.import_loader.start()
        except Exception as e:
            self.import_btn.setEnabled(True)
            self.replace_employees_check.setEnabled(True)
            QMessageBox.critical(self, "오류", f" 파일을 읽을 수 없습니다: {str(e)}")

    def _on_import_error(self, err_msg):
        self.import_btn.setEnabled(True)
        self.replace_employees_check.setEnabled(True)
        QMessageBox.critical(self, "처리 오류", "엑셀 등록 중 오류가 발생했습니다:\n%s" % err_msg)

    def on_import_finished(self, result):
        self.import_btn.setEnabled(True)
        self.replace_employees_check.setEnabled(True)
        success, data = result if isinstance(result, tuple) else (False, result)
        if success:
            msg = data.get("message", "완료되었습니다.")
            QMessageBox.information(self, "임포트 성공", msg)
            self.load_data() # Refresh table
        else:
            QMessageBox.warning(self, "임포트 실패", str(data))

    def on_delete(self):
        eid = self._employee_id_for_row_actions()
        if eid is None:
            QMessageBox.warning(self, "경고", "퇴사 처리할 사원을 표에서 선택하세요.")
            return
        r = self._employee_row_index_for_id(eid)
        if r >= 0:
            st_item = self.table.item(r, 6)
            status_text = (st_item.text() if st_item else "").strip()
            if status_text != "재직":
                QMessageBox.warning(self, "경고", "이미 퇴사 처리된 사원입니다. (퇴사취소 후 다시 시도할 수 있습니다.)")
                return

        if QMessageBox.question(self, "확인", "정말 이 사원을 삭제(퇴사) 처리하시겠습니까?\n(DB에는 남아 있어 같은 사번으로 재등록 시 '재등록' 처리됩니다.)") == QMessageBox.Yes:
            if self._emp_mutate_loader is not None and self._emp_mutate_loader.isRunning():
                QMessageBox.warning(self, "알림", "다른 작업이 진행 중입니다. 잠시 후 다시 시도하세요.")
                return
            self.del_btn.setEnabled(False)
            self.permanent_del_btn.setEnabled(False)
            self._emp_mutate_loader = DataLoader(self.api.delete_employee, eid, False)
            self._emp_mutate_loader.finished.connect(self.on_delete_finished)
            self._emp_mutate_loader.error.connect(self.on_action_error)
            self._emp_mutate_loader.start()

    def on_cancel_resign(self):
        eid = self._employee_id_for_row_actions()
        if eid is None:
            QMessageBox.warning(self, "경고", "퇴사취소할 사원을 표에서 선택하세요.")
            return
        r = self._employee_row_index_for_id(eid)
        if r < 0:
            QMessageBox.warning(self, "경고", "선택한 사원 행을 찾을 수 없습니다.")
            return
        item_status = self.table.item(r, 6)
        status_text = (item_status.text() if item_status else "").strip()
        if status_text != "퇴사":
            QMessageBox.warning(self, "경고", "퇴사자만 퇴사취소할 수 있습니다.")
            return
        name_item = self.table.item(r, 2)
        name = name_item.text() if name_item else "(선택 행)"
        if QMessageBox.question(self, "확인", f"'{name}' 사원을 재직으로 복구하시겠습니까?") != QMessageBox.Yes:
            return
        if self._emp_mutate_loader is not None and self._emp_mutate_loader.isRunning():
            QMessageBox.warning(self, "알림", "다른 작업이 진행 중입니다. 잠시 후 다시 시도하세요.")
            return
        self.cancel_resign_btn.setEnabled(False)
        self._emp_mutate_loader = DataLoader(self.api.update_employee, eid, {"status": "ACTIVE"})
        self._emp_mutate_loader.finished.connect(self.on_action_finished)
        self._emp_mutate_loader.error.connect(self.on_action_error)
        self._emp_mutate_loader.start()

    def on_permanent_delete(self):
        eid = self._employee_id_for_row_actions()
        if eid is None:
            QMessageBox.warning(self, "경고", "삭제할 사원을 표에서 선택하세요.")
            return
        r = self._employee_row_index_for_id(eid)
        if r < 0:
            QMessageBox.warning(self, "경고", "선택한 사원 행을 찾을 수 없습니다.")
            return
        item_status = self.table.item(r, 6)
        name_item = self.table.item(r, 2)
        name = name_item.text() if name_item else "(선택 행)"
        status_text = (item_status.text() if item_status else "").strip()

        if status_text == "재직":
            msg = (
                f"'{name}' 사원은 재직자입니다.\n"
                "완전 삭제하면 DB에서 제거되며, 모든 식사 기록도 삭제됩니다.\n"
                "정말 삭제하시겠습니까?"
            )
        else:
            msg = (
                f"'{name}' 사원을 DB에서 완전히 제거합니다.\n"
                "같은 사번으로 다시 등록할 수 있습니다.\n"
                "관련 식사 기록도 삭제됩니다. 계속하시겠습니까?"
            )
        if QMessageBox.question(
            self, "완전 삭제 확인", msg,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        ) == QMessageBox.Yes:
            if self._emp_mutate_loader is not None and self._emp_mutate_loader.isRunning():
                QMessageBox.warning(self, "알림", "다른 작업이 진행 중입니다. 잠시 후 다시 시도하세요.")
                return
            self.del_btn.setEnabled(False)
            self.permanent_del_btn.setEnabled(False)
            self._emp_mutate_loader = DataLoader(self.api.delete_employee, eid, True)
            self._emp_mutate_loader.finished.connect(self.on_delete_finished)
            self._emp_mutate_loader.error.connect(self.on_action_error)
            self._emp_mutate_loader.start()

    def on_delete_finished(self, result):
        self.del_btn.setEnabled(True)
        self.permanent_del_btn.setEnabled(True)
        if isinstance(result, dict):
            success = bool(result.get("db_ok"))
            f22_rep = result.get("f22_report") or []
        else:
            success = bool(result)
            f22_rep = []
        if success:
            self.clear_inputs()
            self.load_data()
            fails, oks = _f22_sync_result_lines(self.api, f22_rep)
            if not fails and not oks:
                QMessageBox.information(self, "성공", "삭제되었습니다.")
                return
            lines = []
            if fails:
                lines.append("DB에서는 삭제되었습니다. 아래 단말에는 반영되지 않았습니다.")
                lines.append("실패 %d대 · 성공 %d대" % (len(fails), len(oks)))
                lines.append("")
                lines.append("동기화되지 않은 단말")
                for name, msg in fails:
                    lines.append("· %s" % name)
                    if msg:
                        lines.append("  %s" % msg)
                if oks:
                    lines.append("")
                    lines.append("반영된 단말")
                    for name, _msg in oks:
                        lines.append("· %s" % name)
                dark_warning(self, "삭제 완료", "\n".join(lines))
            else:
                lines.append("삭제되었습니다. 단말 %d대에 반영됨" % len(oks))
                for name, _msg in oks:
                    lines.append("· %s" % name)
                dark_information(self, "삭제 완료", "\n".join(lines))
        else:
            QMessageBox.warning(self, "오류", "삭제에 실패했습니다.")

    def _start_f22_employee_sync_from_row(self, data: dict) -> None:
        if not isinstance(data, dict):
            return
        rf = (data.get("rf_card_uid") or data.get("card_uid") or "").strip()
        tpl0 = data.get("fingerprint_template_b64") or None
        tpl1 = data.get("fingerprint_template_b64_2") or None
        tpl2 = data.get("fingerprint_template_b64_3") or None
        tpl3 = data.get("fingerprint_template_b64_4") or None
        if not (rf or tpl0 or tpl1 or tpl2 or tpl3):
            return
        if _f22_att_mode_is_adms():
            self.main_win.statusBar().showMessage(
                "ADMS 단말에 사용자·카드·지문 전송 중… (getrequest 대기)", 10000
            )
        elif tpl0 or tpl1 or tpl2 or tpl3:
            if rf:
                self.main_win.statusBar().showMessage("F22 단말에 카드·사용자·지문 전송 중…", 8000)
            else:
                self.main_win.statusBar().showMessage("F22 단말에 사용자·지문 전송 중…", 8000)
        else:
            self.main_win.statusBar().showMessage("F22 단말에 카드·사용자 등록 전송 중…", 8000)
        self._f22_emp_loader = DataLoader(
            _f22_employee_sync_task,
            self.api,
            data.get("emp_no") or "",
            data.get("name") or "",
            rf,
            "",
            tpl0,
            tpl1,
            tpl2,
            tpl3,
            data.get("fingerprint_slot_fid_1"),
            data.get("fingerprint_slot_fid_2"),
            data.get("fingerprint_slot_fid_3"),
            data.get("fingerprint_slot_fid_4"),
        )
        self._f22_emp_loader.finished.connect(self._on_f22_employee_sync_done)
        self._f22_emp_loader.error.connect(self._on_f22_employee_sync_err)
        self._f22_emp_loader.start()

    def on_register_fingerprint(self):
        eid = self._employee_id_for_row_actions()
        if eid is None:
            QMessageBox.warning(self, "경고", "사원을 표에서 선택하세요.")
            return

        row_data = None
        for r in getattr(self, "_employees_list", []) or []:
            if isinstance(r, dict) and str(r.get("emp_no") or "") == str(eid):
                row_data = r
                break

        emp_no = self.emp_no_input.text().strip()
        name = self.name_input.text().strip()
        rf_card = self.rf_card_uid_input.text().strip()
        did = self.dept_combo.currentData()
        cid = None
        if did is not None:
            for d in self.main_win.departments_data or []:
                if isinstance(d, dict) and d.get("code") == did:
                    cid = d.get("company_code")
                    break
        if row_data:
            emp_no = emp_no or str(row_data.get("emp_no") or "").strip()
            name = name or str(row_data.get("name") or "").strip()
            rf_card = rf_card or str(row_data.get("rf_card_uid") or "").strip()
            if cid is None:
                cid = row_data.get("company_code")
            if did is None:
                did = row_data.get("department_code")
        companies = getattr(self.main_win, "companies_data", []) or []
        if cid is None and companies and isinstance(companies[0], dict):
            cid = companies[0].get("code")

        if not emp_no:
            QMessageBox.warning(self, "지문", "사번을 확인할 수 없습니다.")
            return

        dlg = FingerprintRegisterDialog(
            self.api, self, eid, emp_no, name, cid, did, rf_card
        )
        if dlg.exec_() != QDialog.Accepted:
            return
        saved = dlg.saved_employee
        if not isinstance(saved, dict):
            return
        self.load_data()
        # 다이얼로그에서 DB·단말 동기화를 이미 수행한 경우 결과만 표시
        if getattr(dlg, "terminal_synced", False):
            results = getattr(dlg, "terminal_sync_results", None) or []
            body = _f22_fingerprint_sync_dialog_text(self.api, results)
            title = "단말 동기화"
            if results and all(
                isinstance(r, (list, tuple)) and len(r) >= 2 and r[1] for r in results
            ):
                dark_information(self, title, body)
            elif results:
                dark_warning(self, title, body)
            return
        self._start_f22_employee_sync_from_row(saved)

    def _on_f22_import_fp_finished(self, result):
        self.main_win.statusBar().showMessage("", 1)
        if not isinstance(result, tuple) or len(result) != 2:
            self.fp_register_edit_btn.setEnabled(True)
            QMessageBox.warning(self, "지문", "결과 형식 오류입니다.")
            return
        ok, data = result
        if not ok:
            self.fp_register_edit_btn.setEnabled(True)
            QMessageBox.warning(self, "지문 가져오기", str(data))
            return
        self.load_data()
        self.main_win.statusBar().showMessage("F22 단말에 지문 동기화 중…", 12000)
        self._fp_sync_loader = DataLoader(_f22_sync_after_terminal_import_task, self.api, data)
        self._fp_sync_loader.finished.connect(self._on_f22_import_sync_done)
        self._fp_sync_loader.error.connect(self._on_f22_import_sync_err)
        self._fp_sync_loader.start()

    def _on_f22_import_fp_error(self, err_msg: str):
        self.fp_register_edit_btn.setEnabled(True)
        self.main_win.statusBar().showMessage("", 1)
        QMessageBox.warning(self, "지문 가져오기", str(err_msg))

    def _on_f22_import_sync_done(self, results):
        self.fp_register_edit_btn.setEnabled(True)
        self.main_win.statusBar().showMessage("", 1)
        if not isinstance(results, list):
            results = []
        body = _f22_fingerprint_sync_dialog_text(self.api, results)
        title = "단말 동기화"
        if not results:
            dark_information(self, title, body)
            return
        if all(isinstance(r, (list, tuple)) and len(r) >= 2 and r[1] for r in results):
            dark_information(self, title, body)
        else:
            dark_warning(self, title, body)

    def _on_f22_import_sync_err(self, err_msg: str):
        self.fp_register_edit_btn.setEnabled(True)
        self.main_win.statusBar().showMessage("", 1)
        dark_warning(
            self,
            "단말 동기화",
            "DB, 단말기 적용 완료\n\n단말 동기화 처리 중 오류가 발생했습니다.\n%s" % err_msg,
        )

    def on_action_finished(self, result):
        self.add_btn.setEnabled(True)
        self.edit_btn.setEnabled(True)
        self.fp_register_edit_btn.setEnabled(True)
        self.cancel_resign_btn.setEnabled(True)
        if isinstance(result, tuple):
            success, data = result
        else:
            success, data = bool(result), None
        if success:
            QMessageBox.information(self, "성공", "작업이 완료되었습니다.")
            self.load_data()
            self.clear_inputs()
            if isinstance(data, dict):
                rf = (data.get("rf_card_uid") or data.get("card_uid") or "").strip()
                tpl0 = data.get("fingerprint_template_b64") or None
                tpl1 = data.get("fingerprint_template_b64_2") or None
                tpl2 = data.get("fingerprint_template_b64_3") or None
                tpl3 = data.get("fingerprint_template_b64_4") or None
                if rf or tpl0 or tpl1 or tpl2 or tpl3:
                    self._start_f22_employee_sync_from_row(data)
        else:
            msg = str(data) if data is not None else "작업에 실패했습니다."
            QMessageBox.warning(self, "오류", msg)

    def _on_f22_employee_sync_done(self, results):
        if not isinstance(results, list) or not results:
            return
        body = _f22_fingerprint_sync_dialog_text(self.api, results)
        title = "단말 동기화"
        if all(isinstance(r, (list, tuple)) and len(r) >= 2 and r[1] for r in results):
            dark_information(self, title, body)
        else:
            dark_warning(self, title, body)

    def _on_f22_employee_sync_err(self, err_msg: str):
        dark_warning(self, "F22 단말 동기화", "전송 중 오류:\n%s" % err_msg)

    def on_action_error(self, err_msg):
        self.add_btn.setEnabled(True)
        self.edit_btn.setEnabled(True)
        self.del_btn.setEnabled(True)
        self.cancel_resign_btn.setEnabled(True)
        self.permanent_del_btn.setEnabled(True)
        self.fp_register_edit_btn.setEnabled(True)
        QMessageBox.critical(self, "처리 오류", f"작업 중 오류가 발생했습니다:\n{err_msg}")

    def load_data(self):
        # 재직+퇴사 모두 조회 (status 없음 = 전체).
        # 탭을 오가는 동안 이전 조회 스레드를 지우면 창이 멈춘다.
        if getattr(self, "_employee_load_running", False):
            self._employee_reload_pending = True
            return
        self._employee_reload_pending = False
        self._employee_load_running = True
        loader = DataLoader(self.api.get_employees, self.search_input.text(), None)
        loader.finished.connect(self._on_employee_load_finished)
        loader.error.connect(self._on_employee_list_error)
        self.loader = loader
        loader.start()

    def _schedule_pending_employee_load(self):
        if not getattr(self, "_employee_reload_pending", False):
            return
        self._employee_reload_pending = False
        QTimer.singleShot(0, self.load_data)

    def _on_employee_load_finished(self, data):
        self._employee_load_running = False
        sender = self.sender()
        try:
            self.display_data(data)
        finally:
            if sender is not None:
                sender.deleteLater()
            self._schedule_pending_employee_load()

    def _on_employee_list_error(self, err_msg: str):
        self._employee_load_running = False
        sender = self.sender()
        if sender is not None:
            sender.deleteLater()
        QMessageBox.warning(self, "사원 목록", "불러오기 실패:\n%s" % err_msg)
        self._schedule_pending_employee_load()

    def display_data(self, data):
        if not isinstance(data, list): return
        if getattr(self, "_filling_table", False):
            return
        self._filling_table = True
        self.table.setSortingEnabled(False)
        self.table.blockSignals(True)
        self.table.setUpdatesEnabled(False)
        self._employees_list = list(data)

        selected_ids = set()
        for item in self.table.selectedItems():
            if item.column() == 1 and item.data(Qt.UserRole) is not None:
                selected_ids.add(item.data(Qt.UserRole))

        self._selected_emp_id = None

        self.table.setRowCount(len(data))

        dept_map = {d["code"]: d["name"] for d in self.main_win.departments_data if isinstance(d, dict)}

        for i, row in enumerate(data):
            if not isinstance(row, dict): continue
            item_idx = QTableWidgetItem()
            item_idx.setData(Qt.DisplayRole, i + 1)
            item_idx.setTextAlignment(Qt.AlignCenter)

            item_emp = QTableWidgetItem(str(row.get("emp_no", "")))
            row_id = str(row.get("emp_no") or "")
            item_emp.setData(Qt.UserRole, row_id)

            dept_name = dept_map.get(row.get("department_code"), "Unknown")
            status_label = "재직" if _employee_status_is_active(row.get("status")) else "퇴사"
            rf_uid = str(row.get("rf_card_uid") or row.get("card_uid") or "")
            item_fp = QTableWidgetItem(_employee_fp_cell_text(row))

            item_dept = QTableWidgetItem(dept_name)
            item_dept.setData(Qt.UserRole, row.get("department_code"))

            self.table.setItem(i, 0, item_idx)
            self.table.setItem(i, 1, item_emp)
            self.table.setItem(i, 2, QTableWidgetItem(str(row.get("name", ""))))
            self.table.setItem(i, 3, item_dept)
            self.table.setItem(i, 4, QTableWidgetItem(rf_uid))
            self.table.setItem(i, 5, item_fp)

            item_status = QTableWidgetItem(status_label)
            self.table.setItem(i, 6, item_status)

            if not _employee_status_is_active(row.get("status")):
                for col in range(7):
                    it = self.table.item(i, col)
                    if it: it.setForeground(QColor("#999999"))

            if row_id in selected_ids:
                for col in range(7):
                    it = self.table.item(i, col)
                    if it: it.setSelected(True)
                self._selected_emp_id = row_id

        self.table.blockSignals(False)
        self.table.setUpdatesEnabled(True)
        header = self.table.horizontalHeader()
        header.blockSignals(True)
        self.table.setSortingEnabled(True)
        header.blockSignals(False)
        self._filling_table = False
        QTimer.singleShot(0, self._apply_employee_column_widths)
        if selected_ids:
            self.on_selection_changed()


def _meal_raw_data_status_label(row: dict) -> str:
    """원시데이터 표 상태 열: 취소(is_void) / 수기(manual) / 단말(그 외)."""
    if not isinstance(row, dict):
        return ""
    if row.get("is_void"):
        return "취소"
    p = str(row.get("path") or "").strip().lower()
    if p == "manual":
        return "수기"
        return "단말"


class FailCardEmployeeDialog(QDialog):
    """인증실패 카드번호로 찾은 사원 정보."""

    def __init__(self, card_no: str, rows: list, parent=None, fail_when=None):
        super().__init__(parent)
        self._rows = list(rows or [])
        self._fail_when = fail_when or ("", "")
        self.setWindowTitle("인사 정보")
        self.setObjectName("FailCardEmployeeDialog")
        self.setMinimumWidth(420)
        self.setStyleSheet(
            """
            #FailCardEmployeeDialog { background-color: #1e293b; }
            #FailCardEmployeeDialog QLabel { color: #e2e8f0; font-size: 16px; font-family: 'Malgun Gothic'; }
            #FailCardEmployeeDialog QLabel#InputLabel { color: #94a3b8; font-weight: bold; font-size: 15px; }
            #FailCardEmployeeDialog QLabel#ValueLabel { color: #f8fafc; font-weight: bold; font-size: 18px; }
            """
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 18)
        lay.setSpacing(12)
        title = QLabel("카드번호  %s" % (card_no or ""))
        title.setStyleSheet("color: #f8fafc; font-size: 18px; font-weight: bold; font-family: 'Malgun Gothic';")
        lay.addWidget(title)
        when_date, when_time = self._fail_when
        if when_date or when_time:
            when = QLabel("인증시각  %s %s" % (when_date, when_time))
            when.setStyleSheet("color: #94a3b8; font-size: 14px; font-family: 'Malgun Gothic';")
            lay.addWidget(when)
        self._reg_buttons = []
        if not self._rows:
            empty = QLabel("해당 카드번호의 사원이 없습니다.")
            empty.setWordWrap(True)
            lay.addWidget(empty)
        else:
            for i, row in enumerate(self._rows):
                if i:
                    line = QFrame()
                    line.setFrameShape(QFrame.HLine)
                    line.setStyleSheet("color: #334155;")
                    lay.addWidget(line)
                form = QFormLayout()
                form.setHorizontalSpacing(16)
                form.setVerticalSpacing(8)
                for lab, val in (
                    ("사번", row.get("emp_no") or ""),
                    ("성명", row.get("name") or ""),
                    ("부서", row.get("dept_name") or ""),
                    ("UID", row.get("rf_card_uid") or ""),
                ):
                    name = QLabel(lab)
                    name.setObjectName("InputLabel")
                    value = QLabel(str(val))
                    value.setObjectName("ValueLabel")
                    value.setTextInteractionFlags(Qt.TextSelectableByMouse)
                    form.addRow(name, value)
                lay.addLayout(form)
                reg_btn = QPushButton("원시데이터 등록")
                reg_btn.setObjectName("PrimaryBtn")
                reg_btn.setFixedHeight(40)
                reg_btn.clicked.connect(lambda _checked=False, emp=row: self._on_register(emp))
                self._reg_buttons.append(reg_btn)
                lay.addWidget(reg_btn)
        close_btn = QPushButton("닫기")
        close_btn.setObjectName("SecondaryBtn")
        close_btn.setFixedHeight(40)
        close_btn.clicked.connect(self.accept)
        lay.addWidget(close_btn)

    def _on_register(self, emp: dict):
        parent = self.parent()
        if parent is None or not hasattr(parent, "register_meal_from_fail_card"):
            return
        for btn in self._reg_buttons:
            btn.setEnabled(False)
        parent.register_meal_from_fail_card(emp, self._fail_when, self)

    def set_register_enabled(self, enabled: bool):
        for btn in self._reg_buttons:
            btn.setEnabled(enabled)


class RawDataRecordAddDialog(QDialog):
    """원시데이터 수동 기록 추가/수정: 사원 검색, 날짜, 식사 종류."""

    def __init__(self, raw_screen: "RawDataScreen", edit_log: dict | None = None):
        super().__init__(raw_screen)
        self.raw = raw_screen
        self.edit_log = edit_log
        self.is_edit = bool(edit_log)
        self.setWindowTitle("기록 수정" if self.is_edit else "기록 추가")
        self.setMinimumWidth(440)
        self.selected_user_id = None
        self.setObjectName("RawDataRecordAddDialog")
        # 메인 화면 QTableWidget 과 동일 톤 (#1e293b)
        self.setStyleSheet(
            """
            #RawDataRecordAddDialog {
                background-color: #1e293b;
            }
            #RawDataRecordAddDialog QLabel#InputLabel {
                color: #ffffff;
                font-weight: bold;
                font-family: 'Malgun Gothic', 'Segoe UI', sans-serif;
                font-size: 18px;
            }
            #RawDataRecordAddDialog QLineEdit {
                background-color: #1e293b;
                border: 1px solid #475569;
                border-radius: 8px;
                color: #f8fafc;
                padding: 10px 14px;
                min-height: 44px;
                font-size: 18px;
                font-weight: bold;
                font-family: 'Malgun Gothic', 'Segoe UI', sans-serif;
            }
            #RawDataRecordAddDialog QDateEdit,
            #RawDataRecordAddDialog QComboBox {
                background-color: #1e293b;
                border: 1px solid #475569;
                border-radius: 8px;
                color: #f8fafc;
                min-height: 52px;
                height: 52px;
                font-size: 19px;
                font-weight: bold;
                font-family: 'Malgun Gothic', 'Segoe UI', sans-serif;
            }
            #RawDataRecordAddDialog QComboBox::drop-down {
                border-left: 1px solid #475569;
                width: 30px;
                background-color: #334155;
            }
            """
        )

        lay = QVBoxLayout(self)
        lay.setSpacing(12)
        lay.setContentsMargins(18, 18, 18, 18)

        emp_label = QLabel("사원 검색 (이름)")
        emp_label.setObjectName("InputLabel")
        lay.addWidget(emp_label)
        search_h = QHBoxLayout()
        self.emp_search_input = QLineEdit()
        self.emp_search_input.setPlaceholderText("검색 버튼을 클릭하세요...")
        self.emp_search_input.setReadOnly(True)
        self.emp_search_btn = QPushButton("사원검색")
        self.emp_search_btn.setObjectName("PrimaryBtn")
        self.emp_search_btn.setFixedWidth(100)
        self.emp_search_btn.clicked.connect(self._on_search_employee)
        search_h.addWidget(self.emp_search_input)
        search_h.addWidget(self.emp_search_btn)
        lay.addLayout(search_h)

        self.selected_emp_label = QLabel("선택된 사원: 없음")
        self.selected_emp_label.setStyleSheet("color: #94a3b8; font-size: 14px;")
        lay.addWidget(self.selected_emp_label)

        date_label = QLabel("날짜")
        date_label.setObjectName("InputLabel")
        self.dlg_date = QDateEdit()
        self.dlg_date.setCalendarPopup(True)
        self.dlg_date.setDate(QDate.currentDate())
        date_hint = QLabel("※ 시간은 선택한 식사 종류에 따라 자동 적용됩니다.")
        date_hint.setStyleSheet("color: #94a3b8; font-size: 12px;")
        lay.addWidget(date_label)
        lay.addWidget(self.dlg_date)
        lay.addWidget(date_hint)

        policy_label = QLabel("식사 종류")
        policy_label.setObjectName("InputLabel")
        self.dlg_policy_combo = QComboBox()
        self.dlg_policy_combo.addItem("선택하세요", None)
        for p in raw_screen.policies_list or []:
            self.dlg_policy_combo.addItem(p["meal_type"], p["id"])
        lay.addWidget(policy_label)
        lay.addWidget(self.dlg_policy_combo)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("닫기")
        cancel_btn.setObjectName("SecondaryBtn")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = QPushButton("수정" if self.is_edit else "등록")
        ok_btn.setObjectName("PrimaryBtn")
        ok_btn.clicked.connect(self._on_ok)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(ok_btn)
        lay.addLayout(btn_row)

        if self.is_edit:
            self._apply_edit_log(edit_log)

    def _apply_edit_log(self, log: dict):
        u = log.get("user") or {}
        uid = str((log.get("user") or {}).get("emp_no") or log.get("emp_no") or "").strip() or None
        self.selected_user_id = uid
        name = str(u.get("name") or "")
        emp_no = str(u.get("emp_no") or "")
        self.emp_search_input.setText(name)
        if name or emp_no:
            self.selected_emp_label.setText("선택됨: %s (%s)" % (name, emp_no))
        else:
            self.selected_emp_label.setText("선택된 사원: 없음")
        try:
            dt = datetime.fromisoformat(str(log.get("created_at") or "").replace("Z", ""))
            self.dlg_date.setDate(QDate(dt.year, dt.month, dt.day))
        except Exception:
            self.dlg_date.setDate(QDate.currentDate())
        policy_id = log.get("policy_id")
        sel_i = 0
        for i in range(self.dlg_policy_combo.count()):
            if self.dlg_policy_combo.itemData(i) == policy_id:
                sel_i = i
                break
        self.dlg_policy_combo.setCurrentIndex(sel_i)

    def _on_search_employee(self):
        dialog = EmployeeSearchDialog(self.raw.api, self.raw.main_win, self)
        if dialog.exec_():
            emp = dialog.selected_employee
            if emp:
                self.selected_user_id = emp.get("emp_no")
                self.emp_search_input.setText(str(emp.get("name") or ""))
                self.selected_emp_label.setText(
                    "선택됨: %s (%s)"
                    % (str(emp.get("name") or ""), str(emp.get("emp_no") or ""))
                )

    def _on_ok(self):
        if not self.selected_user_id:
            QMessageBox.warning(self, "경고", "사원을 먼저 선택하세요.")
            return
        if not self.dlg_policy_combo.currentData():
            QMessageBox.warning(self, "경고", "식사 종류를 선택하세요.")
            return
        self.accept()

    def build_payload(self) -> dict:
        policy_id = self.dlg_policy_combo.currentData()
        time_str = self.raw._created_at_time_from_policy(policy_id)
        dt_str = "%sT%s" % (self.dlg_date.date().toString("yyyy-MM-dd"), time_str)
        guest = 0
        if self.is_edit and self.edit_log:
            try:
                guest = int(self.edit_log.get("guest_count") or 0)
            except (TypeError, ValueError):
                guest = 0
        return {
            "emp_no": self.selected_user_id,
            "policy_id": policy_id,
            "created_at": dt_str,
            "guest_count": guest,
        }


class RawDataScreen(QWidget):
    def __init__(self, api, main_win):
        super().__init__()
        self.api = api
        self.main_win = main_win
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        
        header_layout = QHBoxLayout()
        header = QLabel("원시 데이터 관리")
        header.setObjectName("HeaderTitle")
        
        self.start_date_edit = QDateEdit()
        self.start_date_edit.setCalendarPopup(True)
        self.start_date_edit.setDate(QDate.currentDate())
        self.start_date_edit.setFixedWidth(180)

        tilde = QLabel("~")
        tilde.setStyleSheet("color: white; font-size: 18px; font-weight: bold;")
        
        self.end_date_edit = QDateEdit()
        self.end_date_edit.setCalendarPopup(True)
        self.end_date_edit.setDate(QDate.currentDate())
        self.end_date_edit.setFixedWidth(180)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("사번 또는 성명 검색...")
        self.search_input.setFixedWidth(250)
        self.search_input.returnPressed.connect(self.load_data)

        self.search_btn = QPushButton("조회")
        self.search_btn.setObjectName("PrimaryBtn")
        self.search_btn.setFixedWidth(80)
        self.search_btn.clicked.connect(self.load_data)
        
        header_layout.addWidget(header)
        header_layout.addStretch()
        header_layout.addWidget(self.start_date_edit)
        header_layout.addWidget(tilde)
        header_layout.addWidget(self.end_date_edit)
        header_layout.addSpacing(10)
        header_layout.addWidget(self.search_input)
        header_layout.addWidget(self.search_btn)
        self.raw_excel_btn = QPushButton("엑셀")
        self.raw_excel_btn.setObjectName("SecondaryBtn")
        self.raw_excel_btn.setFixedWidth(70)
        self.raw_excel_btn.clicked.connect(self.on_raw_excel)
        header_layout.addWidget(self.raw_excel_btn)

        # Main layout structure: Left (Table) / Right (Inputs)
        main_h_layout = QHBoxLayout()
        layout.addLayout(main_h_layout)
        
        # Left side: Table Area
        left_layout = QVBoxLayout()
        left_layout.addLayout(header_layout)
        
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["No", "날짜", "시간", "이름", "사번", "UID", "식사종류", "상태"]
        )
        setup_standard_table(self.table)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 80)
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.table.itemSelectionChanged.connect(self.on_selection_changed)
        left_layout.addWidget(self.table, 1)

        fail_title = QLabel("RF 카드 인증 실패")
        fail_title.setStyleSheet(
            "font-size: 18px; font-weight: bold; color: #f87171; margin-top: 12px; font-family: 'Malgun Gothic';"
        )
        left_layout.addWidget(fail_title)
        self.fail_table = QTableWidget(0, 6)
        self.fail_table.setHorizontalHeaderLabels(
            ["No", "날짜", "시간", "카드번호", "사유", "단말"]
        )
        setup_standard_table(self.fail_table)
        self.fail_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self.fail_table.setColumnWidth(0, 48)
        # 데이터 5줄만 보이도록 고정 → 위 인증 테이블이 남는 높이를 사용
        _fail_row_h = self.fail_table.verticalHeader().defaultSectionSize()
        _fail_hdr_h = max(40, self.fail_table.horizontalHeader().sizeHint().height())
        self.fail_table.setFixedHeight(_fail_hdr_h + _fail_row_h * 5 + 2)
        self.fail_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.fail_table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.fail_table.itemSelectionChanged.connect(self._on_fail_table_selection_changed)
        fail_row = QHBoxLayout()
        fail_row.setSpacing(10)
        fail_row.addWidget(self.fail_table, 1)
        self.fail_card_input = QLineEdit()
        self.fail_card_input.setPlaceholderText("카드번호")
        self.fail_card_input.setFixedWidth(170)
        self.fail_card_input.setMinimumHeight(40)
        self.fail_card_input.returnPressed.connect(self.on_lookup_fail_card)
        self.fail_card_lookup_btn = QPushButton("조회")
        self.fail_card_lookup_btn.setObjectName("SecondaryBtn")
        self.fail_card_lookup_btn.setFixedWidth(72)
        self.fail_card_lookup_btn.setMinimumHeight(40)
        self.fail_card_lookup_btn.clicked.connect(self.on_lookup_fail_card)
        card_side = QHBoxLayout()
        card_side.setSpacing(8)
        card_side.addWidget(self.fail_card_input)
        card_side.addWidget(self.fail_card_lookup_btn)
        card_wrap = QWidget()
        card_outer = QVBoxLayout(card_wrap)
        card_outer.setContentsMargins(0, 0, 0, 0)
        card_outer.addStretch(1)
        card_outer.addLayout(card_side)
        card_outer.addStretch(1)
        fail_row.addWidget(card_wrap, 0)
        left_layout.addLayout(fail_row, 0)

        main_h_layout.addLayout(left_layout, 7) # 70% width
        
        # Right side: Input Panel
        self.input_panel = QFrame()
        self.input_panel.setObjectName("StatCard")
        self.input_panel.setFixedWidth(350)
        input_layout = QVBoxLayout(self.input_panel)
        input_layout.setSpacing(15)
        
        panel_title = QLabel("기록 관리")
        panel_title.setStyleSheet("font-size: 22px; font-weight: bold; color: #6366f1; margin-bottom: 10px;")
        input_layout.addWidget(panel_title)

        self.policies_list = []
        self.load_policies()

        # 기록 추가/수정: 팝업 다이얼로그에서 입력
        self.add_btn = QPushButton("기록 추가")
        self.add_btn.setObjectName("PrimaryBtn")
        self.add_btn.setFixedHeight(40)
        self.add_btn.clicked.connect(self.on_add)
        input_layout.addWidget(self.add_btn)

        self.edit_btn = QPushButton("기록 수정")
        self.edit_btn.setObjectName("SecondaryBtn")
        self.edit_btn.setFixedHeight(40)
        self.edit_btn.clicked.connect(self.on_update)
        self.edit_btn.setEnabled(False)
        input_layout.addWidget(self.edit_btn)

        self.del_btn = QPushButton("기록 삭제")
        self.del_btn.setObjectName("DangerBtn")
        self.del_btn.setFixedHeight(40)
        self.del_btn.setToolTip(
            "DB에서 지우지 않고 삭제(취소) 처리합니다. 식수 집계·보고서·대시보드에서는 제외됩니다."
        )
        self.del_btn.clicked.connect(self.on_delete)
        self.del_btn.setEnabled(False)
        input_layout.addWidget(self.del_btn)

        input_layout.addStretch()

        main_h_layout.addWidget(self.input_panel)
        self.current_log_id = None
        self.full_data = []
        self._query_gen = 0
        self._exclusive_selecting = False

    def reset_view(self):
        """다른 탭으로 나가면 조회 결과와 검색 조건을 처음 상태로 되돌린다."""
        self._query_gen = getattr(self, "_query_gen", 0) + 1
        self.full_data = []
        self.search_input.clear()
        today = QDate.currentDate()
        self.start_date_edit.setDate(today)
        self.end_date_edit.setDate(today)
        for table in (self.table, self.fail_table):
            header = table.horizontalHeader()
            header.blockSignals(True)
            table.setSortingEnabled(False)
            table.setRowCount(0)
            table.setSortingEnabled(True)
            header.blockSignals(False)
        self.fail_card_input.clear()
        self.clear_inputs()

    def load_data(self):
        start_date = self.start_date_edit.date().toString("yyyy-MM-dd")
        end_date = self.end_date_edit.date().toString("yyyy-MM-dd")
        search = self.search_input.text()
        self._query_gen = getattr(self, "_query_gen", 0) + 1
        query_gen = self._query_gen

        def _bundle():
            ok1, meal = self.api.get_raw_data(search, start_date, end_date)
            if not ok1:
                return False, meal
            ok2, fails = self.api.get_auth_failure_logs(search, start_date, end_date)
            if not ok2:
                return False, fails
            return True, {"meal": meal, "failures": fails}

        self.loader = DataLoader(_bundle)
        self.loader.finished.connect(lambda result, g=query_gen: self.display_bundle(result, g))
        self.loader.start()

    def display_bundle(self, result, query_gen=None):
        if query_gen is not None and query_gen != getattr(self, "_query_gen", None):
            return
        if isinstance(result, tuple) and len(result) >= 2:
            success, payload = result[0], result[1]
            if not success:
                QMessageBox.warning(self, "오류", f"데이터를 가져오는데 실패했습니다.\n{payload}")
                return
            if isinstance(payload, dict):
                self.display_data(payload.get("meal") or [])
                self.display_failure_data(payload.get("failures") or [])
                return
        self.display_data(result)

    def display_failure_data(self, data):
        if not isinstance(data, list):
            return
        self.fail_table.setSortingEnabled(False)
        self.fail_table.setUpdatesEnabled(False)
        self.fail_table.setRowCount(len(data))
        for i, row in enumerate(data):
            if not isinstance(row, dict):
                continue
            created_at = str(row.get("created_at") or "")
            date_part = created_at.split("T")[0]
            time_part = created_at.split("T")[-1][:8]
            item_no = QTableWidgetItem()
            item_no.setData(Qt.DisplayRole, i + 1)
            item_no.setData(Qt.UserRole, row.get("id"))
            self.fail_table.setItem(i, 0, item_no)
            self.fail_table.setItem(i, 1, QTableWidgetItem(date_part))
            self.fail_table.setItem(i, 2, QTableWidgetItem(time_part))
            card = str(row.get("card_no") or "").strip() or "-"
            self.fail_table.setItem(i, 3, QTableWidgetItem(card))
            self.fail_table.setItem(i, 4, QTableWidgetItem(str(row.get("event_label") or "")))
            term = str(row.get("terminal_label") or row.get("terminal_device") or "")
            self.fail_table.setItem(i, 5, QTableWidgetItem(term))
        self.fail_table.setUpdatesEnabled(True)
        self.fail_table.setSortingEnabled(True)

    def display_data(self, data):
        if isinstance(data, tuple) and len(data) >= 2:
            success, payload = data[0], data[1]
            if not success:
                QMessageBox.warning(self, "오류", f"데이터를 가져오는데 실패했습니다.\n{payload}")
                return
            data = payload
        if not isinstance(data, list):
            return
        self.table.setSortingEnabled(False)
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(data))
        self.full_data = data
        for i, row in enumerate(data):
            if not isinstance(row, dict): continue
            created_at = row.get("created_at", "")
            date_part = created_at.split("T")[0]
            time_part = created_at.split("T")[-1][:8]
            
            # Row Number (No) - Use numeric data for sorting
            item_no = QTableWidgetItem()
            item_no.setData(Qt.DisplayRole, i + 1)
            row_id = row.get("id")
            item_no.setData(Qt.UserRole, row_id)
            
            self.table.setItem(i, 0, item_no)
            self.table.setItem(i, 1, QTableWidgetItem(date_part))
            self.table.setItem(i, 2, QTableWidgetItem(time_part))
            self.table.setItem(i, 3, QTableWidgetItem(str(row.get("user", {}).get("name", ""))))
            self.table.setItem(i, 4, QTableWidgetItem(str(row.get("user", {}).get("emp_no", ""))))
            self.table.setItem(i, 5, QTableWidgetItem(_meal_log_uid(row)))

            # 식사종류: 서버에서 내려준 해당 로그의 policy.meal_type 사용 (시간 재계산 시 번외로 바뀌는 현상 방지)
            meal_type = str(row.get("policy", {}).get("meal_type", "") or "번외")
            item_type = QTableWidgetItem(meal_type)
            if meal_type == "번외":
                item_type.setForeground(QColor("#94a3b8")) # Gray color for extra
            self.table.setItem(i, 6, item_type)
            self.table.setItem(i, 7, QTableWidgetItem(_meal_raw_data_status_label(row)))
        self.table.setUpdatesEnabled(True)
        self.table.setSortingEnabled(True)

    def load_policies(self):
        self.pol_loader = DataLoader(self.api.get_policies)
        self.pol_loader.finished.connect(self.on_policies_loaded)
        self.pol_loader.start()

    def on_policies_loaded(self, data):
        if not isinstance(data, list):
            return
        self.policies_list = data

    def _created_at_time_from_policy(self, policy_id):
        """선택한 식사 종류의 시작 시간 + 5분을 HH:mm:ss 로 반환. 없으면 12:00:00."""
        if not policy_id:
            return "12:00:00"
        policy = next((p for p in self.policies_list if p["id"] == policy_id), None)
        if not policy or not policy.get("start_time"):
            return "12:00:00"
        try:
            h, m = map(int, policy["start_time"].split(":")[:2])
            qtime = QTime(h, m).addSecs(300)
            return qtime.toString("HH:mm:ss")
        except Exception:
            return "12:00:00"

    def get_meal_type_by_time(self, time_str):
        if not time_str: return "번외"
        try:
            h, m, s = map(int, time_str.split(":"))
            target_time = QTime(h, m, s)
            
            for p in self.policies_list:
                if not p.get("start_time") or not p.get("end_time"): continue
                
                # Times from API are HH:MM:SS
                sh, sm, ss = map(int, p["start_time"].split(":")[:3])
                eh, em, es = map(int, p["end_time"].split(":")[:3])
                
                start = QTime(sh, sm, ss)
                end = QTime(eh, em, es)
                
                if start <= end:
                    if start <= target_time <= end:
                        return p["meal_type"]
                else: # Overnight
                    if target_time >= start or target_time <= end:
                        return p["meal_type"]
            return "번외"
        except Exception as e:
            print(f"Error judging meal type: {e}")
            return "번외"

    def _drop_other_row_selection(self, source, other):
        """한쪽 표를 고르면 다른 쪽 선택 표시를 되돌린다."""
        if self._exclusive_selecting:
            return
        sm = source.selectionModel()
        if sm is None or not sm.hasSelection():
            return
        self._exclusive_selecting = True
        try:
            other.clearSelection()
            other.setCurrentItem(None)
        finally:
            self._exclusive_selecting = False

    def _on_fail_table_selection_changed(self):
        self._drop_other_row_selection(self.fail_table, self.table)
        selected = self.fail_table.selectedItems()
        if not selected:
            return
        card_item = self.fail_table.item(selected[0].row(), 3)
        card = (card_item.text() if card_item else "").strip()
        if card == "-":
            card = ""
        self.fail_card_input.setText(card)

    def _selected_fail_log_id(self):
        selected = self.fail_table.selectedItems()
        if not selected:
            return None
        item = self.fail_table.item(selected[0].row(), 0)
        if item is None:
            return None
        return item.data(Qt.UserRole)

    def _selected_fail_when(self):
        selected = self.fail_table.selectedItems()
        if not selected:
            return "", ""
        row = selected[0].row()
        date_item = self.fail_table.item(row, 1)
        time_item = self.fail_table.item(row, 2)
        return (
            (date_item.text() if date_item else "").strip(),
            (time_item.text() if time_item else "").strip(),
        )

    def _policy_id_for_clock(self, time_str: str):
        meal = self.get_meal_type_by_time(time_str)
        if not meal or meal == "번외":
            return None, meal or "번외"
        for p in self.policies_list or []:
            if str(p.get("meal_type") or "") == meal:
                return p.get("id"), meal
        return None, meal

    def register_meal_from_fail_card(self, emp: dict, fail_when, dialog):
        """인증실패 시각과 조회된 사원으로 원시데이터(수기)를 등록한다."""
        date_s, time_s = fail_when or ("", "")
        ask = dialog if dialog is not None else self
        if not date_s or not time_s:
            dark_warning(
                ask,
                "원시데이터 등록",
                "인증실패 줄의 날짜·시간이 없습니다.\n실패 목록에서 줄을 선택한 뒤 조회하세요.",
            )
            if dialog is not None:
                dialog.set_register_enabled(True)
            return
        emp_no = str((emp or {}).get("emp_no") or "").strip()
        name = str((emp or {}).get("name") or "").strip()
        if not emp_no:
            dark_warning(ask, "원시데이터 등록", "사번이 없습니다.")
            if dialog is not None:
                dialog.set_register_enabled(True)
            return
        if len(time_s) == 5:
            time_s = time_s + ":00"
        policy_id, meal = self._policy_id_for_clock(time_s)
        if (
            dark_question(
                ask,
                "원시데이터 등록",
                "%s (%s)\n%s %s\n식사: %s\n\n원시데이터에 등록하고, 이 인증실패 기록은 삭제합니다. 계속할까요?"
                % (name, emp_no, date_s, time_s, meal),
            )
            != QMessageBox.Ok
        ):
            if dialog is not None:
                dialog.set_register_enabled(True)
            return
        payload = {
            "emp_no": emp_no,
            "policy_id": policy_id,
            "created_at": "%sT%s" % (date_s, time_s),
            "guest_count": 0,
        }
        fail_id = getattr(self, "_fail_lookup_log_id", None)

        def _job():
            ok, data = self.api.create_manual_raw_data(payload)
            if not ok:
                return False, data
            if fail_id is None:
                return True, data
            deleted, detail = self.api.delete_auth_failure_log(fail_id)
            if not deleted:
                return True, "원시데이터는 등록됐지만 인증실패 기록 삭제에 실패했습니다.\n%s" % detail
            return True, data

        self._fail_reg_dialog = dialog
        self._fail_reg_loader = DataLoader(_job)
        self._fail_reg_loader.finished.connect(self._on_fail_card_register_done)
        self._fail_reg_loader.error.connect(self._on_fail_card_register_err)
        self._fail_reg_loader.start()

    def _on_fail_card_register_done(self, result):
        dialog = getattr(self, "_fail_reg_dialog", None)
        ok, data = result if isinstance(result, tuple) else (False, result)
        if not ok:
            if dialog is not None:
                dialog.set_register_enabled(True)
            dark_warning(self, "원시데이터 등록", str(data))
            return
        if dialog is not None:
            dialog.accept()
        if isinstance(data, str) and data.strip():
            dark_warning(self, "원시데이터 등록", data)
        else:
            dark_information(self, "원시데이터 등록", "원시데이터에 등록하고 인증실패 기록은 삭제했습니다.")
        self.load_data()

    def _on_fail_card_register_err(self, err_msg):
        dialog = getattr(self, "_fail_reg_dialog", None)
        if dialog is not None:
            dialog.set_register_enabled(True)
        dark_warning(self, "원시데이터 등록", str(err_msg))

    def on_lookup_fail_card(self):
        card = self.fail_card_input.text().strip()
        if not card or card == "-":
            QMessageBox.information(self, "카드 조회", "카드번호를 입력하세요.")
            return
        if getattr(self, "_card_lookup_loader", None) is not None and self._card_lookup_loader.isRunning():
            return
        self.fail_card_lookup_btn.setEnabled(False)
        self._card_lookup_loader = DataLoader(self.api.find_employees_by_rf_card, card)
        self._card_lookup_loader.finished.connect(self._on_fail_card_lookup_done)
        self._card_lookup_loader.error.connect(self._on_fail_card_lookup_err)
        self._card_lookup_loader.start()

    def _on_fail_card_lookup_done(self, result):
        self.fail_card_lookup_btn.setEnabled(True)
        ok, data = result if isinstance(result, tuple) else (False, result)
        if not ok:
            QMessageBox.warning(self, "카드 조회", str(data))
            return
        rows = data if isinstance(data, list) else []
        card = self.fail_card_input.text().strip()
        self._fail_lookup_log_id = self._selected_fail_log_id()
        FailCardEmployeeDialog(card, rows, self, self._selected_fail_when()).exec_()
        self.fail_card_input.clear()

    def _on_fail_card_lookup_err(self, err_msg):
        self.fail_card_lookup_btn.setEnabled(True)
        QMessageBox.warning(self, "카드 조회", str(err_msg))

    def on_selection_changed(self):
        self._drop_other_row_selection(self.table, self.fail_table)
        selected = self.table.selectedItems()
        if not selected:
            self.current_log_id = None
            self.add_btn.setEnabled(True)
            self.edit_btn.setEnabled(False)
            self.del_btn.setEnabled(False)
            return

        row_idx = selected[0].row()
        item0 = self.table.item(row_idx, 0)
        log_id = item0.data(Qt.UserRole) if item0 else None
        if log_id is None:
            self.current_log_id = None
            self.edit_btn.setEnabled(False)
            self.del_btn.setEnabled(False)
            return
        log = next((x for x in self.full_data if x["id"] == log_id), None)
        if not log:
            return

        self.current_log_id = log["id"]
        voided = bool(log.get("is_void"))
        self.add_btn.setEnabled(True)
        self.edit_btn.setEnabled(not voided)
        self.del_btn.setEnabled(not voided)

    def clear_inputs(self):
        self.table.clearSelection()
        self.current_log_id = None
        self.add_btn.setEnabled(True)
        self.edit_btn.setEnabled(False)
        self.del_btn.setEnabled(False)

    def on_raw_excel(self):
        """원시데이터 테이블을 엑셀 파일로 저장 후 열기"""
        import os
        import tempfile
        import subprocess
        import sys
        try:
            import openpyxl
            from openpyxl.styles import Font, Alignment
        except ImportError:
            QMessageBox.warning(self, "오류", "엑셀 저장을 위해 openpyxl이 필요합니다.\npip install openpyxl")
            return
        start_str = self.start_date_edit.date().toString("yyyy-MM-dd")
        end_str = self.end_date_edit.date().toString("yyyy-MM-dd")
        title = "원시데이터"
        period_str = f"조회기간: {start_str} ~ {end_str}"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "원시데이터"
        col_count = self.table.columnCount()
        if col_count > 1:
            ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=col_count)
        ws.cell(row=1, column=1, value=title).font = Font(bold=True, size=16)
        ws.cell(row=1, column=1).alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 28
        ws.cell(row=2, column=1, value=period_str)
        for c in range(col_count):
            h = self.table.horizontalHeaderItem(c)
            ws.cell(row=3, column=c + 1, value=h.text() if h else "").font = Font(bold=True)
        for r in range(self.table.rowCount()):
            for c in range(col_count):
                item = self.table.item(r, c)
                val = item.text() if item else ""
                ws.cell(row=r + 4, column=c + 1, value=val)
        for i in range(1, col_count + 1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = 14
        try:
            fd, save_path = tempfile.mkstemp(suffix=".xlsx", prefix="RawData_")
            os.close(fd)
            wb.save(save_path)
        except Exception as e:
            QMessageBox.warning(self, "오류", f"엑셀 파일 생성 실패:\n{e}")
            return
        try:
            if sys.platform == "win32":
                os.startfile(save_path)
            elif sys.platform == "darwin":
                subprocess.run(["open", save_path], check=True)
            else:
                subprocess.run(["xdg-open", save_path], check=True)
        except Exception:
            pass

    def on_add(self):
        if not self.policies_list:
            QMessageBox.warning(
                self,
                "경고",
                "식사 정책이 없거나 아직 불러오는 중입니다. 잠시 후 다시 시도하세요.",
            )
            return
        dlg = RawDataRecordAddDialog(self)
        if dlg.exec_() != QDialog.Accepted:
            return
        data = dlg.build_payload()
        self.action_loader = DataLoader(self.api.create_manual_raw_data, data)
        self.action_loader.finished.connect(self.on_action_finished)
        self.action_loader.start()

    def on_update(self):
        if not self.current_log_id:
            return
        if not self.policies_list:
            QMessageBox.warning(
                self,
                "경고",
                "식사 정책이 없거나 아직 불러오는 중입니다. 잠시 후 다시 시도하세요.",
            )
            return
        log = next(
            (x for x in self.full_data if x.get("id") == self.current_log_id), None
        )
        if not log:
            QMessageBox.warning(self, "경고", "선택한 기록을 찾을 수 없습니다.")
            return
        if log.get("is_void"):
            QMessageBox.warning(self, "경고", "이미 삭제된 기록입니다.")
            return
        dlg = RawDataRecordAddDialog(self, edit_log=log)
        if dlg.exec_() != QDialog.Accepted:
            return
        data = dlg.build_payload()
        self.edit_btn.setEnabled(False)
        self.action_loader = DataLoader(self.api.update_raw_data, self.current_log_id, data)
        self.action_loader.finished.connect(self.on_action_finished)
        self.action_loader.start()

    def on_delete(self):
        if not self.current_log_id:
            return
        log = next((x for x in self.full_data if x.get("id") == self.current_log_id), None)
        if log and log.get("is_void"):
            QMessageBox.warning(self, "경고", "이미 삭제된 기록입니다.")
            return
        if (
            QMessageBox.question(
                self,
                "기록 삭제",
                "이 기록을 삭제 처리할까요?\n"
                "(데이터베이스에서는 물리 삭제되지 않으며, 식수 집계·보고서·대시보드에서는 제외됩니다.)",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            == QMessageBox.No
        ):
            return
        lid = self.current_log_id

        def _void_job():
            ok = self.api.void_log(lid, "원시데이터 기록삭제")
            return (ok, "삭제 처리되었습니다." if ok else "삭제 처리에 실패했습니다.")

        self.del_btn.setEnabled(False)
        self.action_loader = DataLoader(_void_job)
        self.action_loader.finished.connect(self.on_action_finished)
        self.action_loader.start()

    def on_action_finished(self, result):
        success = False
        message = ""
        if isinstance(result, tuple):
            success, detail = result[0], result[1] if len(result) > 1 else None
            if success:
                if isinstance(detail, str) and detail.strip():
                    message = detail.strip()
                else:
                    message = "등록/수정되었습니다."
            else:
                message = str(detail) if detail is not None else "실패"
        else:
            success = bool(result)
            message = "삭제되었습니다." if success else "삭제 실패"

        if success:
            QMessageBox.information(self, "성공", message)
            self.load_data()
            self.clear_inputs()
        else:
            QMessageBox.warning(self, "오류", f"작업 실패: {message}")
            self.edit_btn.setEnabled(True)
            self.del_btn.setEnabled(True)
    
    def on_void_header(self):
        selected = self.table.selectedItems()
        if not selected:
            QMessageBox.warning(self, "경고", "취소할 기록을 선택하세요.")
            return
        
        row_idx = selected[0].row()
        log_id = self.table.item(row_idx, 0).data(Qt.UserRole)
        if log_id is None:
            QMessageBox.warning(self, "경고", "선택한 행의 기록 ID를 찾을 수 없습니다.")
            return
        log = next((x for x in self.full_data if x.get("id") == log_id), None)
        if log is None:
            QMessageBox.warning(self, "경고", "선택한 기록을 찾을 수 없습니다.")
            return
        if log.get("is_void"):
            QMessageBox.warning(self, "경고", "이미 취소된 기록입니다.")
            return
            
        self.on_void(log_id)
    def on_void(self, log_id):
        reason, ok = QInputDialog.getText(self, "취소 사유", "취소 사유를 입력하세요:")
        if ok and reason:
            if self.api.void_log(log_id, reason):
                QMessageBox.information(self, "성공", "취소 처리가 완료되었습니다.")
                # Local UI update for performance
                selected = self.table.selectedItems()
                if selected:
                    row_idx = selected[0].row()
                    self.full_data[row_idx]["is_void"] = True
                    self.table.setItem(row_idx, 6, QTableWidgetItem("취소"))
            else:
                QMessageBox.warning(self, "오류", "처리에 실패했습니다.")

class PolicyScreen(QWidget):
    def __init__(self, api, main_win):
        super().__init__()
        self.api = api
        self.main_win = main_win
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        
        header_layout = QHBoxLayout()
        
        reg_frame = QFrame()
        reg_frame.setObjectName("StatCard")
        reg_layout = QHBoxLayout(reg_frame)
        self.type_input = QLineEdit()
        self.type_input.setPlaceholderText("식사 종류 (예: 조식)")
        self.type_input.setFixedWidth(250)
        self.start_input = QTimeEdit()
        self.start_input.setDisplayFormat("HH:mm:ss")
        self.start_input.setFixedWidth(180)
        self.end_input = QTimeEdit()
        self.end_input.setDisplayFormat("HH:mm:ss")
        self.end_input.setFixedWidth(180)
        self.price_input = QLineEdit()
        self.price_input.setPlaceholderText("단가 (원)")
        self.price_input.setFixedWidth(180)
        
        self.add_btn = QPushButton("등록")
        self.add_btn.setObjectName("PrimaryBtn")
        self.add_btn.setFixedWidth(100)
        self.add_btn.clicked.connect(self.on_add)
        
        self.edit_btn = QPushButton("수정")
        self.edit_btn.setObjectName("SecondaryBtn")
        self.edit_btn.setFixedWidth(100)
        self.edit_btn.setEnabled(False)
        self.edit_btn.clicked.connect(self.on_edit)
        
        self.del_btn = QPushButton("삭제")
        self.del_btn.setObjectName("DangerBtn")
        self.del_btn.setFixedWidth(100)
        self.del_btn.setEnabled(False)
        self.del_btn.clicked.connect(self.on_delete)
        
        reg_layout.addWidget(self.type_input)
        reg_layout.addWidget(self.start_input)
        reg_layout.addWidget(self.end_input)
        reg_layout.addWidget(self.price_input)
        reg_layout.addWidget(self.add_btn)
        reg_layout.addWidget(self.edit_btn)
        reg_layout.addWidget(self.del_btn)
        
        header_layout.addWidget(reg_frame)
        layout.addLayout(header_layout)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["식사 종류", "시작 시간", "종료 시간", "기본 단가"])
        setup_standard_table(self.table)
        self.table.itemSelectionChanged.connect(self.on_selection_changed)
        layout.addWidget(self.table)
        
        help_label = QLabel("* 시간 형식: HH:MM:SS (예: 12:00:00)")
        help_label.setStyleSheet("color: #94a3b8; font-size: 14px; font-weight: bold; font-family: 'Malgun Gothic';")
        layout.addWidget(help_label)
        layout.addStretch()

    def load_data(self):
        QApplication.setOverrideCursor(Qt.WaitCursor)
        self.loader = DataLoader(self.api.get_policies)
        self.loader.finished.connect(self.display_data)
        self.loader.error.connect(self._on_policy_load_error)
        self.loader.start()

    def _on_policy_load_error(self, err_msg: str):
        QApplication.restoreOverrideCursor()
        QMessageBox.warning(self, "식사 정책", "불러오기 실패:\n%s" % err_msg)

    def display_data(self, data):
        QApplication.restoreOverrideCursor()
        if not isinstance(data, list): return
        self.table.setSortingEnabled(False)
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(data))
        meal_names = {"breakfast": "조식", "lunch": "중식", "dinner": "석식"}
        
        for i, row in enumerate(data):
            if not isinstance(row, dict): continue
            meal_type = row.get("meal_type", "")
            row_id = row.get("id")
            item_meal = QTableWidgetItem(meal_type)
            item_meal.setData(Qt.UserRole, row_id)
            item_meal.setData(Qt.UserRole + 1, meal_type)
            self.table.setItem(i, 0, item_meal)
            self.table.setItem(i, 1, QTableWidgetItem(str(row.get("start_time", ""))))
            self.table.setItem(i, 2, QTableWidgetItem(str(row.get("end_time", ""))))

            item_price = QTableWidgetItem(str(row.get("base_price", 0)))
            self.table.setItem(i, 3, item_price)
        self.table.setUpdatesEnabled(True)
        self.table.setSortingEnabled(True)

    def on_selection_changed(self):
        selected = self.table.selectedItems()
        if selected:
            row = selected[0].row()
            item0 = self.table.item(row, 0)
            item1 = self.table.item(row, 1)
            item2 = self.table.item(row, 2)
            item3 = self.table.item(row, 3)
            meal_display = item0.text() if item0 else ""
            start_time = item1.text() if item1 else ""
            end_time = item2.text() if item2 else ""
            price = item3.text() if item3 else ""

            self.type_input.setText(meal_display)

            self.start_input.setTime(QTime.fromString(start_time, "HH:mm:ss"))
            self.end_input.setTime(QTime.fromString(end_time, "HH:mm:ss"))
            self.price_input.setText(price)

            self.edit_btn.setEnabled(item0 is not None and item0.data(Qt.UserRole) is not None)
            self.del_btn.setEnabled(item0 is not None and item0.data(Qt.UserRole) is not None)
        else:
            self.type_input.clear()
            self.start_input.setTime(QTime(0, 0, 0))
            self.end_input.setTime(QTime(0, 0, 0))
            self.price_input.clear()
            self.edit_btn.setEnabled(False)
            self.del_btn.setEnabled(False)

    def clear_inputs(self):
        self.type_input.clear()
        self.start_input.setTime(QTime(0, 0, 0))
        self.end_input.setTime(QTime(0, 0, 0))
        self.price_input.clear()
        self.table.clearSelection()
        self.edit_btn.setEnabled(False)
        self.del_btn.setEnabled(False)

    def on_add(self):
        meal_type = self.type_input.text().strip()
        start_time = self.start_input.time().toString("HH:mm:ss")
        end_time = self.end_input.time().toString("HH:mm:ss")
        price = self.price_input.text().strip()
        
        if not meal_type or not start_time or not end_time or not price:
            QMessageBox.warning(self, "경고", "모든 정보를 입력하세요.")
            return

        try:
            price_val = int(price)
            data = {
                "meal_type": meal_type,
                "start_time": start_time,
                "end_time": end_time,
                "base_price": price_val,
                "guest_price": price_val,
                "is_active": True
            }
            self.add_btn.setEnabled(False)
            self.loader = DataLoader(self.api.create_policy, data)
            self.loader.finished.connect(self.on_action_finished)
            self.loader.start()
        except ValueError:
            QMessageBox.warning(self, "경고", "단가는 숫자여야 합니다.")

    def on_edit(self):
        selected = self.table.selectedItems()
        if not selected: return
        row = selected[0].row()
        item0 = self.table.item(row, 0)
        pid = item0.data(Qt.UserRole) if item0 else None
        if pid is None: return
        
        meal_type = self.type_input.text().strip()
        start_time = self.start_input.time().toString("HH:mm:ss")
        end_time = self.end_input.time().toString("HH:mm:ss")
        price = self.price_input.text().strip()
        
        if not meal_type or not start_time or not end_time or not price:
            QMessageBox.warning(self, "경고", "모든 정보를 입력하세요.")
            return
        
        try:
            price_val = int(price)
            data = {
                "meal_type": meal_type,
                "start_time": start_time,
                "end_time": end_time,
                "base_price": price_val,
                "guest_price": price_val,
                "is_active": True
            }
            self.edit_btn.setEnabled(False)
            self.loader = DataLoader(self.api.update_policy, pid, data)
            self.loader.finished.connect(self.on_action_finished)
            self.loader.start()
        except ValueError:
            QMessageBox.warning(self, "경고", "단가는 숫자여야 합니다.")

    def on_delete(self):
        selected = self.table.selectedItems()
        if not selected: return
        row = selected[0].row()
        item0 = self.table.item(row, 0)
        pid = item0.data(Qt.UserRole) if item0 else None
        if pid is None: return
        meal_name = item0.text() if item0 else "(선택 행)"
        
        if QMessageBox.question(self, "확인", f"'{meal_name}' 정책을 삭제하시겠습니까?") == QMessageBox.Yes:
            self.del_btn.setEnabled(False)
            self.loader = DataLoader(self.api.delete_policy, pid)
            self.loader.finished.connect(self.on_delete_finished)
            self.loader.start()

    def on_action_finished(self, result):
        self.add_btn.setEnabled(True)
        self.edit_btn.setEnabled(True)
        if isinstance(result, tuple):
            success, data = result
        else:
            success, data = bool(result), None
        if success:
            QMessageBox.information(self, "성공", "작업이 완료되었습니다.")
            self.load_data()
            self.clear_inputs()
            if self.main_win:
                self.main_win.refresh_stats()
        else:
            msg = str(data) if data is not None else "작업에 실패했습니다."
            QMessageBox.warning(self, "오류", msg)

    def on_delete_finished(self, success):
        self.del_btn.setEnabled(True)
        if success:
            selected = self.table.selectedItems()
            if selected:
                self.table.removeRow(selected[0].row())
            QMessageBox.information(self, "성공", "삭제되었습니다.")
            self.load_data()
            self.clear_inputs()
            if self.main_win:
                self.main_win.refresh_stats()
        else:
            QMessageBox.warning(self, "오류", "삭제에 실패했습니다.")

class ReportScreen(QWidget):
    def __init__(self, api, main_win):
        super().__init__()
        self.api = api
        self.main_win = main_win
        self.full_data = []
        self.report_detail_employee = None  # 개인별 상세로 선택된 사원
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(20)
        
        # Header
        header_layout = QHBoxLayout()
        header = QLabel("식사 통계 보고서")
        header.setObjectName("HeaderTitle")
        header_layout.addWidget(header)
        header_layout.addStretch()
        layout.addLayout(header_layout)
        
        # Controls Frame
        controls_frame = QFrame()
        controls_frame.setObjectName("StatCard")
        controls_layout = QHBoxLayout(controls_frame)
        controls_layout.setSpacing(15)
        
        date_label = QLabel("조회 기간")
        date_label.setObjectName("InputLabel")
        
        self.start_date_edit = QDateEdit()
        self.start_date_edit.setCalendarPopup(True)
        today = QDate.currentDate()
        self.start_date_edit.setDate(QDate(today.year(), today.month(), 1))
        self.start_date_edit.setFixedWidth(180)
        
        tilde = QLabel("~")
        tilde.setStyleSheet("color: white; font-size: 18px; font-weight: bold;")
        
        self.end_date_edit = QDateEdit()
        self.end_date_edit.setCalendarPopup(True)
        self.end_date_edit.setDate(QDate.currentDate())
        self.end_date_edit.setFixedWidth(180)
        
        self.dept_sub_combo = QComboBox()
        self.dept_sub_combo.setFixedWidth(180)
        self._refresh_dept_sub_combo()
        self.dept_sub_combo.currentIndexChanged.connect(self._on_dept_combo_changed)
        
        # 개인별 상세: 레이블 + 검색 버튼 (원시데이터 사원조회와 동일한 검색 다이얼로그)
        self.report_detail_label = QLabel("개인별 상세")
        self.report_detail_label.setObjectName("InputLabel")
        self.report_detail_search_btn = QPushButton("사원검색")
        self.report_detail_search_btn.setObjectName("PrimaryBtn")
        self.report_detail_search_btn.setFixedWidth(100)
        self.report_detail_search_btn.clicked.connect(self._on_report_detail_search)
        self.report_detail_name_edit = QLineEdit()
        self.report_detail_name_edit.setPlaceholderText("검색 버튼을 클릭하세요...")
        self.report_detail_name_edit.setReadOnly(True)
        self.report_detail_name_edit.setFixedWidth(160)
        self.report_detail_name_edit.setMinimumHeight(36)
        
        self.download_btn = QPushButton("엑셀")
        self.download_btn.setObjectName("SecondaryBtn")
        self.download_btn.setToolTip("엑셀 다운로드")
        self.download_btn.setFixedWidth(70)
        self.download_btn.clicked.connect(self.on_download_excel)
        
        controls_layout.addWidget(date_label)
        controls_layout.addWidget(self.start_date_edit)
        controls_layout.addWidget(tilde)
        controls_layout.addWidget(self.end_date_edit)
        controls_layout.addSpacing(20)
        controls_layout.addWidget(self.dept_sub_combo)
        controls_layout.addSpacing(15)
        controls_layout.addWidget(self.report_detail_label)
        controls_layout.addWidget(self.report_detail_search_btn)
        controls_layout.addWidget(self.report_detail_name_edit)
        controls_layout.addStretch()
        controls_layout.addWidget(self.download_btn)
        
        layout.addWidget(controls_frame)
        
        # Table Area
        self.table = QTableWidget(0, 6)
        setup_standard_table(self.table)
        layout.addWidget(self.table, 1)  # stretch: 테이블이 남는 공간 차지

        # 합계 표시 (테이블 바로 아래, 항상 맨 마지막에 고정)
        footer_bar = QFrame()
        footer_bar.setObjectName("ReportFooterBar")
        footer_bar.setFixedHeight(40)
        footer_bar.setStyleSheet("QFrame#ReportFooterBar { background: transparent; }")
        footer_layout = QHBoxLayout(footer_bar)
        footer_layout.setContentsMargins(8, 4, 8, 4)
        footer_layout.addStretch()
        self.report_footer_label = QLabel("합계: 0(0)")
        self.report_footer_label.setObjectName("ReportFooterLabel")
        self.report_footer_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.report_footer_label.setMinimumWidth(320)
        self.report_footer_label.setSizePolicy(QSizePolicy.MinimumExpanding, QSizePolicy.Preferred)
        self.report_footer_label.setStyleSheet("font-weight: bold; padding: 4px 8px; color: #e2e8f0; font-size: 17px; font-family: 'Malgun Gothic', 'Segoe UI', sans-serif;")
        footer_layout.addWidget(self.report_footer_label)
        layout.addWidget(footer_bar)  # 테이블 다음에 추가 → 항상 하단

    def _refresh_dept_sub_combo(self):
        self.dept_sub_combo.clear()
        self.dept_sub_combo.addItem("개인별", "INDIVIDUAL")   # 개인별 → 상세 리스트(부서별과 동일 컬럼)
        self.dept_sub_combo.addItem("전체부서", None)         # 전체부서/부서별 → 상세 리스트 테이블
        depts = getattr(self.main_win, "departments_data", []) or []
        seen = set()
        for d in depts:
            if isinstance(d, dict):
                n = d.get("name") or "미지정"
                if n not in seen:
                    seen.add(n)
                    self.dept_sub_combo.addItem(n, d.get("code"))
        self.dept_sub_combo.setCurrentIndex(0)

    def _on_dept_combo_changed(self):
        self.report_detail_employee = None
        self.report_detail_name_edit.clear()
        self.display_data()

    def _on_report_detail_search(self):
        dialog = EmployeeSearchDialog(self.api, self.main_win, self)
        if dialog.exec_():
            emp = dialog.selected_employee
            if emp:
                self.report_detail_employee = emp
                self.report_detail_name_edit.setText(emp.get("name") or "")
                self.display_data()

    def reset_report(self):
        """다른 창 갔다 왔을 때 보고서 화면 초기화 (조회는 하지 않음)"""
        self.full_data = []
        self.report_detail_employee = None
        self.report_detail_name_edit.clear()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["날짜", "시간", "식사명", "부서", "이름", "사번"])
        self.report_footer_label.setText("합계: 0(0)")
        self.table.setSortingEnabled(True)

    def _employee_group_key(self, log):
        u = log.get("user") or {}
        if not isinstance(u, dict):
            u = {}
        return (
            str(u.get("emp_no") or u.get("empNo") or ""),
            str(u.get("name") or "미지정"),
            str(u.get("emp_no") or u.get("empNo") or ""),
        )

    def _meal_log_totals(self, log):
        guest = log.get("guest_count") or log.get("guestCount") or 0
        price = log.get("final_price") or log.get("finalPrice") or 0
        try:
            guest = int(guest)
        except (TypeError, ValueError):
            guest = 0
        try:
            price = int(price)
        except (TypeError, ValueError):
            price = 0
        meals = 1 + guest
        return price * meals, meals

    def _order_logs_by_employee(self, data):
        """A사원 전체 → 소계 → B사원 전체 → 소계 …"""
        groups = {}
        order = []
        for log in data:
            key = self._employee_group_key(log)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(log)
        order.sort(key=lambda k: (k[1], k[2]))
        rows = []
        for key in order:
            _eid, name, emp_no = key
            logs = sorted(groups[key], key=lambda lg: str(lg.get("created_at") or ""))
            rows.append({"kind": "header", "name": name, "emp_no": emp_no})
            emp_amount = 0
            emp_meals = 0
            for log in logs:
                amt, meals = self._meal_log_totals(log)
                emp_amount += amt
                emp_meals += meals
                rows.append({"kind": "log", "log": log})
            rows.append(
                {
                    "kind": "subtotal",
                    "name": name,
                    "emp_no": emp_no,
                    "amount": emp_amount,
                    "meals": emp_meals,
                }
            )
        return rows

    def _report_log_row_cells(self, log):
        u = log.get("user") or log.get("User") or {}
        if not isinstance(u, dict):
            u = {}
        created = log.get("created_at")
        if isinstance(created, dict):
            date_part = str(created.get("date", created.get("date_time", "")))[:10]
            time_part = str(created.get("time", ""))[:8]
        elif isinstance(created, str):
            created = created.strip()
            if "T" in created:
                date_part = created.split("T")[0]
                time_part = (created.split("T")[-1].replace("Z", "").strip())[:8]
            else:
                date_part = created[:10] if len(created) >= 10 else created
                time_part = ""
        else:
            date_part = ""
            time_part = ""
        meal_type = str((log.get("policy") or {}).get("meal_type") or "번외")
        dept_name = u.get("department_name")
        if not dept_name and isinstance(u.get("department"), dict):
            dept_name = (u.get("department") or {}).get("name")
        dept_name = str(dept_name or log.get("department_name") or "")
        name_str = str(u.get("name") or log.get("user_name") or "")
        emp_no_str = str(u.get("emp_no") or u.get("empNo") or log.get("emp_no") or "")
        return [date_part, time_part, meal_type, dept_name, name_str, emp_no_str]

    def _populate_detail_report_table(self, data, *, by_employee: bool = False):
        """날짜·시간·식사명·부서·이름·사번 상세 리스트.

        by_employee=True: 사원별 구역(헤더) → 해당 사원 조회기간 내역 → 다음 사원 …
        """
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["날짜", "시간", "식사명", "부서", "이름", "사번"])

        if by_employee:
            display_rows = self._order_logs_by_employee(data)
        else:
            sorted_logs = sorted(data, key=lambda log: str(log.get("created_at") or ""))
            display_rows = [{"kind": "log", "log": log} for log in sorted_logs]

        total_amount = 0
        total_meals = 0
        for row in display_rows:
            if row.get("kind") != "log":
                continue
            amt, meals = self._meal_log_totals(row["log"])
            total_amount += amt
            total_meals += meals

        self.table.setRowCount(len(display_rows))
        header_font = self.table.font()
        header_font.setBold(True)
        sub_font = self.table.font()
        sub_font.setBold(True)
        for i, row in enumerate(display_rows):
            if row.get("kind") == "header":
                title = row.get("name") or "미지정"
                emp_no = row.get("emp_no") or ""
                label = "%s (%s)" % (title, emp_no) if emp_no else title
                head = QTableWidgetItem("▶ " + label)
                head.setFont(header_font)
                head.setBackground(QColor("#334155"))
                head.setForeground(QColor("#f8fafc"))
                self.table.setItem(i, 0, head)
                for c in range(1, 6):
                    blank = QTableWidgetItem("")
                    blank.setBackground(QColor("#334155"))
                    self.table.setItem(i, c, blank)
                continue
            if row.get("kind") == "subtotal":
                label = "합계: %s(%s)" % (
                    "{:,}".format(int(row.get("amount") or 0)),
                    int(row.get("meals") or 0),
                )
                sub = QTableWidgetItem(label)
                sub.setFont(sub_font)
                sub.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                sub.setBackground(QColor("#1e293b"))
                sub.setForeground(QColor("#e2e8f0"))
                self.table.setItem(i, 4, sub)
                self.table.setSpan(i, 4, 1, 2)
                for c in (0, 1, 2, 3):
                    blank = QTableWidgetItem("")
                    blank.setBackground(QColor("#1e293b"))
                    self.table.setItem(i, c, blank)
                continue
            cells = self._report_log_row_cells(row["log"])
            for c, val in enumerate(cells):
                self.table.setItem(i, c, QTableWidgetItem(val))
        if by_employee:
            self.report_footer_label.setText(f"전체 합계: {total_amount:,}({total_meals})")
        else:
            self.report_footer_label.setText(f"합계: {total_amount:,}({total_meals})")
        return by_employee

    def load_data(self):
        start = self.start_date_edit.date().toString("yyyy-MM-dd")
        end = self.end_date_edit.date().toString("yyyy-MM-dd")
        
        self.main_win.statusBar().showMessage("데이터 조회 중...", 3000)
        
        # Get raw data for the range
        self.loader = DataLoader(self.api.get_raw_data, "", start, end)
        self.loader.finished.connect(self.on_data_loaded)
        self.loader.start()

    def on_data_loaded(self, data):
        if isinstance(data, tuple) and len(data) >= 2:
            success, payload = data[0], data[1]
            if not success:
                QMessageBox.warning(self, "오류", f"데이터를 가져오는데 실패했습니다.\n{payload}")
                return
            data = payload
        if not isinstance(data, list):
            QMessageBox.warning(self, "오류", "데이터를 가져오는데 실패했습니다.")
            return

        # Filter out voided logs
        self.full_data = [d for d in data if not d.get("is_void")]
        self.display_data()

    def display_data(self):
        # 전체/부서/개인별 모두 동일 상세 컬럼. 개인별+사원검색 시 해당 사원만 필터.
        self.table.setUpdatesEnabled(False)
        self.table.setSortingEnabled(False)
        self.table.clearSelection()

        if self.report_detail_employee:
            emp_no = str(self.report_detail_employee.get("emp_no") or "")
            data = [
                log for log in self.full_data
                if str((log.get("user") or {}).get("emp_no") or log.get("emp_no") or "") == emp_no
            ]
            by_emp = self._populate_detail_report_table(data, by_employee=False)
            self.table.setSortingEnabled(not by_emp)
            self.table.setUpdatesEnabled(True)
            return

        dept_id = self.dept_sub_combo.currentData()
        if dept_id in (None, "INDIVIDUAL"):
            data = self.full_data
        else:
            want = str(dept_id)
            data = []
            for log in self.full_data:
                u = log.get("user") or {}
                log_dept = str((u or {}).get("department_code") or "")
                if log_dept and log_dept == want:
                    data.append(log)

        by_emp = self._populate_detail_report_table(data, by_employee=(dept_id == "INDIVIDUAL"))
        self.table.setSortingEnabled(not by_emp)
        self.table.setUpdatesEnabled(True)

    def update_meal_type_summary(self):
        # Removed as requested
        pass

    def on_download_excel(self):
        # 현재 보고서 테이블을 엑셀 파일로 생성 후 열기 → 사용자가 보면서 직접 저장
        import os
        import tempfile
        import subprocess
        import sys

        try:
            import openpyxl
            from openpyxl.styles import Font, Alignment
        except ImportError:
            QMessageBox.warning(self, "오류", "엑셀 저장을 위해 openpyxl이 필요합니다.\npip install openpyxl")
            return

        dept_id = self.dept_sub_combo.currentData()
        if self.report_detail_employee:
            title = "개인별 상세 보고서 - " + (self.report_detail_employee.get("name") or "선택 사원")
        elif dept_id == "INDIVIDUAL":
            title = "개인별 보고서"
        elif dept_id is None:
            title = "전체부서 보고서"
        else:
            title = self.dept_sub_combo.currentText() + " 보고서"

        start_str = self.start_date_edit.date().toString("yyyy-MM-dd")
        end_str = self.end_date_edit.date().toString("yyyy-MM-dd")
        period_str = f"조회기간: {start_str} ~ {end_str}"

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "보고서"

        col_count = self.table.columnCount()

        # 1행: 타이틀 (전체 병합, 가운데, 글자 크게)
        if col_count > 1:
            ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=col_count)
        c1 = ws.cell(row=1, column=1, value=title)
        c1.font = Font(bold=True, size=16)
        c1.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 32

        # 2행: 첫 컬럼에 조회기간
        ws.cell(row=2, column=1, value=period_str)

        # 3행: 헤더
        for c in range(col_count):
            h = self.table.horizontalHeaderItem(c)
            ws.cell(row=3, column=c + 1, value=h.text() if h else "")
        header_font = Font(bold=True)
        for c in range(1, col_count + 1):
            ws.cell(row=3, column=c).font = header_font

        # 데이터 행
        right_align = Alignment(horizontal="right", vertical="center")
        for r in range(self.table.rowCount()):
            excel_row = r + 4
            row_texts = []
            for c in range(col_count):
                item = self.table.item(r, c)
                row_texts.append(item.text() if item else "")
            if len(row_texts) > 4 and row_texts[4].startswith("합계:"):
                if col_count > 4:
                    ws.merge_cells(start_row=excel_row, start_column=4, end_row=excel_row, end_column=col_count)
                cell = ws.cell(row=excel_row, column=4, value=row_texts[4])
                cell.font = Font(bold=True)
                cell.alignment = right_align
            else:
                for c in range(col_count):
                    ws.cell(row=excel_row, column=c + 1, value=row_texts[c])

        # 합계 행 (전체 병합, 우측 정렬)
        footer_text = self.report_footer_label.text()
        next_row = self.table.rowCount() + 4
        if col_count > 1:
            ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=col_count)
        footer_cell = ws.cell(row=next_row, column=1, value=footer_text)
        footer_cell.font = Font(bold=True)
        footer_cell.alignment = right_align

        # A4 양식에 맞춘 컬럼 너비 (조회기간이 첫 컬럼에 들어가도록)
        if col_count == 6:
            widths = [24, 10, 8, 12, 14, 20]
        else:
            widths = [12] * (col_count - 1) + [24]
        for i, w in enumerate(widths[:col_count], start=1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w

        # 임시 파일로 저장 후 엑셀 열기 (저장은 사용자가 엑셀에서 수행)
        try:
            fd, save_path = tempfile.mkstemp(suffix=".xlsx", prefix="MealReport_")
            os.close(fd)
            wb.save(save_path)
        except Exception as e:
            QMessageBox.warning(self, "오류", f"엑셀 파일 생성 실패:\n{e}")
            return

        try:
            if sys.platform == "win32":
                os.startfile(save_path)
            elif sys.platform == "darwin":
                subprocess.run(["open", save_path], check=True)
            else:
                subprocess.run(["xdg-open", save_path], check=True)
        except Exception:
            pass

    def on_download_finished(self, data, save_path):
        """엑셀 다운로드 완료 (로컬 생성 시)"""
        self.download_btn.setEnabled(True)
        if data:
            with open(save_path, "wb") as f:
                f.write(data)
            QMessageBox.information(self, "성공", f"Excel 보고서가 저장되었습니다.\n{save_path}")
        else:
            QMessageBox.warning(self, "오류", "Excel 보고서 다운로드에 실패했습니다.")


class AdminScreen(QWidget):
    """관리자 비번설정 화면: 등록·수정·삭제. 테이블: 로그인 ID, 이름, 인증."""
    def __init__(self, api, main_win):
        super().__init__()
        self.api = api
        self.main_win = main_win
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(20)

        header_layout = QHBoxLayout()
        header = QLabel("관리자 비번설정")
        header.setObjectName("HeaderTitle")
        header_layout.addWidget(header)
        header_layout.addStretch()
        layout.addLayout(header_layout)

        # 입력 행: 이름, 등록, 수정, 삭제 (로그인 ID는 등록 시 자동 부여: admin, admin2, …)
        input_row = QHBoxLayout()
        input_row.setSpacing(12)
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("이름")
        self.name_input.setFixedWidth(220)
        self.name_input.setMinimumHeight(44)
        input_row.addWidget(self.name_input)
        input_row.addSpacing(8)

        self.add_btn = QPushButton("등록")
        self.add_btn.setObjectName("PrimaryBtn")
        self.add_btn.setFixedWidth(100)
        self.add_btn.setMinimumHeight(44)
        self.add_btn.clicked.connect(self.on_add)
        self.edit_btn = QPushButton("수정")
        self.edit_btn.setObjectName("SecondaryBtn")
        self.edit_btn.setFixedWidth(100)
        self.edit_btn.setMinimumHeight(44)
        self.edit_btn.clicked.connect(self.on_edit)
        self.del_btn = QPushButton("삭제")
        self.del_btn.setObjectName("DangerBtn")
        self.del_btn.setFixedWidth(100)
        self.del_btn.setMinimumHeight(44)
        self.del_btn.clicked.connect(self.on_delete)

        input_row.addWidget(self.add_btn)
        input_row.addWidget(self.edit_btn)
        input_row.addWidget(self.del_btn)
        input_row.addStretch()
        layout.addLayout(input_row)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["로그인 ID", "이름", "인증"])
        setup_standard_table(self.table)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self.table, 1)

    def load_data(self):
        self.loader = DataLoader(self.api.get_admins)
        self.loader.finished.connect(self.on_loaded)
        self.loader.start()

    def on_loaded(self, data):
        if not isinstance(data, list):
            return
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(data))
        for i, row in enumerate(data):
            rid = row.get("id")
            self.table.setItem(i, 0, QTableWidgetItem(str(row.get("emp_no") or "")))
            self.table.setItem(i, 1, QTableWidgetItem(str(row.get("name") or "")))
            is_verified = row.get("is_verified", False)
            auth_text = "O" if is_verified else "X"
            item = QTableWidgetItem(auth_text)
            item.setData(Qt.UserRole, rid)
            if is_verified:
                item.setForeground(QColor("#10b981"))
            else:
                item.setForeground(QColor("#ef4444"))
            self.table.setItem(i, 2, item)
        self.table.setSortingEnabled(True)

    def _selected_row_id(self):
        sel = self.table.selectedItems()
        if not sel:
            return None
        row = sel[0].row()
        item = self.table.item(row, 2)
        if item:
            return item.data(Qt.UserRole)
        return None

    def _on_selection_changed(self):
        """테이블 선택 시 이름 입력창에 해당 행 값 표시."""
        rid = self._selected_row_id()
        if rid is None:
            return
        sel = self.table.selectedItems()
        if not sel:
            return
        r = sel[0].row()
        name = (self.table.item(r, 1).text() or "").strip()
        self.name_input.setText(name)

    def on_add(self):
        name = self.name_input.text().strip()
        if not name:
            QMessageBox.warning(self, "입력 오류", "이름을 입력하세요.")
            return
        success, result = self.api.create_admin_auto_login_id(name)
        if success:
            row = result if isinstance(result, dict) else {}
            lid = str(row.get("emp_no") or "")
            QMessageBox.information(
                self,
                "등록 완료",
                "관리자가 등록되었습니다.\n로그인 ID: %s\n초기 비밀번호: meal1234! (로그인 연동 시 사용, 변경 권장)"
                % (lid or "(자동)"),
            )
            self.name_input.clear()
            self.load_data()
        else:
            QMessageBox.warning(self, "오류", str(result))

    def on_edit(self):
        rid = self._selected_row_id()
        if rid is None:
            QMessageBox.warning(self, "선택 필요", "수정할 행을 선택하세요.")
            return
        name = self.name_input.text().strip()
        if not name:
            QMessageBox.warning(self, "입력 오류", "이름을 입력하세요.")
            return
        success, detail = self.api.update_admin(rid, name)
        if success:
            QMessageBox.information(self, "수정 완료", "수정되었습니다.")
            self.load_data()
        else:
            QMessageBox.warning(self, "오류", detail or "수정 실패")

    def on_delete(self):
        rid = self._selected_row_id()
        if rid is None:
            QMessageBox.warning(self, "선택 필요", "삭제할 행을 선택하세요.")
            return
        if QMessageBox.question(self, "삭제 확인", "이 관리자를 삭제하시겠습니까?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        if self.api.delete_admin(rid):
            QMessageBox.information(self, "삭제 완료", "삭제되었습니다.")
            self.load_data()
        else:
            QMessageBox.warning(self, "오류", "삭제에 실패했습니다.")


class SettingsScreen(QWidget):
    """장치 설정: 식권프린터·단말 출입 로그 삭제 시각·단말기 목록 (변경 시 자동 저장)."""

    @staticmethod
    def _settings_action_btn(btn: QPushButton, kind: str):
        oid = {"primary": "SettingsCompactPrimaryBtn", "secondary": "SettingsCompactSecondaryBtn", "danger": "SettingsCompactDangerBtn"}.get(kind, "SettingsCompactSecondaryBtn")
        btn.setObjectName(oid)

    @staticmethod
    def _settings_title_gap_px(cm: float = 1.0) -> int:
        try:
            screen = QApplication.primaryScreen()
            if screen is not None:
                return max(20, int(screen.logicalDotsPerInchY() * cm / 2.54))
        except Exception:
            pass
        return 38

    @staticmethod
    def _settings_section_row(hbox: QHBoxLayout, table: QTableWidget, form: QWidget, btns: QWidget):
        """테이블 · 입력 폼 · 세로 버튼(추가/수정/삭제)을 왼쪽→오른쪽으로 배치."""
        hbox.setAlignment(Qt.AlignTop)
        hbox.addWidget(table, 0, Qt.AlignTop | Qt.AlignLeft)
        hbox.addWidget(form, 0, Qt.AlignTop | Qt.AlignLeft)
        hbox.addWidget(btns, 0, Qt.AlignTop | Qt.AlignLeft)

    @staticmethod
    def _settings_action_btn_column(*buttons: QPushButton) -> QWidget:
        """추가/수정/삭제를 섹션 오른쪽에 세로로 나열 (좌우 라운드가 잘리지 않도록 여백 확보)."""
        host = QWidget()
        host.setFixedWidth(100)
        v = QVBoxLayout(host)
        v.setContentsMargins(0, 0, 4, 0)
        v.setSpacing(6)
        v.setAlignment(Qt.AlignTop)
        for b in buttons:
            b.setFixedWidth(80)
            b.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            v.addWidget(b, 0, Qt.AlignLeft)
        v.addStretch(1)
        return host

    @staticmethod
    def _settings_form_label(text: str) -> QLabel:
        """입력폼 제목(IP, 포트 등) — 어두운 섹션 배경에서도 보이도록."""
        lab = QLabel(text)
        lab.setObjectName("SettingsFormLabel")
        return lab

    @staticmethod
    def _status_on_off_item(ok: bool) -> QTableWidgetItem:
        item = QTableWidgetItem("ON" if ok else "OFF")
        item.setTextAlignment(Qt.AlignCenter)
        item.setForeground(QColor("#4ade80" if ok else "#f87171"))
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        return item

    def _terminal_ok_map(self) -> dict:
        """device_no / ip → 연결 여부 (최근 ping/ADMS 리포트 기준)."""
        rep = getattr(self, "_last_conn_rep", None)
        if not isinstance(rep, dict) and self.main_win is not None:
            rep = getattr(self.main_win, "_last_terminal_ping_rep", None)
        out = {"by_no": {}, "by_ip": {}}
        if not isinstance(rep, dict):
            return out
        rows = rep.get("status_rows")
        if not isinstance(rows, list):
            return out
        for r in rows:
            if not isinstance(r, dict):
                continue
            ok = bool(r.get("ok", False))
            dno = str(r.get("device_no") or "").strip()
            ip = str(r.get("ip") or "").strip()
            if dno:
                out["by_no"][dno] = ok
            if ip:
                out["by_ip"][ip] = ok
        # f22 단말 목록과 1:1 매칭 폴백 (status_rows 에 device_no 없을 때)
        if not out["by_no"] and not out["by_ip"]:
            terminals = getattr(self, "_f22_terminals_data", None) or []
            for i, t in enumerate(terminals):
                if not isinstance(t, dict):
                    continue
                ok = False
                if i < len(rows) and isinstance(rows[i], dict):
                    ok = bool(rows[i].get("ok", False))
                dno = str(t.get("device_no") or "").strip()
                ip = str(t.get("ip") or t.get("host") or "").strip()
                if dno:
                    out["by_no"][dno] = ok
                if ip:
                    out["by_ip"][ip] = ok
        return out

    def _is_terminal_connected(self, device_no: str = "", ip: str = "") -> bool:
        m = self._terminal_ok_map()
        dno = str(device_no or "").strip()
        ip_s = str(ip or "").strip()
        if dno and dno in m["by_no"]:
            return bool(m["by_no"][dno])
        if ip_s and ip_s in m["by_ip"]:
            return bool(m["by_ip"][ip_s])
        return False

    def __init__(self, api, main_win):
        super().__init__()
        self.setObjectName("SettingsScreen")
        self.api = api
        self.main_win = main_win
        self._last_conn_rep = None
        self._device_online = {}
        self._aux_probe_loader = None
        self._aux_probe_pending = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 12)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignTop | Qt.AlignLeft)

        header = QLabel("설정")
        header.setObjectName("SettingsPageTitle")
        header.setContentsMargins(0, 0, 0, 0)
        header.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        layout.addWidget(header, 0, Qt.AlignLeft | Qt.AlignTop)

        layout.addSpacing(self._settings_title_gap_px(0.6))

        settings_body = QWidget()
        settings_body.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        body_layout = QVBoxLayout(settings_body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(12)
        body_layout.setAlignment(Qt.AlignTop | Qt.AlignLeft)

        settings_table_w = 559  # 540 + ~5mm(19px)
        settings_form_w = 220
        settings_btn_w = 100
        # 좌우 마진 12+20 + 간격 10*2 + 테두리/라운드 여유
        settings_section_w = settings_table_w + settings_form_w + settings_btn_w + 64
        settings_form_spacing = 6

        grp_style = (
            "QGroupBox { color: #f8fafc; font-weight: bold; font-size: 18px; "
            "padding-top: 18px; margin-top: 4px; background-color: #1e293b; "
            "border: 1px solid #334155; border-radius: 12px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 6px; }"
        )

        def _make_section(title: str) -> QGroupBox:
            g = QGroupBox(title)
            g.setStyleSheet(grp_style)
            g.setMinimumWidth(settings_section_w)
            g.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
            return g

        # —— 세로: 식권프린터 → 경광등 → 단말기 → 단말 연결 상태 ——
        # —— 식권프린터 ——
        printer_group = _make_section("식권프린터")
        printer_h = QHBoxLayout(printer_group)
        printer_h.setContentsMargins(12, 8, 20, 12)
        printer_h.setSpacing(10)
        self.printer_table = QTableWidget(0, 5)
        self.printer_table.setHorizontalHeaderLabels(["No", "연결 단말기", "IP", "포트", "상태"])
        setup_settings_device_table(self.printer_table, settings_table_w)
        self.printer_table.itemSelectionChanged.connect(self._on_printer_selection_changed)
        printer_form = QWidget()
        printer_form.setFixedWidth(settings_form_w)
        printer_rv = QVBoxLayout(printer_form)
        printer_rv.setContentsMargins(0, 0, 0, 0)
        printer_rv.setSpacing(6)
        printer_rv.setAlignment(Qt.AlignTop)
        self.printer_enabled = QCheckBox("프린터 사용")
        self.printer_enabled.stateChanged.connect(self._on_printer_state_changed)
        printer_rv.addWidget(self.printer_enabled)
        pform = QFormLayout()
        pform.setSpacing(settings_form_spacing)
        pform.setVerticalSpacing(settings_form_spacing)
        pform.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        pform.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.printer_ip_edit = QLineEdit()
        self.printer_ip_edit.setPlaceholderText("IP 주소")
        self.printer_port_edit = QLineEdit()
        self.printer_port_edit.setPlaceholderText("포트")
        self.printer_port_edit.setText("9100")
        self.printer_terminal_combo = QComboBox()
        self.printer_terminal_combo.setMaxVisibleItems(12)
        pform.addRow(self._settings_form_label("IP:"), self.printer_ip_edit)
        pform.addRow(self._settings_form_label("포트:"), self.printer_port_edit)
        printer_linked_lbl = self._settings_form_label("연결단말기")
        pform.addRow(printer_linked_lbl, self.printer_terminal_combo)
        printer_rv.addLayout(pform)
        printer_rv.addStretch(1)
        self.printer_add_btn = QPushButton("추가")
        self._settings_action_btn(self.printer_add_btn, "primary")
        self.printer_add_btn.clicked.connect(self._printer_add_or_apply)
        self.printer_edit_btn = QPushButton("수정")
        self._settings_action_btn(self.printer_edit_btn, "secondary")
        self.printer_edit_btn.clicked.connect(self._printer_edit_row)
        self.printer_del_btn = QPushButton("삭제")
        self._settings_action_btn(self.printer_del_btn, "danger")
        self.printer_del_btn.clicked.connect(self._printer_delete_row)
        printer_btns = self._settings_action_btn_column(
            self.printer_add_btn, self.printer_edit_btn, self.printer_del_btn
        )
        self._settings_section_row(printer_h, self.printer_table, printer_form, printer_btns)
        body_layout.addWidget(printer_group, 0, Qt.AlignLeft | Qt.AlignTop)
        self._printer_devices_data = []
        self._printer_editing_index = None

        # —— 경광등 (Q-Light, 단말기별 연결) ——
        qlight_group = _make_section("경광등 (Q-Light)")
        qlight_h = QHBoxLayout(qlight_group)
        qlight_h.setContentsMargins(12, 8, 20, 12)
        qlight_h.setSpacing(10)
        self.qlight_table = QTableWidget(0, 5)
        self.qlight_table.setHorizontalHeaderLabels(
            ["No", "연결 단말기", "IP", "포트", "상태"]
        )
        setup_settings_device_table(self.qlight_table, settings_table_w)
        self.qlight_table.itemSelectionChanged.connect(self._on_qlight_selection_changed)
        qlight_form = QWidget()
        qlight_form.setFixedWidth(settings_form_w)
        qlight_rv = QVBoxLayout(qlight_form)
        qlight_rv.setContentsMargins(0, 0, 0, 0)
        qlight_rv.setSpacing(6)
        qlight_rv.setAlignment(Qt.AlignTop)
        self.qlight_enabled = QCheckBox("경광등 사용")
        self.qlight_enabled.stateChanged.connect(self._on_qlight_state_changed)
        qlight_rv.addWidget(self.qlight_enabled)
        qform = QFormLayout()
        qform.setSpacing(settings_form_spacing)
        qform.setVerticalSpacing(settings_form_spacing)
        qform.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        qform.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.qlight_ip_edit = QLineEdit()
        self.qlight_ip_edit.setPlaceholderText("경광등 IP")
        self.qlight_port_edit = QLineEdit()
        self.qlight_port_edit.setPlaceholderText("포트")
        self.qlight_port_edit.setText("20000")
        self.qlight_terminal_combo = QComboBox()
        self.qlight_terminal_combo.setMaxVisibleItems(12)
        qform.addRow(self._settings_form_label("IP:"), self.qlight_ip_edit)
        qform.addRow(self._settings_form_label("포트:"), self.qlight_port_edit)
        qlight_linked_lbl = self._settings_form_label("연결단말기")
        qform.addRow(qlight_linked_lbl, self.qlight_terminal_combo)
        qlight_rv.addLayout(qform)
        qlight_rv.addStretch(1)
        self.qlight_add_btn = QPushButton("추가")
        self._settings_action_btn(self.qlight_add_btn, "primary")
        self.qlight_add_btn.clicked.connect(self._qlight_add_or_apply)
        self.qlight_edit_btn = QPushButton("수정")
        self._settings_action_btn(self.qlight_edit_btn, "secondary")
        self.qlight_edit_btn.clicked.connect(self._qlight_edit_row)
        self.qlight_del_btn = QPushButton("삭제")
        self._settings_action_btn(self.qlight_del_btn, "danger")
        self.qlight_del_btn.clicked.connect(self._qlight_delete_row)
        qlight_btns = self._settings_action_btn_column(
            self.qlight_add_btn, self.qlight_edit_btn, self.qlight_del_btn
        )
        self._settings_section_row(qlight_h, self.qlight_table, qlight_form, qlight_btns)
        body_layout.addWidget(qlight_group, 0, Qt.AlignLeft | Qt.AlignTop)
        self._qlight_devices_data = []
        self._qlight_editing_index = None

        # —— 단말기 ——
        f22_group = _make_section("단말기")
        self.f22_group = f22_group
        f22_h = QHBoxLayout(f22_group)
        f22_h.setContentsMargins(12, 8, 20, 12)
        f22_h.setSpacing(10)
        self.f22_table = QTableWidget(0, 6)
        self.f22_table.setHorizontalHeaderLabels(
            ["No", "단말기번호", "IP", "포트", "설치위치", "상태"]
        )
        setup_settings_device_table(self.f22_table, settings_table_w)
        self.f22_table.itemSelectionChanged.connect(self._on_f22_selection_changed)
        f22_form_w = QWidget()
        f22_form_w.setFixedWidth(settings_form_w)
        f22_rv = QVBoxLayout(f22_form_w)
        f22_rv.setContentsMargins(0, 0, 0, 0)
        f22_rv.setSpacing(6)
        f22_rv.setAlignment(Qt.AlignTop)
        f22_form = QFormLayout()
        f22_form.setSpacing(settings_form_spacing)
        f22_form.setVerticalSpacing(settings_form_spacing)
        f22_form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        f22_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.f22_device_no = QLineEdit()
        self.f22_device_no.setPlaceholderText("단말기 번호")
        self.f22_ip = QLineEdit()
        self.f22_ip.setPlaceholderText("IP 주소")
        self.f22_port_line = QLineEdit()
        self.f22_port_line.setPlaceholderText("포트")
        self.f22_port_line.setText("4370")
        self.f22_location = QLineEdit()
        self.f22_location.setPlaceholderText("설치 위치")
        self.f22_port_label = self._settings_form_label("포트:")
        f22_form.addRow(self._settings_form_label("단말기번호:"), self.f22_device_no)
        f22_form.addRow(self._settings_form_label("IP:"), self.f22_ip)
        f22_form.addRow(self.f22_port_label, self.f22_port_line)
        f22_form.addRow(self._settings_form_label("설치위치:"), self.f22_location)
        f22_rv.addLayout(f22_form)
        f22_rv.addStretch(1)
        self.f22_add_btn = QPushButton("추가")
        self._settings_action_btn(self.f22_add_btn, "primary")
        self.f22_add_btn.clicked.connect(self._f22_add_or_apply)
        self.f22_edit_btn = QPushButton("수정")
        self._settings_action_btn(self.f22_edit_btn, "secondary")
        self.f22_edit_btn.clicked.connect(self._f22_edit_row)
        self.f22_del_btn = QPushButton("삭제")
        self._settings_action_btn(self.f22_del_btn, "danger")
        self.f22_del_btn.clicked.connect(self._f22_delete_row)
        f22_btns = self._settings_action_btn_column(
            self.f22_add_btn, self.f22_edit_btn, self.f22_del_btn
        )
        self._settings_section_row(f22_h, self.f22_table, f22_form_w, f22_btns)
        body_layout.addWidget(f22_group, 0, Qt.AlignLeft | Qt.AlignTop)
        self._f22_terminals_data = []
        self._f22_editing_index = None

        conn_grp_style = (
            "QGroupBox { color: #f8fafc; font-weight: bold; font-size: 18px; "
            "padding-top: 18px; margin-top: 4px; background-color: #1e293b; "
            "border: 1px solid #334155; border-radius: 12px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 6px; }"
        )
        self.f22_conn_status_group = QGroupBox("단말 연결 상태")
        self.f22_conn_status_group.setStyleSheet(conn_grp_style)
        self.f22_conn_status_group.setMinimumWidth(settings_section_w)
        self.f22_conn_status_group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self._f22_conn_status_layout = QVBoxLayout(self.f22_conn_status_group)
        self._f22_conn_status_layout.setContentsMargins(12, 10, 12, 12)
        self._f22_conn_status_layout.setSpacing(4)
        self.f22_conn_status_hint = QLabel("저장·연결 확인 또는 자동 갱신으로 상태가 표시됩니다.")
        self.f22_conn_status_hint.setStyleSheet("color: #94a3b8; font-size: 14px; font-weight: bold;")
        self._f22_conn_status_layout.addWidget(self.f22_conn_status_hint)
        self._f22_conn_status_rows_host = QWidget()
        self._f22_conn_status_rows_layout = QVBoxLayout(self._f22_conn_status_rows_host)
        self._f22_conn_status_rows_layout.setContentsMargins(0, 0, 0, 0)
        self._f22_conn_status_rows_layout.setSpacing(4)
        self._f22_conn_status_layout.addWidget(self._f22_conn_status_rows_host)
        body_layout.addWidget(self.f22_conn_status_group, 0, Qt.AlignLeft | Qt.AlignTop)

        self._refresh_printer_table()
        self._refresh_qlight_table()
        self._refresh_f22_table()
        self._refresh_linked_terminal_combos()

        actions_row = QHBoxLayout()
        actions_row.setSpacing(10)
        self.device_save_ping_btn = QPushButton("저장·단말 연결 확인")
        self.device_save_ping_btn.setObjectName("SettingsSaveBtn")
        self.device_save_ping_btn.setToolTip(
            "DB에 장치 설정을 저장한 뒤, 등록된 F22 단말기(IP)에 접속해 연결 여부를 확인합니다."
        )
        self.device_save_ping_btn.clicked.connect(self._on_save_and_ping_f22)
        actions_row.addWidget(self.device_save_ping_btn)
        self.f22_resync_all_rb = QRadioButton("등록 단말 전체")
        self.f22_resync_sel_rb = QRadioButton("선택 단말만")
        self.f22_resync_sel_rb.setToolTip("선택 시 단말 선택 창이 열립니다.")
        self.f22_resync_all_rb.setChecked(True)
        self.f22_resync_group = QButtonGroup(self)
        self.f22_resync_group.addButton(self.f22_resync_all_rb)
        self.f22_resync_group.addButton(self.f22_resync_sel_rb)
        self.f22_resync_group.buttonClicked.connect(self._on_f22_resync_mode_changed)
        actions_row.addWidget(self.f22_resync_all_rb)
        actions_row.addWidget(self.f22_resync_sel_rb)
        self._f22_resync_selected_terminal = None
        self.f22_bulk_resync_btn = QPushButton("단말 재동기화")
        self.f22_bulk_resync_btn.setObjectName("SettingsActionBtn")
        self.f22_bulk_resync_btn.setToolTip(
            "재직 사원 중 카드·지문이 있는 사원을 단말에 맞춥니다. "
            "DB에 없는 사번, 퇴사자, 카드·지문이 없는 사번은 단말에서 삭제합니다."
        )
        self.f22_bulk_resync_btn.clicked.connect(self._on_f22_bulk_resync)
        actions_row.addWidget(self.f22_bulk_resync_btn)
        actions_row.addStretch()
        body_layout.addLayout(actions_row)

        layout.addWidget(settings_body, 0, Qt.AlignLeft | Qt.AlignTop)
        layout.addStretch(1)
        self._f22_bulk_resync_thread = None
        self._f22_bulk_resync_dialog = None
        self._sync_f22_ui_for_att_mode()

    def update_connection_status(self, rep: dict):
        """설정 탭 단말 연결 상태 패널 (사이드바와 동일 내용) + 테이블 상태 열 갱신."""
        if isinstance(rep, dict):
            self._last_conn_rep = dict(rep)
        lay = getattr(self, "_f22_conn_status_rows_layout", None)
        if lay is None:
            return
        hint = getattr(self, "f22_conn_status_hint", None)
        if hint is not None:
            hint.hide()
        conn_ic = disconn_ic = None
        if self.main_win is not None:
            conn_ic = getattr(self.main_win, "_term_conn_icon", None)
            disconn_ic = getattr(self.main_win, "_term_disconn_icon", None)
        render_terminal_status_rows(lay, rep, conn_ic, disconn_ic, compact=True)
        # 단말 표는 단말 연결, 프린터·경광등은 각 IP 접속 결과
        self._refresh_printer_table()
        self._refresh_qlight_table()
        self._refresh_f22_table()
        self._schedule_aux_device_probe()

    def refresh_connection_status_now(self):
        if self.main_win is not None:
            self.main_win.refresh_terminal_status_async(force=True)

    def _sync_f22_ui_for_att_mode(self):
        try:
            from meal_db.connection import load_meal_db_dotenv

            load_meal_db_dotenv()
        except Exception:
            pass
        adms = _f22_att_mode_is_adms()
        if adms:
            self.f22_group.setTitle("단말기 (ADMS PUSH)")
            self.f22_port_label.setVisible(False)
            self.f22_port_line.setVisible(False)
            self.device_save_ping_btn.setText("저장·ADMS 연결 확인")
            self.device_save_ping_btn.setToolTip(
                "장치 설정 저장 후 ADMS 연결(registry) 상태 확인. 연결 서버는 앱 시작 시 자동 기동."
            )
            self.f22_conn_status_group.setTitle("단말 연결 상태 (ADMS)")
            self.f22_table.setHorizontalHeaderLabels(
                ["No", "단말기번호", "IP", "포트", "설치위치"]
            )
            self.f22_table.setColumnHidden(3, True)
        else:
            self.f22_group.setTitle("단말기")
            self.f22_port_label.setVisible(True)
            self.f22_port_line.setVisible(True)
            self.device_save_ping_btn.setText("저장·단말 연결 확인")
            self.device_save_ping_btn.setToolTip(
                "DB에 장치 설정을 저장한 뒤, 등록된 F22 단말기(IP)에 접속해 연결 여부를 확인합니다."
            )
            self.f22_conn_status_group.setTitle("단말 연결 상태")
            self.f22_table.setHorizontalHeaderLabels(
                ["No", "단말기번호", "IP", "포트", "설치위치"]
            )
            self.f22_table.setColumnHidden(3, False)
        self._refresh_f22_table()
        rep = getattr(self.main_win, "_last_terminal_ping_rep", None) if self.main_win else None
        if isinstance(rep, dict):
            self.update_connection_status(rep)

    def _registered_terminal_nos(self):
        out = []
        seen = set()
        for t in self._f22_terminals_data:
            d = str(t.get("device_no") or "").strip()
            if d and d not in seen:
                seen.add(d)
                out.append(d)
        return out

    def _fill_terminal_combo(self, combo: QComboBox, select_value: str):
        select_value = str(select_value or "").strip()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("(없음)", "")
        for n in self._registered_terminal_nos():
            combo.addItem(n, n)
        if select_value and select_value not in self._registered_terminal_nos():
            combo.addItem("%s (미등록)" % select_value, select_value)
        idx = 0
        for i in range(combo.count()):
            data = combo.itemData(i)
            dv = "" if data is None else str(data)
            if dv == select_value:
                idx = i
                break
        combo.setCurrentIndex(idx)
        combo.blockSignals(False)

    def _refresh_linked_terminal_combos(self):
        ps = self.printer_terminal_combo.currentData()
        ps = "" if ps is None else str(ps).strip()
        self._fill_terminal_combo(self.printer_terminal_combo, ps)
        qs = self.qlight_terminal_combo.currentData()
        qs = "" if qs is None else str(qs).strip()
        self._fill_terminal_combo(self.qlight_terminal_combo, qs)

    def _apply_qlight_enables(self):
        en = self.qlight_enabled.isChecked()
        for w in (
            self.qlight_table,
            self.qlight_ip_edit,
            self.qlight_port_edit,
            self.qlight_terminal_combo,
            self.qlight_add_btn,
            self.qlight_edit_btn,
            self.qlight_del_btn,
        ):
            w.setEnabled(en)

    def _on_qlight_state_changed(self):
        self._apply_qlight_enables()
        self.save_settings(quiet=True)

    def _refresh_qlight_table(self):
        self.qlight_table.setSortingEnabled(False)
        self.qlight_table.setRowCount(len(self._qlight_devices_data))
        for i, row in enumerate(self._qlight_devices_data):
            no_item = QTableWidgetItem()
            no_item.setData(Qt.DisplayRole, i + 1)
            no_item.setData(Qt.UserRole, i)
            no_item.setTextAlignment(Qt.AlignCenter)
            self.qlight_table.setItem(i, 0, no_item)
            linked = str(row.get("linked_terminal", ""))
            self.qlight_table.setItem(i, 1, QTableWidgetItem(linked))
            self.qlight_table.setItem(i, 2, QTableWidgetItem(str(row.get("ip", ""))))
            self.qlight_table.setItem(i, 3, QTableWidgetItem(str(row.get("port", ""))))
            ok = self._is_aux_device_online(row.get("ip"), row.get("port"))
            self.qlight_table.setItem(i, 4, self._aux_status_item(ok))
        self.qlight_table.setSortingEnabled(True)

    def _qlight_data_index_from_current_row(self):
        r = self.qlight_table.currentRow()
        if r < 0:
            return -1
        it = self.qlight_table.item(r, 0)
        if it is None:
            return -1
        v = it.data(Qt.UserRole)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
        if r < len(self._qlight_devices_data):
            return r
        return -1

    def _load_qlight_row_into_form(self, data_i: int):
        t = self._qlight_devices_data[data_i]
        self.qlight_ip_edit.setText(str(t.get("ip", "")))
        self.qlight_port_edit.setText(str(t.get("port", 20000)))
        self._fill_terminal_combo(self.qlight_terminal_combo, str(t.get("linked_terminal", "")))
        self._qlight_editing_index = data_i
        self.qlight_add_btn.setText("적용")

    def _on_qlight_selection_changed(self):
        di = self._qlight_data_index_from_current_row()
        if di < 0 or di >= len(self._qlight_devices_data):
            self.qlight_ip_edit.clear()
            self.qlight_port_edit.setText("20000")
            self._fill_terminal_combo(self.qlight_terminal_combo, "")
            self._qlight_editing_index = None
            self.qlight_add_btn.setText("추가")
            return
        self._load_qlight_row_into_form(di)

    def _clear_qlight_form(self):
        self.qlight_ip_edit.clear()
        self.qlight_port_edit.setText("20000")
        self._fill_terminal_combo(self.qlight_terminal_combo, "")
        self._qlight_editing_index = None
        self.qlight_add_btn.setText("추가")
        self.qlight_table.clearSelection()

    def _qlight_add_or_apply(self):
        ip = (self.qlight_ip_edit.text() or "").strip()
        pv = self._parse_port_line(self.qlight_port_edit.text(), 20000)
        if not ip:
            QMessageBox.warning(self, "입력", "경광등 IP를 입력하세요.")
            return
        if pv is None:
            QMessageBox.warning(self, "입력", "포트는 1~65535 사이 숫자여야 합니다.")
            return
        raw_lt = self.qlight_terminal_combo.currentData()
        linked = "" if raw_lt is None else str(raw_lt).strip()
        if not linked:
            QMessageBox.warning(self, "입력", "연결할 단말기 번호를 선택하세요.")
            return
        entry = {
            "ip": ip,
            "port": pv,
            "linked_terminal": linked,
            "on_success": True,
            "on_failure": True,
        }
        if self._qlight_editing_index is not None and 0 <= self._qlight_editing_index < len(self._qlight_devices_data):
            self._qlight_devices_data[self._qlight_editing_index] = entry
            self._qlight_editing_index = None
        else:
            for row in self._qlight_devices_data:
                if str(row.get("linked_terminal") or "").strip() == linked:
                    QMessageBox.warning(self, "중복", "이미 해당 단말기에 경광등이 등록되어 있습니다. 수정하세요.")
                    return
            self._qlight_devices_data.append(entry)
        self._refresh_qlight_table()
        self._clear_qlight_form()
        self.save_settings(quiet=True)

    def _qlight_edit_row(self):
        if self._qlight_editing_index is not None:
            self._qlight_add_or_apply()
            return
        di = self._qlight_data_index_from_current_row()
        if di < 0 or di >= len(self._qlight_devices_data):
            QMessageBox.information(self, "선택", "수정할 경광등 행을 선택하세요.")
            return
        self._load_qlight_row_into_form(di)

    def _qlight_delete_row(self):
        di = self._qlight_data_index_from_current_row()
        if di < 0 or di >= len(self._qlight_devices_data):
            QMessageBox.information(self, "선택", "삭제할 경광등 행을 선택하세요.")
            return
        if QMessageBox.question(self, "확인", "선택한 경광등 등록을 삭제하시겠습니까?") != QMessageBox.Yes:
            return
        del self._qlight_devices_data[di]
        self._qlight_editing_index = None
        self._refresh_qlight_table()
        self._clear_qlight_form()
        self.save_settings(quiet=True)

    def _load_qlight_from_data(self, data: dict):
        self._qlight_devices_data = []
        qd = data.get("qlight_devices")
        if isinstance(qd, list):
            for row in qd:
                if not isinstance(row, dict):
                    continue
                try:
                    pv = int(row.get("port") or 20000)
                    if not (1 <= pv <= 65535):
                        pv = 20000
                except (TypeError, ValueError):
                    pv = 20000
                self._qlight_devices_data.append(
                    {
                        "ip": str(row.get("ip") or "").strip(),
                        "port": pv,
                        "linked_terminal": str(row.get("linked_terminal") or "").strip(),
                        "on_success": True,
                        "on_failure": True,
                    }
                )
        else:
            h = (data.get("qlight_host") or "").strip()
            if h:
                try:
                    pv = int(data.get("qlight_port") or 20000)
                    if not (1 <= pv <= 65535):
                        pv = 20000
                except (TypeError, ValueError):
                    pv = 20000
                self._qlight_devices_data.append(
                    {
                        "ip": h,
                        "port": pv,
                        "linked_terminal": "",
                        "on_success": True,
                        "on_failure": True,
                    }
                )
        self._qlight_editing_index = None
        self._refresh_qlight_table()
        self._clear_qlight_form()

    def _apply_printer_enables(self):
        en = self.printer_enabled.isChecked()
        for w in (
            self.printer_table,
            self.printer_ip_edit,
            self.printer_port_edit,
            self.printer_terminal_combo,
            self.printer_add_btn,
            self.printer_edit_btn,
            self.printer_del_btn,
        ):
            w.setEnabled(en)

    def _on_printer_state_changed(self):
        self._apply_printer_enables()
        self.save_settings(quiet=True)

    @staticmethod
    def _parse_port_line(text, default):
        s = (text or "").strip()
        if not s:
            return default
        try:
            p = int(s)
            if 1 <= p <= 65535:
                return p
        except ValueError:
            pass
        return None

    def _refresh_printer_table(self):
        self.printer_table.setSortingEnabled(False)
        self.printer_table.setRowCount(len(self._printer_devices_data))
        for i, row in enumerate(self._printer_devices_data):
            no_item = QTableWidgetItem()
            no_item.setData(Qt.DisplayRole, i + 1)
            no_item.setData(Qt.UserRole, i)
            no_item.setTextAlignment(Qt.AlignCenter)
            self.printer_table.setItem(i, 0, no_item)
            linked = str(row.get("linked_terminal", ""))
            self.printer_table.setItem(i, 1, QTableWidgetItem(linked))
            self.printer_table.setItem(i, 2, QTableWidgetItem(str(row.get("ip", ""))))
            self.printer_table.setItem(i, 3, QTableWidgetItem(str(row.get("port", ""))))
            ok = self._is_aux_device_online(row.get("ip"), row.get("port"))
            self.printer_table.setItem(i, 4, self._aux_status_item(ok))
        self.printer_table.setSortingEnabled(True)

    def _printer_data_index_from_current_row(self):
        """정렬 후에도 `_printer_devices_data` 인덱스(0..n-1)를 반환. 없으면 -1."""
        r = self.printer_table.currentRow()
        if r < 0:
            return -1
        it = self.printer_table.item(r, 0)
        if it is None:
            return -1
        v = it.data(Qt.UserRole)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
        if r < len(self._printer_devices_data):
            return r
        return -1

    def _load_printer_row_into_form(self, data_i: int):
        t = self._printer_devices_data[data_i]
        self.printer_ip_edit.setText(str(t.get("ip", "")))
        self.printer_port_edit.setText(str(t.get("port", 9100)))
        self._fill_terminal_combo(self.printer_terminal_combo, str(t.get("linked_terminal", "")))
        self._printer_editing_index = data_i
        self.printer_add_btn.setText("적용")

    def _on_printer_selection_changed(self):
        di = self._printer_data_index_from_current_row()
        if di < 0 or di >= len(self._printer_devices_data):
            self.printer_ip_edit.clear()
            self.printer_port_edit.setText("9100")
            self._fill_terminal_combo(self.printer_terminal_combo, "")
            self._printer_editing_index = None
            self.printer_add_btn.setText("추가")
            return
        self._load_printer_row_into_form(di)

    def _clear_printer_form(self):
        self.printer_ip_edit.clear()
        self.printer_port_edit.setText("9100")
        self._fill_terminal_combo(self.printer_terminal_combo, "")
        self._printer_editing_index = None
        self.printer_add_btn.setText("추가")
        self.printer_table.clearSelection()

    def _printer_add_or_apply(self):
        ip = (self.printer_ip_edit.text() or "").strip()
        pv = self._parse_port_line(self.printer_port_edit.text(), 9100)
        if pv is None:
            QMessageBox.warning(self, "경고", "포트는 1~65535 숫자로 입력하세요.")
            return
        if not ip:
            QMessageBox.warning(self, "경고", "IP를 입력하세요.")
            return
        raw_lt = self.printer_terminal_combo.currentData()
        lt = "" if raw_lt is None else str(raw_lt).strip()
        entry = {"ip": ip, "port": pv, "linked_terminal": lt}
        if self._printer_editing_index is not None and 0 <= self._printer_editing_index < len(self._printer_devices_data):
            self._printer_devices_data[self._printer_editing_index] = entry
            self._printer_editing_index = None
        else:
            self._printer_devices_data.append(entry)
        self._refresh_printer_table()
        self._clear_printer_form()
        self.save_settings(quiet=True)

    def _printer_edit_row(self):
        if self._printer_editing_index is not None:
            self._printer_add_or_apply()
            return
        di = self._printer_data_index_from_current_row()
        if di < 0 or di >= len(self._printer_devices_data):
            QMessageBox.warning(self, "경고", "수정할 행을 선택하세요.")
            return
        self._load_printer_row_into_form(di)

    def _printer_delete_row(self):
        di = self._printer_data_index_from_current_row()
        if di < 0 or di >= len(self._printer_devices_data):
            QMessageBox.warning(self, "경고", "삭제할 행을 선택하세요.")
            return
        if QMessageBox.question(self, "확인", "선택한 프린터를 삭제할까요?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        del self._printer_devices_data[di]
        self._printer_editing_index = None
        self._refresh_printer_table()
        self._clear_printer_form()
        self.save_settings(quiet=True)

    def _clear_f22_form(self):
        self.f22_device_no.clear()
        self.f22_ip.clear()
        self.f22_port_line.setText("4370")
        self.f22_location.clear()
        self._f22_editing_index = None
        self.f22_add_btn.setText("추가")
        self.f22_table.clearSelection()

    @staticmethod
    def _f22_serial_from_row(t: dict) -> str:
        return str(t.get("serial_no") or t.get("adms_sn") or t.get("device_sn") or "").strip()

    def _f22_data_index_from_current_row(self):
        """정렬 후에도 `_f22_terminals_data` 인덱스. 없으면 -1."""
        r = self.f22_table.currentRow()
        if r < 0:
            return -1
        it = self.f22_table.item(r, 0)
        if it is None:
            return -1
        v = it.data(Qt.UserRole)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
        if r < len(self._f22_terminals_data):
            return r
        return -1

    def _load_f22_row_into_form(self, data_i: int):
        t = self._f22_terminals_data[data_i]
        self.f22_device_no.setText(str(t.get("device_no", "")))
        self.f22_ip.setText(str(t.get("ip", "")))
        self.f22_port_line.setText(str(t.get("port", 4370)))
        self.f22_location.setText(str(t.get("install_location", "")))
        self._f22_editing_index = data_i
        self.f22_add_btn.setText("적용")

    def _on_f22_selection_changed(self):
        di = self._f22_data_index_from_current_row()
        if di < 0 or di >= len(self._f22_terminals_data):
            self.f22_device_no.clear()
            self.f22_ip.clear()
            self.f22_port_line.setText("4370")
            self.f22_location.clear()
            self._f22_editing_index = None
            self.f22_add_btn.setText("추가")
            return
        self._load_f22_row_into_form(di)

    def _refresh_f22_table(self):
        self.f22_table.setSortingEnabled(False)
        self.f22_table.setRowCount(len(self._f22_terminals_data))
        for i, row in enumerate(self._f22_terminals_data):
            no_item = QTableWidgetItem()
            no_item.setData(Qt.DisplayRole, i + 1)
            no_item.setData(Qt.UserRole, i)
            no_item.setTextAlignment(Qt.AlignCenter)
            self.f22_table.setItem(i, 0, no_item)
            dno = str(row.get("device_no", ""))
            ip = str(row.get("ip", ""))
            self.f22_table.setItem(i, 1, QTableWidgetItem(dno))
            self.f22_table.setItem(i, 2, QTableWidgetItem(ip))
            self.f22_table.setItem(i, 3, QTableWidgetItem(str(row.get("port", ""))))
            self.f22_table.setItem(i, 4, QTableWidgetItem(str(row.get("install_location", ""))))
            ok = self._is_terminal_connected(device_no=dno, ip=ip)
            self.f22_table.setItem(i, 5, self._status_on_off_item(ok))
        self.f22_table.setSortingEnabled(True)

    def _f22_add_or_apply(self):
        ip = (self.f22_ip.text() or "").strip()
        port_val = self._parse_port_line(self.f22_port_line.text(), 4370)
        if port_val is None:
            QMessageBox.warning(self, "경고", "포트는 1~65535 숫자로 입력하세요.")
            return
        if not ip:
            QMessageBox.warning(self, "경고", "IP를 입력하세요.")
            return
        dno = (self.f22_device_no.text() or "").strip()
        if not dno:
            QMessageBox.warning(self, "경고", "단말기 번호를 입력하세요.")
            return
        master = dno == "1"
        loc = (self.f22_location.text() or "").strip()

        def _enroll_fids_from_row(src):
            keys = ("enroll_fid0", "enroll_fid1", "enroll_fid2", "enroll_fid3")
            defaults = (0, 1, 2, 3)
            out = []
            for k, d in zip(keys, defaults):
                try:
                    out.append(max(0, min(9, int(src.get(k, d)))))
                except (TypeError, ValueError):
                    out.append(d)
            return tuple(out)

        if self._f22_editing_index is not None and 0 <= self._f22_editing_index < len(self._f22_terminals_data):
            e0, e1, e2, e3 = _enroll_fids_from_row(self._f22_terminals_data[self._f22_editing_index])
        else:
            e0, e1, e2, e3 = 0, 1, 2, 3
        entry = {
            "device_no": dno,
            "ip": ip,
            "port": port_val,
            "serial_no": "",
            "terminal_name": "",
            "install_location": loc,
            "master_enrollment": master,
            "enroll_fid0": e0,
            "enroll_fid1": e1,
            "enroll_fid2": e2,
            "enroll_fid3": e3,
            "authorize_timezone_id": 1,
            "authorize_door_id": 1,
        }
        if self._f22_editing_index is not None and 0 <= self._f22_editing_index < len(self._f22_terminals_data):
            self._f22_terminals_data[self._f22_editing_index] = entry
            self._f22_editing_index = None
        else:
            self._f22_terminals_data.append(entry)
        self._refresh_f22_table()
        self._clear_f22_form()
        self._refresh_linked_terminal_combos()
        self.save_settings(quiet=True)

    def _f22_edit_row(self):
        if self._f22_editing_index is not None:
            self._f22_add_or_apply()
            return
        di = self._f22_data_index_from_current_row()
        if di < 0 or di >= len(self._f22_terminals_data):
            QMessageBox.warning(self, "경고", "수정할 단말기 행을 선택하세요.")
            return
        self._load_f22_row_into_form(di)

    def _f22_delete_row(self):
        di = self._f22_data_index_from_current_row()
        if di < 0 or di >= len(self._f22_terminals_data):
            QMessageBox.warning(self, "경고", "삭제할 단말기 행을 선택하세요.")
            return
        if QMessageBox.question(
            self, "확인", "선택한 단말기를 목록에서 삭제할까요?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        del self._f22_terminals_data[di]
        self._f22_editing_index = None
        self._refresh_f22_table()
        self._clear_f22_form()
        self._refresh_linked_terminal_combos()
        self.save_settings(quiet=True)

    def _load_printer_from_data(self, data: dict):
        self._printer_devices_data = []
        pd = data.get("printer_devices")
        if isinstance(pd, list) and pd:
            for p in pd:
                if not isinstance(p, dict):
                    continue
                try:
                    pv = int(p.get("port"))
                    if not (1 <= pv <= 65535):
                        pv = 9100
                except (TypeError, ValueError):
                    pv = 9100
                lt = str(p.get("linked_terminal") or "").strip()
                self._printer_devices_data.append(
                    {"ip": str(p.get("ip") or p.get("host") or "").strip(), "port": pv, "linked_terminal": lt}
                )
        else:
            h = (data.get("printer_host") or "").strip()
            try:
                pv = int(data.get("printer_port") or 9100)
                if not (1 <= pv <= 65535):
                    pv = 9100
            except (TypeError, ValueError):
                pv = 9100
            if h or pv:
                self._printer_devices_data.append({"ip": h, "port": pv, "linked_terminal": ""})
        self._printer_editing_index = None
        self._refresh_printer_table()
        self._clear_printer_form()

    def load_data(self):
        data = self.api.get_device_settings() if self.api else None
        if not data:
            self.printer_enabled.blockSignals(True)
            try:
                self.printer_enabled.setChecked(False)
            finally:
                self.printer_enabled.blockSignals(False)
            self.qlight_enabled.blockSignals(True)
            try:
                self.qlight_enabled.setChecked(False)
            finally:
                self.qlight_enabled.blockSignals(False)
            self._printer_devices_data = []
            self._qlight_devices_data = []
            self._f22_terminals_data = []
            self._f22_editing_index = None
            self._refresh_printer_table()
            self._clear_printer_form()
            self._refresh_qlight_table()
            self._clear_qlight_form()
            self._refresh_f22_table()
            self._clear_f22_form()
            self._refresh_linked_terminal_combos()
            self._apply_printer_enables()
            self._apply_qlight_enables()
            return
        self.printer_enabled.blockSignals(True)
        try:
            self.printer_enabled.setChecked(bool(data.get("printer_enabled")))
        finally:
            self.printer_enabled.blockSignals(False)
        self.qlight_enabled.blockSignals(True)
        try:
            self.qlight_enabled.setChecked(bool(data.get("qlight_enabled")))
        finally:
            self.qlight_enabled.blockSignals(False)
        self._load_printer_from_data(data)
        self._load_qlight_from_data(data)
        self._f22_terminals_data = []
        terminals = data.get("f22_terminals")
        if isinstance(terminals, list):
            for t in terminals:
                if not isinstance(t, dict):
                    continue
                try:
                    pv = int(t.get("port"))
                    if not (1 <= pv <= 65535):
                        pv = 4370
                except (TypeError, ValueError):
                    pv = 4370
                try:
                    ef0 = max(0, min(9, int(t.get("enroll_fid0", 0))))
                except (TypeError, ValueError):
                    ef0 = 0
                try:
                    ef1 = max(0, min(9, int(t.get("enroll_fid1", 1))))
                except (TypeError, ValueError):
                    ef1 = 1
                try:
                    ef2 = max(0, min(9, int(t.get("enroll_fid2", 2))))
                except (TypeError, ValueError):
                    ef2 = 2
                try:
                    ef3 = max(0, min(9, int(t.get("enroll_fid3", 3))))
                except (TypeError, ValueError):
                    ef3 = 3
                dno = str(t.get("device_no") or t.get("terminal_no") or "").strip()
                master_flag = dno == "1"
                self._f22_terminals_data.append(
                    {
                        "device_no": dno,
                        "ip": str(t.get("ip") or t.get("host") or "").strip(),
                        "port": pv,
                        "serial_no": str(
                            t.get("serial_no") or t.get("adms_sn") or t.get("device_sn") or ""
                        ).strip(),
                        "terminal_name": "",
                        "install_location": str(t.get("install_location") or t.get("location") or "").strip(),
                        "master_enrollment": master_flag,
                        "enroll_fid0": ef0,
                        "enroll_fid1": ef1,
                        "enroll_fid2": ef2,
                        "enroll_fid3": ef3,
                        "authorize_timezone_id": 1,
                        "authorize_door_id": 1,
                    }
                )
        self._f22_editing_index = None
        self._refresh_f22_table()
        self._clear_f22_form()
        self._refresh_linked_terminal_combos()
        self._apply_printer_enables()
        self._apply_qlight_enables()
        self._schedule_aux_device_probe()

    def _aux_device_key(self, ip, port) -> str:
        try:
            port_i = int(port)
        except (TypeError, ValueError):
            return ""
        host = str(ip or "").strip()
        if not host:
            return ""
        return "%s:%s" % (host, port_i)

    def _is_aux_device_online(self, ip, port) -> bool:
        key = self._aux_device_key(ip, port)
        if not key:
            return False
        return bool(getattr(self, "_device_online", {}).get(key, False))

    def _aux_status_item(self, ok: bool) -> QTableWidgetItem:
        item = self._status_on_off_item(ok)
        item.setToolTip("장치 접속됨" if ok else "장치 접속 안 됨")
        return item

    def _schedule_aux_device_probe(self):
        prev = getattr(self, "_aux_probe_loader", None)
        if prev is not None and prev.isRunning():
            self._aux_probe_pending = True
            return
        self._aux_probe_pending = False
        printers = list(getattr(self, "_printer_devices_data", []) or [])
        qlights = list(getattr(self, "_qlight_devices_data", []) or [])
        loader = DataLoader(_probe_printer_qlight, printers, qlights)
        loader.finished.connect(self._on_aux_device_probe)
        self._aux_probe_loader = loader
        loader.start()

    def _on_aux_device_probe(self, result):
        if isinstance(result, dict):
            self._device_online = result
        self._refresh_printer_table()
        self._refresh_qlight_table()
        if getattr(self, "_aux_probe_pending", False):
            self._aux_probe_pending = False
            QTimer.singleShot(0, self._schedule_aux_device_probe)

    def save_settings(self, quiet=False):
        if not self.api:
            return
        p0 = self._printer_devices_data[0] if self._printer_devices_data else {"ip": "", "port": 9100, "linked_terminal": ""}
        q0 = self._qlight_devices_data[0] if self._qlight_devices_data else {"ip": "", "port": 20000, "linked_terminal": ""}
        f22_list = []
        for t in self._f22_terminals_data:
            try:
                ef0 = max(0, min(9, int(t.get("enroll_fid0", 0))))
            except (TypeError, ValueError):
                ef0 = 0
            try:
                ef1 = max(0, min(9, int(t.get("enroll_fid1", 1))))
            except (TypeError, ValueError):
                ef1 = 1
            try:
                ef2 = max(0, min(9, int(t.get("enroll_fid2", 2))))
            except (TypeError, ValueError):
                ef2 = 2
            try:
                ef3 = max(0, min(9, int(t.get("enroll_fid3", 3))))
            except (TypeError, ValueError):
                ef3 = 3
            dno_save = str(t.get("device_no", "")).strip()
            master_flag = dno_save == "1"
            f22_list.append(
                {
                    "device_no": dno_save,
                    "ip": str(t.get("ip", "")).strip(),
                    "port": int(t.get("port", 4370)),
                    "serial_no": self._f22_serial_from_row(t),
                    "terminal_name": "",
                    "install_location": str(t.get("install_location", "")).strip(),
                    "master_enrollment": master_flag,
                    "enroll_fid0": ef0,
                    "enroll_fid1": ef1,
                    "enroll_fid2": ef2,
                    "enroll_fid3": ef3,
                    "authorize_timezone_id": 1,
                    "authorize_door_id": 1,
                }
            )
        def _norm_dev(rows, default_port):
            out = []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                try:
                    pv = int(r.get("port", default_port))
                    if not (1 <= pv <= 65535):
                        pv = default_port
                except (TypeError, ValueError):
                    pv = default_port
                out.append(
                    {
                        "ip": str(r.get("ip") or "").strip(),
                        "port": pv,
                        "linked_terminal": str(r.get("linked_terminal") or "").strip(),
                    }
                )
            return out

        def _norm_qlight(rows, default_port):
            out = []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                try:
                    pv = int(r.get("port", default_port))
                    if not (1 <= pv <= 65535):
                        pv = default_port
                except (TypeError, ValueError):
                    pv = default_port
                out.append(
                    {
                        "ip": str(r.get("ip") or "").strip(),
                        "port": pv,
                        "linked_terminal": str(r.get("linked_terminal") or "").strip(),
                        "on_success": True,
                        "on_failure": True,
                    }
                )
            return out

        payload = {
            "printer_enabled": self.printer_enabled.isChecked(),
            "printer_host": str(p0.get("ip", "")).strip(),
            "printer_port": int(p0.get("port", 9100)),
            "printer_devices": _norm_dev(self._printer_devices_data, 9100),
            "qlight_enabled": self.qlight_enabled.isChecked(),
            "qlight_host": str(q0.get("ip", "")).strip(),
            "qlight_port": int(q0.get("port", 20000)),
            "qlight_devices": _norm_qlight(self._qlight_devices_data, 20000),
            "f22_terminals": f22_list,
        }
        ok, result = self.api.put_device_settings(payload)
        if ok:
            self._schedule_aux_device_probe()
            if not quiet:
                QMessageBox.information(self, "저장 완료", "장치 설정이 저장되었습니다.")
        else:
            if not quiet:
                QMessageBox.warning(self, "저장 실패", result or "저장에 실패했습니다.")
        return ok, (result if not ok else None)

    def _on_save_and_ping_f22(self):
        ok, err = self.save_settings(quiet=True)
        if not ok:
            QMessageBox.warning(self, "저장 실패", str(err or "저장에 실패했습니다."))
            return
        if _f22_att_mode_is_adms():
            mw = self.main_win
            if mw is None:
                QMessageBox.warning(self, "ADMS 연결", "메인 창을 찾을 수 없습니다.")
                return
            QApplication.setOverrideCursor(Qt.WaitCursor)
            mw.start_adms_connection_probe(self._on_adms_probe_finished)
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        self._f22_ping_loader = DataLoader(_device_status_task, self.api, self.main_win)
        self._f22_ping_loader.finished.connect(self._on_f22_ping_dialog_done)
        self._f22_ping_loader.error.connect(self._on_f22_ping_dialog_error)
        self._f22_ping_loader.start()

    def _on_adms_probe_finished(self, rep: dict):
        QApplication.restoreOverrideCursor()
        if not isinstance(rep, dict):
            QMessageBox.warning(self, "ADMS 연결", "응답 형식 오류")
            return
        body_lines = list(rep.get("detail_lines") or [])
        if rep.get("all_ok"):
            body_lines.append("")
            body_lines.append("연결 확인됨. 로그 수신은 단말 연결 ON 후 자동 시작됩니다.")
        else:
            body_lines.append("")
            body_lines.append("단말 ADMS URL·IP 확인 후 다시 시도하세요.")
        body = "\n".join(body_lines)
        title = "ADMS 연결 확인"
        if rep.get("all_ok"):
            QMessageBox.information(self, title, body)
        else:
            QMessageBox.warning(self, title, body)
        if self.main_win is not None:
            self.main_win.apply_terminal_ping_report(rep)

    def _on_f22_ping_dialog_done(self, rep):
        QApplication.restoreOverrideCursor()
        if not isinstance(rep, dict):
            QMessageBox.warning(self, "단말 연결", "응답 형식 오류")
            return
        body = "\n".join(rep.get("detail_lines") or [])
        title = "ADMS 연결 확인" if _f22_att_mode_is_adms() else "단말 연결 확인"
        if rep.get("all_ok"):
            QMessageBox.information(self, title, body)
        else:
            QMessageBox.warning(self, title, body)
        if self.main_win is not None:
            self.main_win.apply_terminal_ping_report(rep)

    def _on_f22_ping_dialog_error(self, err_msg: str):
        QApplication.restoreOverrideCursor()
        QMessageBox.warning(self, "단말 연결", str(err_msg))

    def _f22_terminal_label(self, t: dict) -> str:
        dno = str(t.get("device_no") or t.get("terminal_no") or "").strip()
        if dno:
            return dno
        ip = str(t.get("ip") or t.get("host") or "").strip()
        sn = str(t.get("serial_no") or t.get("adms_sn") or t.get("device_sn") or "").strip()
        return ip or sn or "단말"

    def _pick_f22_terminal_for_resync(self):
        terminals = [t for t in (self._f22_terminals_data or []) if isinstance(t, dict)]
        if not terminals:
            QMessageBox.warning(self, "단말 재동기화", "등록된 F22 단말이 없습니다.")
            return None
        dlg = F22TerminalPickDialog(terminals, self, adms=_f22_att_mode_is_adms())
        if dlg.exec_() != QDialog.Accepted:
            return None
        return dlg.selected_terminal()

    def _on_f22_resync_mode_changed(self, btn):
        if btn is self.f22_resync_all_rb:
            self._f22_resync_selected_terminal = None
            return
        if btn is not self.f22_resync_sel_rb:
            return
        t = self._pick_f22_terminal_for_resync()
        if t is None:
            self.f22_resync_all_rb.blockSignals(True)
            self.f22_resync_all_rb.setChecked(True)
            self.f22_resync_all_rb.blockSignals(False)
            self._f22_resync_selected_terminal = None
            return
        self._f22_resync_selected_terminal = t

    def _on_f22_bulk_resync(self):
        terminals_only = None
        term_desc = ""
        if self.f22_resync_sel_rb.isChecked():
            t = getattr(self, "_f22_resync_selected_terminal", None)
            if t is None:
                t = self._pick_f22_terminal_for_resync()
                if t is None:
                    self.f22_resync_all_rb.setChecked(True)
                    return
                self._f22_resync_selected_terminal = t
            terminals_only = [t]
            term_desc = _f22_terminal_pick_label(t, _f22_att_mode_is_adms())
        else:
            n = len(self._f22_terminals_data or [])
            if n < 1:
                QMessageBox.warning(self, "단말 재동기화", "등록된 F22 단말이 없습니다.")
                return
            term_desc = "등록 단말 %d대" % n

        if _f22_att_mode_is_adms():
            from meal_db.adms_push_server import get_runtime_adms_server

            srv = get_runtime_adms_server()
            if srv is None or not srv.is_running():
                QMessageBox.warning(
                    self,
                    "단말 재동기화",
                    "ADMS 연결 서버가 실행 중이 아닙니다.\n"
                    "앱을 재시작하거나 단말 연결을 확인한 뒤 다시 시도하세요.",
                )
                return

        confirm = (
            "DB와 단말을 비교해 단말 사용자를 DB와 같게 맞춥니다.\n"
            "재직이면서 카드 또는 지문이 있는 사원만 단말에 남기고 다시 등록합니다.\n"
            "DB에 없거나 퇴사자, 카드·지문이 없는 사번은 단말에서 삭제합니다.\n"
            "단말 관리자 계정은 삭제하지 않습니다.\n\n"
            "대상 단말: %s\n\n"
            "계속하시겠습니까?" % term_desc
        )
        if QMessageBox.question(
            self,
            "단말 재동기화",
            confirm,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) != QMessageBox.Yes:
            return

        dlg = F22BulkResyncProgressDialog(self)
        self._f22_bulk_resync_dialog = dlg
        thread = F22BulkResyncThread(self.api, terminals_only, self)
        self._f22_bulk_resync_thread = thread
        thread.progress.connect(dlg.update_progress)
        thread.finished_summary.connect(self._on_f22_bulk_resync_done)
        thread.error.connect(self._on_f22_bulk_resync_err)
        dlg.cancel_btn.clicked.connect(thread.request_cancel)
        self.f22_bulk_resync_btn.setEnabled(False)
        thread.finished.connect(lambda: self.f22_bulk_resync_btn.setEnabled(True))
        thread.start()
        dlg.exec_()

    def _on_f22_bulk_resync_done(self, summary):
        dlg = self._f22_bulk_resync_dialog
        if dlg is not None:
            dlg.accept()
        self._f22_bulk_resync_dialog = None
        self._f22_bulk_resync_thread = None
        body = _f22_bulk_resync_dialog_text(summary if isinstance(summary, dict) else {})
        fail = int((summary or {}).get("fail_emp") or 0) if isinstance(summary, dict) else 1
        delete_fail = int((summary or {}).get("delete_fail") or 0) if isinstance(summary, dict) else 0
        if fail > 0 or delete_fail > 0 or (isinstance(summary, dict) and summary.get("cancelled")):
            QMessageBox.warning(self, "단말 재동기화 완료", body)
        else:
            QMessageBox.information(self, "단말 재동기화 완료", body)

    def _on_f22_bulk_resync_err(self, err_msg: str):
        dlg = self._f22_bulk_resync_dialog
        if dlg is not None:
            dlg.reject()
        self._f22_bulk_resync_dialog = None
        self._f22_bulk_resync_thread = None
        QMessageBox.warning(self, "단말 재동기화", str(err_msg))


class MainWindow(QMainWindow):
    def __init__(self, api=None):
        super().__init__()
        if api is not None:
            self.api = api
        else:
            try:
                from meal_db.connection import ensure_driver
                from meal_db.local_client import LocalMealClient

                ensure_driver()
                self.api = LocalMealClient()
            except Exception as e:
                QMessageBox.critical(
                    self,
                    "로컬 DB",
                    "로컬 DB에 연결할 수 없습니다.\nMariaDB 준비·스키마 적용·MEAL_DB_* 환경 변수를 확인하세요.\n\n%s"
                    % e,
                )
                sys.exit(1)
        self.companies_data = []
        self.departments_data = []
        self._term_status_loader = None
        self.setWindowTitle("Meal Auth - Admin Management System (로컬 DB)")
        # 화면보다 큰 최소 크기 요구로 setGeometry 경고 나오지 않도록 상한 설정
        screen = QApplication.primaryScreen()
        if screen:
            available = screen.availableGeometry()
            max_w, max_h = available.width(), available.height()
            self.setMinimumSize(min(1024, max_w), min(700, max_h))
        self.resize(1280, 850)
        # 화면 가용 영역 안으로 초기 크기만 제한 (이후 사용자가 창 크기 조절 가능)
        if screen:
            available = screen.availableGeometry()
            w = min(self.width(), available.width())
            h = min(self.height(), available.height())
            self.resize(w, h)
        self.setStyleSheet(QSS)
        _app_root = Path(__file__).resolve().parent
        self._term_conn_icon = QPixmap(str(_app_root / "conn.png"))
        self._term_disconn_icon = QPixmap(str(_app_root / "disconn.png"))
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        self.sidebar = QWidget()
        self.sidebar.setObjectName("Sidebar")
        sidebar_layout = QVBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        logo = QLabel("Meal Admin")
        logo.setObjectName("Logo")
        sidebar_layout.addWidget(logo)
        self.nav_btns = []
        menus = [
            ("대시보드", 0), ("회사 관리", 1), ("부서 관리", 2),
            ("사원 관리", 3), ("원시 데이터", 4), ("식사 정책", 5), ("보고서", 6), ("관리자 비번설정", 7), ("설정", 8)
        ]
        for name, idx in menus:
            btn = QPushButton(name)
            btn.setObjectName("MenuBtn")
            btn.setProperty("active", "false")
            btn.clicked.connect(lambda chk, i=idx: self.switch_screen(i))
            sidebar_layout.addWidget(btn)
            self.nav_btns.append(btn)
        self._sidebar_terminal_host = QWidget()
        self._sidebar_terminal_host.setObjectName("SidebarTerminalHost")
        st_outer = QVBoxLayout(self._sidebar_terminal_host)
        st_outer.setContentsMargins(8, 14, 8, 6)
        st_outer.setSpacing(6)
        self._sidebar_terminal_title = QLabel("단말기상태")
        self._sidebar_terminal_title.setObjectName("SidebarTerminalTitle")
        st_outer.addWidget(self._sidebar_terminal_title)
        self._sidebar_terminal_rows_layout = QVBoxLayout()
        self._sidebar_terminal_rows_layout.setSpacing(6)
        st_outer.addLayout(self._sidebar_terminal_rows_layout)
        sidebar_layout.addWidget(self._sidebar_terminal_host)
        sidebar_layout.addStretch()
        main_layout.addWidget(self.sidebar)
        content_container = QWidget()
        content_container.setObjectName("ContentArea")
        self.content_layout = QVBoxLayout(content_container)
        self.stack = QStackedWidget()
        self.dashboard = DashboardScreen(self)
        self.companies = CompanyScreen(self.api, self)
        self.departments = DepartmentScreen(self.api, self)
        self.employees = EmployeeScreen(self.api, self)
        self.raw_data = RawDataScreen(self.api, self)
        self.policies = PolicyScreen(self.api, self)
        self.reports = ReportScreen(self.api, self)
        self.admin_screen = AdminScreen(self.api, self)
        self.settings_screen = SettingsScreen(self.api, self)
        self.stack.addWidget(self.dashboard)
        self.stack.addWidget(self.companies)
        self.stack.addWidget(self.departments)
        self.stack.addWidget(self.employees)
        self.stack.addWidget(self.raw_data)
        self.stack.addWidget(self.policies)
        self.stack.addWidget(self.reports)
        self.stack.addWidget(self.admin_screen)
        self.stack.addWidget(self.settings_screen)
        self.content_layout.addWidget(self.stack)
        main_layout.addWidget(content_container)
        self._dashboard_stats_timer = QTimer(self)
        self._dashboard_stats_timer.setInterval(60000)
        self._dashboard_stats_timer.timeout.connect(self._on_dashboard_stats_timer)
        self._f22_receive_active = False
        self._adms_log_receive_user_paused = False  # 사용자가 「수신중지」한 경우 자동 재시작 금지
        self._last_terminal_ping_rep = None
        self._f22_att_sync_loader = None
        self._f22_bootstrap_loader = None
        self._f22_live_threads: list = []
        self._adms_push_server = None
        self._adms_push_bridge = AdmsPushBridge()
        self._adms_push_bridge.attendance.connect(self._on_f22_live_attendance)
        self._adms_push_bridge.auth_failure.connect(self._on_adms_auth_failure)
        self._adms_push_bridge.status.connect(self._on_adms_push_status)
        self._qlight_queue: queue.Queue = queue.Queue()
        self._qlight_worker_stop = threading.Event()
        self._qlight_worker = threading.Thread(target=self._qlight_worker_loop, daemon=True)
        self._qlight_worker.start()
        try:
            if (os.environ.get("MEAL_F22_ATT_GAP_MS") or "").strip():
                self._f22_att_poll_gap_ms = int(os.environ.get("MEAL_F22_ATT_GAP_MS", "1500"))
            else:
                sec = int(os.environ.get("MEAL_F22_ATT_INTERVAL_SEC", "1"))
                self._f22_att_poll_gap_ms = int(sec * 1000)
        except ValueError:
            self._f22_att_poll_gap_ms = 1000
        self._f22_att_poll_gap_ms = max(300, min(60000, int(self._f22_att_poll_gap_ms)))
        self._live_stable_counts = {}
        self._stats_refresh_pending = False
        self._recent_logs_pending_day = None
        self._attlog_clear_fired_date = None
        self._attlog_clear_fired_keys = set()
        self._attlog_clear_pending_keys = []
        self._attlog_clear_loader = None
        self._attlog_clear_timer = QTimer(self)
        self._attlog_clear_timer.setInterval(60000)
        self._attlog_clear_timer.timeout.connect(self._tick_attlog_clear_schedule)
        self._attlog_clear_timer.start()
        self.switch_screen(0)
        self._term_status_timer = QTimer(self)
        self._term_status_timer.timeout.connect(self.refresh_terminal_status_async)
        self._term_status_timer.setInterval(15000 if _f22_att_mode_is_adms() else 45000)
        self._term_status_timer.start()
        self._apply_sidebar_terminal_rows(
            {"all_ok": False, "status_rows": [{"label": "단말기:", "ok": False}]}
        )
        self._startup_in_progress = False
        self._startup_overlay = None
        QTimer.singleShot(0, self._begin_startup_sequence)

    def _startup_steps(self):
        steps = [
            ("db", "데이터베이스 연결 확인"),
            ("data", "기본 데이터 로드 중…"),
        ]
        if _f22_att_mode_is_adms():
            steps.append(("adms", "ADMS 연결 서버 시작 중…"))
        steps.append(("terminal", "단말기 연결 확인 중…"))
        steps.append(("receive", "출입 로그 수신 준비 중…"))
        return steps

    def _begin_startup_sequence(self):
        self._startup_steps_def = self._startup_steps()
        labels = [s[1] for s in self._startup_steps_def]
        self._startup_in_progress = True
        self._startup_step = 0
        self._startup_waiting_dept = False
        cw = self.centralWidget()
        self._startup_overlay = StartupOverlay(cw, labels)
        self._startup_overlay.setGeometry(cw.rect())
        self._startup_overlay.show()
        self._startup_overlay.raise_()
        cw.installEventFilter(self)
        self._run_startup_step()

    def _startup_current_step_id(self) -> str:
        steps = getattr(self, "_startup_steps_def", None) or self._startup_steps()
        idx = getattr(self, "_startup_step", 0)
        if idx < 0 or idx >= len(steps):
            return ""
        return str(steps[idx][0])

    def _startup_overlay_status(self, text: str):
        overlay = getattr(self, "_startup_overlay", None)
        if overlay is not None:
            overlay.set_status(text)

    def _startup_stop_terminal_timer(self):
        t = getattr(self, "_startup_terminal_timer", None)
        if t is not None:
            t.stop()
        self._startup_terminal_timer = None

    def _startup_stop_receive_timer(self):
        t = getattr(self, "_startup_receive_timer", None)
        if t is not None:
            t.stop()
        self._startup_receive_timer = None

    def _startup_terminal_wait_sec(self) -> int:
        try:
            sec = int(os.environ.get("MEAL_STARTUP_TERMINAL_WAIT_SEC", "15"))
        except ValueError:
            sec = 15
        return max(5, min(60, sec))

    def _startup_terminal_retry_rounds(self) -> int:
        try:
            rounds = int(os.environ.get("MEAL_STARTUP_TERMINAL_RETRY_ROUNDS", "1"))
        except ValueError:
            rounds = 1
        return max(0, min(3, rounds))

    def _begin_startup_terminal_adms(self):
        self._startup_stop_terminal_timer()
        try:
            settings = self.api.get_device_settings()
        except Exception:
            settings = {}
        terminals = (settings or {}).get("f22_terminals") if isinstance(settings, dict) else None
        if not isinstance(terminals, list) or not terminals:
            self._startup_overlay_status("등록된 F22 단말이 없습니다")
            QTimer.singleShot(200, lambda: self._startup_advance(error=True))
            return
        self._startup_terminal_attempts = 0
        self._startup_terminal_round = 0
        self._startup_terminal_max = self._startup_terminal_wait_sec()
        self._startup_terminal_retry_max = self._startup_terminal_retry_rounds()
        per = self._startup_terminal_max
        retry = self._startup_terminal_retry_max
        if retry > 0:
            hint = "최대 %d초 × %d회 시도" % (per, retry + 1)
        else:
            hint = "최대 %d초" % per
        self._startup_overlay_status(
            "단말기 연결 확인 중… — 단말이 PC ADMS로 접속할 때까지 대기 (%s)" % hint
        )
        self._tick_startup_terminal_adms()

    def _startup_retry_terminal_adms(self):
        """1차 대기 실패 후 ADMS 서버 재기동·재시도."""
        self._startup_terminal_round = int(getattr(self, "_startup_terminal_round", 0)) + 1
        self._startup_terminal_attempts = 0
        round_no = self._startup_terminal_round + 1
        total = int(getattr(self, "_startup_terminal_retry_max", 1)) + 1
        self._startup_overlay_status(
            "단말 연결 없음 — %d/%d차 재시도 중… (ADMS 서버 재확인)" % (round_no, total)
        )
        self._ensure_adms_listener()
        QTimer.singleShot(400, self._tick_startup_terminal_adms)

    def _tick_startup_terminal_adms(self):
        if not getattr(self, "_startup_in_progress", False):
            return
        if self._startup_current_step_id() != "terminal":
            return
        self._startup_terminal_attempts = int(getattr(self, "_startup_terminal_attempts", 0)) + 1
        attempt = self._startup_terminal_attempts
        max_sec = int(getattr(self, "_startup_terminal_max", 30))
        try:
            rep = self._build_terminal_status_report()
            self.apply_terminal_ping_report(rep)
        except Exception as e:
            self._on_terminal_status_refresh_error(str(e))
            rep = getattr(self, "_last_terminal_ping_rep", None) or {}
        if isinstance(rep, dict) and rep.get("all_ok"):
            self._startup_last_terminal_rep = rep
            self._startup_overlay_status("단말기 연결됨")
            self._startup_stop_terminal_timer()
            self._startup_advance()
            return
        if attempt >= max_sec:
            self._startup_stop_terminal_timer()
            round_idx = int(getattr(self, "_startup_terminal_round", 0))
            retry_max = int(getattr(self, "_startup_terminal_retry_max", 1))
            if round_idx < retry_max:
                self._startup_retry_terminal_adms()
                return
            self._startup_overlay_status(
                "단말 연결 실패 — 앱은 계속 실행됩니다 (백그라운드에서 자동 재확인)"
            )
            self._startup_advance(error=True)
            return
        round_idx = int(getattr(self, "_startup_terminal_round", 0))
        round_total = int(getattr(self, "_startup_terminal_retry_max", 1)) + 1
        self._startup_overlay_status(
            "단말기 연결 확인 중… %d차 (%d/%d초)" % (round_idx + 1, attempt, max_sec)
            + (" — 단말 ADMS·네트워크 확인" if round_idx == 0 else " — 재시도")
        )
        self._startup_stop_terminal_timer()
        self._startup_terminal_timer = QTimer(self)
        self._startup_terminal_timer.setSingleShot(True)
        self._startup_terminal_timer.timeout.connect(self._tick_startup_terminal_adms)
        self._startup_terminal_timer.start(1000)

    def _begin_startup_receive(self):
        if (os.environ.get("MEAL_F22_NO_AUTO_RECEIVE", "").strip().lower() in ("1", "true", "yes")):
            self._startup_overlay_status("출입 로그 자동 수신 비활성화 (환경 변수)")
            QTimer.singleShot(200, self._startup_advance)
            return
        if _f22_att_mode_is_adms():
            self._startup_receive_attempts = 0
            self._startup_overlay_status("출입 로그 수신 준비 중…")
            self._tick_startup_receive_adms()
            return
        self._auto_start_f22_receive()
        self._startup_receive_attempts = 0
        self._tick_startup_receive_pyzk()

    def _tick_startup_receive_adms(self):
        if not getattr(self, "_startup_in_progress", False):
            return
        if self._startup_current_step_id() != "receive":
            return
        self._startup_receive_attempts = int(getattr(self, "_startup_receive_attempts", 0)) + 1
        try:
            rep = self._build_terminal_status_report()
            self.apply_terminal_ping_report(rep)
            self._maybe_auto_start_adms_log_receive(rep)
        except Exception as e:
            self._on_terminal_status_refresh_error(str(e))
            rep = {}
        receive_on = bool(getattr(self, "_f22_receive_active", False))
        terminals_ok = isinstance(rep, dict) and bool(rep.get("all_ok"))
        if receive_on and terminals_ok:
            self._startup_overlay_status("출입 로그 수신 시작됨")
            self._startup_stop_receive_timer()
            self._startup_advance()
            return
        if not terminals_ok:
            self._startup_overlay_status(
                "단말 미연결 — 수신은 보류하고 앱을 계속 실행합니다 (연결 시 자동 시작)"
            )
            self._startup_stop_receive_timer()
            self._startup_advance(error=True)
            return
        if self._startup_receive_attempts >= 8:
            self._set_adms_log_receive(True)
            self.refresh_terminal_status_async(force=True)
            self._startup_overlay_status("출입 로그 수신 시작됨")
            self._startup_stop_receive_timer()
            self._startup_advance()
            return
        self._startup_overlay_status("출입 로그 수신 준비 중… (%d/8)" % self._startup_receive_attempts)
        self._startup_stop_receive_timer()
        self._startup_receive_timer = QTimer(self)
        self._startup_receive_timer.setSingleShot(True)
        self._startup_receive_timer.timeout.connect(self._tick_startup_receive_adms)
        self._startup_receive_timer.start(500)

    def _tick_startup_receive_pyzk(self):
        if not getattr(self, "_startup_in_progress", False):
            return
        if self._startup_current_step_id() != "receive":
            return
        self._startup_receive_attempts = int(getattr(self, "_startup_receive_attempts", 0)) + 1
        if bool(getattr(self, "_f22_receive_active", False)):
            self._startup_overlay_status("출입 로그 수신 시작됨")
            self._startup_advance()
            return
        if self._startup_receive_attempts >= 8:
            self._startup_advance(error=not bool(getattr(self, "_f22_receive_active", False)))
            return
        self._startup_overlay_status("출입 로그 수신 준비 중… (%d/8)" % self._startup_receive_attempts)
        QTimer.singleShot(500, self._tick_startup_receive_pyzk)

    def _startup_advance(self, *, error: bool = False):
        if not getattr(self, "_startup_in_progress", False):
            return
        idx = self._startup_step
        if self._startup_overlay is not None:
            self._startup_overlay.set_step_state(idx, "error" if error else "done")
        self._startup_step += 1
        self._run_startup_step()

    def _run_startup_step(self):
        if not getattr(self, "_startup_in_progress", False):
            return
        steps = getattr(self, "_startup_steps_def", None) or self._startup_steps()
        idx = self._startup_step
        if idx >= len(steps):
            self._finish_startup_sequence()
            return
        if self._startup_overlay is not None:
            self._startup_overlay.set_step_state(idx, "active")
        step_id = steps[idx][0]
        if step_id == "db":
            QTimer.singleShot(150, self._startup_advance)
        elif step_id == "data":
            self._startup_waiting_dept = True
            self.on_company_changed()
            self.refresh_stats()
            t = getattr(self, "_startup_load_timer", None)
            if t is not None:
                t.stop()
            self._startup_load_timer = QTimer(self)
            self._startup_load_timer.setSingleShot(True)
            self._startup_load_timer.timeout.connect(self._startup_advance)
            self._startup_load_timer.start(3000)
        elif step_id == "adms":
            ok = self._ensure_adms_listener()
            if not ok:
                QTimer.singleShot(400, lambda: self._startup_advance(error=True))
            else:
                self._show_startup_att_mode_hint()
                QTimer.singleShot(400, self._startup_advance)
        elif step_id == "terminal":
            if _f22_att_mode_is_adms():
                self._begin_startup_terminal_adms()
            else:
                loader = getattr(self, "_startup_term_loader", None)
                if loader is not None and loader.isRunning():
                    return
                self._startup_overlay_status("단말기 연결 확인 중…")
                self._startup_term_loader = DataLoader(_device_status_task, self.api, self)
                self._startup_term_loader.finished.connect(self._on_startup_terminal_done)
                self._startup_term_loader.error.connect(self._on_startup_terminal_err)
                self._startup_term_loader.start()
        elif step_id == "receive":
            self._begin_startup_receive()

    def _on_startup_terminal_done(self, rep):
        if isinstance(rep, dict):
            self.apply_terminal_ping_report(rep)
            if not rep.get("all_ok"):
                self._startup_overlay_status("일부 단말 연결 실패")
                self._startup_advance(error=True)
                return
        self._startup_advance()

    def _on_startup_terminal_err(self, msg):
        self._on_terminal_status_refresh_error(str(msg))
        self._startup_advance(error=True)

    def _finish_startup_sequence(self):
        self._startup_in_progress = False
        self._startup_waiting_dept = False
        self._startup_stop_terminal_timer()
        self._startup_stop_receive_timer()
        t = getattr(self, "_startup_load_timer", None)
        if t is not None:
            t.stop()
        overlay = getattr(self, "_startup_overlay", None)
        if overlay is not None:
            overlay.mark_all_done()
            QTimer.singleShot(500, self._dismiss_startup_overlay)
        cw = self.centralWidget()
        if cw is not None:
            cw.removeEventFilter(self)

    def _dismiss_startup_overlay(self):
        overlay = getattr(self, "_startup_overlay", None)
        if overlay is not None:
            overlay.dismiss()
        self._startup_overlay = None

    def eventFilter(self, obj, event):
        if (
            obj is self.centralWidget()
            and event.type() == QEvent.Resize
            and getattr(self, "_startup_overlay", None) is not None
            and self._startup_overlay.isVisible()
        ):
            self._startup_overlay.setGeometry(self.centralWidget().rect())
        return super().eventFilter(obj, event)

    def _show_startup_att_mode_hint(self):
        if _f22_att_mode_is_adms():
            from meal_db.adms_push_server import push_server_endpoint_from_env

            _, port = push_server_endpoint_from_env()
            ip = self._local_lan_ip_hint()
            self.statusBar().showMessage(
                "ADMS — 연결 서버 자동 기동 · 단말 접속 시 ON → 로그 수신 자동 · http://%s:%s/iclock/"
                % (ip, port),
                15000,
            )
            self._update_receive_btn_adms_tooltip()
        else:
            self.statusBar().showMessage(
                "pyzk(4370) 모드 — ADMS 전용 단말은 .env 에 MEAL_F22_ATT_MODE=adms 후 앱 재시작",
                12000,
            )

    def _apply_sidebar_terminal_rows(self, rep: dict):
        """'{설치위치} 단말기:' 바로 뒤 ON/OFF, 끝에 conn/disconn 아이콘."""
        render_terminal_status_rows(
            getattr(self, "_sidebar_terminal_rows_layout", None),
            rep,
            getattr(self, "_term_conn_icon", None),
            getattr(self, "_term_disconn_icon", None),
        )

    def _build_terminal_status_report(self) -> dict:
        return _device_status_task(self.api, self)

    def _auto_start_f22_receive(self):
        """기동 시: ADMS=연결 서버만, live/poll=수신 자동 시작. 끄려면 MEAL_F22_NO_AUTO_RECEIVE=1"""
        if (os.environ.get("MEAL_F22_NO_AUTO_RECEIVE", "").strip().lower() in ("1", "true", "yes")):
            return
        if _f22_att_mode_is_adms():
            self._ensure_adms_listener()
            QTimer.singleShot(800, lambda: self.refresh_terminal_status_async(force=True))
            return
        btn = getattr(self.dashboard, "receive_btn", None)
        if not btn or btn.isChecked():
            return
        btn.setChecked(True)

    def _ensure_adms_listener(self) -> bool:
        srv = getattr(self, "_adms_push_server", None)
        if srv is not None and srv.is_running():
            return True
        return self._start_adms_listener()

    def _set_adms_log_receive(self, active: bool, *, update_btn: bool = True):
        self._f22_receive_active = bool(active)
        btn = getattr(self.dashboard, "receive_btn", None)
        if btn and update_btn:
            btn.blockSignals(True)
            btn.setChecked(bool(active))
            btn.setText("수신중지" if active else "수신시작")
            btn.blockSignals(False)

    def _adms_any_terminal_connected(self, rep: dict) -> bool:
        if not isinstance(rep, dict):
            return False
        rows = rep.get("status_rows")
        if isinstance(rows, list) and rows:
            return any(isinstance(r, dict) and r.get("ok") for r in rows)
        return bool(rep.get("all_ok"))

    def _ensure_adms_log_receive_for_ingest(self) -> bool:
        """ADMS 출입 기록 처리 가능 여부. 사용자 수동 중지면 자동으로 켜지 않음."""
        if not _f22_att_mode_is_adms():
            return bool(getattr(self, "_f22_receive_active", False))
        if getattr(self, "_f22_receive_active", False):
            return True
        if getattr(self, "_adms_log_receive_user_paused", False):
            return False
        if os.environ.get("MEAL_F22_NO_AUTO_RECEIVE", "").strip().lower() in ("1", "true", "yes"):
            return False
        self._set_adms_log_receive(True)
        self.statusBar().showMessage("단말 기록 수신 — 로그 수신 자동 시작", 6000)
        return True

    def _maybe_auto_start_adms_log_receive(self, rep: dict = None):
        """단말 연결 ON 확인 후 로그 수신 자동 시작 (사용자 「수신중지」 중이면 생략)."""
        if not _f22_att_mode_is_adms():
            return
        if getattr(self, "_adms_log_receive_user_paused", False):
            return
        if getattr(self, "_startup_in_progress", False) and self._startup_current_step_id() != "receive":
            return
        if self._f22_receive_active:
            return
        if (os.environ.get("MEAL_F22_NO_AUTO_RECEIVE", "").strip().lower() in ("1", "true", "yes")):
            return
        if rep is None:
            rep = _build_adms_status_report(self, self.api)
        if not isinstance(rep, dict) or not self._adms_any_terminal_connected(rep):
            return
        self._set_adms_log_receive(True)
        self.statusBar().showMessage("단말 연결됨 — 로그 수신 자동 시작", 8000)

    def apply_terminal_ping_report(self, rep: dict):
        if not isinstance(rep, dict):
            return
        self._last_terminal_ping_rep = dict(rep)
        self._apply_sidebar_terminal_rows(rep)
        ss = getattr(self, "settings_screen", None)
        if ss is not None:
            ss.update_connection_status(rep)
        if _f22_att_mode_is_adms():
            self._maybe_auto_start_adms_log_receive(rep)

    def on_dashboard_receive_toggled(self, checked: bool):
        if _f22_att_mode_is_adms():
            if checked:
                self._adms_log_receive_user_paused = False
                if not self._ensure_adms_listener():
                    self._set_adms_log_receive(False)
                    return
                self._set_adms_log_receive(True)
                self.statusBar().showMessage("ADMS 로그 수신 시작 (연결 서버 유지)", 6000)
            else:
                self._adms_log_receive_user_paused = True
                self._set_adms_log_receive(False)
                self.statusBar().showMessage(
                    "ADMS 로그 수신 중지 — 연결 서버는 계속 실행, 사이드바 ON 유지", 8000
                )
            QTimer.singleShot(300, lambda: self.refresh_terminal_status_async(force=True))
            return
        self._f22_receive_active = bool(checked)
        btn = getattr(self.dashboard, "receive_btn", None)
        if btn:
            btn.setText("수신중지" if checked else "수신시작")
        if not checked:
            self._stop_f22_live_threads()
            self._stop_adms_push_server()
            self.statusBar().showMessage("단말 로그 수신을 중지했습니다.", 4000)
            self.refresh_terminal_status_async(force=True)
            return
        if _f22_att_mode_is_live():
            self._stop_adms_push_server()
            self._stop_f22_live_threads()
            self._bootstrap_f22_live_with_backfill()
        else:
            self._stop_adms_push_server()
            self._run_f22_attendance_sync_worker()
        QTimer.singleShot(500, lambda: self.refresh_terminal_status_async(force=True))

    def _uncheck_receive_btn(self):
        if _f22_att_mode_is_adms():
            self._set_adms_log_receive(False)
            return
        btn = getattr(self.dashboard, "receive_btn", None)
        if not btn:
            return
        btn.blockSignals(True)
        btn.setChecked(False)
        btn.setText("수신시작")
        btn.blockSignals(False)
        self._f22_receive_active = False

    def _on_adms_push_status(self, msg: str):
        if msg:
            self.statusBar().showMessage(str(msg)[:200], 8000)
        if msg and ("ADMS registry" in msg or "ADMS push" in msg or "ADMS cdata" in msg):
            if getattr(self, "_startup_in_progress", False):
                step = self._startup_current_step_id()
                if step == "terminal" and _f22_att_mode_is_adms():
                    QTimer.singleShot(80, self._tick_startup_terminal_adms)
                elif step == "receive" and _f22_att_mode_is_adms():
                    QTimer.singleShot(80, self._tick_startup_receive_adms)
                else:
                    self.refresh_terminal_status_async()
            else:
                self.refresh_terminal_status_async()

    def _update_receive_btn_adms_tooltip(self):
        btn = getattr(self.dashboard, "receive_btn", None)
        if not btn:
            return
        from meal_db.adms_push_server import push_server_endpoint_from_env

        _, port = push_server_endpoint_from_env()
        ip = self._local_lan_ip_hint()
        btn.setToolTip(
            "ADMS: 연결 서버는 앱 시작 시 자동. 단말 ON 후 로그 수신 자동. "
            "수신중지=로그만 중지. http://%s:%s/iclock/" % (ip, port)
        )

    def start_adms_connection_probe(self, on_finished):
        """ADMS 연결 확인 — 연결 서버가 떠 있는 상태에서 registry 확인."""
        self._adms_probe_callback = on_finished
        self._adms_probe_attempts = 0
        self._adms_probe_max = 20
        if not self._ensure_adms_listener():
            on_finished({"all_ok": False, "detail_lines": ["ADMS 연결 서버 시작 실패"]})
            return
        t = getattr(self, "_adms_probe_timer", None)
        if t is not None:
            t.stop()
        self._adms_probe_timer = QTimer(self)
        self._adms_probe_timer.timeout.connect(self._tick_adms_connection_probe)
        self._adms_probe_timer.start(1000)
        self.statusBar().showMessage(
            "ADMS 단말 연결 확인 중… (최대 %d초, 단말이 PC로 접속해야 합니다)" % self._adms_probe_max,
            10000,
        )

    def _tick_adms_connection_probe(self):
        self._adms_probe_attempts += 1
        rep = _build_adms_status_report(self, self.api)
        done = bool(rep.get("all_ok")) or self._adms_probe_attempts >= self._adms_probe_max
        if not done:
            return
        t = getattr(self, "_adms_probe_timer", None)
        if t is not None:
            t.stop()
        cb = getattr(self, "_adms_probe_callback", None)
        self._adms_probe_callback = None
        if cb is not None:
            cb(rep)
        self.refresh_terminal_status_async(force=True)

    def _start_adms_listener(self, for_probe: bool = False) -> bool:
        from meal_db.adms_push_server import (
            AdmsPushServer,
            default_resolve_device_from_settings,
            push_server_endpoint_from_env,
            set_runtime_adms_server,
        )

        self._stop_adms_push_server()
        host, port = push_server_endpoint_from_env()
        try:
            self._adms_push_server = AdmsPushServer(
                host,
                port,
                on_attendance=self._adms_push_bridge.attendance.emit,
                resolve_device=default_resolve_device_from_settings(self.api.get_device_settings),
                on_status=self._adms_push_bridge.status.emit,
                on_auth_failure=self._adms_push_bridge.auth_failure.emit,
            )
            self._adms_push_server.start()
            set_runtime_adms_server(self._adms_push_server)
            ip_hint = self._local_lan_ip_hint()
            if for_probe:
                self.statusBar().showMessage(
                    "ADMS 연결 확인 — http://%s:%s/iclock/" % (ip_hint, port),
                    10000,
                )
            else:
                self.statusBar().showMessage(
                    "ADMS 연결 서버 시작 — 단말 접속 대기 · http://%s:%s/iclock/" % (ip_hint, port),
                    12000,
                )
            QTimer.singleShot(800, lambda: self.refresh_terminal_status_async(force=True))
            return True
        except Exception as e:
            self._adms_push_server = None
            set_runtime_adms_server(None)
            self.statusBar().showMessage("ADMS 서버 시작 실패: %s" % str(e)[:120], 12000)
            return False

    def _start_adms_push_server(self, for_probe: bool = False):
        """(호환) 연결 서버 기동."""
        return self._start_adms_listener(for_probe=for_probe)

    def _local_lan_ip_hint(self) -> str:
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "PC_IP"

    def _stop_adms_push_server(self):
        from meal_db.adms_push_server import set_runtime_adms_server

        srv = getattr(self, "_adms_push_server", None)
        self._adms_push_server = None
        set_runtime_adms_server(None)
        if srv is not None:
            try:
                srv.stop()
            except Exception:
                pass

    def _stop_f22_live_threads(self):
        threads = list(getattr(self, "_f22_live_threads", []) or [])
        self._f22_live_threads = []
        for t in threads:
            try:
                t.stop_listener()
            except Exception:
                pass
        for t in threads:
            try:
                t.wait(2000)
            except Exception:
                pass

    def _bootstrap_f22_live_with_backfill(self):
        """실시간 전에 단말 버퍼를 한 번 get_attendance로 DB 반영(수신시작 전 쌓인 기록)."""
        if not getattr(self, "_f22_receive_active", False):
            return
        if self._f22_bootstrap_loader is not None and self._f22_bootstrap_loader.isRunning():
            return
        self.statusBar().showMessage(
            "실시간 수신 전: 단말에 쌓인 기록을 먼저 가져오는 중…", 8000
        )
        self._f22_bootstrap_loader = DataLoader(self.api.sync_attendance_from_f22_terminals)
        self._f22_bootstrap_loader.finished.connect(self._on_f22_backfill_before_live_done)
        self._f22_bootstrap_loader.error.connect(self._on_f22_backfill_before_live_err)
        self._f22_bootstrap_loader.start()

    def _on_f22_backfill_before_live_done(self, result):
        if not getattr(self, "_f22_receive_active", False):
            return
        if isinstance(result, dict):
            self.statusBar().showMessage(
                result.get("detail", "선행 동기화 완료")[:200], 8000
            )
            if self.stack.currentIndex() == 0:
                self.refresh_stats()
        self._start_f22_live_listeners()

    def _on_f22_backfill_before_live_err(self, msg: str):
        if not getattr(self, "_f22_receive_active", False):
            return
        self.statusBar().showMessage(
            "선행 동기화 실패 — 실시간만 계속합니다: %s" % str(msg)[:120], 10000
        )
        self._start_f22_live_listeners()

    def _start_f22_live_listeners(self):
        self._live_stable_counts = {}
        data = self.api.get_device_settings()
        terminals = data.get("f22_terminals") if isinstance(data, dict) else None
        if not isinstance(terminals, list) or not terminals:
            self.statusBar().showMessage("등록된 F22 단말이 없어 실시간 수신을 켤 수 없습니다.", 8000)
            self._uncheck_receive_btn()
            return
        try:
            pwd = int(os.environ.get("ZK_DEVICE_COMM_PASSWORD", "0") or "0")
        except ValueError:
            pwd = 0
        started = 0
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
            th = F22LiveAttendanceThread(ip, port, pwd, device_key, label)
            th.attendance.connect(self._on_f22_live_attendance)
            th.thread_failed.connect(self._on_f22_live_thread_failed)
            self._f22_live_threads.append(th)
            th.start()
            started += 1
        if started == 0:
            self.statusBar().showMessage("F22 목록에 유효한 IP가 없습니다.", 8000)
            self._uncheck_receive_btn()
            return
        self.statusBar().showMessage(
            "실시간(이벤트) 수신 중 — 단말 %d대 (기본 모드, poll 은 MEAL_F22_ATT_MODE=poll)" % started,
            8000,
        )

    def _meal_ticket_printer_endpoint(self, terminal_device: str):
        """식권 프린터 (host, port) 또는 None. 환경변수 MEAL_BIXOLON_HOST 가 있으면 최우선."""
        host = (os.environ.get("MEAL_BIXOLON_HOST") or "").strip()
        if host:
            try:
                port = int(os.environ.get("MEAL_BIXOLON_PORT", "9100") or 9100)
            except (TypeError, ValueError):
                port = 9100
            return (host, max(1, min(65535, port)))
        try:
            data = self.api.get_device_settings()
        except Exception:
            return None
        if not isinstance(data, dict) or not data.get("printer_enabled"):
            return None
        plist = data.get("printer_devices")
        if isinstance(plist, list) and plist:
            ip_part = ""
            if terminal_device and ":" in str(terminal_device):
                ip_part = str(terminal_device).split(":")[0].strip()
            dno = ""
            terminals = data.get("f22_terminals") or []
            if isinstance(terminals, list) and ip_part:
                for t in terminals:
                    if not isinstance(t, dict):
                        continue
                    if (str(t.get("ip") or "").strip()) == ip_part:
                        dno = str(t.get("device_no") or "").strip()
                        break
            for p in plist:
                if not isinstance(p, dict):
                    continue
                if dno and str(p.get("linked_terminal") or "").strip() == dno:
                    ph = (p.get("ip") or "").strip()
                    if ph:
                        try:
                            pp = int(p.get("port") or 9100)
                        except (TypeError, ValueError):
                            pp = 9100
                        return (ph, max(1, min(65535, pp)))
            p0 = plist[0]
            ph = (p0.get("ip") or "").strip()
            if not ph:
                return None
            try:
                pp = int(p0.get("port") or 9100)
            except (TypeError, ValueError):
                pp = 9100
            return (ph, max(1, min(65535, pp)))
        h = (data.get("printer_host") or "").strip()
        if not h:
            return None
        try:
            pv = int(data.get("printer_port") or 9100)
        except (TypeError, ValueError):
            pv = 9100
        return (h, max(1, min(65535, pv)))

    def _qlight_row_config(self, q: dict):
        if not isinstance(q, dict):
            return None
        qh = (q.get("ip") or "").strip()
        if not qh:
            return None
        try:
            qp = int(q.get("port") or 20000)
        except (TypeError, ValueError):
            qp = 20000
        return {
            "host": qh,
            "port": max(1, min(65535, qp)),
            "on_success": True,
            "on_failure": True,
        }

    def _qlight_config_for_terminal(self, terminal_device: str):
        """단말 이벤트 device → 경광등 (host, port) 또는 None. meal_manage: qlight_host/port 우선."""
        host = (os.environ.get("MEAL_QLIGHT_HOST") or os.environ.get("QLIGHT_HOST") or "").strip()
        if host:
            try:
                port = int(os.environ.get("MEAL_QLIGHT_PORT") or os.environ.get("QLIGHT_PORT") or "20000")
            except (TypeError, ValueError):
                port = 20000
            return {
                "host": host,
                "port": max(1, min(65535, port)),
                "on_success": True,
                "on_failure": True,
            }
        try:
            data = self.api.get_device_settings()
        except Exception:
            return None
        if not isinstance(data, dict) or not data.get("qlight_enabled"):
            return None
        # meal_manage 와 동일: 저장된 qlight_host / qlight_port 를 우선 사용
        h = (data.get("qlight_host") or "").strip()
        if h:
            try:
                pv = int(data.get("qlight_port") or 20000)
            except (TypeError, ValueError):
                pv = 20000
            return {
                "host": h,
                "port": max(1, min(65535, pv)),
                "on_success": True,
                "on_failure": True,
            }
        qlist = data.get("qlight_devices")
        if not isinstance(qlist, list) or not qlist:
            return None
        dno = self._terminal_device_no_from_event(terminal_device)
        fallback_unlinked = None
        for q in qlist:
            if not isinstance(q, dict):
                continue
            linked = str(q.get("linked_terminal") or "").strip()
            if dno and linked == dno:
                cfg = self._qlight_row_config(q)
                if cfg:
                    return cfg
            if not linked and fallback_unlinked is None:
                cfg = self._qlight_row_config(q)
                if cfg:
                    fallback_unlinked = cfg
        if fallback_unlinked is not None:
            return fallback_unlinked
        if len(qlist) == 1:
            return self._qlight_row_config(qlist[0])
        return self._qlight_row_config(qlist[0])

    def _qlight_worker_loop(self):
        """경광등 명령 순차 처리 — 동시 TCP 접속으로 인한 간헐 실패 방지."""
        while not self._qlight_worker_stop.is_set():
            try:
                job = self._qlight_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if not job:
                continue
            host = job.get("host") or ""
            port = int(job.get("port") or 20000)
            mode = (job.get("mode") or "ok").strip().lower()
            try:
                import qlight_st45l
            except ImportError:
                print("[QLIGHT] import qlight_st45l failed")
                continue
            ok = False
            try:
                if mode == "alert":
                    ok = qlight_st45l.trigger_alert(
                        host, port, blink=False, sound=qlight_st45l.default_alert_sound()
                    )
                else:
                    ok = qlight_st45l.trigger_ok(
                        host, port, blink=False, sound=qlight_st45l.default_ok_sound()
                    )
                print(
                    "[QLIGHT] trigger_%s host=%s port=%s sound=%s result=%s"
                    % (
                        "alert" if mode == "alert" else "ok",
                        host,
                        port,
                        qlight_st45l.default_alert_sound()
                        if mode == "alert"
                        else qlight_st45l.default_ok_sound(),
                        ok,
                    )
                )
            except Exception as e:
                print("[QLIGHT] trigger exception: %s" % e)

            def _done(success=ok, h=host, p=port, m=mode):
                if success:
                    label = "실패(빨강)" if m == "alert" else "성공(녹색)"
                    self.statusBar().showMessage(
                        "경광등 신호 전송 완료 — %s %s:%s" % (label, h, p), 3000
                    )
                else:
                    self.statusBar().showMessage(
                        "경광등 신호 전송 실패 (%s:%s)" % (h, p), 5000
                    )

            try:
                QTimer.singleShot(0, _done)
            except Exception:
                pass

    def _trigger_qlight_async(self, terminal_device: str, *, mode: str):
        """mode: ok(녹색) | alert(빨강)."""
        cfg = self._qlight_config_for_terminal(terminal_device)
        if not cfg:
            try:
                data = self.api.get_device_settings()
                if isinstance(data, dict) and data.get("qlight_enabled"):
                    def _skip():
                        self.statusBar().showMessage(
                            "경광등 설정을 찾지 못했습니다. IP·연결단말기·저장 여부를 확인하세요.",
                            5000,
                        )

                    QTimer.singleShot(0, _skip)
            except Exception:
                pass
            return
        mode = (mode or "").strip().lower()
        if mode == "alert" and not cfg.get("on_failure", True):
            return
        if mode != "alert" and not cfg.get("on_success", True):
            return
        host = cfg["host"]
        port = int(cfg["port"])

        def _pending():
            label = "실패(빨강)" if mode == "alert" else "성공(녹색)"
            self.statusBar().showMessage("경광등 신호 전송 중 — %s %s:%s" % (label, host, port), 3000)

        try:
            QTimer.singleShot(0, _pending)
        except Exception:
            pass

        try:
            self._qlight_queue.put(
                {"host": host, "port": port, "mode": mode},
                block=False,
            )
            print("[QLIGHT] queued host=%s port=%s mode=%s" % (host, port, mode))
        except Exception:
            def _warn():
                self.statusBar().showMessage(
                    "경광등 신호 대기열 오류 (%s:%s)" % (host, port), 5000
                )

            try:
                QTimer.singleShot(0, _warn)
            except Exception:
                pass

    def _terminal_device_no_from_event(self, terminal_device: str) -> str:
        """실시간 이벤트 device → 설정 f22_terminals 의 단말기 번호(device_no)."""
        td = str(terminal_device or "").strip()
        if not td:
            return ""
        try:
            data = self.api.get_device_settings()
        except Exception:
            return ""
        if not isinstance(data, dict):
            return ""
        terminals = data.get("f22_terminals") or []
        if not isinstance(terminals, list):
            return ""
        if td.startswith("adms:"):
            sn = td[5:].strip()
            for t in terminals:
                if not isinstance(t, dict):
                    continue
                t_sn = str(
                    t.get("serial_no") or t.get("adms_sn") or t.get("device_sn") or ""
                ).strip()
                if sn and t_sn and t_sn == sn:
                    return str(t.get("device_no") or "").strip()
            return ""
        ip_part = td.split(":")[0].strip() if ":" in td else td
        if not ip_part:
            return ""
        for t in terminals:
            if isinstance(t, dict) and str(t.get("ip") or "").strip() == ip_part:
                return str(t.get("device_no") or "").strip()
        return ""

    def _print_meal_ticket_async(self, ingest_result: dict, terminal_device: str):
        """DB 반영 성공 시 빅솔론 ESC/POS 식권 출력(네트워크, UI 블로킹 방지)."""
        if int(ingest_result.get("inserted") or 0) != 1:
            return
        ep = self._meal_ticket_printer_endpoint(terminal_device)
        if not ep:
            return
        ph, pp = ep
        try:
            nv = int(os.environ.get("MEAL_BIXOLON_NV_IMAGE", "1") or "1")
        except ValueError:
            nv = 1
        nv = max(1, min(255, nv))
        emp_no = str(ingest_result.get("emp_no") or "")
        name = str(ingest_result.get("emp_name") or "")
        dt = str(ingest_result.get("date_time_str") or "")
        meal = str(ingest_result.get("meal_type") or "")
        dno = self._terminal_device_no_from_event(terminal_device)

        def _job():
            ok = bixolon_print.print_image_only(
                ph, pp, nv, emp_no, name, dt, meal, terminal_no=dno
            )
            if not ok:

                def _warn():
                    self.statusBar().showMessage(
                        "식권 프린터 출력 실패 (%s:%s)" % (ph, pp), 5000
                    )

                try:
                    QTimer.singleShot(0, _warn)
                except Exception:
                    pass

        threading.Thread(target=_job, daemon=True).start()

    def _on_f22_live_attendance(self, rec: dict):
        if not isinstance(rec, dict):
            return
        if _f22_att_mode_is_adms() and not self._ensure_adms_log_receive_for_ingest():
            self.statusBar().showMessage(
                "ADMS 로그 수신이 꺼져 있어 기록을 저장하지 않았습니다. 대시보드에서 「수신시작」을 누르세요.",
                10000,
            )
            return
        stream_live = _attendance_stream_is_live(rec)
        device_live = _attendance_is_live_for_ticket(rec)
        try:
            sk = terminal_attendance_stable_key(rec)
            if sk:
                n = self._live_stable_counts.get(sk, 0)
                self._live_stable_counts[sk] = n + 1
                suff = None if n == 0 else str(n)
            else:
                suff = None
            r = self.api.ingest_terminal_attendance_record(rec, dedup_salt=suff)
        except Exception as e:
            self.statusBar().showMessage("실시간 기록 저장 실패: %s" % str(e)[:100], 6000)
            return
        # 프린터·경광등: 방금 인증만. 저장된 후 밀려 온 기록은 저장만.
        should_device = (
            device_live
            and not int(r.get("unknown") or 0)
            and not int(r.get("out_of_range") or 0)
        )
        age_sec = None
        ts = rec.get("timestamp")
        if isinstance(ts, datetime):
            tsn = ts.astimezone().replace(tzinfo=None) if ts.tzinfo else ts.replace(tzinfo=None)
            age_sec = round((datetime.now() - tsn).total_seconds(), 1)
        print(
            "[DEVICE] attendance device=%s stream_live=%s device_live=%s age_sec=%s "
            "inserted=%s skipped=%s unknown=%s trigger=%s"
            % (
                rec.get("device"),
                stream_live,
                device_live,
                age_sec,
                r.get("inserted"),
                r.get("skipped"),
                r.get("unknown"),
                should_device,
            )
        )
        if should_device:
            if r.get("is_today", True) and int(r.get("inserted") or 0):
                self._print_meal_ticket_async(r, str(rec.get("device") or ""))
            self._trigger_qlight_async(str(rec.get("device") or ""), mode="ok")
        if int(r.get("unknown") or 0):
            uid = str(rec.get("user_id") or "").strip()
            self.statusBar().showMessage(
                "단말 사용자 ID(%s)를 사원 DB에서 찾지 못했습니다(사번·카드번호·활성 상태 확인)."
                % (uid or "?"),
                10000,
            )
        elif int(r.get("inserted") or 0):
            self.statusBar().showMessage(
                "식사 기록 저장: %s %s"
                % (r.get("emp_name") or "", r.get("date_time_str") or ""),
                4000,
            )
        if self.stack.currentIndex() == 0 and int(r.get("inserted") or 0):
            QTimer.singleShot(0, self.refresh_stats)

    def _on_adms_auth_failure(self, rec: dict):
        if not isinstance(rec, dict):
            return
        if _f22_att_mode_is_adms() and not self._ensure_adms_log_receive_for_ingest():
            return
        try:
            r = self.api.ingest_terminal_auth_failure_record(rec)
        except Exception as e:
            self.statusBar().showMessage("인증 실패 기록 저장 오류: %s" % str(e)[:100], 8000)
            return
        if int(r.get("inserted") or 0):
            card = str(r.get("card_no") or "").strip() or "(카드번호 없음)"
            reason = str(r.get("event_label") or "인증 실패")
            self.statusBar().showMessage(
                "RF 카드 인증 실패 기록: %s — %s" % (card, reason),
                6000,
            )
        # 인증 실패 경광등도 라이브(rtlog)만
        if _attendance_is_live_for_ticket(rec) and not int(r.get("out_of_range") or 0):
            self._trigger_qlight_async(str(rec.get("device") or ""), mode="alert")
        if int(r.get("inserted") or 0):
            if self.stack.currentIndex() == 4:
                raw = getattr(self, "raw_data", None)
                if raw is not None:
                    QTimer.singleShot(0, raw.load_data)
            if self.stack.currentIndex() == 0:
                QTimer.singleShot(0, self.load_recent_logs)

    def _on_f22_live_thread_failed(self, msg: str):
        self.statusBar().showMessage(str(msg)[:160], 10000)

    def _tick_attlog_clear_schedule(self):
        if self._attlog_clear_loader is not None and self._attlog_clear_loader.isRunning():
            return
        try:
            cfg = self.api.get_attlog_clear_settings()
        except Exception:
            return
        mins = int(cfg.get("minutes_after_meal_end") or 0)
        now = datetime.now()
        today = now.date()
        if self._attlog_clear_fired_date != today:
            self._attlog_clear_fired_keys.clear()
            self._attlog_clear_fired_date = today
        policies = self.api.get_active_policies_for_attlog_schedule()
        due_keys = []
        for p in policies:
            if not isinstance(p, dict):
                continue
            try:
                pid = int(p["id"])
            except (TypeError, ValueError, KeyError):
                continue
            key = (today.isoformat(), pid)
            if key in self._attlog_clear_fired_keys:
                continue
            try:
                trig = _meal_policy_attlog_trigger_datetime(today, p.get("end_time"), mins)
            except Exception:
                continue
            if trig <= now < trig + timedelta(minutes=2):
                due_keys.append(key)
        if not due_keys:
            return
        self._attlog_clear_pending_keys = due_keys
        self._attlog_clear_loader = DataLoader(self.api.sync_attendance_from_f22_terminals)
        self._attlog_clear_loader.finished.connect(self._on_attlog_clear_sync_done)
        self._attlog_clear_loader.error.connect(self._on_attlog_clear_sync_err)
        self._attlog_clear_loader.start()

    def _on_attlog_clear_sync_done(self, result):
        self._attlog_clear_loader = None
        for k in self._attlog_clear_pending_keys or []:
            self._attlog_clear_fired_keys.add(k)
        self._attlog_clear_pending_keys = []
        if isinstance(result, dict):
            self.statusBar().showMessage(
                "예약 단말 로그 정리: %s" % (result.get("detail", "완료")[:160]), 8000
            )

    def _on_attlog_clear_sync_err(self, msg: str):
        self._attlog_clear_loader = None
        self._attlog_clear_pending_keys = []
        self.statusBar().showMessage("예약 단말 로그 정리 실패: %s" % str(msg)[:120], 10000)

    def _chain_f22_att_poll(self):
        """이전 읽기 완료 후 짧은 간격으로 다음 읽기 예약(실시간에 가깝게)."""
        if not getattr(self, "_f22_receive_active", False):
            return
        if _f22_att_mode_is_live() or _f22_att_mode_is_adms():
            return
        self._run_f22_attendance_sync_worker()

    def _schedule_next_f22_att_poll(self):
        if not getattr(self, "_f22_receive_active", False):
            return
        if _f22_att_mode_is_live() or _f22_att_mode_is_adms():
            return
        QTimer.singleShot(int(self._f22_att_poll_gap_ms), self._chain_f22_att_poll)

    def _run_f22_attendance_sync_worker(self):
        if not getattr(self, "_f22_receive_active", False):
            return
        if _f22_att_mode_is_live() or _f22_att_mode_is_adms():
            return
        if self._f22_att_sync_loader is not None and self._f22_att_sync_loader.isRunning():
            return
        self._f22_att_sync_loader = DataLoader(self.api.sync_attendance_from_f22_terminals)
        self._f22_att_sync_loader.finished.connect(self._on_f22_att_sync_done)
        self._f22_att_sync_loader.error.connect(self._on_f22_att_sync_err)
        self._f22_att_sync_loader.start()

    def _on_f22_att_sync_done(self, result):
        if isinstance(result, dict):
            self.statusBar().showMessage(result.get("detail", "단말 동기화 완료"), 8000)
        else:
            self.statusBar().showMessage("단말 동기화 완료", 5000)
        # DB를 비운 뒤에도 표가 남지 않도록, 신규 삽입 여부와 관계없이 대시보드면 통계·로그 표 갱신
        if self.stack.currentIndex() == 0:
            self.refresh_stats()
        if not _f22_att_mode_is_live() and not _f22_att_mode_is_adms():
            self._schedule_next_f22_att_poll()

    def _on_f22_att_sync_err(self, msg: str):
        self.statusBar().showMessage("단말 로그 수신 실패: %s" % str(msg)[:120], 10000)
        if self.stack.currentIndex() == 0:
            self.refresh_stats()
        if not _f22_att_mode_is_live() and not _f22_att_mode_is_adms():
            self._schedule_next_f22_att_poll()

    def refresh_terminal_status_async(self, force: bool = False):
        if _f22_att_mode_is_adms():
            if force or self._term_status_loader is None or not self._term_status_loader.isRunning():
                try:
                    rep = self._build_terminal_status_report()
                    self.apply_terminal_ping_report(rep)
                except Exception as e:
                    self._on_terminal_status_refresh_error(str(e))
            return
        if not force and self._term_status_loader is not None and self._term_status_loader.isRunning():
            return
        self._term_status_loader = DataLoader(_device_status_task, self.api, self)
        self._term_status_loader.finished.connect(self._on_terminal_status_refresh_done)
        self._term_status_loader.error.connect(self._on_terminal_status_refresh_error)
        self._term_status_loader.start()

    def _on_terminal_status_refresh_done(self, rep):
        if isinstance(rep, dict):
            self.apply_terminal_ping_report(rep)

    def _on_terminal_status_refresh_error(self, msg):
        rep = {
            "all_ok": False,
            "short": "단말: 확인 실패",
            "detail_lines": [str(msg)],
            "status_rows": [{"label": "단말기:", "ok": False}],
        }
        self._last_terminal_ping_rep = rep
        self._apply_sidebar_terminal_rows(rep)

    def resizeEvent(self, event):
        """창 크기가 화면 가용 영역을 넘지 않도록 제한 (setGeometry 경고 방지)"""
        screen = QApplication.primaryScreen()
        if screen:
            available = screen.availableGeometry()
            w = event.size().width()
            h = event.size().height()
            if w > available.width() or h > available.height():
                w = min(w, available.width())
                h = min(h, available.height())
                event = QResizeEvent(QSize(w, h), event.oldSize())
        super().resizeEvent(event)

    def on_company_changed(self):
        self.company_sync_loader = DataLoader(self.api.get_companies)
        self.company_sync_loader.finished.connect(self.on_company_sync_finished)
        self.company_sync_loader.error.connect(self.on_company_sync_error)
        self.company_sync_loader.start()

    def on_company_sync_error(self, err_msg: str):
        self.companies_data = []
        self.statusBar().showMessage("회사 목록 로드 실패 — DB·MEAL_DB_* 확인", 8000)
        QMessageBox.warning(
            self,
            "DB",
            "회사 목록을 불러오지 못했습니다. MariaDB 실행·접속 정보·스키마를 확인하세요.\n\n%s" % err_msg,
        )

    def on_company_sync_finished(self, data):
        if isinstance(data, list):
            self.companies_data = data
            self.departments.update_company_combo(self.companies_data)
            self.employees.update_company_combo(self.companies_data)
            self.sync_departments()

    def sync_departments(self):
        self.dept_sync_loader = DataLoader(self.api.get_departments)
        self.dept_sync_loader.finished.connect(self.on_dept_sync_finished)
        self.dept_sync_loader.error.connect(self.on_dept_sync_error)
        self.dept_sync_loader.start()

    def on_dept_sync_error(self, err_msg: str):
        self.departments_data = []
        self.statusBar().showMessage("부서 목록 로드 실패", 8000)
        QMessageBox.warning(self, "DB", "부서 목록을 불러오지 못했습니다.\n\n%s" % err_msg)

    def on_dept_sync_finished(self, data):
        if isinstance(data, list):
            self.departments_data = data
            if self.stack.currentIndex() == 2:
                self.departments.load_data()
            self.employees.update_dept_combo()
            self.reports._refresh_dept_sub_combo()
        if getattr(self, "_startup_waiting_dept", False):
            self._startup_waiting_dept = False
            t = getattr(self, "_startup_load_timer", None)
            if t is not None:
                t.stop()
            QTimer.singleShot(80, self._startup_advance)

    def _on_dashboard_stats_timer(self):
        if self.stack.currentIndex() == 0:
            self.refresh_stats()

    def refresh_stats(self):
        self.refresh_active_screen()
        if hasattr(self, "stat_loader") and self.stat_loader.isRunning():
            self._stats_refresh_pending = True
            return
        self.stat_loader = DataLoader(self.api.get_stats)
        self.stat_loader.finished.connect(self.display_stats)
        self.stat_loader.error.connect(self.on_stat_loader_error)
        self.stat_loader.start()

    def on_stat_loader_error(self, err_msg: str):
        self.statusBar().showMessage("오늘 통계 로드 실패: %s" % str(err_msg)[:120], 10000)
        if getattr(self, "_stats_refresh_pending", False):
            self._stats_refresh_pending = False
            QTimer.singleShot(200, self.refresh_stats)
        
    def refresh_active_screen(self):
        idx = self.stack.currentIndex()
        if idx == 3: # EmployeeScreen
            if not hasattr(self.employees, 'loader') or not self.employees.loader.isRunning():
                self.employees.load_data()
        elif idx == 4: # RawDataScreen
            pass # Explicit search only
    def display_stats(self, stats):
        if stats and isinstance(stats, dict):
            # Dynamic stats update
            self.dashboard.update_stats(stats)
            # 테이블도 통계와 같은 기준일로 조회 (타임존/날짜 불일치 방지)
            self.load_recent_logs(stats.get("date"))
        if getattr(self, "_stats_refresh_pending", False):
            self._stats_refresh_pending = False
            QTimer.singleShot(0, self.refresh_stats)

    def load_recent_logs(self, date_value=None):
        # 통계의 date 사용 (없으면 OS 로컬 타임존 기준 오늘)
        if date_value:
            if hasattr(date_value, "strftime"):
                day_str = date_value.strftime("%Y-%m-%d")
            else:
                day_str = str(date_value)[:10]
        else:
            day_str = datetime.now().astimezone().date().strftime("%Y-%m-%d")
        if hasattr(self, "recent_loader") and self.recent_loader.isRunning():
            self._recent_logs_pending_day = day_str
            return
        self._recent_logs_pending_day = None
        day_arg = day_str

        def _dashboard_logs_bundle():
            ok1, meal = self.api.get_raw_data("", day_arg, day_arg, non_void_only=True)
            if not ok1:
                return False, meal
            ok2, fails = self.api.get_auth_failure_logs("", day_arg, day_arg)
            if not ok2:
                return False, fails
            return True, {"meal": meal, "failures": fails}

        self.recent_loader = DataLoader(_dashboard_logs_bundle)
        self.recent_loader.finished.connect(self.on_recent_logs_finished)
        self.recent_loader.error.connect(self.on_recent_loader_error)
        self.recent_loader.start()

    def on_recent_loader_error(self, err_msg: str):
        self.statusBar().showMessage("최근 식사 로그 로드 실패: %s" % str(err_msg)[:120], 10000)

    def on_recent_logs_finished(self, data):
        if isinstance(data, tuple) and len(data) >= 2:
            if not data[0]:
                self.statusBar().showMessage(
                    "대시보드 로그 로드 실패: %s" % str(data[1])[:120], 10000
                )
                return
            data = data[1]
        if isinstance(data, dict):
            self.dashboard.update_recent(data.get("meal") or [])
            self.dashboard.update_recent_failures(data.get("failures") or [])
        elif isinstance(data, list):
            self.dashboard.update_recent(data)
        pending = getattr(self, "_recent_logs_pending_day", None)
        if pending:
            self._recent_logs_pending_day = None
            QTimer.singleShot(0, lambda d=pending: self.load_recent_logs(d))
    def switch_screen(self, idx):
        if getattr(self, "_dashboard_stats_timer", None):
            self._dashboard_stats_timer.stop()
        prev = self.stack.currentIndex()
        if prev == 4 and idx != 4 and hasattr(self, "raw_data"):
            self.raw_data.reset_view()
        self.stack.setCurrentIndex(idx)
        for i, btn in enumerate(self.nav_btns):
            btn.setProperty("active", "true" if i == idx else "false")
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        if idx == 0:
            self.refresh_terminal_status_async(force=True)
            self.refresh_stats()
            self._dashboard_stats_timer.start()
        if idx == 1: self.companies.load_data()
        if idx == 2: self.departments.load_data()  # refresh table when opening tab (filters by selected company)
        if idx == 3:
            self.employees.update_dept_combo()  # 부서 콤보 갱신 (회사 1개 기준)
            self.employees.load_data()
        if idx == 4: pass # Explicit search only
        if idx == 5: self.policies.load_data()
        if idx == 6:
            self.reports._refresh_dept_sub_combo()
            self.reports.load_data()
        if idx == 7:  # 관리자 비번설정
            self.admin_screen.load_data()
        if idx == 8:
            self.settings_screen.load_data()
            self.settings_screen._sync_f22_ui_for_att_mode()
            self.refresh_terminal_status_async(force=True)

    def closeEvent(self, event):
        self._f22_receive_active = False
        t = getattr(self, "_adms_probe_timer", None)
        if t is not None:
            t.stop()
        self._stop_f22_live_threads()
        self._stop_adms_push_server()
        ev = getattr(self, "_qlight_worker_stop", None)
        if ev is not None:
            ev.set()
        self.api.close()
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())
