# Periscribe

> **Periscribe** = *peri-* (둘러본다) + *scribe* (기록자).
> AI 코딩 에이전트(현재 Claude Code)를 **외부에서 관찰하며 개입하지 않고 기록**하는 도구.

Claude Code가 자동으로 남기는 transcript(JSONL)를 **읽기 전용**으로 tail 하여 파싱하고,
**Supabase(클라우드 Postgres)** 에 적재합니다. 웹 UI는 Supabase에 직접 붙어 Realtime으로
약 1초 이내에 새 활동을 보여줍니다. 별도 중앙 서버가 필요 없으며, Collector를 PC마다 깔면
멀티 PC로 바로 확장됩니다.

전체 설계는 [`periscribe-spec.md`](./periscribe-spec.md) 참고.

```
[ Agent PC (머신마다) ]                      [ Supabase (클라우드) ]
 Claude Code ─append→ transcript .jsonl       ┌───────────────────────┐
                          │ watch              │ Postgres: events 테이블 │
                          ▼                    │ Realtime / Auth / RLS  │
 Collector ──insert(on conflict)─────────────▶└──────────┬────────────┘
   └ offset checkpoint                                    │ 구독 + 조회
                                                          ▼
                                                  [ Web UI (정적 페이지) ]
```

## 구성 요소

| 디렉터리        | 내용                                                                 |
|-----------------|----------------------------------------------------------------------|
| `collector/`    | 로컬 Collector (Python, 표준 라이브러리만). transcript tail → 파싱 → Supabase insert |
| `supabase/`     | `events` 테이블 스키마 · 인덱스 · Realtime · RLS 정책 SQL              |
| `web/`          | 정적 Web UI (HTML/JS + `@supabase/supabase-js` CDN)                   |

## 빠른 시작

### 1. Supabase 준비
1. 전용 Supabase 프로젝트 생성(다른 프로젝트와 분리 권장).
2. SQL Editor에서 [`supabase/schema.sql`](./supabase/schema.sql) 실행 → 테이블/인덱스/RLS/Realtime 구성.
3. 키 확인:
   - **service_role 키** (또는 insert 전용 키) → Collector용. **로컬에만 보관.**
   - **anon 키** → Web UI용 (read-only RLS).

### 2. Collector 실행 (에이전트가 도는 PC)
```bash
cd collector
cp config.example.json config.json   # 값 채우기 (Supabase URL, service_role 키 등)
python -m periscribe                  # 표준 라이브러리만 사용, 의존성 없음
```
주요 동작:
- 기본적으로 기존 파일은 **EOF부터** 읽음(과거 폭주 방지). `--backfill N`으로 마지막 N줄 백필.
- insert **성공 후에만** 오프셋 체크포인트를 영속 → 크래시/오프라인 후 마지막 확정 지점부터 재개.
- 멱등성: `event_id` PK + `on conflict do nothing`.

설정값은 `config.json` 또는 환경변수(`PERISCRIBE_*`)로 지정. 자세한 건
[`collector/config.example.json`](./collector/config.example.json) 주석 참고.

### 3. Web UI 보기
```bash
cd web
cp config.example.js config.js       # Supabase URL + anon 키
# 정적 파일이라 아무 정적 서버로 서빙 가능:
python -m http.server 8080
# http://localhost:8080
```

## 보안 원칙
- Collector 쓰기 키는 **로컬에만**, 절대 웹 페이지에 넣지 않음.
- Web UI는 **anon 키 + read-only RLS**.
- transcript엔 자격증명이 섞일 수 있음 → 전용 프로젝트 + RLS 활성화. 필요 시 수집 단계 레닥션(`redact` 설정) 사용.

## 멀티 PC
Collector를 각 PC에 설치하고 `machine_id`만 다르게 부여하면 끝. 같은 `events` 테이블로
적재되고 UI에서 `machine_id`로 구분·필터됩니다.

## 라이선스
MIT
