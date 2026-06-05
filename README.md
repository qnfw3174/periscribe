# Periscribe

> **Periscribe** = *peri-* (둘러본다) + *scribe* (기록자).
> AI 코딩 에이전트(현재 Claude Code)를 **외부에서 관찰하며 개입하지 않고 기록**하는 도구.

Claude Code가 자동으로 남기는 transcript(JSONL)를 **읽기 전용**으로 tail 하여 파싱하고,
**디바이스 토큰**으로 Edge Function(ingest)을 통해 **Supabase**에 적재합니다. 관리자는 웹에
로그인해 **본인이 등록한 머신만** Realtime으로 봅니다(멀티테넌트 격리).

전체 설계는 [`periscribe-spec.md`](./periscribe-spec.md), 배포는 [docs/DEPLOY.md](./docs/DEPLOY.md) 참고.

```
[ PC ] Collector ──(device_token)──▶ [ Edge Function: ingest ] ──▶ events/devices
        transcript tail               토큰검증 → owner 스탬프 insert    (RLS: owner=auth.uid())
        (읽기 권한 없음)              service_role는 함수 안에만           ▲ 구독·조회(로그인)
                                                                    [ Web UI (Vercel, 로그인) ]
```

## 구성 요소

| 디렉터리        | 내용                                                                 |
|-----------------|----------------------------------------------------------------------|
| `collector/`    | Collector (Python 표준 라이브러리만). transcript tail → 파싱 → ingest 함수로 적재 |
| `supabase/`     | `schema.sql`(events/devices/RLS) + `functions/ingest`(적재 게이트웨이) |
| `web/`          | 정적 Web UI: 로그인 게이트 + 머신 관리(토큰 발급/revoke) + 헬스 + 필터/분류 |
| `deploy/`,`packaging/` | Windows 자동실행 스크립트 / 단일 exe 빌드                        |

## 빠른 시작 (멀티테넌트 서비스)
배포 전체 절차는 [docs/DEPLOY.md](./docs/DEPLOY.md). 핵심 흐름:

1. **Supabase**: `supabase/schema.sql` 실행 + `supabase/functions/ingest` 배포(verify_jwt=false)
   + 관리자 로그인 계정 생성 + 공개 가입 OFF.
2. **Web UI**: Vercel(Root=`web`, env `SUPABASE_URL`/`SUPABASE_ANON_KEY`) 배포 → 관리자 로그인.
3. **머신 추가**: 웹 **⚙ 머신 관리** → 토큰 발급 → 표시된 설치 명령을 그 PC에서 실행.
4. **각 PC(Collector)**: 디바이스 토큰으로 ingest 함수에 적재. 소스 실행 예:
   ```bash
   cd collector
   python -m periscribe --ingest-url <URL>/functions/v1/ingest --device-token <발급토큰>
   ```
   (또는 `config.json`의 `ingest_url`/`device_token`을 채우고 `python -m periscribe`.)
   - 기본 EOF부터(과거 폭주 방지), `--backfill N`으로 백필. 오프셋 체크포인트로 손실 없이 재개.
   - 멱등성: `event_id` PK + `on conflict do nothing`(함수 측).

## 보안 모델 (멀티테넌트 서비스)
- **디바이스 토큰**: 각 PC는 자기 머신 토큰만 보유 → 유출돼도 그 머신 insert만 가능, 읽기 불가.
- **Edge Function(ingest)**: service_role은 함수 안에만. 토큰을 검증해 owner를 스탬프하여 적재.
- **관리자 격리**: events/devices 읽기 RLS = `owner_id = auth.uid()` → 본인 머신만 조회.
- **Web UI**: anon 키 + 로그인 게이트. anon 단독으론 RLS에 막혀 아무것도 못 읽음.
- transcript엔 자격증명이 섞일 수 있음 → 수집 단계 레닥션(`redact`) 권장.

## 배포 (멀티테넌트)
전체 절차는 **[docs/DEPLOY.md](./docs/DEPLOY.md)** 참고. 요약:
- Supabase: `schema.sql` 적용 + `functions/ingest` 배포(verify_jwt=false) + 관리자 계정 + 공개 가입 OFF
- Web: Vercel(Root=`web`, 환경변수 `SUPABASE_URL`/`SUPABASE_ANON_KEY`) → 로그인 벽
- 머신 추가: 웹 **⚙ 머신 관리**에서 토큰 발급 → 그 PC에서 설치 명령 실행
- 각 PC: 단일 `periscribe.exe`(빌드는 `packaging/`) 또는 소스 실행(`python -m periscribe`)

## 라이선스
MIT
