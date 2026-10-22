"""MariaDB Connector/Python (공식, pip 패키지명 `mariadb`) 연결.

문서: https://mariadb.com/kb/en/about-mariadb-connector-python/
연결 파라미터는 mariadb.connect() 키워드와 동일하게 맞춘다.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple


def _parse_dotenv_line(line: str) -> Optional[Tuple[str, str]]:
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    if "=" not in s:
        return None
    key, _, rest = s.partition("=")
    key = key.strip()
    if not key or not key.startswith("MEAL_"):
        return None
    val = rest.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        val = val[1:-1].replace("\\n", "\n").replace('\\"', '"').replace("\\'", "'")
    return key, val


def _dotenv_candidates() -> List[Path]:
    """exe 옆, exe 상위(프로젝트 루트), 개발 시 소스 루트, 현재 폴더 순."""
    paths: List[Path] = []
    explicit = (os.environ.get("MEAL_DB_ENV_FILE") or "").strip()
    if explicit:
        paths.append(Path(explicit))
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        paths.append(exe_dir / ".env")
        parent = exe_dir
        for _ in range(4):
            parent = parent.parent
            paths.append(parent / ".env")
    paths.append(Path(__file__).resolve().parent.parent / ".env")
    paths.append(Path.cwd() / ".env")
    return paths


def load_meal_db_dotenv() -> None:
    """`.env`에서 MEAL_* 환경 변수를 로드한다.

    빌드된 exe는 exe와 같은 폴더, 그리고 그 상위 폴더(프로젝트 루트)를 본다.
    MEAL_DB_* 뿐 아니라 MEAL_F22_ATT_MODE, MEAL_ADMS_PUSH_* 등 앱 설정도 포함.
    이미 설정된 환경 변수는 덮어쓰지 않는다.
    """
    paths = _dotenv_candidates()
    seen: set = set()

    def _apply_meal_key(k: str, v: str) -> None:
        """MEAL_F22_* / MEAL_ADMS_* 는 .env 값 우선. MEAL_DB_* 는 비어 있을 때만 채움."""
        if k.startswith("MEAL_F22_") or k.startswith("MEAL_ADMS_"):
            os.environ[k] = v
            return
        cur = os.environ.get(k)
        if cur is None or (isinstance(cur, str) and cur.strip() == ""):
            os.environ[k] = v

    for path in paths:
        try:
            rp = path.resolve()
        except OSError:
            rp = path
        if rp in seen:
            continue
        seen.add(rp)
        if not path.is_file():
            continue
        try:
            raw = path.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        for line in raw.splitlines():
            parsed = _parse_dotenv_line(line)
            if not parsed:
                continue
            k, v = parsed
            _apply_meal_key(k, v)

try:
    import mariadb
except ImportError as e:
    mariadb = None  # type: ignore
    _IMPORT_ERR = e
else:
    _IMPORT_ERR = None


class _DictConnection:
    """cursor() 기본값을 dictionary=True 로 두어 기존 DictCursor 사용 코드와 맞춤."""

    __slots__ = ("_raw",)

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def cursor(self, cursorclass=None, **kwargs):
        if cursorclass is not None:
            return self._raw.cursor(cursorclass, **kwargs)
        if "dictionary" not in kwargs and "named_tuple" not in kwargs:
            kwargs = dict(kwargs)
            kwargs["dictionary"] = True
        return self._raw.cursor(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)


def driver_info() -> str:
    """UI/로그용: 공식 커넥터 버전 문자열."""
    if mariadb is None:
        return "mariadb 모듈 없음"
    ver = getattr(mariadb, "__version__", "?")
    capi = getattr(mariadb, "mariadbapi_version", None)
    if capi:
        return "MariaDB Connector/Python %s (MariaDB Connector/C %s)" % (ver, capi)
    return "MariaDB Connector/Python %s" % ver


def db_connect_kwargs(**extra: Any) -> Dict[str, Any]:
    try:
        connect_timeout = int(os.environ.get("MEAL_DB_CONNECT_TIMEOUT", "10"))
    except ValueError:
        connect_timeout = 10
    # 공식 connect() 인자: host, port, user, password, database(db 별칭), unix_socket, connect_timeout 등
    kw: Dict[str, Any] = {
        "host": os.environ.get("MEAL_DB_HOST", "127.0.0.1"),
        "port": int(os.environ.get("MEAL_DB_PORT", "3306")),
        "user": os.environ.get("MEAL_DB_USER", "root"),
        "password": os.environ.get("MEAL_DB_PASSWORD", ""),
        "database": os.environ.get("MEAL_DB_NAME", "meal_rf_finger"),
        "autocommit": False,
    }
    sock = (os.environ.get("MEAL_DB_UNIX_SOCKET") or os.environ.get("MEAL_DB_SOCKET") or "").strip()
    if sock:
        kw["unix_socket"] = sock
    if connect_timeout > 0:
        kw["connect_timeout"] = connect_timeout
    kw.update(extra)
    return kw


def _open_mariadb_connection(kw: Dict[str, Any]) -> Any:
    """mariadb.connect 호출. 구버전에서 connect_timeout 미지원 시 한 번 재시도."""
    try:
        return mariadb.connect(**kw)
    except TypeError as e:
        msg = str(e).lower()
        if "connect_timeout" in msg and "connect_timeout" in kw:
            kw2 = {k: v for k, v in kw.items() if k != "connect_timeout"}
            return mariadb.connect(**kw2)
        raise


def ensure_driver() -> None:
    if mariadb is None:
        raise RuntimeError(
            "MariaDB Connector/Python 이 필요합니다. 공식 패키지: pip install mariadb\n원인: %s" % _IMPORT_ERR
        )
    # PyMySQL 등 다른 패키지와 혼동 방지 (공식 커넥터는 C API 버전 문자열을 노출)
    if not getattr(mariadb, "mariadbapi_version", None):
        raise RuntimeError(
            "설치된 `mariadb` 모듈이 MariaDB Connector/Python 이 아닌 것 같습니다. "
            "공식: https://pypi.org/project/mariadb/\n"
            "pip uninstall 로 충돌 패키지 제거 후 pip install mariadb"
        )


def _password_hint(kw: Dict[str, Any], err_text: str) -> str:
    et = str(err_text).lower()
    pwd = kw.get("password")
    if pwd not in (None, ""):
        return ""
    if "using password: no" in et or ("access denied" in et and "password" in et):
        return (
            "\n비밀번호 미전달: MEAL_DB_PASSWORD 가 비어 있습니다.\n"
            "`.env`를 pc_app.exe 와 같은 폴더에 두거나, 프로젝트 폴더(`meal_rf_finger`)에 두세요.\n"
            "한 줄: MEAL_DB_PASSWORD=실제비밀번호"
        )
    return ""


def _connection_diagnostics(kw: Dict[str, Any], err_text: str = "") -> str:
    safe_user = kw.get("user", "")
    core = (
        "드라이버: %s\n"
        "접속: host=%s port=%s database=%s user=%s\n"
        "환경변수: MEAL_DB_HOST, MEAL_DB_PORT, MEAL_DB_USER, MEAL_DB_PASSWORD, MEAL_DB_NAME\n"
        "로컬 설정 파일: exe 옆 또는 프로젝트 루트 `.env`\n"
        "스키마 생성: python -m meal_db.apply_schema"
        % (driver_info(), kw.get("host"), kw.get("port"), kw.get("database"), safe_user)
    )
    return core + _password_hint(kw, err_text)


@contextmanager
def get_connection(*, commit: bool = True) -> Iterator[_DictConnection]:
    ensure_driver()
    kw = db_connect_kwargs()
    try:
        raw = _open_mariadb_connection(kw)
    except mariadb.Error as e:
        raise RuntimeError("%s\n\n%s" % (e, _connection_diagnostics(kw, str(e)))) from e
    except OSError as e:
        # 연결 거부·네트워크 등
        raise RuntimeError("%s\n\n%s" % (e, _connection_diagnostics(kw, str(e)))) from e
    conn = _DictConnection(raw)
    try:
        yield conn
        if commit:
            conn.commit()
    except mariadb.Error as e:
        conn.rollback()
        raise RuntimeError("%s\n\n%s" % (e, _connection_diagnostics(kw, str(e)))) from e
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


load_meal_db_dotenv()
