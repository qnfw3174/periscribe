# Periscribe

> **Periscribe** = *peri-* (둘러본다) + *scribe* (기록자).
> AI 코딩 에이전트(현재 Claude Code)를 **외부에서 관찰하며 개입하지 않고 기록**하는 도구.

에이전트가 자동으로 남기는 transcript(JSONL)를 **읽기 전용**으로 tail 하고, 선택적으로
**API 프록시**와 **OS 프로세스 감사**를 더해, **디바이스 토큰**으로 Edge Function(ingest)을 통해
**Supabase**에 적재합니다. 관리자는 웹에 로그인해 **본인이 등록한 머신만** Realtime으로 봅니다.
payload는 **종단간 암호화**되어 서비스 운영자도 내용을 읽을 수 없습니다.

전체 아키텍처는 **[docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md)**,
배포는 [docs/DEPLOY.md](./docs/DEPLOY.md), E2EE 설계는 [docs/E2EE-DESIGN.md](./docs/E2EE-DESIGN.md) 참고.

```
[ PC ] Collector ──(device_token)──▶ [ Edge Function: ingest ] ──▶ events/devices
        3개 소스 tail                 토큰검증 → owner 스탬프 insert    (RLS: owner=auth.uid())
        payload는 암호화 후 전송      service_role는 함수 안에만           ▲ 구독·조회(로그인)
                                                                    [ Web UI (Vercel, 로그인) ]
```

## 수집 소스 3계층

서로 다른 신뢰 수준의 세 채널을 **하나의 이벤트 스키마로 정규화**합니다. 어느 채널이든 결국
`watch_dir` 아래 `.jsonl`이 되므로 tail → 체크포인트 → 암호화 → 적재 파이프라인 하나를 재사용합니다.

| `source` | 무엇을 보는가 | 켜는 법 |
|---|---|---|
| `claude-code` | 에이전트가 스스로 남긴 대화·도구 호출 (transcript) | 기본 동작 |
| `api` | Anthropic Messages API 요청/응답 원본 + 정책 통제 | `api_log_enabled` + `periscribe-proxy` |
| `os-exec` | 실제로 생성된 프로세스 (Claude 서브트리만) | `os_exec_enabled` + `periscribe audit-setup` |

transcript는 "무엇을 했다고 기록했는가", API 로그는 "실제로 무엇이 오갔는가",
OS 감사는 "실제로 어떤 프로세스가 떴는가"를 답합니다. 셋은 서로를 교차 검증합니다.
transcript와 API 이벤트는 같은 `session_id`로 합쳐져 웹에서 한 대화로 보입니다.

## 구성 요소

| 디렉터리 | 내용 |
|---|---|
| `collector/` | Collector. transcript/API/OS 이벤트 tail → 파싱 → 암호화 → ingest 적재. 표준 라이브러리 + `cryptography` |
| `supabase/` | `schema.sql`(events/devices/owner_keys/RLS) + `functions/ingest`(적재 게이트웨이) |
| `web/` | 정적 Web UI: 로그인 게이트 + 머신 관리(토큰 발급/revoke) + E2EE 잠금해제·복호화 + 필터/분류 |
| `packaging/` | 실행 파일 3종 빌드(PyInstaller + Inno Setup) |
| `deploy/` | Windows 자동실행 스크립트 |
| `.devcontainer/` | 컨테이너 격리 템플릿 |

### 실행 파일 3종

각각 독립 인스톨러로 빌드되며 서로를 spawn/kill 하지 않습니다.

| 실행 파일 | 역할 |
|---|---|
| `periscribe.exe` | 컬렉터 + 설치 GUI + 트레이 패널 + 프록시 **라우팅** 토글 + `audit-setup` |
| `periscribe-proxy.exe` | API 프록시 **서버 본체**. 자체 CA로 TLS 종료 후 검사·통제·로깅 |
| `periscribe-agent.exe` | VS Code 없이 Docker 샌드박스에서 Claude Code 실행 |

## 빠른 시작

처음 도입하시는 분은 **[docs/GETTING-STARTED.md](./docs/GETTING-STARTED.md)** 를 보세요 — Supabase 준비부터
자기 PC 설치까지 순서대로, 약 30분입니다. 아래는 그 요약이고 상세 절차는 [docs/DEPLOY.md](./docs/DEPLOY.md).

1. **Supabase**: `supabase/schema.sql` 실행 + `supabase/functions/ingest` 배포(verify_jwt=false)
   + 관리자 로그인 계정 생성 + 공개 가입 OFF.
2. **Web UI**: Vercel(Root=`web`, env `SUPABASE_URL`/`SUPABASE_ANON_KEY`) 배포 → 관리자 로그인.
3. **암호화 설정**: 웹에서 키 셋업(패스프레이즈 + 복구코드). **이걸 하기 전에는 컬렉터가 적재를
   보류합니다** — 평문이 서버로 나가는 경로를 만들지 않기 위한 의도된 동작입니다.
4. **머신 추가**: 웹 **⚙ 머신 관리** → 토큰 발급 → 표시된 설치 명령을 그 PC에서 실행.
5. **각 PC(Collector)**: `periscribe.exe` 더블클릭 → GUI 설치 창에 토큰 붙여넣기(권장).
   소스 실행은 `collector/config.json`에 `ingest_url`/`device_token`을 채우고:
   ```bash
   pip install -e collector        # 의존성(cryptography) 설치. 테스트까지: -e collector[dev]
   cd collector
   python -m periscribe
   ```
   - 기본 EOF부터(과거 폭주 방지). 과거 기록은 웹 **⚙ 머신 관리**의 **과거 백필** 버튼으로 끌어옴.
     오프셋 체크포인트로 손실 없이 재개.
   - 멱등성: `event_id` PK + `on conflict do nothing`(함수 측).

### 선택 기능 켜기

- **API 프록시**: `periscribe-proxy.exe` 실행(서버) → `periscribe proxy on`(라우팅).
  라우팅은 머신 `settings.json`의 `ANTHROPIC_BASE_URL`/`NODE_EXTRA_CA_CERTS`만 건드립니다.
  통제 정책은 `%LOCALAPPDATA%\Periscribe\proxy-policy.json`(핫리로드).
- **OS 실행 감사**: `periscribe audit-setup`(관리자 1회, Sysmon 설치) → `os_exec_enabled: true`.
- **샌드박스 실행**: `periscribe-agent <작업폴더>` — 컨테이너 정책은
  `%LOCALAPPDATA%\Periscribe\policies\_default.json`.

## 보안 모델
- **디바이스 토큰**: 각 PC는 자기 머신 토큰만 보유 → 유출돼도 그 머신 insert만 가능, 읽기 불가.
- **Edge Function(ingest)**: service_role은 함수 안에만. 토큰을 검증해 owner를 스탬프하여 적재.
- **관리자 격리**: events/devices 읽기 RLS = `owner_id = auth.uid()` → 본인 머신만 조회.
- **Web UI**: anon 키 + 로그인 게이트. anon 단독으론 RLS에 막혀 아무것도 못 읽음.
- **E2EE**: per-device DEK로 payload를 AES-256-GCM 암호화. DEK는 owner 공개키(RSA-OAEP)로 봉인.
  평문 DEK·패스프레이즈·개인키는 서버를 통과하지 않음 → **운영자도 저장된 내용을 못 읽음**.
  한계(웹 클라이언트 신뢰)는 [docs/E2EE-DESIGN.md § 7](./docs/E2EE-DESIGN.md)에 명시.
- transcript엔 자격증명이 섞일 수 있음 → 수집 단계 레닥션(`redact`) 또는 프록시
  `redact_patterns`(전송 전 마스킹) 사용 권장.

## 통제 (관찰이 기본, 통제는 선택)

에이전트 **자신이 끌 수 없는 계층**에서만 통제를 겁니다.

- **API 프록시 정책**: 요청 차단(Anthropic 미전송, 프록시가 합성 응답) / 레닥션 / system 주입 /
  위험 `tool_use` 실행 전 게이팅. 정책이 깨지면 통제를 적용하지 않고 통과(**fail-open**) —
  통제 실패가 에이전트를 멈추게 하지 않습니다.
- **컨테이너 정책**: 워크스페이스 읽기전용, 경로별 ro/rw 예외, 네트워크 차단, capability,
  메모리·CPU·PID 제한. Claude Code 설정과 무관한 OS 계층입니다.

## 컨테이너(샌드박스) 연동
컨테이너의 `~/.claude`를 호스트 `<container_root>/<name>`에 bind 해서 ① 로그인 토큰을 유지하고
② transcript를 호스트 Collector가 그대로 수집합니다(웹에서 🐳 태그).
`periscribe-agent`로 실행하거나 [`.devcontainer/`](./.devcontainer) 템플릿을 씁니다.
설계 배경은 [agent-container-isolation.md](./agent-container-isolation.md),
사용법은 [docs/CONTAINERS.md](./docs/CONTAINERS.md).

## 문서

| 문서 | 목적 |
|---|---|
| [docs/GETTING-STARTED.md](./docs/GETTING-STARTED.md) | **처음 도입하는 분은 여기부터** — Supabase부터 설치까지 |
| [docs/manual.html](./docs/manual.html) | 기능별 상세 사용법(토큰만 받아 설치하는 분 포함) |
| [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md) | 현행 시스템 전체상 — 기여하려면 여기부터 |
| [docs/TEST-RUN.md](./docs/TEST-RUN.md) | 관리자 PC ↔ 클라이언트 PC 실동작 테스트 순서 |
| [periscribe-spec.md](./periscribe-spec.md) | 초기 코어 설계(transcript 수집). 히스토리 |
| [docs/E2EE-DESIGN.md](./docs/E2EE-DESIGN.md) | 종단간 암호화 키 계층·플로우·한계 |
| [docs/DEPLOY.md](./docs/DEPLOY.md) | 배포 절차 |
| [docs/CONTAINERS.md](./docs/CONTAINERS.md) | 컨테이너 사용법 |
| [docs/central-proxy-design.md](./docs/central-proxy-design.md) | 중앙 프록시 모드(설계 완료, **미구현**) |

## 라이선스
MIT
