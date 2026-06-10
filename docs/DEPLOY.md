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
1. 전용 프로젝트에서 `supabase/schema.sql` 실행 → events/devices/sessions + owner RLS.
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

## 3. 머신 추가 (관리자가 웹에서)
1. 로그인 후 상단 **⚙ 머신 관리** → 머신 이름 입력 → **+ 토큰 발급**.
2. 표시된 **디바이스 토큰**과 **설치 명령**을 복사(토큰은 이때 한 번만 표시됨).
   ```
   periscribe.exe install --token <발급된토큰> --url https://<project>.supabase.co/functions/v1/ingest
   ```
3. 그 PC에서 위 명령을 실행(아래 4번). 잠시 후 헬스바에 🟢로 나타남.
4. 분실/이탈 시 머신 관리에서 **revoke** → 그 토큰은 즉시 무효.

## 4. 각 PC에 설치 (Windows)
- **단일 exe(권장, 배포 시)**: 관리자에게 받은 `periscribe.exe`로 위 install 명령 실행 →
  config 작성 + 부팅 자동실행 등록(무콘솔). Python 불필요. (빌드는 `packaging/` 참고.)
- **소스 실행(개발/빌드 전)**: 저장소를 받고 `collector/`에서
  ```
  python -m periscribe --ingest-url <URL> --device-token <토큰>
  ```
  또는 `config.json`에 `ingest_url`/`device_token`을 넣고 `python -m periscribe`.

## 5. 운영
- **헬스**: 각 머신이 주기적 ingest로 `last_seen` 갱신. 웹 헬스바에서 온라인/대기 표시.
- **레닥션**: 배포 config는 `redact: true` 권장(토큰/키/비번 마스킹).
- **로그**: `log_file` 지정 시 `collector/logs/`에 기록(로테이션).
- **revoke**: 머신 관리에서 즉시. 토큰 회전 = revoke 후 새 토큰 발급·재설치.
- **용량 주의**: 보존정책(prune)은 미포함. 누적 시 Supabase 용량/함수 호출 한도 확인.

## 5b. Claude OS 감사 (선택, Windows)
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

## 5c. Claude API 게이트웨이 (로깅 + 요청측 통제, transcript 비의존)
Claude의 **인풋/아웃풋/작업을 transcript(Claude 자기기록) 없이** 외부 관찰자로 잡고, 요청 단계에서 통제한다.
로컬 리버스 프록시가 Claude↔Anthropic 사이에 앉아 트래픽을 도청·중계한다(우리가 API를 호출하는 게 아니라
Claude 트래픽을 관찰). **무관리자**. 대상 PC마다:
```
periscribe.exe proxy-setup        # 자체 CA 생성 + ~/.claude/settings.json env(ANTHROPIC_BASE_URL+NODE_EXTRA_CA_CERTS) 머지 + api_log_enabled=true
# → 최초 1회만 Claude 재시작(CA가 세션 시작 시에만 로드됨). 이후 켜기/끄기는 떠 있는 세션도 무중단(env 핫리로드).
```
또는 명령어 없이 **`periscribe-proxy.exe` 더블클릭**(별도 다운로드, 무관리자) → GUI에서 켜기/끄기.
같은 토글은 `periscribe.exe proxy-gui` / `periscribe.exe proxy on|off|status` 로도 가능. Collector 설치가 선행돼야 한다.
- 컬렉터가 프록시를 supervised subprocess 로 띄움(죽으면 재기동). Claude가 우리 CA를 신뢰(NODE_EXTRA_CA_CERTS)해
  TLS 복호화 → 요청/응답을 `source='api'`, kind=user_prompt/assistant_text/tool_use/tool_result 로 매핑 →
  기존 파이프라인(E2EE) 수집 → 웹 **🛰 API**. 세션은 요청 metadata.user_id 의 session_id 로 묶임(한 대화=한 세션);
  이 id 는 transcript 의 session_id 와 같아 **같은 대화가 transcript·API 한 세션으로 합쳐짐** → 웹 상단 **출처 탭
  (전체 / 📝 Transcript / 🛰 API / 🐚 OS)** 으로 한 세션 안에서 나눠 본다.
- **요청측 통제**: `%LOCALAPPDATA%\Periscribe\proxy-policy.json` 편집(핫리로드):
  `block_patterns`(매치 시 차단·에러 반환), `redact_patterns`(전송 전 마스킹), `inject_system`(시스템프롬프트 가드레일).
- 끄기: `periscribe.exe proxy-teardown` (또는 GUI의 "프록시 끄기") → settings.json 에서 ANTHROPIC_BASE_URL 제거
  (실행 중 세션 포함 즉시 직결). 상주 CA(NODE_EXTRA_CA_CERTS)는 유지 — 다음 켜기가 무중단이 되기 위한 조건.
  완전 제거(상주 CA 포함)는 `periscribe.exe uninstall`.
- **주의:** Claude API 가용성이 프록시에 의존(죽으면 직결 안 됨 → supervise+fail-open로 완화). 로컬에서 API 평문을
  봄(서버 적재는 E2EE, transcript와 동일 경계). 응답 스트림은 무수정(요청측 통제만; tool_use 응답 게이팅은 후속).
- **동시성 요건(v0.2.1+):** Claude는 병렬 툴콜/서브에이전트로 동시 연결을 일상적으로 만든다. 프록시는 backlog 128 +
  TLS 핸드셰이크를 워커 스레드에서 수행해야 함(기본 backlog 5 + accept 루프 핸드셰이크였던 구버전은 동시 8연결부터
  ECONNREFUSED → Claude connection error). 회귀 테스트: `tests/test_proxy_concurrency.py`.

## 6. 보안 체크리스트
- [ ] ingest 함수만 service_role 사용(시크릿). PC/웹/깃엔 service_role 없음.
- [ ] 공개 가입 비활성화, 관리자 계정만 존재.
- [ ] events/devices 읽기 RLS = owner 스코핑(다른 관리자/anon 차단).
- [ ] 디바이스 토큰은 각 PC 로컬 config에만. 분실 시 revoke.
- [ ] 레닥션 ON.
