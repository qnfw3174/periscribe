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
  enc_version     int  not null default 0,                  -- 0=평문(레거시), 1=E2EE(payload/raw가 envelope 암호문)
  payload         jsonb not null default '{}'::jsonb,       -- enc_version=1이면 {v,kid,n,ct} envelope
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
  uninstalled_at    timestamptz,                           -- uninstaller가 신호 보내면 스탬프(+자동 revoke)
  last_error        text,                                  -- 컬렉터가 하트비트로 보고하는 최근 오류(관측)
  last_error_at     timestamptz,
  -- E2EE: 이 디바이스의 per-device DEK를 owner 공개키로 봉인(RSA-OAEP)한 값(base64).
  -- 컬렉터가 로컬 생성한 DEK를 공개키로 wrap해 하트비트로 보내면 함수가 여기 저장.
  -- 평문 DEK·패스프레이즈·개인키는 서버가 절대 보지 않음(제로지식). 웹이 개인키로 unwrap해 복호.
  wrapped_dek       text,
  dek_kid           int not null default 1
);
create index if not exists devices_owner_idx on public.devices(owner_id);
-- 기존 배포(devices 테이블이 이미 있는 경우)에도 E2EE 컬럼을 추가(create table if not exists는 스킵되므로).
alter table public.devices add column if not exists wrapped_dek text;
alter table public.devices add column if not exists dek_kid    int not null default 1;
-- 디바이스 연속성: machine_guid(머신 고유 식별, Windows MachineGuid)로 재설치해도 같은 행에 연결.
--   ingest가 (owner_id, machine_guid)로 디바이스를 찾는다. 재설치 시 새 토큰이라도 guid가 같으면 이어짐.
alter table public.devices add column if not exists machine_guid text;
-- dek_keys: kid→봉인DEK 히스토리. 재설치로 새 DEK 세대가 생겨도 옛 세대를 덮어쓰지 않고 누적 → 옛 로그 복호 유지.
alter table public.devices add column if not exists dek_keys jsonb not null default '{}'::jsonb;
-- (owner, machine_guid) 고유: 머신당 디바이스 1개. guid 없는 레거시 행은 제외(partial).
create unique index if not exists devices_owner_guid_uidx
  on public.devices(owner_id, machine_guid) where machine_guid is not null;

do $$ begin
  if not exists (select 1 from pg_publication_tables where pubname='supabase_realtime' and tablename='devices') then
    alter publication supabase_realtime add table public.devices;
  end if;
end $$;
-- DELETE/UPDATE Realtime 이벤트가 owner_id를 포함해 RLS를 통과하도록(기본=PK만이면 삭제 이벤트가
-- RLS 평가 불가로 누락 → 웹에 유령 디바이스가 남음). full로 전체 컬럼을 old 레코드에 싣는다.
alter table public.devices replica identity full;

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

-- 디바이스 "완전 삭제": 디바이스 행 + 그 머신의 events 까지 제거. events엔 authenticated용
-- delete RLS가 없으므로(읽기 전용) security definer 함수로 소유 검증 후 함께 지운다.
-- (backfill_requests는 device_id FK on delete cascade로 자동 정리)
create or replace function public.purge_device(p_device uuid)
returns void language plpgsql security definer
set search_path = public as $$
begin
  if not exists (select 1 from public.devices where id = p_device and owner_id = auth.uid()) then
    raise exception 'device not found or not owner';
  end if;
  delete from public.events  where device_id = p_device and owner_id = auth.uid();
  delete from public.devices where id = p_device and owner_id = auth.uid();
end $$;
revoke all on function public.purge_device(uuid) from anon, public;
grant execute on function public.purge_device(uuid) to authenticated;

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
-- 2c. owner_keys — 관리자(owner)별 E2EE 키 자료. 전부 "비밀 아님"(패스프레이즈 없인 무용).
--    public_key: owner 공개키(SPKI). 컬렉터가 per-device DEK를 이걸로 봉인.
--    wrapped_private_key: 개인키(PKCS8)를 패스프레이즈 유도 KEK로 봉인(envelope).
--    wrapped_private_key_recovery: 복구코드로 한 번 더 봉인(분실 대비, 선택).
--    개인키 평문·패스프레이즈는 서버에 절대 올라가지 않는다(웹에서만 복원).
-- =====================================================================
create table if not exists public.owner_keys (
  owner_id                     uuid primary key references auth.users(id) on delete cascade,
  public_key                   text not null,               -- SPKI base64 (RSA-OAEP 공개키)
  wrapped_private_key          jsonb not null,              -- {v,n,ct} KEK로 봉인한 PKCS8
  wrapped_private_key_recovery jsonb,                       -- 복구코드로 봉인(선택)
  kdf                          text not null default 'pbkdf2-sha256',
  kdf_params                   jsonb not null,              -- { salt, iterations }
  kid                          int  not null default 1,     -- 공개키 세대(회전 대비)
  created_at                   timestamptz not null default now()
);

-- RLS: 관리자는 자기 키 자료만 읽고/쓴다. 함수(service_role)는 RLS 우회로 public_key만 읽음.
alter table public.owner_keys enable row level security;
drop policy if exists ok_rw on public.owner_keys;
create policy ok_rw on public.owner_keys
  for all to authenticated using (owner_id = auth.uid()) with check (owner_id = auth.uid());

-- =====================================================================
-- 2d. session_catalog — 컬렉터가 보고하는 "로컬에 존재하는 세션 목록"(내용 미적재 포함).
--    웹이 전체 과거 세션을 최근변경순으로 나열하고, 선택 시 백필로 내용을 끌어오게 함.
--    컬렉터가 하트비트에 catalog를 실어 보내면 ingest가 owner/device 스탬프해 upsert.
-- =====================================================================
create table if not exists public.session_catalog (
  owner_id     uuid not null references auth.users(id) on delete cascade,
  device_id    uuid not null references public.devices(id) on delete cascade,
  session_id   text not null,
  project      text,
  container_id text,
  file_mtime   timestamptz,                  -- transcript 파일 최종 변경시각(목록 정렬 기준)
  size_bytes   bigint,
  updated_at   timestamptz not null default now(),
  primary key (device_id, session_id)
);
create index if not exists session_catalog_owner_mtime_idx
  on public.session_catalog(owner_id, file_mtime desc);

alter table public.session_catalog enable row level security;
drop policy if exists sc_read on public.session_catalog;
create policy sc_read on public.session_catalog
  for select to authenticated using (owner_id = auth.uid());

do $$ begin
  if not exists (select 1 from pg_publication_tables where pubname='supabase_realtime' and tablename='session_catalog') then
    alter publication supabase_realtime add table public.session_catalog;
  end if;
end $$;

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
-- 4. 보존 정책 — 오래된 이벤트 정리(무한 증가 방지). 기본 90일, pg_cron 일간 실행.
-- =====================================================================
create or replace function public.prune_events(retain_days int default 90)
returns bigint language plpgsql security definer
set search_path = public as $$
declare deleted bigint;
begin
  delete from public.events where received_at < now() - make_interval(days => retain_days);
  get diagnostics deleted = row_count;
  return deleted;
end $$;
revoke all on function public.prune_events(int) from anon, authenticated;  -- 관리자/cron만

-- pg_cron: 매일 03:00 보존정책 실행(보존일 조정은 prune_events 인자 변경).
create extension if not exists pg_cron;
do $$
begin
  if exists (select 1 from cron.job where jobname = 'periscribe-prune') then
    perform cron.unschedule('periscribe-prune');
  end if;
  perform cron.schedule('periscribe-prune', '0 3 * * *', $j$ select public.prune_events(90) $j$);
end $$;
