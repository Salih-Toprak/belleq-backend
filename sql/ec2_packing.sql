-- ============================================================================
-- EC2 packing — data model
-- Adds the `hosts` concept (shared/dedicated EC2 running a master) and the
-- per-context columns needed for placement, resource caps, and usage metering.
-- Idempotent — safe to run repeatedly in the Supabase SQL Editor.
--
-- Note: workspace_id == user_id for now (one workspace per user). A real
-- `workspaces` table can be introduced later for multi-member Team plans.
-- ============================================================================

-- ── hosts: a provisioned EC2 running one master (+ qdrant) ──────────────────
create table if not exists public.hosts (
  id              uuid primary key default gen_random_uuid(),
  host_type       text not null check (host_type in ('shared', 'dedicated')),
  pool            text,                       -- shared-pool name, e.g. 'euw1-02'
  workspace_id    uuid,                       -- set on dedicated hosts only
  plan            text,                       -- plan this host serves
  region          text not null default 'eu-west-1',
  instance_type   text not null,
  ec2_instance_id text,
  public_ip       text,
  master_endpoint text,                       -- e.g. http://1.2.3.4:9000
  master_api_key  text,

  -- schedulable capacity (instance total minus master/qdrant/OS reserve)
  cpu_budget      numeric not null default 0,
  ram_budget_mb   integer not null default 0,
  disk_budget_gb  integer not null default 0,
  -- currently reserved by placed contexts
  cpu_used        numeric not null default 0,
  ram_used_mb     integer not null default 0,
  disk_used_gb    integer not null default 0,

  status          text not null default 'provisioning',  -- provisioning|ready|error|terminated
  error_message   text,
  created_at      timestamptz default now(),
  ready_at        timestamptz
);

create index if not exists hosts_pool_idx on public.hosts (host_type, pool, status);
create index if not exists hosts_ws_idx   on public.hosts (workspace_id);

alter table public.hosts enable row level security;
-- Backend uses the service key (bypasses RLS). Owners may read their dedicated host.
drop policy if exists "owners read own dedicated host" on public.hosts;
create policy "owners read own dedicated host"
  on public.hosts for select
  using (auth.uid() = workspace_id);

-- ── containers (contexts): placement, caps, and usage metering ──────────────
alter table public.containers
  add column if not exists workspace_id          uuid,
  add column if not exists host_id               uuid references public.hosts(id),
  add column if not exists qdrant_collection      text,
  add column if not exists plan                   text,
  add column if not exists ram_cap_mb             integer,
  add column if not exists cpu_cap_vcpu           numeric,
  add column if not exists disk_cap_gb            integer,
  add column if not exists kb_storage_used_bytes  bigint  not null default 0,
  add column if not exists query_count_today      integer not null default 0,
  add column if not exists query_reset_at         timestamptz;

-- Backfill workspace_id from the existing owner for any pre-existing rows.
update public.containers set workspace_id = user_id where workspace_id is null;

create index if not exists containers_ws_idx   on public.containers (workspace_id);
create index if not exists containers_host_idx on public.containers (host_id);

-- environment_id is now legacy (kept for back-compat; the new flow uses host_id).
-- The collapsed flow has no environment, so this column must allow NULL.
alter table public.containers alter column environment_id drop not null;

-- The collapsed flow inserts the context row as 'provisioning' before the
-- container exists; api_key is assigned at creation now but older schemas had
-- it NOT NULL, so relax it to keep provisioning robust.
alter table public.containers alter column api_key drop not null;

-- Optional: error_message on contexts for the dashboard to surface failures.
alter table public.containers add column if not exists error_message text;
