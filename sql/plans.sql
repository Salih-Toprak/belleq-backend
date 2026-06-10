-- ============================================================================
-- plans — DB source of truth for per-plan limits, caps, and host sizing.
-- The backend (plan_config.py) reads this; the hardcoded values there are just
-- the seed + fallback. Edit a row here and it takes effect within ~60s, no
-- redeploy. -1 = unlimited.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.plans (
  key             text PRIMARY KEY,                 -- starter | pro | team | enterprise
  max_contexts    int     NOT NULL,                 -- -1 = unlimited
  kb_storage_gb   int     NOT NULL,                 -- -1 = unlimited
  queries_per_day int     NOT NULL,                 -- -1 = unlimited
  memory_days     int     NOT NULL,                 -- -1 = unlimited
  ram_cap_mb      int     NOT NULL,                 -- per-context RAM cap
  cpu_cap_vcpu    numeric NOT NULL,                 -- per-context vCPU cap
  disk_cap_gb     int     NOT NULL,                 -- per-context disk cap
  hosting         text    NOT NULL CHECK (hosting IN ('shared','dedicated')),
  instance_type   text    NOT NULL,                 -- EC2 type for this plan's host(s)
  updated_at      timestamptz DEFAULT now()
);

ALTER TABLE public.plans ENABLE ROW LEVEL SECURITY;
-- No anon policy => not publicly readable. The backend uses the service-role
-- key, which bypasses RLS.

INSERT INTO public.plans
  (key, max_contexts, kb_storage_gb, queries_per_day, memory_days,
   ram_cap_mb, cpu_cap_vcpu, disk_cap_gb, hosting, instance_type)
VALUES
  ('starter',     3,   2,    500,   30,  256, 0.25,  1, 'shared',    't3.large'),
  ('pro',        10,  20,   5000,  365,  512, 0.5,   2, 'dedicated', 't3.small'),
  ('team',       25, 100,  20000,   -1,  512, 0.5,   2, 'dedicated', 't3.medium'),
  ('enterprise', -1,  -1,     -1,   -1, 1024, 1.0,  10, 'dedicated', 't3.large')
ON CONFLICT (key) DO UPDATE SET
  max_contexts    = excluded.max_contexts,
  kb_storage_gb   = excluded.kb_storage_gb,
  queries_per_day = excluded.queries_per_day,
  memory_days     = excluded.memory_days,
  ram_cap_mb      = excluded.ram_cap_mb,
  cpu_cap_vcpu    = excluded.cpu_cap_vcpu,
  disk_cap_gb     = excluded.disk_cap_gb,
  hosting         = excluded.hosting,
  instance_type   = excluded.instance_type,
  updated_at      = now();

-- Examples (run anytime, no redeploy):
--   Make every enterprise host a free-tier flex large that packs:
--     update public.plans set instance_type='m7i-flex.large', cpu_cap_vcpu=0.25, ram_cap_mb=256 where key='enterprise';
--   Resize the Starter shared pool:
--     update public.plans set instance_type='m7i-flex.large' where key='starter';
