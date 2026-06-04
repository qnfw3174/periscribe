# Periscribe 배포 가이드 (멀티 PC)

여러 Windows PC에 Collector를 깔고, Web UI를 공개 호스팅(Vercel)하며, 머신 헬스까지
운영하는 절차. 보안 모델이 핵심이니 순서대로 진행하세요.

```
[ PC 1..N ] Claude Code ─append→ transcript.jsonl
                                      │ (Collector: service_role, 로컬 전용)
                                      ▼
                         [ Supabase ] events + machines (RLS: 읽기=인증사용자)
                                      ▲ 하트비트            │ Realtime + 조회(로그인 후)
                                      └────────────[ Web UI (Vercel, 로그인 필요) ]
```

## 보안 모델 (먼저 이해)
- **Collector → service_role 키**: RLS 우회 insert. **각 PC 로컬에만** 저장(`config.json`,
  gitignore). 절대 웹/깃에 넣지 않음.
- **Web UI → anon 키**: 공개 사이트에 노출되지만, `events`/`machines` 읽기 RLS가 **인증
  사용자(authenticated) 전용**이라 **로그인 없이는 아무것도 못 읽음**. 이게 공개 배포의 안전장치.
- 따라서 **공개 가입(signup)을 반드시 비활성화**해야 함(임의 가입자가 로그인해 읽는 것 차단).

---

## 1. Supabase (1회 설정)
1. 전용 프로젝트에서 `supabase/schema.sql` 실행 → `events`/`machines` 테이블, 인덱스,
   Realtime, RLS(읽기=authenticated) 구성.
2. **로그인 사용자 생성**: Dashboard → Authentication → Users → *Add user*
   (이메일/비밀번호, *Auto Confirm* 켜기).
3. **공개 가입 비활성화**: Authentication → Providers/Sign In → Email의 *Allow new users to
   sign up* **끄기**. (만든 사용자만 로그인 가능.)
4. 키 확보: Settings → API
   - `service_role` (secret) → Collector용. 로컬에만.
   - `anon` (publishable) → Web UI용.

## 2. Web UI 배포 (Vercel)
1. GitHub 저장소를 Vercel에 Import.
2. 프로젝트 설정:
   - **Root Directory = `web`**
   - **Environment Variables**: `SUPABASE_URL`, `SUPABASE_ANON_KEY` 등록.
   - Build/Output은 `web/vercel.json`이 처리(`node generate-config.js` → `config.js` 생성).
3. Deploy. 배포된 URL 접속 → **로그인 화면** → 1번에서 만든 계정으로 로그인.
   - 로그인 전엔 데이터가 보이지 않아야 정상(RLS 작동).

> 로컬에서 미리 확인: `web/`에서 `SUPABASE_URL`/`SUPABASE_ANON_KEY` 환경변수 주고
> `node generate-config.js` → `config.js` 생성됨. 아무 정적 서버로 띄워 로그인 테스트.

## 3. 각 PC에 Collector 설치 (Windows)
PC마다 1회:
1. **사전조건**: Python 3.8+ 설치(PATH 등록). (의존성 없음 — 표준 라이브러리만.)
2. 저장소를 받거나 `collector/` + `deploy/` 폴더를 복사.
3. PowerShell에서:
   ```powershell
   cd deploy\windows
   .\install-collector.ps1 -SupabaseUrl "https://xxx.supabase.co"
   # service_role 키는 프롬프트로 입력(로컬 config.json에만 저장). machine_id는 비우면 hostname.
   ```
   스크립트가 하는 일:
   - `collector/config.json` 생성 (redact ON, 파일 로그, 하트비트 30s).
   - **작업 스케줄러 등록**: *로그온 시* 자동 시작 + *실패 시 1분마다 재시작*, `pythonw`로 무콘솔.
   - 즉시 시작 → 잠시 후 Web UI 헬스바에 이 PC가 🟢로 표시됨.
4. 확인: Web UI 상단 헬스바에 머신 칩 표시 / `collector/logs/collector.log`.
5. 관리:
   ```powershell
   .\uninstall-collector.ps1          # 작업 제거 + 프로세스 정지
   .\uninstall-collector.ps1 -Purge   # config/checkpoints/logs 까지 삭제
   .\run-collector.ps1 -Backfill 100  # 작업 없이 콘솔에서 직접 실행(디버그)
   ```

> 멀티 PC: `machine_id`는 hostname이라 자동으로 구분됨. Web UI에서 머신 필터/헬스바로 식별.

## 4. 운영 메모
- **헬스**: 각 Collector가 30초마다 `machines.last_seen` 갱신. UI는 ~75초 내면 온라인.
  멈춘 PC는 stale(⚪)로 표시.
- **자동 재시작**: 작업 스케줄러(프로세스 죽으면 재시작) + Collector 내부 루프의 예외 흡수 이중화.
- **로그**: `collector/logs/collector.log` (5MB×3 로테이션).
- **레닥션**: 배포 config는 `redact: true` 기본(토큰/키/JWT/비번 패턴 마스킹). 패턴은
  `collector/periscribe/parser.py`의 `_REDACT_PATTERNS`에서 보강 가능.
- **재시작 누락 방지**: Collector가 꺼진 동안 *새로 시작된 세션*도 재시작 시 처음부터 수집됨
  (체크포인트 유무로 최초실행/재시작 구분).
- **용량/비용 주의(중요)**: 이번 범위에 **보존정책(prune)은 미포함**. 멀티 PC로 데이터가
  누적되면 Supabase 무료 티어 용량을 넘을 수 있음. 필요 시 `supabase/schema.sql`의
  `prune_events` 헬퍼(주석)를 pg_cron으로 활성화하거나 주기적으로 오래된 행 삭제.

## 5. 보안 체크리스트
- [ ] service_role 키는 **각 PC 로컬 config.json에만**(gitignore 확인). 웹/깃에 없음.
- [ ] Supabase **공개 가입 비활성화**, 로그인 사용자만 존재.
- [ ] `events`/`machines` 읽기 RLS = `authenticated`(anon 직접 조회 시 빈 결과).
- [ ] Web UI는 anon 키만 사용. 배포 URL은 로그인 벽 뒤.
- [ ] 민감정보 레닥션 ON.
