# Periscribe 배포 가이드 (멀티테넌트 서비스)

관리자별로 격리된 서비스로 운영하는 절차. 각 관리자는 **본인이 등록한 머신만** 보고,
각 PC는 **자기 디바이스 토큰만** 보유한다(service_role은 어디에도 배포되지 않음).

```
[ PC ] periscribe ──(device_token, HTTPS)──▶ [ Edge Function: ingest ]
        transcript tail                          ├ 토큰 sha256 → devices 조회 → owner 확정
        (읽기 권한 없음)                          ├ events insert (owner_id/device_id 스탬프)
                                                  └ devices.last_seen 갱신(하트비트)
                                                        │ (service_role은 함수 안에만)
[ 관리자 ] 웹 로그인 ──(anon + JWT)──▶ events/devices  RLS: owner_id = auth.uid()
```

## 보안 모델 (먼저 이해)
- **디바이스 토큰**: 각 PC가 가진 유일한 자격. 유출돼도 **그 머신의 insert만** 가능,
  데이터 읽기·타 머신 쓰기 불가. 웹에서 머신별로 발급, 분실 시 revoke.
- **service_role**: Edge Function 시크릿으로만 존재. PC·웹·깃 어디에도 없음.
- **관리자 격리**: events/devices 읽기 RLS = `owner_id = auth.uid()`. 다른 관리자 데이터 안 보임.
- **anon 키**: 공개 사이트에 노출돼도 RLS가 인증을 요구하므로 단독으론 무력.

---

## 1. Supabase (1회 설정)
1. 전용 프로젝트에서 `supabase/schema.sql` 실행 → events/devices/owner_keys/session_catalog/
   backfill_requests/delete_requests/sessions 뷰 + owner RLS + 보존정책(pg_cron).
2. **Edge Function 배포**: `supabase/functions/ingest/` 를 `verify_jwt=false`로 배포
   (Supabase CLI `supabase functions deploy ingest --no-verify-jwt`, 또는 대시보드).
   - 함수는 환경의 `SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY`(자동 주입)를 사용.
3. **관리자 로그인 사용자 생성**: Authentication → Users → Add user(이메일/비번, Auto Confirm).
4. **공개 가입 비활성화**: Authentication → Email → *Allow new users to sign up* 끄기.
   (관리자만 로그인 가능하게.)

## 2. Web UI 배포 (Vercel)
1. 저장소를 Vercel에 Import, **Root Directory = `web`**.
2. 환경변수 `SUPABASE_URL`, `SUPABASE_ANON_KEY` 등록(빌드 시 `generate-config.js`가 주입).
3. Deploy → 배포 URL 접속 → 관리자 계정으로 로그인.

## 3. 암호화 키 설정 (머신 추가 전 반드시)
웹 로그인 직후 뜨는 **🔐 암호화 설정** 모달에서 패스프레이즈를 정하고 **복구코드를 보관**한다.
이 시점에 owner 키쌍이 생성되고 공개키가 서버에 저장된다(개인키는 패스프레이즈로 봉인된 형태로만).

> ⚠ **이걸 먼저 하지 않으면 컬렉터가 적재를 보류한다.** 공개키를 받기 전에는 평문을 올리지
> 않는 의도된 동작이다(store-and-forward). "설치는 됐는데 이벤트가 안 보임"의 주된 원인.

- 패스프레이즈는 서버로 전송되지 않는다. **분실 시 복구코드로만** 복구되며, 둘 다 잃으면 영구 복호 불가.
- 재방문 시에는 세션마다 한 번 **🔓 잠금 해제**를 해야 로그 내용이 보인다(잠금 상태엔 🔒).
- 상세 설계·한계는 [E2EE-DESIGN.md](./E2EE-DESIGN.md).

## 4. 머신 추가 (관리자가 웹에서)
1. 로그인 후 상단 **⚙ 머신 관리** → 머신 이름 입력 → **+ 토큰 발급**.
2. 표시된 **디바이스 토큰**을 복사(이때 한 번만 표시됨).
3. 그 PC에서 `periscribe-setup.exe` 실행 → 설치(무관리자) 후 뜨는 창에 토큰 붙여넣기(§5).
   잠시 후 헬스바에 🟢로 나타남.
4. 분실/이탈 시 머신 관리에서 **revoke** → 그 토큰은 즉시 무효.

## 5. 각 PC에 설치 (Windows)
- **설치 프로그램(권장, 역할별 분리)**: 무관리자로 `%LOCALAPPDATA%\Programs\...`에 설치. 실행 시
  임시추출 없는 **onedir** 구조(빌드 `packaging/build.ps1` = PyInstaller onedir + Inno Setup).
  **제거는 "설정 → 앱"(프로그램 추가/제거)**.
  - `periscribe-setup.exe` — 컬렉터(상주 + 트레이 + 라우팅). 첫 실행 창에 토큰 붙여넣기 → 자동시작 등록.
  - `periscribe-proxy-setup.exe` — 프록시 서버(단독; **중앙 서버에도 이것만** 설치). 선택.
  - `periscribe-agent-setup.exe` — 에이전트 런처(Docker 필요). 선택.
- **소스 실행(개발/빌드 전)**: 저장소를 받고 `pip install -e collector` 로 의존성(`cryptography`)을
  설치한 뒤, `collector/config.json`에 `ingest_url`/`device_token`을 넣고 `python -m periscribe`.
  테스트까지 돌리려면 `pip install -e collector[dev]` 후 `cd collector && python -m pytest tests/`.

### 배포 엔드포인트 설정 (빌드하는 사람만)

ingest URL 은 **소스에 하드코딩되어 있지 않다** — 저장소를 포크한 사람이 자기 Supabase
프로젝트를 쓰게 하기 위함이다. 빌드 전에 둘 중 하나를 해둔다.

```powershell
# (a) 환경변수로 주입
$env:PERISCRIBE_DEFAULT_INGEST_URL = "https://<project>.supabase.co/functions/v1/ingest"
.\packaging\build.ps1

# (b) 파일로 관리 (권장 — .gitignore 됨)
copy collector\dist.example.json collector\dist.json   # 열어서 ingest_url 채우기
.\packaging\build.ps1
```

`build.ps1` 이 각 onedir 폴더에 `dist.json` 을 써넣고, 설치본은 exe 옆의 그 파일을 읽는다.
지정하지 않으면 빌드는 경고만 내고 진행되며, 설치 단계에서 사용자에게 설명 메시지가 표시된다.
설치 후에는 `%LOCALAPPDATA%\Periscribe\config.json` 의 `ingest_url` 이 진실의 원천이다.

## 6. 운영
- **헬스**: 각 머신이 주기적 ingest로 `last_seen` 갱신. 웹 헬스바에서 온라인/대기 표시.
- **레닥션**: 배포 config는 `redact: true` 권장(토큰/키/비번 마스킹).
- **로그**: `log_file` 지정 시 `collector/logs/`에 기록(로테이션).
- **revoke**: 머신 관리에서 즉시. 토큰 회전 = revoke 후 새 토큰 발급·재설치.
- **과거 백필**: 설치 이전 기록은 그 PC 로컬에만 있다. 웹의 **⟳ 이 세션 과거 전체 불러오기**로
  요청하면 하트비트 응답을 통해 해당 머신이 처음부터 재적재한다(멱등).
- **세션 삭제**: 웹 **🗂 세션 관리**에서 삭제 시 중앙 DB는 즉시, **수집 PC의 로컬 transcript 파일까지**
  삭제 명령이 큐잉되어 그 PC가 온라인일 때 지워진다.
- **보존정책**: `schema.sql`이 pg_cron 으로 매일 03:00 `prune_events(90)` 을 실행한다(기본 90일).
  보존기간 변경은 그 cron 인자를 고친다. 그래도 Supabase 용량·함수 호출 한도는 주기적으로 확인.

## 6b. Claude OS 감사 (선택, Windows)
transcript는 Claude Code가 한 것만 본다(Claude 의존). **Claude가 OS에서 실제 실행한 작업**(도구가 띄운
프로세스 + 그 하위)을 OS 레벨로 robust하게 보강한다(Sysmon 기반). **범위는 Claude 프로세스 트리로 한정 —
사람의 일반 쉘 명령은 안 잡음**(transcript와 같은 범위). **대상 PC마다 로컬 관리자 1회**:
```
periscribe.exe audit-setup        # UAC 승격 → Sysmon 설치 + 전체 create/terminate 로깅 + 로그읽기 권한 + os_exec_enabled=true
```
이후 (무관리자) 컬렉터가 Sysmon 이벤트로그를 폴링, **ProcessGuid 계보로 claude.exe 서브트리만** 골라
`source='os-exec'`, `kind='process_exec'` 이벤트를 기존 파이프라인(E2EE 포함)으로 수집 → 웹에서 **🐚 Claude OS**
배지(위험도 룰엔진 동일 적용). Claude 실행(루트)당 한 세션으로 묶인다. 끄기: config `os_exec_enabled=false` (+ `Sysmon64 -u`).
- **주의:** 커널 드라이버(Sysmon) 설치 필요(관리자 1회). 적재는 **Claude 것만**(프라이버시·볼륨 부담 작음); 단
  로컬 Sysmon 로그엔 전체 프로세스가 남음(계보 추적용, 링버퍼). 컨테이너 내부는 미지원(Docker Desktop 제약;
  리눅스 호스트 eBPF는 후속).

## 6c. Claude API 게이트웨이 (로깅 + 통제, transcript 비의존)
Claude의 **인풋/아웃풋/작업을 transcript(Claude 자기기록) 없이** 외부 관찰자로 잡고, 요청 단계에서 통제한다.
리버스 프록시 서버가 Claude↔Anthropic 사이에 앉아 트래픽을 도청·중계한다(우리가 API를 호출하는 게 아니라
Claude 트래픽을 관찰). **무관리자**. **구성: 프록시 서버(독립 프로그램) + 머신 라우팅(컬렉터 패널)으로 분리**:
```
# 1) 프록시 '서버' 본체 — 독립 프로그램. 사용자가 직접 실행(지금은 각 머신, 나중엔 중앙 서버 1대).
periscribe-proxy.exe              # 더블클릭 → 서버 실행(자체 CA 생성 + 트래픽 가로채·차단·게이팅·로깅)

# 2) 머신 라우팅 ON/OFF — '머신에서 하는 일'. 컬렉터 컨트롤 패널의 토글, 또는 CLI:
periscribe.exe proxy on           # 이 머신 Claude 를 프록시로 라우팅(서버 가동 검증 후에만). off=직결
# → 최초 1회만 Claude 재시작(CA가 세션 시작 시에만 로드). 컬렉터 패널을 한 번 연 뒤엔 CA 사전상주로 무중단.
```
컨트롤 패널: `periscribe.exe` 더블클릭(설치됨) → 트레이 상주 창에서 프록시 ON/OFF. 창 닫기=트레이로.
- **서버와 라우팅은 분리**: 프록시 서버(`periscribe-proxy.exe`)는 컬렉터가 띄우지 않는 **독립 프로그램**.
  컬렉터는 이 머신의 라우팅(settings.json env)만 켜고/끈다. 서버가 spool(`_apilog/*.jsonl`)에 쓴 로그를
  컬렉터가 다른 transcript 와 똑같이 수집·업로드한다(느슨한 파일 파이프라인). **가디언 없음** — 서버가 죽으면
  자동 직결복구가 없으니 패널/`proxy off` 로 직결 전환한다. Claude가 우리 CA를 신뢰(NODE_EXTRA_CA_CERTS)해
  TLS 복호화 → 요청/응답을 `source='api'`, kind=user_prompt/assistant_text/tool_use/tool_result 로 매핑 →
  기존 파이프라인(E2EE) 수집 → 웹 **🛰 API**. 세션은 요청 metadata.user_id 의 session_id 로 묶임(한 대화=한 세션);
  이 id 는 transcript 의 session_id 와 같아 **같은 대화가 transcript·API 한 세션으로 합쳐짐** → 웹 상단 **출처 탭
  (전체 / 📝 Transcript / 🛰 API / 🐚 OS)** 으로 한 세션 안에서 나눠 본다.
- **통제 정책**: `%LOCALAPPDATA%\Periscribe\proxy-policy.json` 편집(매 요청 핫리로드):
  - `block_patterns` — 매치 시 **Anthropic에 보내지 않고** 프록시가 `block_message` 텍스트로 합성 응답.
    검사 대상은 **마지막 assistant 이후의 신규 user 입력**만 — 전체 히스토리를 보면 한 번 차단된
    프롬프트가 매 요청 재차단되어 세션이 영구히 막힌다.
  - `redact_patterns` — 전송 전 `messages` 텍스트 마스킹.
  - `inject_system` — system 프롬프트에 가드레일 텍스트 append.
  - `gate_tool_use` + `tool_block_patterns` — **응답측 게이팅**. 응답을 버퍼해 위험한 `tool_use`를
    탐지하면 **실행 전에** 차단하고 `tool_block_message` 로 대답한다.
  - 정책 파일이 깨졌거나 없으면 **통제를 적용하지 않고 통과**한다(fail-open) — 통제 실패가
    Claude 를 멈추게 하지 않기 위함.
- 끄기: `periscribe.exe proxy off` (또는 GUI의 "프록시 끄기") → settings.json 의 ANTHROPIC_BASE_URL 을
  **직결 URL 로 덮어쓰기**(키 삭제가 아님 — Claude 는 settings env 를 프로세스 env 에 병합만 해서 삭제는 실행 중
  세션에 반영되지 않음; 값 변경이라야 떠 있는 세션도 즉시 직결). 상주 CA(NODE_EXTRA_CA_CERTS)는 유지 —
  다음 켜기가 무중단이 되기 위한 조건. 완전 제거(상주 CA 포함)는 `periscribe.exe uninstall`(이때도
  ANTHROPIC_BASE_URL 은 직결 기본값으로 남김 — 실행 중 세션 보호).
- **주의:** 켜진 동안 Claude API 가용성이 프록시에 의존한다 — 가디언/자동복구가 없으므로 프록시가 죽으면
  `proxy off` 로 직결 전환해야 한다(이 PC에서 직접 토글 시 자기 세션이 잠깐 끊길 수 있어 별도 PC 테스트 권장).
  로컬에서 API 평문을 봄(서버 적재는 E2EE, transcript와 동일 경계).
- **동시성 요건(v0.2.1+):** Claude는 병렬 툴콜/서브에이전트로 동시 연결을 일상적으로 만든다. 프록시는 backlog 128 +
  TLS 핸드셰이크를 워커 스레드에서 수행해야 함(기본 backlog 5 + accept 루프 핸드셰이크였던 구버전은 동시 8연결부터
  ECONNREFUSED → Claude connection error). 회귀 테스트: `tests/test_proxy_concurrency.py`.

## 7. 보안 체크리스트
- [ ] ingest 함수만 service_role 사용(시크릿). PC/웹/깃엔 service_role 없음.
- [ ] 공개 가입 비활성화, 관리자 계정만 존재.
- [ ] events/devices 읽기 RLS = owner 스코핑(다른 관리자/anon 차단).
- [ ] 디바이스 토큰은 각 PC 로컬 config에만. 분실 시 revoke.
- [ ] 암호화 키 설정 완료 + **복구코드를 별도 보관**(분실 시 복호 불가).
- [ ] 레닥션 ON.
