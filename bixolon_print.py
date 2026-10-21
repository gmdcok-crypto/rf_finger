# -*- coding: utf-8 -*-
"""
빅솔론(Bixolon) SRP-350III 네트워크 인쇄 (ESC/POS over TCP).
80mm 열전사 영수증/식권용. 포트 9100 RAW.

pc_app 연동 시:
- 환경변수 MEAL_BIXOLON_HOST / MEAL_BIXOLON_PORT 로 프린터 고정 가능 (설정보다 우선).
- NV 이미지 번호는 MEAL_BIXOLON_NV_IMAGE (1~255, 기본 1). 프린터에 미리 등록된 번호와 맞출 것.
- 그 외에는 설정 > 식권프린터(사용 체크, IP/포트, 단말 연결)를 사용.

통신 테스트 + 텍스트 + NV 이미지 출력.
식권 상단 오른쪽에 단말기번호(설정 device_no) 출력 가능(terminal_no).
"""
import socket

# ESC/POS (이미지 출력용)
ESC = b'\x1b'
GS = b'\x1d'
FS = b'\x1c'
INIT = ESC + b'@'
CUT_PARTIAL = GS + b'V\x01'
# FS p n m: NV 메모리 이미지 인쇄 (n=이미지번호 1~255, m=0)
PRINT_NV_IMAGE = FS + b'p'
LF = b'\n'
# 이미지 위 여백: ESC J n (약 2mm)
FEED_2MM = ESC + b'J' + bytes([16])
# 정렬: ESC a 0=왼쪽 1=가운데 2=오른쪽
ALIGN_LEFT = ESC + b'a\x00'
ALIGN_RIGHT = ESC + b'a\x02'
# ESC ! n: 글자 속성. 0x18=2배높이+2배폭(더블사이즈)
DOUBLE_SIZE = ESC + b'!\x18'
NORMAL_SIZE = ESC + b'!\x00'


def _connect(host: str, port: int = 9100, timeout: float = 5.0) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect((host, port))
    return s


def test_connection(host: str, port: int = 9100, timeout: float = 5.0) -> bool:
    """프린터 포트 연결만 확인. 성공 시 True, 실패 시 False."""
    try:
        s = _connect(host, port, timeout)
        s.close()
        return True
    except Exception:
        return False


def _send(sock: socket.socket, data: bytes, encoding: str = "cp949") -> None:
    if isinstance(data, str):
        try:
            data = data.encode(encoding)
        except UnicodeEncodeError:
            data = data.encode(encoding, errors="replace")
    sock.sendall(data)


def print_image_only(
    host: str,
    port: int = 9100,
    stored_image_number: int = 1,
    emp_no: str = "",
    name: str = "",
    date_time_str: str = "",
    meal_type: str = "",
    terminal_no: str = "",
    timeout: float = 5.0,
) -> bool:
    """
    솔루션: 텍스트 먼저 → 다음 식권용 이미지 → 컷.
    - 최초 1장: 텍스트만 출력되고, 컷 뒤 남는 부분에 다음 사람용 이미지가 인쇄됨.
    - 2장부터: 이미 인쇄된 이미지 밑에 텍스트 출력, 그 뒤에 다음 장용 이미지 인쇄 후 컷.
    terminal_no: 설정의 단말기 번호(device_no). 있으면 상단 오른쪽에 한 줄 인쇄.
    """
    try:
        s = _connect(host, port, timeout)
        try:
            s.sendall(INIT)
            # 텍스트 찍기 전 1라인 공백
            s.sendall(LF)
            tn = (terminal_no or "").strip()
            if tn:
                s.sendall(ALIGN_RIGHT)
                _send(s, "단말기번호 %s\n" % tn)
                s.sendall(ALIGN_LEFT)
            # 1) 텍스트: 사번 이름(왼쪽) + 공백 + 식사종류(2포인트 큼) + 공백 + 날짜시간(오른쪽)
            if emp_no or name or date_time_str or meal_type:
                left = f"{emp_no} {name}".strip()
                right = (date_time_str or "").strip()
                mid = (meal_type or "").strip()
                line_width = 39
                # 식사종류는 더블사이즈로 찍어서 폭 2배 가정
                mid_width = len(mid) * 2 if mid else 0
                pad_total = max(0, line_width - len(left) - mid_width - len(right))
                pad1 = pad_total // 2
                pad2 = pad_total - pad1
                _send(s, left + " " * pad1)
                if mid:
                    s.sendall(DOUBLE_SIZE)
                    _send(s, mid)
                    s.sendall(NORMAL_SIZE)
                _send(s, " " * pad2 + right + "\n")
            # 2) 다음 식권 상단에 나올 이미지 인쇄 (컷 뒤 남는 부분에 인쇄됨)
            s.sendall(FEED_2MM)
            s.sendall(PRINT_NV_IMAGE + bytes([min(255, max(1, stored_image_number)), 0]))
            s.sendall(CUT_PARTIAL)
        finally:
            s.close()
        return True
    except Exception:
        return False
