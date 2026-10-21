# ADMS / Security PUSH 참조 자료

F22 ADMS 단말 연동의 **유일한 정식 프로토콜 기준**입니다.  
**pyzk(4370 TCP)** 또는 구형 Pull PUSH SDK(`USERINFO`/`FINGERTMP`) 문서와 혼용하지 않습니다.

## 정식 PDF (원본)

| 파일 | 설명 |
|------|------|
| [Security_PUSH_Communication_Protocol_20240712.pdf](./Security_PUSH_Communication_Protocol_20240712.pdf) | ZKTeco **Security PUSH Communication Protocol** (2024-07-12, 299p) |

원본 출처: 사용자 제공 (`Downloads` → 프로젝트 `docs/adms/`에 보관)

## 검색용 텍스트 인덱스

| 파일 | 설명 |
|------|------|
| [Security_PUSH_20240712_index.txt](./Security_PUSH_20240712_index.txt) | PDF 전 페이지 OCR/추출 텍스트 (에이전트·grep 검색용) |

PDF를 직접 열기 어려울 때는 이 파일에서 `getrequest`, `templatev10`, `DATA UPDATE user` 등으로 검색합니다.

## 구현 요약 (식사앱)

| 파일 | 역할 |
|------|------|
| [COMMAND_REFERENCE.md](./COMMAND_REFERENCE.md) | PDF에서 발췌한 **getrequest / user / templatev10** 명령 치트시트 |
| `meal_db/adms_push_server.py` | HTTP `/iclock/*` 수신 |
| `meal_db/adms_commands.py` | getrequest 명령 문자열 생성 |
| `meal_db/adms_user_sync.py` | 사원 → 단말 동기화 |

환경 변수:

- `MEAL_F22_ATT_MODE=adms` — ADMS 모드
- `MEAL_ADMS_CMD_PROTOCOL=security` — **기본**, 20240712 형식 (`user` / `templatev10`)
- `MEAL_ADMS_CMD_PROTOCOL=legacy` — 구형 USERINFO (비권장, Security PUSH 단말 아님)
- `MEAL_ADMS_CMD_PROTOCOL=auto` — security 실패(-629) 시 legacy 재시도

## PDF 주요 섹션 (페이지)

| 주제 | PDF 페이지 (대략) |
|------|-------------------|
| Download Cache Command (`/iclock/getrequest`) | §8.1, p.109–110 |
| Deliver User Information (`user`) | §9.1.1, p.113–114 |
| Deliver Fingerprint Template (`templatev10`) | §9.1.1, p.125–126 |
| Upload Command Result (`/iclock/devicecmd`) | §7 (devicecmd) |

## 유지보수

PDF가 갱신되면:

1. `docs/adms/`에 새 PDF 추가 (파일명에 날짜 포함)
2. `Security_PUSH_20240712_index.txt` 재생성 (아래 명령)
3. `COMMAND_REFERENCE.md` 및 `meal_db/adms_commands.py` 대조

```powershell
Set-Location d:\meal_rf_finger
python -c "
import pypdf
path = r'docs\adms\Security_PUSH_Communication_Protocol_20240712.pdf'
out = r'docs\adms\Security_PUSH_20240712_index.txt'
r = pypdf.PdfReader(path)
lines = ['# index', 'pages: %d' % len(r.pages), '']
for i, page in enumerate(r.pages):
    t = (page.extract_text() or '').replace('\r','')
    if t.strip():
        lines += ['=== PAGE %d ===' % (i+1), t, '']
open(out,'w',encoding='utf-8').write('\n'.join(lines))
print('ok', out)
"

```
