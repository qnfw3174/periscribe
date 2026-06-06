-- Periscribe — Supabase schema (멀티테넌트 서비스 모델)
-- 관리자(auth.users)별 격리: 모든 행이 owner_id를 가지며 RLS로 owner=auth.uid()만 조회.
-- 적재는 Edge Function(ingest)의 service_role만 수행(각 PC는 디바이스 토큰만 보유, 직접 insert 불가).
-- Supabase SQL Editor에서 실행. 재실행 안전(idempotent).

-- =====================================================================
-- 1. events 테이블 (= 시스템의 계약)
-- =====================================================================
create table if not exists public.events (
  event_id        text primary key,                       -- 멱등성 키 (transcript uuid[#block])
  owner_id        uuid not null,                           -- 소유 관리자(auth.users). Edge Function이 스탬프
  device_id       uuid,                                    -- 적재한 디바이스(devices.id)
  schema_version  int  not null default 1,
  source          text not null default 'claude-code',
  machine_id      text not null,
  session_id      text not null,
  agent_id        text,
  is_sidechain    boolean not null default false,
  parent_uuid     text,
  ts              timestamptz,                             -- transcript timestamp (세션 내/시간순 기준)
  received_at     timestamptz not null default now(),      -- 수집 시각
  kind            text not null,                           -- user_prompt|assistant_text|tool_use|tool_result|session_meta
  tool            text,
  tool_use_id     text,
  is_error        boolean,
  project         text,
  cwd             text,
  container_id    text,                                    -- 컨테이너(샌드박스) 세션 태깅. native는 null
  payload         jsonb not null default '{}'::jsonb,
  raw             jsonb
);

create index if not exists events_owner_ts_idx      on public.events (owner_id, ts);
create index if not exists events_session_ts_idx    on public.events (session_id, ts);
create index if not exists events_tool_use_id_idx    on public.events (tool_use_id);
create index if not exists events_kind_idx           on public.events (kind);

-- Realtime
do $$ begin
  if not exists (select 1 from pg_publication_tables where pubname='supabase_realtime' and tablename='events') then
    alter publication supabase_realtime add table public.events;
  end if;
end $$;

-- RLS: 인증 관리자가 자기 소유 행만 읽음. 직접 insert 정책 없음(Edge Function service_role만 적재).
alter table public.events enable row level security;
drop policy if exists events_read on public.events;
create policy events_read on public.events
  for select to authenticated using (owner_id = auth.uid());

-- =====================================================================
-- 2. devices 테이블 (머신 레지스트리 + 디바이스 토큰 + 하트비트)
-- =====================================================================
create table if not exists public.devices (
  id                uuid primary key default gen_random_uuid(),
  owner_id          uuid not null references auth.users(id) on delete cascade,
  token_hash        text not null unique,                  -- sha256(device_token). 원문은 저장 안 함
  name              text,
  machine_id        text,
  platform          text,
  collector_version text,
  created_at        timestamptz not null default now(),
  last_seen         timestamptz,                           -- ingest 호출 때마다 함수가 갱신(하트비트)
  revoked           boolean not null default false,
  uninstalled_at    timestamptz                            -- uninstaller가 신호 보내면 스탬프(+자동 revoke)
);
create index if not exists devices_owner_idx on public.devices(owner_id);

do $$ begin
  if not exists (select 1 from pg_publication_tables where pubname='supabase_realtime' and tablename='devices') then
    alter publication supabase_realtime add table public.devices;
  end if;
end $$;

-- RLS: 관리자가 자기 디바이스만 조회/생성/수정(revoke). 함수의 service_role은 RLS 우회.
alter table public.devices enable row level security;
drop policy if exists devices_read on public.devices;
create policy devices_read on public.devices
  for select to authenticated using (owner_id = auth.uid());
drop policy if exists devices_insert on public.devices;
create policy devices_insert on public.devices
  for insert to authenticated with check (owner_id = auth.uid());
drop policy if exists devices_update on public.devices;
create policy devices_update on public.devices
  for update to authenticated using (owner_id = auth.uid()) with check (owner_id = auth.uid());
drop policy if exists devices_delete on public.devices;
create policy devices_delete on public.devices
  for delete to authenticated using (owner_id = auth.uid());

-- =====================================================================
-- 2b. backfill_requests — 웹에서 "이 세션 과거 전체 불러오기" 요청.
--    Edge Function(ingest)이 하트비트 응답으로 디바이스에 전달 → 컬렉터가 로컬 파일을
--    처음부터 재적재(멱등). 관리자는 자기 것만 insert/조회, 함수(service_role)가 done 처리.
-- =====================================================================
create table if not exists public.backfill_requests (
  id           uuid primary key default gen_random_uuid(),
  owner_id     uuid not null references auth.users(id) on delete cascade,
  device_id    uuid references public.devices(id) on delete cascade,  -- 이 세션을 적재한 디바이스
  session_id   text not null,
  status       text not null default 'pending',                       -- pending | done
  requested_at timestamptz not null default now(),
  done_at      timestamptz
);
create index if not exists backfill_owner_idx
  on public.backfill_requests(owner_id);
create index if not exists backfill_device_pending_idx
  on public.backfill_requests(device_id, status);

alter table public.backfill_requests enable row level security;
drop policy if exists bf_read on public.backfill_requests;
create policy bf_read on public.backfill_requests
  for select to authenticated using (owner_id = auth.uid());
drop policy if exists bf_insert on public.backfill_requests;
create policy bf_insert on public.backfill_requests
  for insert to authenticated with check (owner_id = auth.uid());
-- update/delete 정책 없음 → 함수의 service_role만 done 처리(관리자 직접 수정 불가).

-- =====================================================================
-- 3. sessions 뷰 — 필터 드롭다운(세션/머신)을 DB 전체에서 채움.
--    security_invoker=true 로 events RLS(owner 스코핑)를 상속 → 자동 격리.
-- =====================================================================
create or replace view public.sessions with (security_invoker = true) as
select
  session_id,
  (array_agg(machine_id order by received_at desc))[1] as machine_id,
  (array_agg(project    order by received_at desc))[1] as project,
  count(*)::int as event_count,
  min(ts) as first_ts,
  max(ts) as last_ts,
  max(received_at) as last_received,
  (array_agg(container_id order by received_at desc))[1] as container_id
from public.events
group by session_id;

grant select on public.sessions to authenticated;

-- =====================================================================
-- 4. (선택) 보존 정책 헬퍼 — 오래된 이벤트 정리.
-- =====================================================================
-- create or replace function public.prune_events(retain_days int default 30)
-- returns bigint language plpgsql security definer as $$
-- declare deleted bigint;
-- begin
--   delete from public.events where received_at < now() - make_interval(days => retain_days);
--   get diagnostics deleted = row_count; return deleted;
-- end $$;
-- -- pg_cron 예: select cron.schedule('periscribe-prune','0 3 * * *',$$select public.prune_events(30)$$);
