# Periscribe — 시스템 아키텍처 (현행)

> 이 문서는 **현재 구현된 시스템 전체**를 한 장으로 설명한다.
> [`periscribe-spec.md`](../periscribe-spec.md)는 transcript 수집만 다루던 초기 코어 설계 문서이며,
> 이후 API 프록시·OS 실행 감사·컨테이너 샌드박스가 추가되었다. 전체 그림은 이 문서를 기준으로 본다.

---

## 1. 한 줄 정의

**관찰 대상(AI 코딩 에이전트)이 스스로 끌 수 없는 세 개의 채널로 활동을 수집해,
운영자조차 내용을 읽을 수 없는 형태로 중앙에 적재하는 감사 도구.**

Langfuse류 LLM 옵저버빌리티와의 차이는 § 6.

---

## 2. 수집 소스 3계층

Periscribe의 핵심 구조는 **서로 다른 신뢰 수준의 세 채널을 하나의 이벤트 스키마로 정규화**하는 것이다.
어느 채널이든 최종적으로 `watch_dir` 아래 `.jsonl` 파일이 되므로,
**Tailer → Checkpoint → E2EE → ingest 파이프라인 하나를 그대로 재사용**한다.

| `source` | 채널 | 수집 경로 | 무엇을 보는가 | 관찰 대상이 끌 수 있나 |
|---|---|---|---|---|
| `claude-code` | transcript | `~/.claude/projects/**/*.jsonl` 직접 tail | 에이전트가 스스로 기록한 대화·도구 호출 | 파일을 지우면 가능(사후) |
| `api` | API 프록시 | `watch_dir/_apilog/<machine>.jsonl` spool | Anthropic Messages API 요청/응답 원본 | 라우팅 해제 시 가능 |
| `os-exec` | OS 실행 감사 | `watch_dir/_osexec/<machine>.jsonl` spool | 실제로 생성된 프로세스(Sysmon) | 관리자 권한 없이는 불가 |

세 채널은 **서로를 교차 검증**한다. transcript는 "에이전트가 무엇을 했다고 기록했는가",
API 로그는 "실제로 무엇이 오갔는가", OS 감사는 "실제로 어떤 프로세스가 떴는가"를 각각 답한다.

### 2.1 세션 통합

`apilog.session_id_for()`는 Anthropic 요청 본문의 `metadata.user_id`에 실려 오는 `session_id`를
**접두사 없이 그대로** 사용한다. 따라서 transcript 이벤트와 API 이벤트가 **같은 `session_id`로 합쳐지고**,
웹에서는 하나의 대화 안에서 `source` 탭으로 두 관점을 전환해 볼 수 있다.
`metadata`가 없으면 대화 지문(`system` + `messages[0]` 해시)으로 폴백한다.

OS 실행 감사는 별도 세션(`osexec-<machine>-<guid>`)으로 묶인다.

---

## 3. 전체 데이터 흐름

```
[ 관찰 대상 PC ]                                    [ Supabase (중앙) ]
                                                    ┌───────────────────────────┐
 Claude Code ──append──▶ transcript .jsonl          │ events   (메타=평문,       │
      │                        │                     │           payload=암호문)  │
      │ HTTPS                  │                     │ devices  (봉인 DEK, 하트비트)│
      ▼                        │                     │ owner_keys (공개키+봉인개인키)│
 periscribe-proxy ─┬─▶ api.anthropic.com             │ session_catalog            │
  (자체 CA로 TLS   │                                  │ backfill/delete_requests   │
   종료·검사·통제) └──▶ _apilog/*.jsonl               │ ingest() service_role      │
                             │                       └────────────┬──────────────┘
 Sysmon(EID 1/5) ──▶ _osexec/*.jsonl                              │
                             │                                     │ Realtime + 조회
              ┌──────────────┴──────────────┐                      │ (암호문)
              ▼                                                     ▼
        Collector (periscribe.exe)                        [ Web UI (Vercel) ]
         tail → parse → AES-GCM 암호화                      패스프레이즈 → 개인키 복원
              └── device_token ──▶ ingest                   → DEK unwrap → payload 복호
```

**적재 게이트웨이 원칙**: 각 PC는 디바이스 토큰만 보유한다. `service_role` 키는 Edge Function
내부에만 존재하며, 함수가 토큰을 검증해 `owner_id`/`device_id`를 스탬프한 뒤 insert한다.
토큰이 유출돼도 **그 머신의 insert만** 가능하고 읽기는 불가능하다.

---

## 4. 실행 파일 3종

`packaging/`에서 각각 독립된 인스톨러로 빌드된다. 서로를 spawn/kill 하지 않는다.

### 4.1 `periscribe.exe` — 컬렉터 겸 머신 에이전트

- 인자 없이 실행: 설치돼 있으면 트레이 컨트롤 패널, 아니면 GUI 설치 창
- `run` 수집 루프, `setup` 콘솔 설치, `panel` 트레이, `uninstall` 자동시작 해제
- `proxy on|off|toggle|status` — 머신의 `settings.json` env(`ANTHROPIC_BASE_URL`,
  `NODE_EXTRA_CA_CERTS`) 토글. **라우팅만 걸고 프록시 서버 본체는 띄우지 않는다**
- `audit-setup` — Sysmon 설치·설정(관리자 1회, UAC 승격)
- 자동시작은 HKCU Run 레지스트리(관리자 권한 불필요)

### 4.2 `periscribe-proxy.exe` — API 프록시 서버 본체

- 사용자가 직접 띄우는 독립 프로그램. 최소 상태창(customtkinter) 또는 `--no-ui` 콘솔
- 기동 시 `proxycert.ensure_certs()`로 자체 CA(3650일)와 리프 인증서(825일) 보장
- 리프 SAN: `127.0.0.1`, `::1`, `localhost`, `host.docker.internal`
- CA는 한 번 만들면 재사용하고 리프만 재발급한다 — 클라이언트가 이미 신뢰 중인 `ca.pem`을 깨지 않기 위함
- `api_proxy_bind`가 `0.0.0.0`이면 컨테이너·LAN에서 접근 가능(신뢰 네트워크 전용)

### 4.3 `periscribe-agent.exe` — 샌드박스 실행기

- VS Code 없이 Docker(또는 podman) 컨테이너에서 Claude Code 실행
- 컨테이너의 `~/.claude`를 호스트 `<container_root>/<name>`에 bind
  → ① 로그인 토큰 유지(재로그인 불필요) ② transcript를 호스트 컬렉터가 그대로 수집(웹에서 🐳`<name>`)
- 표준 라이브러리만 사용하며 컬렉터 코드를 import하지 않는다(작은 exe 유지)

---

## 5. 통제 계층

Periscribe는 관찰이 기본이지만, 두 지점에서 **에이전트가 끌 수 없는 통제**를 건다.

### 5.1 API 프록시 정책 (`proxy-policy.json`, 매 요청 핫리로드)

| 키 | 시점 | 동작 |
|---|---|---|
| `block_patterns` | 요청 전 | 매치 시 **Anthropic에 보내지 않고** 프록시가 `block_message`로 합성 응답 |
| `redact_patterns` | 요청 전 | `messages` 텍스트를 `[REDACTED]`로 마스킹해 전송 |
| `inject_system` | 요청 전 | system 프롬프트에 가드레일 텍스트 append |
| `gate_tool_use` + `tool_block_patterns` | 응답 후 | 위험 `tool_use`를 **실행 전에** 탐지·차단 |

두 가지 설계 원칙:

- **fail-open**: 정책 파일이 깨졌거나 없으면 통제를 적용하지 않고 통과시킨다.
  통제 실패가 Claude를 멈추게 하지 않는다.
- **trailing 검사**: 차단 판정은 *마지막 assistant 이후의 신규 user 입력*만 대상으로 한다.
  Claude는 매 요청에 전체 히스토리를 재전송하므로, 전체를 검사하면 한 번 차단된 프롬프트가
  매 요청 재차단되어 **세션이 영구히 막힌다**. 차단 시 합성 assistant 응답을 끼워 넣어
  그 프롬프트를 assistant 경계 뒤로 밀어내는 것이 이 설계의 짝이다.

### 5.2 컨테이너 정책 (`%LOCALAPPDATA%\Periscribe\policies\*.json`)

Claude Code 설정과 **무관한 계층**이다 — 에이전트가 자기 힘으로 끌 수 없다.
`_default.json`(머신 전체) → `<name>.json`(박스별) → `--policy`(명시) 순으로 덮어쓴다.
편집하면 다음 실행부터 적용되고 이미지 재빌드는 필요 없다.

`workspace_writable`, `writable_paths`, `readonly_paths`, `network`,
`drop_all_capabilities`, `no_new_privileges`, `read_only_rootfs`, `memory`, `cpus`, `pids`

경로 규칙은 워크스페이스 하위로만 허용한다(밖을 가리키면 경고 후 무시). 잘못된 키·형식은
실행을 막지 않고 허용적 기본값으로 폴백하며 경고만 남긴다.

---

## 6. OS 실행 감사 (`audit_win.py`)

Sysmon이 **전체** 프로세스 생성(EID 1)/종료(EID 5)를 이벤트로그에 남기면,
컬렉터가 `wevtutil`로 폴링해 **`ProcessGuid` 계보로 Claude 서브트리만** 골라낸다.
사람이 직접 친 셸 명령은 수집하지 않는다.

- 루트 판정: `image` 또는 `cmdline`에 `claude.exe` / `claude-code` 부분일치
- 하위 판정: 부모 `ProcessGuid`가 이미 추적 중이면 포함 → 깊은 손자까지 따라감
- EID 5 수신 시 추적 셋에서 제거, 상한 5,000개(EID 5 유실 대비 누수 방지)
- 커서(`EventRecordID`)와 추적 셋을 함께 영속 → 재시작 후 이어서 수집
- spool 쓰기 실패 시 커서를 전진시키지 않는다 → 다음 폴에서 재시도(멱등)

전체 create 이벤트를 로깅하는 이유는 **계보 추적에 필요하기 때문**이다
(깊은 손자의 부모가 셸이 아닐 수 있다). 중앙에 적재되는 것은 Claude 서브트리뿐이다.

---

## 7. E2EE 요약

상세는 [`E2EE-DESIGN.md`](./E2EE-DESIGN.md). 아키텍처 관점의 요점만:

```
패스프레이즈 ─PBKDF2(600k)→ KEK ─AES-GCM unwrap→ owner 개인키 (웹에서만)
                                        ▲
owner 공개키 ─RSA-OAEP wrap→ per-device DEK ─AES-256-GCM→ events.payload/raw
```

- 컬렉터 설치는 **토큰 입력만**. DEK는 첫 실행에 로컬 자동 생성되어 `config.json`에 기록된다
- 관리자가 웹에서 키를 설정하기 전(= 공개키 수신 전)에는 **적재를 보류**한다.
  평문이 서버로 나가는 경로 자체가 없다
- `devices.dek_keys`는 kid별 봉인 DEK를 **누적**한다 → 재설치로 새 세대가 생겨도 옛 로그 복호 유지
- 메타데이터(`kind`/`tool`/`ts`/`session_id`/`machine_id`/`project`/`cwd`)는 필터·인덱스용으로 평문

---

## 8. 중앙 스키마에서 알아둘 것

`supabase/schema.sql` 중 초기 설계 이후 추가된 부분:

| 객체 | 역할 |
|---|---|
| `session_catalog` | 컬렉터가 하트비트로 보고하는 **로컬 존재 세션 목록**(내용 미적재 포함). 웹이 전체 과거를 나열하고 선택 백필 |
| `backfill_requests` | 웹 → 하트비트 응답 → 컬렉터가 해당 파일을 처음부터 재적재(멱등) |
| `delete_requests` | 세션 삭제 시 **머신의 로컬 transcript 파일까지** 지우라는 명령 큐 |
| `purge_device` / `purge_session` / `purge_sessions` | `security definer` 함수. events에는 authenticated delete RLS가 없으므로 소유 검증 후 함수가 지운다 |
| `devices.machine_guid` | Windows MachineGuid 기준 디바이스 연속성 — 재설치로 토큰이 바뀌어도 같은 행에 이어짐 |
| `prune_events` + `pg_cron` | 매일 03:00, 기본 90일 보존 |
| `sessions` 뷰 | `security_invoker=true`로 events RLS를 상속 → 드롭다운도 자동으로 owner 격리 |

`devices` 테이블은 `replica identity full`이다. 기본값(PK만)이면 DELETE Realtime 이벤트가
RLS를 통과하지 못해 웹에 유령 디바이스가 남기 때문이다.

---

## 9. 의존성

| 대상 | 의존성 |
|---|---|
| 컬렉터 수집 로직 | Python 표준 라이브러리 + `cryptography`(E2EE·인증서) |
| GUI 설치 창·상태창 | `customtkinter` (optional-dependencies `gui`, exe에 번들) |
| `periscribe-agent` | 표준 라이브러리만 + 런타임에 Docker/podman |
| OS 실행 감사 | Sysmon (Microsoft, 사용자가 별도 설치 — 재배포하지 않음) |
| 웹 UI | `@supabase/supabase-js` (정적 페이지, 빌드 없음) |

---

## 10. 설계상 알려진 한계

정직하게 남긴다. 상세 근거는 각 문서 참조.

| 한계 | 내용 |
|---|---|
| 웹 E2EE | 복호화 JS를 운영자가 서빙한다. *at-rest* 제로지식은 성립하지만 능동적 악의 운영자는 못 막는다 ([E2EE-DESIGN.md § 7](./E2EE-DESIGN.md)) |
| 중앙 프록시 | 평문 프롬프트와 API 키가 서버를 통과한다 → 제로지식과 양립 불가. 고객사 사내망 설치 전용 ([central-proxy-design.md § 4](./central-proxy-design.md), 미구현) |
| 라이브 stdout | 실행 중 명령의 진행 출력은 transcript에 없다. 명령 종료 후 결과가 한 번에 들어온다 |
| 프록시 ON 경계 | 프록시가 꺼져 있던 기간의 턴은 소급 기록하지 않는다(의도된 동작). 대신 ON 직후 첫 요청의 `tool_result`는 짝 `tool_use`가 없는 고아일 수 있다 |
| 플랫폼 | OS 실행 감사·인스톨러·자동시작은 Windows 전용. 컬렉터 코어는 크로스플랫폼 |
