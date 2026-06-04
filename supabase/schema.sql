-- Periscribe — Supabase schema
-- events 테이블 + 인덱스 + Realtime + RLS.
-- Supabase SQL Editor에서 한 번 실행하세요. 재실행 안전(idempotent)하도록 작성.

-- =====================================================================
-- 1. events 테이블 (= 시스템의 계약, spec §5)
-- =====================================================================
create table if not exists public.events (
  event_id        text primary key,                       -- 멱등성 키 (transcript uuid[#block])
  schema_version  int  not null default 1,
  source          text not null default 'claude-code',
  machine_id      text not null,
  session_id      text not null,
  agent_id        text,
  is_sidechain    boolean not null default false,
  parent_uuid     text,
  ts              timestamptz,                             -- transcript timestamp (세션 내 순서)
  received_at     timestamptz not null default now(),      -- 수집기 수신 시각 (머신 간 순서)
  kind            text not null,                           -- user_prompt|assistant_text|tool_use|tool_result|session_meta
  tool            text,                                    -- 예: Bash, Edit
  tool_use_id     text,                                    -- 명령 <-> 결과 상관
  is_error        boolean,
  project         text,
  cwd             text,
  payload         jsonb not null default '{}'::jsonb,
  raw             jsonb                                    -- 원본 라인(선택, 용량 고려)
);

-- =====================================================================
-- 2. 인덱스 (spec §5.2)
-- =====================================================================
create index if not exists events_session_ts_idx     on public.events (session_id, ts);
create index if not exists events_machine_recv_idx    on public.events (machine_id, received_at);
create index if not exists events_tool_use_id_idx     on public.events (tool_use_id);
create index if not exists events_kind_idx            on public.events (kind);

-- =====================================================================
-- 3. Realtime publication (웹 UI가 INSERT를 구독하려면 필수)
-- =====================================================================
do $$
begin
  if not exists (
    select 1 from pg_publication_tables
    where pubname = 'supabase_realtime' and tablename = 'events'
  ) then
    alter publication supabase_realtime add table public.events;
  end if;
end $$;

-- =====================================================================
-- 4. RLS (spec §5.3, §8.2)
--    - Collector: service_role 키 사용 → RLS 우회(insert).
--      (service_role 대신 전용 insert 키를 쓰려면 아래 insert 정책 참고.)
--    - Web UI: anon 키 → read 전용.
-- =====================================================================
alter table public.events enable row level security;

-- 읽기: 인증 사용자(authenticated)만 허용. 공개 배포(Vercel)에서 anon 키가 노출돼도
-- 로그인 없이는 데이터를 읽을 수 없게 한다(이게 공개 호스팅의 핵심 안전장치).
-- Collector는 service_role이라 RLS를 우회해 영향 없음.
drop policy if exists events_read on public.events;
create policy events_read
  on public.events
  for select
  to authenticated
  using (true);

-- (선택) service_role 대신 전용 insert-only 키(authenticated 등)를 쓸 경우 활성화:
-- drop policy if exists events_insert on public.events;
-- create policy events_insert
--   on public.events
--   for insert
--   to authenticated
--   with check (true);

-- 참고: anon/authenticated 에게 update/delete 정책을 만들지 않았으므로 기본 거부됨(읽기 전용).

-- =====================================================================
-- 5. (선택) 보존 정책 헬퍼 — 오래된 이벤트 정리 (spec §8.1)
--    pg_cron 확장이 있으면 스케줄링 가능.
-- =====================================================================
-- create or replace function public.prune_events(retain_days int default 30)
-- returns bigint language plpgsql as $$
-- declare deleted bigint;
-- begin
--   delete from public.events where received_at < now() - make_interval(days => retain_days);
--   get diagnostics deleted = row_count;
--   return deleted;
-- end $$;
--
-- -- pg_cron 예 (매일 03:00):
-- -- select cron.schedule('periscribe-prune', '0 3 * * *', $$select public.prune_events(30)$$);

-- =====================================================================
-- 6. machines 테이블 — Collector 하트비트(헬스). 멀티 PC 온라인 상태 표시.
--    Collector(service_role)가 주기적으로 upsert, Web UI(authenticated)는 읽기만.
-- =====================================================================
create table if not exists public.machines (
  machine_id        text primary key,
  hostname          text,
  platform          text,
  source            text not null default 'claude-code',
  collector_version text,
  started_at        timestamptz,
  last_seen         timestamptz not null default now()
);

create index if not exists machines_last_seen_idx on public.machines (last_seen);

-- Realtime: UI가 온라인/오프라인 전환을 실시간 반영
do $$
begin
  if not exists (
    select 1 from pg_publication_tables
    where pubname = 'supabase_realtime' and tablename = 'machines'
  ) then
    alter publication supabase_realtime add table public.machines;
  end if;
end $$;

alter table public.machines enable row level security;

-- 읽기: 인증 사용자만(events와 동일 정책)
drop policy if exists machines_read on public.machines;
create policy machines_read
  on public.machines
  for select
  to authenticated
  using (true);
-- 쓰기(upsert)는 Collector의 service_role이 RLS 우회 → 별도 정책 불필요.

-- =====================================================================
-- 7. sessions 뷰 — 필터 드롭다운(세션/머신)을 DB 전체에서 채우기 위함.
--    security_invoker=true 로 events의 RLS(authenticated 전용)를 상속한다.
-- =====================================================================
create or replace view public.sessions with (security_invoker = true) as
select
  session_id,
  (array_agg(machine_id order by received_at desc))[1] as machine_id,
  (array_agg(project    order by received_at desc))[1] as project,
  count(*)::int as event_count,
  min(ts) as first_ts,
  max(ts) as last_ts,
  max(received_at) as last_received
from public.events
group by session_id;

grant select on public.sessions to authenticated;
