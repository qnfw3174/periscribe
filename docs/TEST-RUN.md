# Periscribe — 실동작 테스트 가이드 (관리자 PC ↔ 클라이언트 PC)

> **목적**: 실제 두 대(또는 한 대 겸용)에서 end-to-end 동작을 확인한다.
> 기능별 상세 체크리스트는 [`test-checklist.html`](./test-checklist.html), 배포 절차는
> [`DEPLOY.md`](./DEPLOY.md). 이 문서는 **어느 PC에서 무엇을 어떤 순서로** 하는지에만 집중한다.
>
> 🅐 = 관리자 PC에서 · 🅒 = 클라이언트(감시 대상) PC에서 · ⏸ = **여기서 상대 PC로 넘어가는 동기화 지점**

## 준비물

- 관리자 PC: 브라우저, Supabase 프로젝트, (배포된) 웹 URL
- 클라이언트 PC: Claude Code 설치·로그인 완료, Windows
- 한 대로도 가능하다. 그 경우 아래 🅐/🅒 를 같은 PC에서 순서대로 하면 된다.
  단 **브라우저 세션과 컬렉터가 같은 계정 아래 돈다**는 점만 인지할 것.

---

# PHASE 0 — 🅐 전제 확인

이미 배포돼 있으면 건너뛴다. 처음이면 [`DEPLOY.md`](./DEPLOY.md) §1~2를 먼저 수행.

| # | 확인 | 방법 | 통과 기준 |
|---|---|---|---|
| 0-1 | 스키마 적용 | Supabase → Table Editor | `events`, `devices`, `owner_keys`, `session_catalog`, `backfill_requests`, `delete_requests` 존재 |
| 0-2 | ingest 함수 배포 | Edge Functions 목록 | `ingest` 가 있고 **verify_jwt = false** |
| 0-3 | 관리자 계정 | Authentication → Users | 계정 1개 존재, 공개 가입 OFF |
| 0-4 | 웹 접속 | 배포 URL 열기 | 로그인 화면이 뜸 |

> ❗ 0-2의 `verify_jwt` 가 true면 컬렉터가 401로 전량 실패한다. 증상이 "설치는 됐는데 무반응"이라
> 원인을 찾기 어려우니 여기서 확실히 확인할 것.

---

# PHASE 1 — 🅐 관리자 PC: 키 설정과 토큰 발급

**이 단계를 먼저 하지 않으면 클라이언트가 아무것도 올리지 않는다.** 컬렉터는 owner 공개키를
받기 전까지 적재를 보류한다(평문을 서버로 보내지 않기 위한 의도된 동작).

### 1-1. 로그인 → 암호화 키 설정

1. 웹 로그인
2. 🔐 암호화 설정 모달에서 패스프레이즈 입력
3. **복구코드를 텍스트 파일로 저장** ← 분실하면 영구 복호 불가

✅ 확인: 재로그인 시 🔓 잠금해제 창이 뜨고, 패스프레이즈로 풀린다.

### 1-2. 머신 토큰 발급

1. 상단 **⚙ 머신 관리** → 머신 이름 입력(예: `test-client`) → **+ 토큰 발급**
2. 표시된 토큰 복사 (**한 번만 표시된다**)

✅ 확인: 머신 목록에 항목이 생기고 상태는 아직 회색/대기.

### ⏸ 동기화 지점 1
토큰을 클라이언트 PC로 옮긴다. **메신저·이메일로 보내지 말 것** — 실제 자격증명이다.
테스트라면 USB나 로컬 메모장으로.

---

# PHASE 2 — 🅒 클라이언트 PC: 컬렉터 설치와 기본 수집

## 경로 A. 빌드된 exe 로 (실사용 시나리오)

### 2-1. 설치

`periscribe-setup.exe` 실행 → 설치 → 창에 **토큰 붙여넣기** → 머신 이름 확인 → 설치

✅ 확인
- 관리자 권한을 묻지 않는다 (HKCU Run 등록이라 불필요)
- 설치 후 트레이 컨트롤 패널로 전환된다
- `%LOCALAPPDATA%\Periscribe\config.json` 에 `ingest_url`·`device_token` 이 들어 있다

❗ **"ingest 엔드포인트가 설정되지 않았습니다"** 가 뜨면 빌드 시 URL 주입이 누락된 것이다.
[`DEPLOY.md` §5 배포 엔드포인트 설정](./DEPLOY.md) 참고 후 재빌드하거나, config.json 의
`ingest_url` 을 직접 채운다.

## 경로 B. 소스로 (개발/심사 재현 시나리오)

```bash
cd collector
pip install -e .
copy config.example.json config.json     # ingest_url, device_token 채우기
python -m periscribe
```

✅ 확인: 콘솔에 적재 로그가 흐르고 예외로 죽지 않는다.

### 2-2. 첫 수집

Claude Code 를 열고 **아무 한 턴**을 실행한다 (예: "hello 라고만 답해줘").

### ⏸ 동기화 지점 2 → 🅐 웹에서 확인

| 확인 | 통과 기준 | 안 되면 |
|---|---|---|
| 헬스바 | 이 머신이 🟢 온라인 | 하트비트 미도달 → 아래 진단표 |
| 세션 피드 | 방금 프롬프트/응답이 1초 내외로 표시 | 적재는 되는데 🔒로 보이면 잠금해제 안 한 것 |
| 내용 복호 | 텍스트가 읽힌다 | — |

**여기까지 되면 핵심 파이프라인(수집→암호화→적재→복호)이 전부 검증된 것이다.**
남은 단계는 선택 기능이므로 시간이 없으면 여기서 멈춰도 된다.

---

# PHASE 3 — 🅐 웹 기능 확인

컬렉터가 도는 상태로 관리자 PC에서 진행한다.

| # | 항목 | 절차 | 통과 기준 |
|---|---|---|---|
| 3-1 | 실시간 | 클라이언트에서 한 턴 더 실행 | 새로고침 없이 피드에 추가됨 |
| 3-2 | 과거 백필 | 세션 관리 → 과거 백필 | 설치 이전 기록이 **중복 없이** 채워짐 |
| 3-3 | 출처 탭 | 전체/Transcript/API/OS 전환 | 현재는 Transcript 만 차 있음 |
| 3-4 | 필터 | 심각도·검색·실패만 | 기대대로 걸러짐 |
| 3-5 | revoke | 머신 관리 → revoke | 클라이언트 적재가 즉시 실패로 전환 |
| 3-6 | 재활성 | revoke 해제 | 다시 적재됨 |

> 3-5는 **되돌릴 수 있는지 확인한 뒤** 하는 게 좋다. 삭제(purge)는 로그까지 지우므로 마지막에.

---

# PHASE 4 — 🅒 API 프록시 (선택)

### 4-1. 서버 기동

`periscribe-proxy.exe` 실행. 최초 실행 시 CA와 리프 인증서가 생성된다.

✅ 확인: `%LOCALAPPDATA%\Periscribe\ca.pem` 생성됨.

### 4-2. 라우팅 ON

트레이 패널 → **프록시 켜기** (또는 `periscribe proxy on`)

✅ 확인
- 🟢 켜짐 표시
- ❗ **처음 켤 때는 실행 중이던 Claude 세션을 1회 재시작해야 한다** (신뢰 CA가 세션 시작 시에만
  로드됨). 이후 토글은 무중단.

### 4-3. 통제 동작

`%LOCALAPPDATA%\Periscribe\proxy-policy.json` 을 편집한다(핫리로드, 재시작 불필요).

```json
{
  "block_patterns": ["aaaa"],
  "block_message": "이 요청은 정책상 차단되었습니다.",
  "redact_patterns": ["TOKEN=\\S+"],
  "gate_tool_use": true,
  "tool_block_patterns": ["\\brm\\b"],
  "tool_block_message": "위험한 명령이 차단되었습니다."
}
```

| # | 테스트 | 입력 | 통과 기준 |
|---|---|---|---|
| 4-a | 요청 차단 | 프롬프트에 `aaaa` 포함 | 차단 메시지가 **assistant 응답으로** 옴(에러 아님). **다음 프롬프트는 정상 동작** ← 세션이 안 막히는지가 핵심 |
| 4-b | 레닥션 | `TOKEN=abc123` 포함 | 웹 🛰 API 탭에서 `[REDACTED]` 확인 |
| 4-c | 도구 게이팅 | "이 파일 rm 으로 지워줘" | 실행 전 차단, **파일이 실제로 남아 있음** |
| 4-d | fail-open | 정책 파일을 깨진 JSON으로 저장 후 한 턴 | 통제 없이 정상 통과(멈추지 않음) |

### ⏸ → 🅐 웹에서 확인
🛰 API 탭에 요청/응답이 기록되고, **transcript 이벤트와 같은 세션으로 묶여 보이는지** 확인.
이게 세션 통합(`metadata.user_id`) 설계의 검증 포인트다.

### 4-4. OFF 복귀

패널 → 프록시 끄기 → 즉시 Anthropic 직결. 테스트 종료 시 반드시 끄고 마무리한다.

---

# PHASE 5 — 🅒 OS 실행 감사 (선택, 관리자 권한)

```
periscribe audit-setup
```

UAC 승격 → Sysmon 설치 → `os_exec_enabled: true` 설정 → **컬렉터 재시작**

Claude Code 로 셸 작업을 시킨 뒤 🅐 웹 🐚 OS 탭 확인.

✅ 통과 기준
- Claude 서브트리 프로세스만 보인다
- **사람이 직접 친 셸 명령은 안 보인다** ← 이게 안 지켜지면 필터링 결함

❗ 되돌리기: config 의 `os_exec_enabled: false`, 필요시 `Sysmon64 -u`.

---

# PHASE 6 — 🅒 컨테이너 샌드박스 (선택, Docker 필요)

```
periscribe-agent <작업폴더> --name box1
```

| # | 테스트 | 절차 | 통과 기준 |
|---|---|---|---|
| 6-a | 기본 실행 | 위 명령 | 컨테이너에서 Claude 진입, 🅐 웹에 🐳`box1` 태그로 수집 |
| 6-b | 로그인 유지 | 종료 후 재실행 | 재로그인 요구 없음 |
| 6-c | 망 차단 | 정책 `"network": false` | 컨테이너에서 외부 접속 실패 |
| 6-d | 읽기전용 | 정책 `"workspace_writable": false` | 파일 쓰기 거부 |
| 6-e | 프록시 연계 | config `api_proxy_bind: "0.0.0.0"` + 프록시 실행 + `--proxy` | 컨테이너 트래픽이 🛰 API 에 기록 |
| 6-f | CA 없을 때 | 프록시 미실행 상태로 `--proxy` | 경고 후 **실행 중단**(조용히 통제 없이 뜨지 않음) |

정책 파일: `%LOCALAPPDATA%\Periscribe\policies\_default.json` 또는 `box1.json`

---

# 진단표 — 막혔을 때

| 증상 | 가장 흔한 원인 | 확인 |
|---|---|---|
| 웹에 머신이 안 뜸 | 키 설정 전이라 적재 보류 중 | 🅐 PHASE 1-1 완료했는가 |
| 〃 | ingest 함수 `verify_jwt=true` | Supabase Edge Function 설정 |
| 〃 | 토큰 오타/공백 | config.json 의 `device_token` |
| 이벤트는 오는데 🔒 | 브라우저 잠금해제 안 함 | 패스프레이즈 입력 |
| 컬렉터가 조용히 죽음 | 설정 오류(exit 2) | `%LOCALAPPDATA%\Periscribe\logs\collector.log` |
| 프록시 켜기 거부 | 서버 미실행/CA 없음 | `periscribe-proxy.exe` 먼저 실행 |
| 프록시 ON인데 API 로그 없음 | Claude 세션이 CA 로드 전에 시작됨 | Claude 재시작(최초 1회) |
| 한 번 차단 후 계속 막힘 | trailing 검사 회귀 | 4-a 재현 → 버그로 보고 |
| OS 탭이 빔 | Sysmon 로그 읽기 권한 | Event Log Readers 그룹 확인, 재로그인 |

**로그 위치**
- 컬렉터: `%LOCALAPPDATA%\Periscribe\logs\collector.log`
- 트레이: `%LOCALAPPDATA%\Periscribe\logs\panel.log`
- 프록시 상태: `periscribe proxy status`

---

# 테스트 후 정리

1. 🅒 프록시 OFF (직결 복귀)
2. 🅒 필요시 `os_exec_enabled: false`
3. 🅐 테스트 머신 revoke → 삭제(purge)
4. 🅒 제어판에서 제거

> 시연영상 촬영 전이라면 **PHASE 2 → 4-a → 4-c → 3-1 순서**가 그대로 콘티가 된다.
