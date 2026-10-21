"""ZKTeco Live20R USB 지문 등록기 — pyzkfp 우선, zkteco_live20r(ctypes) 폴백."""

from __future__ import annotations

import base64
import os
import secrets
import sys

from PyQt5.QtCore import Qt, QMutex, QThread, QTimer, QWaitCondition, pyqtSignal
from PyQt5.QtGui import QColor, QImage, QPalette, QPixmap
from PyQt5.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QMainWindow,
    QWidget,
)

_FP_DARK_DIALOG_QSS = """
QDialog#FingerprintRegisterDialog {
    background-color: #0f172a;
    color: #f8fafc;
}
QDialog#FingerprintRegisterDialog QLabel#FpEmpLabel,
QDialog#FingerprintRegisterDialog QLabel#FpStatusLabel {
    background-color: #1e293b;
    color: #ffffff;
    font-size: 20px;
    font-weight: bold;
    font-family: 'Malgun Gothic', 'Segoe UI', sans-serif;
    border: 1px solid #475569;
    border-radius: 8px;
    padding: 10px 14px;
    min-height: 28px;
}
QDialog#FingerprintRegisterDialog QLabel#FpMatchLabel {
    background-color: transparent;
    color: #ffffff;
    font-size: 18px;
    font-weight: bold;
    font-family: 'Malgun Gothic', 'Segoe UI', sans-serif;
    border: none;
    padding: 4px 8px;
}
QMessageBox {
    background-color: #0f172a;
    color: #f8fafc;
}
QMessageBox QLabel {
    color: #f8fafc;
    background-color: transparent;
    font-size: 16px;
    font-weight: bold;
    font-family: 'Malgun Gothic', 'Segoe UI', sans-serif;
}
QMessageBox QPushButton {
    background-color: #3b82f6;
    color: #ffffff;
    border: none;
    border-radius: 8px;
    padding: 8px 18px;
    min-height: 34px;
    min-width: 88px;
    font-size: 15px;
    font-weight: bold;
    font-family: 'Malgun Gothic', 'Segoe UI', sans-serif;
}
QMessageBox QPushButton:hover {
    background-color: #2563eb;
}
QMessageBox QPushButton:pressed {
    background-color: #1d4ed8;
}
"""


def _apply_windows_dark_titlebar(widget) -> None:
    """Windows 10/11 창 제목표시줄을 어두운 모드로 맞춤."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        hwnd = int(widget.winId())
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        value = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd,
            DWMWA_USE_IMMERSIVE_DARK_MODE,
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
        # older Windows builds
        DWMWA_USE_IMMERSIVE_DARK_MODE_BEFORE_20H1 = 19
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd,
            DWMWA_USE_IMMERSIVE_DARK_MODE_BEFORE_20H1,
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
    except Exception:
        pass


def _apply_dark_palette(widget) -> None:
    pal = QPalette(widget.palette())
    bg = QColor("#0f172a")
    fg = QColor("#f8fafc")
    base = QColor("#1e293b")
    pal.setColor(QPalette.Window, bg)
    pal.setColor(QPalette.WindowText, fg)
    pal.setColor(QPalette.Base, base)
    pal.setColor(QPalette.AlternateBase, QColor("#111b2d"))
    pal.setColor(QPalette.Text, fg)
    pal.setColor(QPalette.Button, QColor("#334155"))
    pal.setColor(QPalette.ButtonText, fg)
    pal.setColor(QPalette.BrightText, QColor("#ffffff"))
    pal.setColor(QPalette.Highlight, QColor("#6366f1"))
    pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    widget.setPalette(pal)
    widget.setAutoFillBackground(True)


_FP_ALERT_QSS = """
QDialog#FpDarkAlertDialog {
    background-color: #0f172a;
    color: #f8fafc;
}
QDialog#FpDarkAlertDialog QLabel#FpAlertTitle {
    color: #f8fafc;
    font-size: 22px;
    font-weight: bold;
    font-family: 'Malgun Gothic', 'Segoe UI', sans-serif;
}
QDialog#FpDarkAlertDialog QLabel#FpAlertBody {
    color: #e2e8f0;
    font-size: 16px;
    font-weight: bold;
    font-family: 'Malgun Gothic', 'Segoe UI', sans-serif;
    background-color: #1e293b;
    border: 1px solid #475569;
    border-radius: 8px;
    padding: 14px 16px;
}
QDialog#FpDarkAlertDialog QPushButton#PrimaryBtn {
    background-color: #3b82f6;
    color: white;
    border-radius: 8px;
    padding: 10px 20px;
    font-weight: bold;
    font-size: 16px;
    min-height: 40px;
    min-width: 100px;
    font-family: 'Malgun Gothic';
    border: none;
}
QDialog#FpDarkAlertDialog QPushButton#PrimaryBtn:hover { background-color: #2563eb; }
QDialog#FpDarkAlertDialog QPushButton#SecondaryBtn {
    background-color: #64748b;
    color: #f8fafc;
    border-radius: 8px;
    padding: 10px 20px;
    font-weight: bold;
    font-size: 16px;
    min-height: 40px;
    min-width: 100px;
    font-family: 'Malgun Gothic';
    border: none;
}
QDialog#FpDarkAlertDialog QPushButton#SecondaryBtn:hover { background-color: #475569; }
"""


class DarkAlertDialog(QDialog):
    """지문등록 확인창과 동일한 다크 알림/확인 다이얼로그."""

    def __init__(
        self,
        parent,
        title: str,
        text: str,
        *,
        kind: str = "info",
        buttons=("ok",),
    ):
        super().__init__(parent)
        self.setObjectName("FpDarkAlertDialog")
        self.setWindowTitle(title or "알림")
        self.setModal(True)
        self.setMinimumWidth(420)
        _apply_parent_stylesheet(self, parent)
        self.setStyleSheet((self.styleSheet() or "") + "\n" + _FP_ALERT_QSS)
        _apply_dark_palette(self)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(14)

        title_lab = QLabel(title or "알림")
        title_lab.setObjectName("FpAlertTitle")
        title_lab.setAlignment(Qt.AlignCenter)
        lay.addWidget(title_lab)

        body = QLabel(text or "")
        body.setObjectName("FpAlertBody")
        body.setWordWrap(True)
        body.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        body.setMinimumWidth(360)
        lay.addWidget(body)

        row = QHBoxLayout()
        row.addStretch(1)
        if "cancel" in buttons:
            cancel_btn = QPushButton("취소")
            cancel_btn.setObjectName("SecondaryBtn")
            cancel_btn.clicked.connect(self.reject)
            row.addWidget(cancel_btn)
        ok_btn = QPushButton("확인")
        ok_btn.setObjectName("PrimaryBtn")
        ok_btn.clicked.connect(self.accept)
        ok_btn.setDefault(True)
        row.addWidget(ok_btn)
        lay.addLayout(row)

        QTimer.singleShot(0, lambda: _apply_windows_dark_titlebar(self))

    def showEvent(self, event):
        super().showEvent(event)
        _apply_windows_dark_titlebar(self)


def dark_question(parent, title: str, text: str, buttons=QMessageBox.Ok | QMessageBox.Cancel):
    dlg = DarkAlertDialog(parent, title, text, kind="question", buttons=("ok", "cancel"))
    return QMessageBox.Ok if dlg.exec_() == QDialog.Accepted else QMessageBox.Cancel


def dark_warning(parent, title: str, text: str):
    dlg = DarkAlertDialog(parent, title, text, kind="warning", buttons=("ok",))
    return dlg.exec_()


def dark_information(parent, title: str, text: str):
    dlg = DarkAlertDialog(parent, title, text, kind="info", buttons=("ok",))
    return dlg.exec_()


class StepProgressOverlay(QWidget):
    """앱 시작 화면과 같은 단계별 진행 오버레이."""

    _STATE_STYLE = {
        "pending": ("○", "#64748b"),
        "active": ("▶", "#60a5fa"),
        "done": ("✓", "#4ade80"),
        "error": ("✗", "#f87171"),
    }

    def __init__(self, parent, title: str, step_labels: list):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet("background-color: rgba(15, 23, 42, 0.94);")
        outer = QVBoxLayout(self)
        outer.setAlignment(Qt.AlignCenter)
        panel = QFrame()
        panel.setObjectName("FpStepPanel")
        panel.setStyleSheet(
            "QFrame#FpStepPanel { background-color: #1e293b; border: 1px solid #475569; "
            "border-radius: 14px; }"
        )
        panel.setMinimumWidth(420)
        panel.setMaximumWidth(540)
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(28, 24, 28, 24)
        pl.setSpacing(14)
        title_lab = QLabel(title or "진행 중")
        title_lab.setAlignment(Qt.AlignCenter)
        title_lab.setStyleSheet(
            "color: #f8fafc; font-size: 24px; font-weight: bold; font-family: 'Malgun Gothic';"
        )
        pl.addWidget(title_lab)
        self._subtitle = QLabel("준비 중…")
        self._subtitle.setAlignment(Qt.AlignCenter)
        self._subtitle.setWordWrap(True)
        self._subtitle.setStyleSheet(
            "color: #94a3b8; font-size: 15px; font-weight: bold; font-family: 'Malgun Gothic';"
        )
        pl.addWidget(self._subtitle)
        self._base_labels = [str(t) for t in step_labels]
        self._row_labels: list = []
        for txt in self._base_labels:
            row = QLabel("○  %s" % txt)
            row.setStyleSheet(
                "color: #64748b; font-size: 16px; font-weight: bold; font-family: 'Malgun Gothic'; "
                "padding: 2px 0;"
            )
            self._row_labels.append(row)
            pl.addWidget(row)
        outer.addWidget(panel)

    def set_step_state(self, index: int, state: str, detail: str = ""):
        if index < 0 or index >= len(self._row_labels):
            return
        prefix, color = self._STATE_STYLE.get(state, self._STATE_STYLE["pending"])
        base = self._base_labels[index]
        shown = ("%s — %s" % (base, detail)) if detail else base
        self._row_labels[index].setText("%s  %s" % (prefix, shown))
        self._row_labels[index].setStyleSheet(
            "color: %s; font-size: 16px; font-weight: bold; font-family: 'Malgun Gothic'; "
            "padding: 2px 0;" % color
        )
        if state == "active":
            self._subtitle.setText(shown)

    def set_status(self, text: str):
        self._subtitle.setText(str(text or ""))

    def mark_all_done(self):
        for i in range(len(self._row_labels)):
            self.set_step_state(i, "done")
        self._subtitle.setText("완료")

    def dismiss(self):
        self.hide()
        self.deleteLater()




def _zk_match_score_to_percent(score: int) -> int:
    if score < 0:
        return 0
    try:
        mx = max(1, int(os.environ.get("ZK_DB_MATCH_SCORE_MAX", "100")))
    except ValueError:
        mx = 100
    return min(100, max(0, int(round(100.0 * float(score) / float(mx)))))


def _fp_led_ready_pyzkfp(zk) -> None:
    for code in (102, 101):
        try:
            zk.SetParameters(code, (1, 0, 0, 0))
            return
        except Exception:
            continue


def _fp_led_idle_pyzkfp(zk) -> None:
    for code in (102, 101):
        try:
            zk.SetParameters(code, (0, 0, 0, 0))
        except Exception:
            pass


def _apply_parent_stylesheet(widget, parent) -> None:
    p = parent
    while p is not None:
        if isinstance(p, QMainWindow):
            ss = p.styleSheet()
            if ss:
                widget.setStyleSheet(ss)
            return
        try:
            p = p.parent()
        except Exception:
            break


class FingerprintEnrollTwoFingerThread(QThread):
    """손가락 2개 × 2회 캡처 + DBMatch. 완료 시 finished_ok."""

    need_confirm = pyqtSignal(str)
    progress = pyqtSignal(str)
    match_percent = pyqtSignal(int, str)
    preview = pyqtSignal(bytes, int, int)
    finished_ok = pyqtSignal(bytes, bytes, int, int, bytes, int, int)
    failed = pyqtSignal(str)

    def __init__(self, min_match_percent: int = 80):
        super().__init__()
        self._min_pct = max(1, min(100, int(min_match_percent)))
        self._mutex = QMutex()
        self._cond = QWaitCondition()
        self._cancel_requested = False

    def acknowledge_confirm(self) -> None:
        self._mutex.lock()
        self._cond.wakeAll()
        self._mutex.unlock()

    def request_cancel(self) -> None:
        self._cancel_requested = True
        self.acknowledge_confirm()

    def _wait_confirm(self) -> bool:
        self._mutex.lock()
        self._cond.wait(self._mutex)
        cancelled = self._cancel_requested
        self._mutex.unlock()
        return not cancelled

    def _wait_one_pyzkfp(self, zk):
        _fp_led_ready_pyzkfp(zk)
        try:
            while not self.isInterruptionRequested():
                r = zk.AcquireFingerprint()
                if r:
                    _fp_led_idle_pyzkfp(zk)
                    return r
                self.msleep(45)
        finally:
            _fp_led_idle_pyzkfp(zk)
        return None

    def _wait_one_ctypes(self, sess):
        try:
            sess.led_scan_ready()
            while not self.isInterruptionRequested():
                got = sess.try_acquire_once()
                if got:
                    try:
                        sess.led_scan_idle()
                    except Exception:
                        pass
                    return got
                self.msleep(45)
        finally:
            try:
                sess.led_scan_idle()
            except Exception:
                pass
        return None

    def _enroll_two_fingers_pyzkfp(self, zk, w, h):
        self.progress.emit("첫 번째 손가락을 센서에 대십시오.")
        r1 = self._wait_one_pyzkfp(zk)
        if not r1:
            self.failed.emit("취소되었습니다.")
            return
        t1a, img1 = r1
        self.preview.emit(bytes(img1), w, h)
        self.need_confirm.emit(
            "첫 번째 지문이 캡처되었습니다.\n"
            "같은 손가락으로 한 번 더 찍습니다.\n[확인]을 누르면 다음 단계로 진행합니다."
        )
        if not self._wait_confirm():
            self.failed.emit("취소되었습니다.")
            return

        slot0 = pct0 = None
        while not self.isInterruptionRequested():
            self.progress.emit("같은 첫 번째 손가락을 다시 대십시오.")
            r2 = self._wait_one_pyzkfp(zk)
            if not r2:
                self.failed.emit("취소되었습니다.")
                return
            t1b, img2 = r2
            self.preview.emit(bytes(img2), w, h)
            score = int(zk.DBMatch(t1a, t1b))
            if score < 0:
                self.failed.emit("첫 번째 손가락 매칭 오류 (코드 %s)" % score)
                return
            pct = _zk_match_score_to_percent(score)
            self.match_percent.emit(pct, "첫 번째 손가락(동일 여부)")
            if pct >= self._min_pct:
                slot0 = bytes(bytearray(t1b))
                pct0 = pct
                break
            self.progress.emit(
                "일치율 %d%% — %d%% 이상 필요. 같은 손가락으로 다시 찍어 주세요." % (pct, self._min_pct)
            )
            self.msleep(400)
        if slot0 is None:
            return

        self.need_confirm.emit(
            "첫 번째 손가락 확인 완료.\n이제 다른 손가락을 사용합니다.\n[확인] 후 두 번째 손가락을 대십시오."
        )
        if not self._wait_confirm():
            self.failed.emit("취소되었습니다.")
            return

        self.progress.emit("두 번째 손가락을 센서에 대십시오.")
        r3 = self._wait_one_pyzkfp(zk)
        if not r3:
            self.failed.emit("취소되었습니다.")
            return
        t2a, img3 = r3
        self.preview.emit(bytes(img3), w, h)
        self.need_confirm.emit(
            "두 번째 지문이 캡처되었습니다.\n같은 손가락으로 한 번 더 찍습니다.\n[확인]을 누르면 캡처합니다."
        )
        if not self._wait_confirm():
            self.failed.emit("취소되었습니다.")
            return

        slot1 = pct1 = None
        last_img = bytes(img3)
        while not self.isInterruptionRequested():
            self.progress.emit("같은 두 번째 손가락을 다시 대십시오.")
            r4 = self._wait_one_pyzkfp(zk)
            if not r4:
                self.failed.emit("취소되었습니다.")
                return
            t2b, img4 = r4
            last_img = bytes(img4)
            self.preview.emit(last_img, w, h)
            score = int(zk.DBMatch(t2a, t2b))
            if score < 0:
                self.failed.emit("두 번째 손가락 매칭 오류 (코드 %s)" % score)
                return
            pct = _zk_match_score_to_percent(score)
            self.match_percent.emit(pct, "두 번째 손가락(동일 여부)")
            if pct >= self._min_pct:
                slot1 = bytes(bytearray(t2b))
                pct1 = pct
                break
            self.progress.emit(
                "일치율 %d%% — %d%% 이상 필요. 같은 손가락으로 다시 찍어 주세요." % (pct, self._min_pct)
            )
            self.msleep(400)
        if slot1 is None:
            return

        self.need_confirm.emit(
            "두 손가락 등록 확인.\nDB 저장 전 첫 번째 손가락을 한 번 더 대주세요.\n[확인] 후 캡처합니다."
        )
        if not self._wait_confirm():
            self.failed.emit("취소되었습니다.")
            return

        while not self.isInterruptionRequested():
            self.progress.emit("첫 번째 손가락을 다시 대십시오 (최종 확인).")
            r5 = self._wait_one_pyzkfp(zk)
            if not r5:
                self.failed.emit("취소되었습니다.")
                return
            t5, img5 = r5
            last_img = bytes(img5)
            self.preview.emit(last_img, w, h)
            score = int(zk.DBMatch(t5, slot0))
            if score < 0:
                self.failed.emit("최종 확인 매칭 오류 (코드 %s)" % score)
                return
            pct = _zk_match_score_to_percent(score)
            self.match_percent.emit(pct, "첫 손가락 최종 확인(저장 전)")
            if pct >= self._min_pct:
                break
            self.progress.emit(
                "일치율 %d%% — %d%% 이상 필요. 첫 번째 손가락으로 다시 찍어 주세요." % (pct, self._min_pct)
            )
            self.msleep(400)

        self.finished_ok.emit(slot0, slot1, pct0, pct1, last_img, w, h)

    def _run_ctypes(self):
        from zkteco_live20r import ZkFpmSession

        with ZkFpmSession() as sess:
            if not sess._db_handle:
                raise RuntimeError("DBMatch 미지원 libzkfp — ZKFinger SDK 확인")
            w, h = sess.width, sess.height

            self.progress.emit("첫 번째 손가락을 센서에 대십시오.")
            r1 = self._wait_one_ctypes(sess)
            if not r1:
                self.failed.emit("취소되었습니다.")
                return
            t1a, img1 = r1
            self.preview.emit(img1, w, h)
            self.need_confirm.emit(
                "첫 번째 지문이 캡처되었습니다.\n"
                "같은 손가락으로 한 번 더 찍습니다.\n[확인]을 누르면 다음 단계로 진행합니다."
            )
            if not self._wait_confirm():
                self.failed.emit("취소되었습니다.")
                return

            slot0 = pct0 = None
            last_img = img1
            while not self.isInterruptionRequested():
                self.progress.emit("같은 첫 번째 손가락을 다시 대십시오.")
                r2 = self._wait_one_ctypes(sess)
                if not r2:
                    self.failed.emit("취소되었습니다.")
                    return
                t1b, img2 = r2
                last_img = img2
                self.preview.emit(last_img, w, h)
                score = int(sess.db_match_templates(t1a, t1b))
                if score < 0:
                    self.failed.emit("첫 번째 손가락 매칭 오류 (코드 %s)" % score)
                    return
                pct = _zk_match_score_to_percent(score)
                self.match_percent.emit(pct, "첫 번째 손가락(동일 여부)")
                if pct >= self._min_pct:
                    slot0 = t1b
                    pct0 = pct
                    break
                self.progress.emit(
                    "일치율 %d%% — %d%% 이상 필요. 같은 손가락으로 다시 찍어 주세요." % (pct, self._min_pct)
                )
                self.msleep(400)
            if slot0 is None:
                return

            self.need_confirm.emit(
                "첫 번째 손가락 확인 완료.\n이제 다른 손가락을 사용합니다.\n[확인] 후 두 번째 손가락을 대십시오."
            )
            if not self._wait_confirm():
                self.failed.emit("취소되었습니다.")
                return

            self.progress.emit("두 번째 손가락을 센서에 대십시오.")
            r3 = self._wait_one_ctypes(sess)
            if not r3:
                self.failed.emit("취소되었습니다.")
                return
            t2a, img3 = r3
            self.preview.emit(img3, w, h)
            self.need_confirm.emit(
                "두 번째 지문이 캡처되었습니다.\n같은 손가락으로 한 번 더 찍습니다.\n[확인]을 누르면 캡처합니다."
            )
            if not self._wait_confirm():
                self.failed.emit("취소되었습니다.")
                return

            slot1 = pct1 = None
            while not self.isInterruptionRequested():
                self.progress.emit("같은 두 번째 손가락을 다시 대십시오.")
                r4 = self._wait_one_ctypes(sess)
                if not r4:
                    self.failed.emit("취소되었습니다.")
                    return
                t2b, img4 = r4
                last_img = img4
                self.preview.emit(last_img, w, h)
                score = int(sess.db_match_templates(t2a, t2b))
                if score < 0:
                    self.failed.emit("두 번째 손가락 매칭 오류 (코드 %s)" % score)
                    return
                pct = _zk_match_score_to_percent(score)
                self.match_percent.emit(pct, "두 번째 손가락(동일 여부)")
                if pct >= self._min_pct:
                    slot1 = t2b
                    pct1 = pct
                    break
                self.progress.emit(
                    "일치율 %d%% — %d%% 이상 필요." % (pct, self._min_pct)
                )
                self.msleep(400)
            if slot1 is None:
                return

            self.need_confirm.emit(
                "두 손가락 등록 확인.\nDB 저장 전 첫 번째 손가락을 한 번 더 대주세요.\n[확인] 후 캡처합니다."
            )
            if not self._wait_confirm():
                self.failed.emit("취소되었습니다.")
                return

            while not self.isInterruptionRequested():
                self.progress.emit("첫 번째 손가락을 다시 대십시오 (최종 확인).")
                r5 = self._wait_one_ctypes(sess)
                if not r5:
                    self.failed.emit("취소되었습니다.")
                    return
                t5, img5 = r5
                last_img = img5
                self.preview.emit(last_img, w, h)
                score = int(sess.db_match_templates(t5, slot0))
                if score < 0:
                    self.failed.emit("최종 확인 매칭 오류 (코드 %s)" % score)
                    return
                pct = _zk_match_score_to_percent(score)
                self.match_percent.emit(pct, "첫 손가락 최종 확인(저장 전)")
                if pct >= self._min_pct:
                    break
                self.msleep(400)

            self.finished_ok.emit(slot0, slot1, pct0, pct1, last_img, w, h)

    def _run_pyzkfp(self):
        from pyzkfp import ZKFP2

        zk = ZKFP2()
        zk.Init()
        zk.OpenDevice(0)
        db_ok = False
        try:
            zk.DBInit()
            db_ok = True
            self._enroll_two_fingers_pyzkfp(zk, int(zk.width), int(zk.height))
        finally:
            _fp_led_idle_pyzkfp(zk)
            if db_ok:
                try:
                    zk.DBFree()
                except Exception:
                    pass
            try:
                zk.CloseDevice()
            except Exception:
                pass
            try:
                zk.Terminate()
            except Exception:
                pass

    def run(self):
        self._cancel_requested = False
        try:
            mp = int(os.environ.get("FP_ENROLL_MIN_MATCH_PCT", str(self._min_pct)))
        except ValueError:
            mp = self._min_pct
        self._min_pct = max(1, min(100, mp))
        try:
            self._run_pyzkfp()
        except Exception as py_err:
            try:
                self._run_ctypes()
            except Exception as ct_err:
                self.failed.emit(
                    "지문 등록기 연결 실패.\npyzkfp: %s\nctypes: %s" % (py_err, ct_err)
                )


class FingerprintRegisterDialog(QDialog):
    """Live20R USB — DB 저장 + 단말 동기화 후 saved_employee dict 반환."""

    def __init__(self, api, parent, emp_id, emp_no, name, company_id, department_id, rf_card_uid):
        super().__init__(parent)
        self.api = api
        self.emp_id = emp_id
        self.emp_no = (emp_no or "").strip()
        self.emp_name = (name or "").strip()
        self.company_id = company_id
        self.department_id = department_id
        self.rf_card_uid = (rf_card_uid or "").strip()
        self.saved_employee = None
        self.terminal_synced = False
        self.terminal_sync_results = None
        self._thread = None
        self._sync_loader = None
        self._step_overlay = None
        self._match_pct0 = 0
        self._match_pct1 = 0

        self.setWindowTitle("지문 등록")
        self.setObjectName("FingerprintRegisterDialog")
        self.setMinimumSize(540, 580)
        _apply_parent_stylesheet(self, parent)
        self.setStyleSheet((self.styleSheet() or "") + "\n" + _FP_DARK_DIALOG_QSS)
        _apply_dark_palette(self)

        lay = QVBoxLayout(self)
        lay.setSpacing(12)
        lay.setContentsMargins(20, 20, 20, 20)

        self.preview_frame = QFrame()
        self.preview_frame.setObjectName("StatCard")
        self.preview_frame.setMinimumHeight(300)
        pf = QVBoxLayout(self.preview_frame)
        self.image_label = QLabel("지문 미리보기")
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setMinimumSize(280, 280)
        self.image_label.setStyleSheet(
            "background-color: #0f172a; color: #94a3b8; border: 1px solid #334155; border-radius: 8px;"
        )
        pf.addWidget(self.image_label, 1)
        lay.addWidget(self.preview_frame, 1)

        # 사원이름(사번)
        emp_text = "%s (%s)" % (self.emp_name, self.emp_no) if self.emp_no else (self.emp_name or "—")
        self.emp_label = QLabel(emp_text)
        self.emp_label.setObjectName("FpEmpLabel")
        self.emp_label.setAlignment(Qt.AlignCenter)
        self.emp_label.setAutoFillBackground(True)
        lay.addWidget(self.emp_label)

        # 설명란 — 사원명과 동일 크기 라인
        self.status_label = QLabel(" ")
        self.status_label.setObjectName("FpStatusLabel")
        self.status_label.setWordWrap(True)
        self.status_label.setAlignment(Qt.AlignCenter | Qt.AlignVCenter)
        self.status_label.setAutoFillBackground(True)
        lay.addWidget(self.status_label)

        # 하단: 지문읽기 | 일치율(중앙) | 닫기
        row = QHBoxLayout()
        row.setSpacing(12)
        self.read_btn = QPushButton("지문읽기")
        self.read_btn.setObjectName("PrimaryBtn")
        self.read_btn.clicked.connect(self._on_read)

        self.match_pct_label = QLabel("일치율: —")
        self.match_pct_label.setObjectName("FpMatchLabel")
        self.match_pct_label.setAlignment(Qt.AlignCenter)
        self.match_pct_label.setAutoFillBackground(True)

        close_btn = QPushButton("닫기")
        close_btn.setObjectName("SecondaryBtn")
        close_btn.clicked.connect(self.reject)
        row.addWidget(self.read_btn, 0, Qt.AlignVCenter)
        row.addStretch(1)
        row.addWidget(self.match_pct_label, 0, Qt.AlignVCenter)
        row.addStretch(1)
        row.addWidget(close_btn, 0, Qt.AlignVCenter)
        lay.addLayout(row)

        QTimer.singleShot(0, lambda: _apply_windows_dark_titlebar(self))

    def showEvent(self, event):
        super().showEvent(event)
        _apply_windows_dark_titlebar(self)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        ov = getattr(self, "_step_overlay", None)
        if ov is not None and ov.isVisible():
            ov.setGeometry(self.rect())

    def closeEvent(self, event):
        self._stop_thread()
        super().closeEvent(event)

    def reject(self):
        self._stop_thread()
        super().reject()

    def _stop_thread(self):
        th = self._thread
        if th is not None and th.isRunning():
            th.request_cancel()
            th.requestInterruption()
            th.wait(5000)
        self._thread = None

    def _show_step_overlay(self, title: str, labels: list):
        self._hide_step_overlay()
        ov = StepProgressOverlay(self, title, labels)
        ov.setGeometry(self.rect())
        ov.show()
        ov.raise_()
        self._step_overlay = ov
        return ov

    def _hide_step_overlay(self):
        ov = getattr(self, "_step_overlay", None)
        if ov is not None:
            ov.dismiss()
        self._step_overlay = None

    def _set_step(self, index: int, state: str, detail: str = ""):
        ov = getattr(self, "_step_overlay", None)
        if ov is not None:
            ov.set_step_state(index, state, detail)

    @staticmethod
    def _image_to_pixmap(image: bytes, w: int, h: int):
        if not image or w <= 0 or h <= 0 or len(image) < w * h:
            return None
        try:
            qimg = QImage(image[: w * h], w, h, w, QImage.Format_Grayscale8)
            if qimg.isNull():
                return None
            return QPixmap.fromImage(qimg).scaled(280, 280, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        except Exception:
            return None

    def _on_read(self):
        self._stop_thread()
        self.saved_employee = None
        self.terminal_synced = False
        self.terminal_sync_results = None
        self._hide_step_overlay()
        self.read_btn.setEnabled(False)
        self.status_label.setText("리더 연결 중… LED가 켜지면 손가락을 대십시오.")
        self.match_pct_label.setText("일치율: —")
        self.image_label.setText("등록 진행 중…")

        self._thread = FingerprintEnrollTwoFingerThread()
        self._thread.need_confirm.connect(self._on_need_confirm)
        self._thread.progress.connect(self.status_label.setText)
        self._thread.match_percent.connect(self._on_match_percent)
        self._thread.preview.connect(self._on_preview)
        self._thread.finished_ok.connect(self._on_finished_ok)
        self._thread.failed.connect(self._on_failed)
        self._thread.finished.connect(lambda: self.read_btn.setEnabled(True))
        self._thread.start()

    def _on_need_confirm(self, msg: str):
        if dark_question(self, "지문 등록", msg, QMessageBox.Ok | QMessageBox.Cancel) == QMessageBox.Ok:
            if self._thread:
                self._thread.acknowledge_confirm()
        else:
            if self._thread:
                self._thread.request_cancel()

    def _on_match_percent(self, pct: int, label: str):
        self.match_pct_label.setText("일치율: %d%% — %s" % (pct, label))

    def _on_preview(self, image: bytes, w: int, h: int):
        pm = self._image_to_pixmap(image, w, h)
        if pm:
            self.image_label.setPixmap(pm)
            self.image_label.setText("")

    def _on_finished_ok(self, slot0, slot1, pct0, pct1, _img, _w, _h):
        """첫 손가락 최종 확인까지 끝난 뒤 → DB·단말 단계만 표시."""
        self._match_pct0 = int(pct0 or 0)
        self._match_pct1 = int(pct1 or 0)

        ov = self._show_step_overlay(
            "지문 저장",
            [
                "데이터베이스 저장",
                "단말기 전송",
            ],
        )
        ov.set_step_state(0, "active", "저장 중…")
        ov.set_step_state(1, "pending")
        self.status_label.setText("DB 저장 중…")

        uid = secrets.token_hex(16)
        b64_1 = base64.b64encode(slot0).decode("ascii")
        b64_2 = base64.b64encode(slot1).decode("ascii")
        payload = {
            "company_id": self.company_id,
            "department_id": self.department_id,
            "emp_no": self.emp_no,
            "name": self.emp_name,
            "rf_card_uid": self.rf_card_uid or None,
            "fingerprint_uid": uid,
            "fingerprint_template_b64": b64_1,
            "fingerprint_template_b64_2": b64_2,
            "fingerprint_slot_fid_1": 0,
            "fingerprint_slot_fid_2": 1,
        }
        ok, data = self.api.update_employee(self.emp_id, payload)
        if not ok:
            self._set_step(0, "error", "실패")
            if self._step_overlay is not None:
                self._step_overlay.set_status("DB 저장 실패")
            dark_warning(self, "저장 실패", str(data))
            self._hide_step_overlay()
            return

        self.saved_employee = data if isinstance(data, dict) else payload
        self._set_step(0, "done", "완료")
        self.status_label.setText(
            "DB 저장 완료 (손가락1 %d%%, 손가락2 %d%%)" % (self._match_pct0, self._match_pct1)
        )

        saved = self.saved_employee if isinstance(self.saved_employee, dict) else payload
        rf = (saved.get("rf_card_uid") or saved.get("card_uid") or self.rf_card_uid or "").strip()
        tpl0 = saved.get("fingerprint_template_b64") or b64_1
        tpl1 = saved.get("fingerprint_template_b64_2") or b64_2
        if not (rf or tpl0 or tpl1):
            self._set_step(1, "done", "대상 없음")
            if self._step_overlay is not None:
                self._step_overlay.mark_all_done()
                self._step_overlay.set_status("등록 완료")
            QTimer.singleShot(700, self._finish_ok)
            return

        self._set_step(1, "active", "전송 중…")
        if self._step_overlay is not None:
            self._step_overlay.set_status("단말기에 사용자·지문 전송 중…")
        self.status_label.setText("단말기 전송 중…")

        self._sync_loader = _TerminalSyncThread(
            self.api,
            saved.get("emp_no") or self.emp_no,
            saved.get("name") or self.emp_name,
            rf,
            tpl0,
            tpl1,
            saved.get("fingerprint_template_b64_3"),
            saved.get("fingerprint_template_b64_4"),
            saved.get("fingerprint_slot_fid_1"),
            saved.get("fingerprint_slot_fid_2"),
            saved.get("fingerprint_slot_fid_3"),
            saved.get("fingerprint_slot_fid_4"),
        )
        self._sync_loader.finished_ok.connect(self._on_terminal_sync_done)
        self._sync_loader.failed.connect(self._on_terminal_sync_err)
        self._sync_loader.start()

    def _on_terminal_sync_done(self, results):
        self.terminal_synced = True
        self.terminal_sync_results = results if isinstance(results, list) else []
        ok_all = False
        if self.terminal_sync_results:
            ok_all = all(
                isinstance(r, (list, tuple)) and len(r) >= 2 and r[1]
                for r in self.terminal_sync_results
            )
        if ok_all:
            self._set_step(1, "done", "완료")
            if self._step_overlay is not None:
                self._step_overlay.mark_all_done()
                self._step_overlay.set_status("DB·단말기 저장 완료")
            self.status_label.setText("DB·단말기 저장 완료")
        else:
            self._set_step(1, "error", "일부 실패")
            if self._step_overlay is not None:
                self._step_overlay.set_status("단말 전송에 문제가 있습니다")
            self.status_label.setText("단말 전송 일부 실패")
        QTimer.singleShot(900, self._finish_ok)

    def _on_terminal_sync_err(self, err_msg: str):
        self.terminal_synced = True
        self.terminal_sync_results = []
        self._set_step(1, "error", "실패")
        if self._step_overlay is not None:
            self._step_overlay.set_status("단말 전송 오류")
        self.status_label.setText("단말 전송 오류")
        dark_warning(self, "단말기 전송", str(err_msg))
        QTimer.singleShot(400, self._finish_ok)

    def _finish_ok(self):
        self._hide_step_overlay()
        self.accept()

    def _on_failed(self, msg: str):
        self._hide_step_overlay()
        self.status_label.setText("오류")
        self.image_label.setText("지문 미리보기")
        dark_warning(self, "지문 등록", msg)


class _TerminalSyncThread(QThread):
    finished_ok = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(
        self,
        api,
        emp_no,
        name,
        rf,
        tpl0,
        tpl1,
        tpl2=None,
        tpl3=None,
        slot1=None,
        slot2=None,
        slot3=None,
        slot4=None,
    ):
        super().__init__()
        self.api = api
        self.emp_no = emp_no
        self.name = name
        self.rf = rf
        self.tpl0 = tpl0
        self.tpl1 = tpl1
        self.tpl2 = tpl2
        self.tpl3 = tpl3
        self.slot1 = slot1
        self.slot2 = slot2
        self.slot3 = slot3
        self.slot4 = slot4

    def run(self):
        try:
            results = self.api.sync_employee_to_f22_terminals(
                str(self.emp_no or ""),
                str(self.name or ""),
                str(self.rf or ""),
                "",
                self.tpl0,
                self.tpl1,
                self.tpl2,
                self.tpl3,
                self.slot1,
                self.slot2,
                self.slot3,
                self.slot4,
            )
            self.finished_ok.emit(results)
        except Exception as e:
            self.failed.emit(str(e))
