# Security PUSH 20240712 — 식사앱 ADMS 명령 치트시트

> 출처: `Security_PUSH_Communication_Protocol_20240712.pdf`  
> 구현: `meal_db/adms_commands.py`, `meal_db/adms_push_server.py`

## 1. getrequest (서버 → 단말)

단말: `GET /iclock/getrequest?SN={SerialNumber}`

서버 응답 (명령 있음):

```text
C:{CmdID}:DATA UPDATE user CardNo={card}\tPin={pin}\tPassword=\tGroup=0\tStartTime=0\tEndTime=0\tName={name}\tPrivilege=0
```

- 한 줄 = 한 명령, 줄 끝 `\n`
- 명령 없음: `OK`
- `C:` 접두, `CmdID`는 서버가 부여

## 2. user — 사용자 등록/수정

**테이블명: `user`** (USERINFO 아님)

```text
C:{CmdID}:DATA UPDATE user CardNo={CardNo}\tPin={Pin}\tPassword={Password}\tGroup={Group}\tStartTime={StartTime}\tEndTime={EndTime}\tName={Name}\tPrivilege={Privilege}
```

2024 PDF에는 `Disable=` 필드도 있음. 미설정 시 생략 가능(예제는 생략).

| 필드 | 설명 |
|------|------|
| CardNo | 카드 번호 (unsigned int 문자열 또는 hex `[ %02x… ]`) |
| Pin | 사번(user ID) |
| Password | 비밀번호 (없으면 빈 값) |
| Group | 그룹 (기본 0) |
| StartTime / EndTime | 0 = 유효기간 제한 없음 |
| Name | 이름 (한국어: UTF-8) |
| Privilege | 0=일반, 14=슈퍼관리자 등 |

예제 (PDF p.113):

```text
C:295:DATA UPDATE user CardNo= Pin=1 Password=234 Group=0 StartTime=0 EndTime=0 Name= Privilege=0
```

## 3. templatev10 — 지문 템플릿

**테이블명: `templatev10`** (FINGERTMP 아님)

```text
C:{CmdID}:DATA UPDATE templatev10 Pin={Pin}\tFingerID={0-9}\tValid=1\tTemplate={base64}
```

| 필드 | 설명 |
|------|------|
| FingerID | 0–9 |
| Valid | 0=무효, 1=유효, 3=협박 |
| Template | 바이너리 지문 템플릿의 **Base64** |

예제 (PDF p.126):

```text
C:333:DATA UPDATE templatev10 Pin=1 FingerID=6 Valid=1 Template=Si9TUzIxAAAD...
```

## 4. user 삭제

```text
C:{CmdID}:DATA DELETE user Pin={Pin}
```

지문·권한도 함께 지울 때 (PDF 예):

```text
DATA DELETE userauthorize Pin=1
DATA DELETE templatev10 Pin=1
DATA DELETE user Pin=1
```

## 5. devicecmd (단말 → 서버)

단말: `POST /iclock/devicecmd?SN=...`

본문 예:

```text
ID={CmdID}&Return=0&CMD=DATA UPDATE
```

- `Return=0` 성공
- `Return=-629` 테이블명/명령 형식 오류 → `user`/`templatev10` 형식 확인

## 6. 본 프로젝트 매핑

| 앱 데이터 | ADMS 필드 |
|-----------|-----------|
| `emp_no` | `Pin` |
| `name` | `Name` |
| `rf_card_uid` | `CardNo` (32bit int 문자열) |
| 지문 slot 0–3 | `FingerID` 0–3 |
| `fingerprint_template_b64_*` | `Template` (decode → re-encode base64) |

**사용하지 않음:** pyzk 4370, `DATA UPDATE USERINFO`, `DATA UPDATE FINGERTMP`
