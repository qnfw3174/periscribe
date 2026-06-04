# Periscribe — 구현 명세

> **Periscribe** = *peri-*(밖에서 둘러본다) + *scribe*(기록자). AI 에이전트를 **외부에서 관찰하며(개입하지 않고) 기록**하는 도구.
>
> 이 문서는 구현 담당자(다른 AI 에이전트)에게 전달하기 위한 핸드오프 명세입니다.
> **저장소는 Supabase(클라우드 Postgres)** 를 처음부터 사용합니다 — 이로써 별도 중앙 서버를 만들 필요가 없고, 로컬 1대 → 멀티 PC 확장이 거의 추가 작업 없이 됩니다.
> 로컬에서 도는 건 **Collector 프로세스 하나**뿐이고, 저장·실시간·인증은 Supabase가, 조회는 웹 UI가 Supabase에 직접 붙어 처리합니다.

---

## 1. 목적과 범위

AI 코딩 에이전트(현재는 Claude Code)가 수행하는 셸 명령·도구 호출·결과를 **로깅하고, 변화를 거의 즉시(약 1초 이내) 웹 UI로 확인**하는 시스템.

- **관찰 방식**: Claude Code 설정을 일절 건드리지 않는 **수동 관찰**. Claude Code가 자동으로 남기는 transcript(JSONL) 파일을 외부에서 읽기만 한다.
- **읽기 전용 원칙**: transcript는 **읽기만** 한다. 에이전트 동작을 막거나 지연시키지 않는다(hook 방식 대비 transcript 방식을 택한 핵심 이유).
- **이번 단계(로컬 1대)**: 에이전트가 도는 PC에서 Collector 1개가 transcript를 읽어 Supabase에 적재. 웹 UI는 Supabase에 직접 붙어 본다.
- **멀티 PC**: 같은 Collector를 PC마다 깔면 끝. 모두 같은 Supabase로 적재되고, 웹 UI 하나에서 `machine_id`로 구분해 통합 조회. **별도 서버 구축 불필요.**

### 1.1 비목표 (Non-goals)
- **토큰 단위 실시간 스트리밍이 아니다.** 이벤트(콘텐츠 블록) 단위로 충분하다.
- 실행 중 명령의 stdout을 한 줄씩 라이브로 보여주지 않는다(§3.4 한계).
- 에이전트 동작 차단/강제(enforcement)는 목표가 아니다. 관찰만 한다.

---

## 2. 배경 사실 (구현 전 반드시 숙지)

### 2.1 transcript 위치와 형식
- 경로: `~/.claude/projects/<URL-인코딩된-프로젝트-경로>/<session-id>.jsonl`
  - Windows: `%USERPROFILE%\.claude\projects\...`
- 프로젝트 경로는 폴더명으로 URL 인코딩됨 (`/Users/you/code/my-app` → `-Users-you-code-my-app`).
- 파일명 = 세션 UUID. **1 세션 = 1 파일.**
- **Append-only**: 진행되며 새 줄이 덧붙고, 기존 줄은 재작성/삭제되지 않는다.
- **JSONL**: 한 줄당 JSON 객체 하나. pretty-print 아님.

### 2.2 쓰기 타이밍 (실시간성의 근거)
- transcript 쓰기는 **콘텐츠 블록 단위**로, 내부 쓰기 큐가 **약 100ms 지연 플러시**한다. 파일에 들어가는 단위는 토큰이 아니라 이벤트(블록)다.
- 관찰 지연 ≈ (블록 완성) + (~100ms 플러시) + (읽기 주기). 읽기 주기 0.3~0.5s 폴링이면 체감 약 1초 이내. inotify/ReadDirectoryChangesW로 즉시 감지해도 ~100ms 쓰기 플러시가 바닥이라 더 내려봐야 의미 적다.

### 2.3 라인 구조 (파싱 대상)
각 줄의 top-level 필드(버전에 따라 가감): `uuid`, `parentUuid`, `sessionId`, `timestamp`, `type`, `cwd`, `version`, `gitBranch`, `isSidechain`, `userType`, `agentId`, `slug`, `message`.

- `type: "user"` — 사용자 프롬프트(`message.content`가 문자열) 또는 도구 결과(`message.content`가 `tool_result` 블록 배열).
- `type: "assistant"` — 모델 응답. `message.content`는 블록 배열: `{type:"text"|"tool_use"|"thinking", ...}`.
  - `tool_use` 블록: `id`, `name`(`"Bash"`, `"Edit"`, `"Read"`...), `input`(객체).
  - Bash의 `input`: `command`, `description`(선택), `run_in_background`(선택 bool).
- `type: "summary"`, `type: "system"` 등 — 무관/메타. 파서가 죽지 않고 무시.

### 2.4 도구 결과
- `tool_result` 블록은 보통 `type: "user"` 라인의 `message.content` 배열 안. 필드: `tool_use_id`, `is_error`, `content`(문자열 또는 `{type:"text", text}` 배열).

### 2.5 반드시 방어적으로 처리할 형식 변동
- **멀티블록 폴백**: 비스트리밍 폴백 경로에서 한 줄에 여러 콘텐츠 블록이 묶여 들어오는 경우가 존재 → **항상 `content`를 배열로 보고 순회**.
- **포맷 진화**: `type`/필드 구성은 버전마다 바뀜. 모르는 형태도 예외 없이 skip(키 존재 확인, try/except).

### 2.6 서브에이전트(사이드체인)
- 서브에이전트는 `isSidechain: true` + 별도 `agentId`로 사이드체인 transcript를 남긴다.
- `run_in_background: true` 명령은 결과 줄이 `async_launched`로 뜨고 실제 출력은 별도 파일로 간다(별도 출력 파일 추적은 이번 단계 선택, §7).

---

## 3. 아키텍처 (Supabase / A안)

### 3.1 구성
```
[ Agent PC (머신마다) ]                         [ Supabase (클라우드) ]
 Claude Code ─append→ transcript .jsonl          ┌──────────────────────┐
                          │ watch                 │ Postgres: events 테이블 │
                          ▼                        │ Realtime (postgres    │
 Collector(읽기·파싱) ──insert(on conflict)──────▶│   changes)            │
   └ offset checkpoint                            │ Auth / RLS            │
                                                  └──────────┬───────────┘
                                                             │ Realtime 구독 + 조회
                                                             ▼
                                                   [ Web UI (정적 페이지) ]
                                                   Supabase client 직접 사용
```

- **로컬에서 도는 건 Collector 하나.** 저장(events 테이블)·실시간 푸시(Realtime)·인증(Auth/RLS)은 Supabase가 담당.
- **웹 UI는 커스텀 백엔드 없이** Supabase client로 Realtime 구독 + 과거 조회. 즉 "보기 위해" 띄울 서버가 없다.

### 3.2 멀티 PC가 거의 공짜인 이유
이 구조에서 멀티 PC = "Collector를 여러 대에 설치"가 전부. 모두 같은 events 테이블로 적재되고, 각 이벤트의 `machine_id`로 구분/필터. 우리가 이전 설계에서 후속으로 미뤘던 **중앙 ingest 서버·통합 UI·인증이 Supabase 기성 기능으로 이미 충족**됨.

### 3.3 손실 없는 적재 (store-and-forward)
별도 로컬 DB(outbox)를 두지 않아도 된다 — **transcript 파일 자체가 영속 버퍼**이기 때문:

1. Collector가 transcript의 새 줄을 읽어 파싱.
2. 이벤트를 Supabase에 **배치 insert** (멱등: `on conflict (event_id) do nothing`).
3. insert **성공 후에만** 파일 오프셋 체크포인트를 디스크에 영속.
4. 네트워크 실패 시 오프셋을 전진시키지 않고 재시도. transcript는 디스크에 그대로 남아 있으므로 오프라인이어도 **이벤트를 잃지 않고**, 복구되면 마지막 확정 지점부터 이어서 보냄.

(읽기 속도와 네트워크를 분리하고 싶으면 작은 로컬 큐를 둘 수 있으나 **필수 아님.** Claude Code의 세션 보존기간(기본 30일) 안에만 전송되면 됨.)

### 3.4 알아야 할 한계 (over-engineering 방지)
- **실행 중 명령의 라이브 stdout은 transcript에 없다.** 명령은 발행 시점에 한 줄, 결과는 명령 종료 후 하나의 result 줄로 한꺼번에 들어온다. 진행 중 출력 라이브는 범위 밖.

---

## 4. 구성 요소와 책임

- **Collector (로컬, 머신마다 필수)**: 감시 디렉터리 하위 `*.jsonl` 발견·tail(파일별 오프셋, 새 파일 감지, 회전/트렁케이트 감지, 미완성 마지막 줄 보류) → 파싱 → Supabase 배치 insert → 성공 후 오프셋 체크포인트. 동시 세션 다수 처리.
- **Parser**: transcript 한 줄 → 0개 이상 정규화 이벤트(§5). 방어적(§2.5).
- **Sink (인터페이스)**: `emit(events)` 한 군데로 출력 추상화. 기본 구현 = Supabase insert. (향후 다른 백엔드로 교체 가능하도록 인터페이스 유지.)
- **Supabase**: events 테이블 + Realtime + Auth/RLS. 우리가 만들 코드가 아니라 설정 대상.
- **Web UI (정적 페이지)**: Supabase client로 Realtime 구독(신규 INSERT 수신) + 과거 조회(필터·페이지네이션). 세션별 그룹핑, 머신/세션/kind/시간 필터, Bash 강조, 실패 결과 시각 구분. **커스텀 백엔드 불필요.**

---

## 5. 이벤트 스키마 = 시스템의 계약 (= Supabase `events` 테이블)

코어는 typed 컬럼, kind별 가변 필드는 `jsonb`. `machine_id`/`source`/이중 타임스탬프는 멀티·확장 대비로 처음부터 포함(로컬은 기본값).

| 컬럼 | Postgres 타입 | 기본값/유래 | 설명 |
|---|---|---|---|
| `event_id` | `text` PRIMARY KEY | transcript 라인 `uuid` | **멱등성 키.** 재읽기/재전송 중복 방지 (`on conflict do nothing`) |
| `schema_version` | `int` | `1` | 수집기/스키마 독립 진화 |
| `source` | `text` | `'claude-code'` | 향후 다른 에이전트 수용 |
| `machine_id` | `text` | hostname(또는 최초 생성 UUID) | 머신 식별. **지금 안 넣으면 나중에 전 데이터 마이그레이션** |
| `session_id` | `text` | `sessionId` | 세션 그룹핑 |
| `agent_id` | `text` null | `agentId` | 서브에이전트 식별 |
| `is_sidechain` | `bool` | `isSidechain` | 서브에이전트 여부 |
| `parent_uuid` | `text` null | `parentUuid` | 스레딩/순서 보조 |
| `ts` | `timestamptz` | transcript `timestamp` | **세션 내** 순서 기준 |
| `received_at` | `timestamptz` | `now()` (수집기 수신 시각) | **머신 간** 통합 순서 기준(시계 어긋남 대비) |
| `kind` | `text` | (파싱 결과) | `user_prompt`·`assistant_text`·`tool_use`·`tool_result`·`session_meta` |
| `tool` | `text` null | `tool_use.name` | 예: `Bash`, `Edit` |
| `tool_use_id` | `text` null | `tool_use.id` / `tool_result.tool_use_id` | **명령 ↔ 결과 상관** |
| `is_error` | `bool` null | `tool_result.is_error` | 결과 실패 여부 |
| `project` | `text` | transcript 폴더명 | 디코딩 또는 원본 |
| `cwd` | `text` null | 라인 `cwd` | |
| `payload` | `jsonb` | kind별 가변(§5.1) | |
| `raw` | `jsonb` null (선택) | 원본 라인 | 재처리용. 용량 고려 옵션 |

### 5.1 `payload` (kind별)
- `tool_use`+Bash: `{ command, description, run_in_background }`
- `tool_use`+기타: `{ input 요약 또는 원본 }`
- `tool_result`: `{ output_full }` — **전문 저장**(표시용 절단은 UI 단에서). §7.1
- `user_prompt` / `assistant_text`: `{ text }`

### 5.2 인덱스 & Realtime
- 인덱스: `(session_id, ts)`, `(machine_id, received_at)`, `(tool_use_id)`, `(kind)`.
- **Realtime 활성화**: `events` 테이블을 Realtime publication에 추가해야 웹 UI가 INSERT를 구독 가능.

### 5.3 권한 (RLS / 키)
- **Collector**(로컬, 비-브라우저): events에 **insert 권한**을 가진 키 사용. service_role 키 또는 insert-only RLS 정책을 가진 전용 키 권장. 이 키는 **로컬에만 보관**, 절대 웹 페이지에 넣지 않음.
- **Web UI**(브라우저): anon 키 + **read 전용 RLS**. 멀티 사용자/머신 스코핑이 필요하면 RLS 정책으로 `machine_id`/user 기준 제한.

---

## 6. 지금 반드시 지킬 이음새 (Supabase 채택으로 다수는 이미 충족됨)

1. **스키마 = 계약** (§5): 멀티/확장 필드를 1단계부터 포함. 로컬은 기본값.
2. **멱등성**: `event_id`(=transcript `uuid`) PK + `on conflict do nothing`. at-least-once에도 중복 없음.
3. **Store-and-forward**: transcript 파일을 영속 버퍼로 활용, insert 성공 후에만 오프셋 전진(§3.3).
4. **Sink 추상화**: 출력은 `emit(events)` 인터페이스 뒤에. 기본 = Supabase. (백엔드 교체 여지 유지.)
5. **체크포인트 규율**: 오프셋은 Supabase 적재 **확정 후에만** 디스크 영속. 크래시/오프라인 후 마지막 확정 지점부터 재개.

> 이전 설계에서 "멀티 단계로 미루던" 중앙 서버·통합 UI·기본 인증은 Supabase 채택으로 **이미 해소**. 남은 것은 §8의 보안 강화 정도.

---

## 7. 운영 규칙·엣지케이스

- **동시 세션 다수**: 오프셋은 **파일별**, 이벤트는 **`session_id`별** 키잉.
- **미완성 마지막 줄**: 개행으로 끝나지 않으면 다음 폴링까지 보류, 완성되면 **정확히 한 번만** 처리.
- **파일 회전/트렁케이트**: inode 변경 시 오프셋 0, 크기가 오프셋보다 작아지면 0 리셋.
- **시작 시점**: 기존 파일은 기본적으로 **EOF부터**(과거 폭주 방지), 옵션 `--backfill N`으로 마지막 N줄 백필. 실행 중 새 파일은 처음부터.
- **서브에이전트**: `is_sidechain`/`agent_id`로 저장. UI에서 부모 세션 아래 중첩/별도 표시 여부는 구현 시 결정(스키마는 둘 다 수용).
- **배치 insert**: 폴링 1회분 새 이벤트를 모아 한 번에 insert(왕복 절감). 실패 시 그 배치 통째 재시도(멱등이라 안전).

---

## 8. 저장·보존·민감정보 (A안에서는 1단계부터 중요)

### 8.1 저장 손실(truncation)
- 로깅 목적이므로 **저장은 전문(full output)을 `payload`에 둔다.** 표시용 절단은 UI 단에서만.
- 백그라운드 에이전트가 끼면 세션이 수 MB까지 커질 수 있으므로 **보존 정책**(기간/용량 prune 또는 rotate)을 설정값으로. Postgres 용량/비용 관점에서도 정기 정리 권장.

### 8.2 민감정보 — Supabase(클라우드) 적재라 day 1부터 해당
transcript엔 파일 내용·명령 출력·때로 자격증명이 들어온다. 클라우드 저장이므로 처음부터 다음을 권장:
- **전용 Supabase 프로젝트**(다른 프로젝트와 분리), **RLS 활성화**.
- Collector의 쓰기 키는 **로컬에만**, 브라우저엔 anon(read-only) 키만.
- 필요 시 **수집 단계 레닥션**(토큰/API 키/비밀번호 패턴 마스킹)을 켤 수 있는 자리 마련.
- 멀티 사용자라면 RLS로 `machine_id`/user 기준 행 접근 제한.

---

## 9. 멀티 PC 확장 시 추가로 할 일 (대부분 설정 수준)

- Collector를 각 PC에 설치 + 각자 `machine_id` 부여(이미 스키마 수용).
- UI는 그대로 — `machine_id` 필터/그룹 컬럼만 노출.
- RLS 정책 정교화(머신/사용자 스코핑), 키 배포 방식 정리.
- (선택) 머신 간 시계 보정은 `received_at` 기준 정렬로 충분, 별도 로직 불필요.

---

## 10. 권장 기술 스택 (강제 아님)

- **Collector**: 언어 무관. Python 3.8+ 표준 라이브러리 + `supabase-py`(또는 PostgREST로 직접 insert)면 충분. 파일 watch는 폴링으로 시작, 추후 inotify/watchdog.
- **저장소**: Supabase(Postgres + Realtime + Auth). events 테이블 1개.
- **Web UI**: 정적 HTML/JS + `@supabase/supabase-js`. Realtime `postgres_changes` 구독으로 신규 이벤트 수신, REST로 과거 조회. 별도 백엔드 없음.
- **크로스플랫폼 경로**: `~/.claude/projects` / Windows `%USERPROFILE%\.claude\projects`. 설정으로 override.

### 10.1 설정값(최소)
감시 디렉터리, `machine_id`, 폴링 주기, Supabase URL·키(쓰기/읽기), 오프셋 영속 경로, 보존 정책, `--backfill`. 설정 파일 또는 환경변수.

---

## 11. 완료 기준 (로컬 1단계 Definition of Done)

- [ ] Claude Code 설정 변경 없이, 실행 중 세션의 새 활동이 약 1초 이내 **Supabase events 테이블에 적재**되고 웹 UI에 **Realtime으로** 나타난다.
- [ ] Bash 명령이 강조 표시되고, 결과가 `tool_use_id`로 매칭되며, 실패가 시각적으로 구분된다.
- [ ] 비-Bash 도구 호출·사용자 프롬프트·assistant 텍스트도 시간순으로 보인다.
- [ ] 모든 이벤트가 §5 스키마로 저장되고 결과 전문이 보존된다.
- [ ] 프로세스 재시작·네트워크 단절 후에도 누락·중복 없이 마지막 확정 지점부터 재개된다(오프셋 영속 + `event_id` 멱등 + 오프라인 시 미전진).
- [ ] 동시 다중 세션, 미완성 줄, 파일 회전/트렁케이트, 멀티블록 라인, 모르는 `type` 라인에서 죽지 않는다.
- [ ] 쓰기 키는 로컬에만, 웹 UI는 read-only 키 + RLS로 동작한다.
- [ ] 과거 로그를 머신/세션/kind/시간으로 필터·조회할 수 있다.
- [ ] (멀티 검증) 두 번째 PC에 Collector를 깔면 같은 UI에서 `machine_id`로 구분되어 함께 보인다.

---

## 12. 부록: 파서 추출 규칙 요약

- `type=="assistant"`의 `message.content[]` 순회:
  - `tool_use` → `kind=tool_use`. Bash면 `payload.command/description/run_in_background`, 그 외는 `input` 요약/원본.
  - `text` → `kind=assistant_text`, `payload.text`.
  - `thinking` → 저장 여부 선택(기본 무시 가능).
- `type=="user"`:
  - `content`가 배열 + `tool_result` → `kind=tool_result`, `tool_use_id`/`is_error`/`payload.output_full`.
  - `content`가 문자열 → `kind=user_prompt`, `payload.text`.
- 공통: `event_id=uuid`, `session_id=sessionId`, `ts=timestamp`, `received_at=now()`, `machine_id`/`source` 기본값, `is_sidechain`/`agent_id`/`parent_uuid`/`cwd`/`project` 채움.
- 빈 줄·JSON 실패·`dict` 아님·모르는 `type` → 조용히 skip(예외 전파 금지).

---

*문서 버전 2 (Periscribe / Supabase A안). 저장소를 Supabase로 확정하여 중앙 서버 없이 로컬 1대 → 멀티 PC 확장이 Collector 설치만으로 가능하도록 설계됨. 이전 SQLite 기반 초안(agent-activity-logger-spec)을 대체함.*
