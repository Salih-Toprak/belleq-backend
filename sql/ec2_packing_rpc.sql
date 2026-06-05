-- ============================================================================
-- EC2 packing — atomic capacity reservation
-- PostgREST can't do `set x = x + n` arithmetic, so capacity changes go through
-- these RPC functions. reserve_host_capacity is a single guarded UPDATE, so two
-- concurrent placements can never over-commit a host.
-- Run in the Supabase SQL Editor. Idempotent (CREATE OR REPLACE).
-- ============================================================================

create or replace function public.reserve_host_capacity(
  p_host_id  uuid,
  p_cpu      numeric,
  p_ram_mb   integer,
  p_disk_gb  integer
) returns boolean
language plpgsql
as $$
declare
  affected integer;
begin
  update public.hosts
     set cpu_used     = cpu_used     + p_cpu,
         ram_used_mb  = ram_used_mb  + p_ram_mb,
         disk_used_gb = disk_used_gb + p_disk_gb
   where id = p_host_id
     and status = 'ready'
     and cpu_used     + p_cpu     <= cpu_budget
     and ram_used_mb  + p_ram_mb  <= ram_budget_mb
     and disk_used_gb + p_disk_gb <= disk_budget_gb;
  get diagnostics affected = row_count;
  return affected > 0;
end;
$$;

create or replace function public.release_host_capacity(
  p_host_id  uuid,
  p_cpu      numeric,
  p_ram_mb   integer,
  p_disk_gb  integer
) returns void
language plpgsql
as $$
begin
  update public.hosts
     set cpu_used     = greatest(0, cpu_used     - p_cpu),
         ram_used_mb  = greatest(0, ram_used_mb  - p_ram_mb),
         disk_used_gb = greatest(0, disk_used_gb - p_disk_gb)
   where id = p_host_id;
end;
$$;
