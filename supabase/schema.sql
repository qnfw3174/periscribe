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

-- 읽기: anon/authenticated 모두 select 허용 (멀티 사용자 스코핑이 필요하면 USING 절을 좁히세요)
drop policy if exists events_read on public.events;
create policy events_read
  on public.events
  for select
  to anon, authenticated
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
