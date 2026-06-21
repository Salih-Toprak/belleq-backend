-- ============================================================================
-- Agent execution layer: agents, tasks, step-by-step run logs, and the
-- shared-KB review queue. All four live on the (static) backend so they survive
-- the ephemeral EC2 master/container being terminated — same rationale as
-- workspace_connectors.sql.
--
-- Durable state is owned here. The execution engine runs in the per-context
-- belleq-user container (where the KB + LLM + connectors are reachable); the
-- backend triggers a run, the container runs the loop and returns the result +
-- step log + cost, and the backend persists it into these tables.
--
-- Secrets: agents.api_key_encrypted holds ONLY Fernet ciphertext (BYOK provider
-- keys), encrypted with CREDENTIAL_ENCRYPTION_KEY via crypto.py. It is never
-- returned by any API serializer and never logged. RLS is enabled with no
-- policies so the browser (anon/authenticated) can never read these tables; the
-- backend's service key bypasses RLS.
--
-- Run in the Supabase SQL Editor (Dashboard > SQL Editor > New query).
-- ============================================================================

-- ── agents ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.agents (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  context_id         uuid NOT NULL,
  workspace_id       text NOT NULL,                 -- owner (containers.workspace_id)
  name               text NOT NULL DEFAULT '',
  role_description   text NOT NULL DEFAULT '',      -- core of the system prompt
  kb_scope           text NOT NULL DEFAULT 'scoped',-- master | scoped | both
  kb_section_ids     jsonb NOT NULL DEFAULT '[]'::jsonb,
  connector_ids      jsonb NOT NULL DEFAULT '[]'::jsonb,
  provider           text NOT NULL DEFAULT 'belleq',-- belleq | byok | openrouter
  api_key_encrypted  text,                          -- Fernet ciphertext; BYOK/openrouter only; never returned
  model              text NOT NULL DEFAULT '',      -- e.g. claude-sonnet-4-6, gpt-4o
  budget_limit_usd   double precision,              -- max spend/day; null = unlimited
  status             text NOT NULL DEFAULT 'active',-- active | paused | archived
  notify_enabled     boolean NOT NULL DEFAULT false,-- message me via a communication connector on run finish
  notify_connector_ids jsonb NOT NULL DEFAULT '[]'::jsonb,-- which communication connectors to notify through
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS agents_context_idx   ON public.agents (context_id);
CREATE INDEX IF NOT EXISTS agents_workspace_idx ON public.agents (workspace_id);

-- ── tasks ─────────────────────────────────────────────────────────────────--
CREATE TABLE IF NOT EXISTS public.agent_tasks (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id      uuid NOT NULL,
  context_id    uuid NOT NULL,
  workspace_id  text NOT NULL,
  instruction   text NOT NULL DEFAULT '',
  status        text NOT NULL DEFAULT 'pending',    -- pending | running | completed | failed
  trigger       text NOT NULL DEFAULT 'manual',     -- manual | <cron expr> | webhook
  run_token     text,                               -- per-run secret authorizing live step callbacks
  result        text,
  kb_writes     jsonb NOT NULL DEFAULT '[]'::jsonb,
  tokens_used   integer NOT NULL DEFAULT 0,
  cost_usd      double precision NOT NULL DEFAULT 0,
  created_at    timestamptz NOT NULL DEFAULT now(),
  completed_at  timestamptz
);

CREATE INDEX IF NOT EXISTS agent_tasks_agent_idx  ON public.agent_tasks (agent_id);
CREATE INDEX IF NOT EXISTS agent_tasks_status_idx ON public.agent_tasks (status);
-- Budget sum query: spend per agent since start-of-day.
CREATE INDEX IF NOT EXISTS agent_tasks_agent_created_idx
  ON public.agent_tasks (agent_id, created_at);

-- ── step-by-step execution log ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.agent_runs (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id         uuid NOT NULL,
  agent_id        uuid NOT NULL,
  step_number     integer NOT NULL DEFAULT 0,
  type            text NOT NULL,                     -- kb_read | connector_call | llm_call | kb_write
  input_summary   text NOT NULL DEFAULT '',
  output_summary  text NOT NULL DEFAULT '',
  timestamp       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS agent_runs_task_idx  ON public.agent_runs (task_id);
CREATE INDEX IF NOT EXISTS agent_runs_agent_idx ON public.agent_runs (agent_id);

-- ── shared-KB review queue ───────────────────────────────────────────────────
-- kb_write(scope="shared") does NOT auto-write to the shared KB — it is queued
-- here for human approval. Approving upserts the content into the context KB.
CREATE TABLE IF NOT EXISTS public.kb_review_queue (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  context_id    uuid NOT NULL,
  workspace_id  text NOT NULL,
  agent_id      uuid,
  task_id       uuid,
  content       text NOT NULL DEFAULT '',
  tags          jsonb NOT NULL DEFAULT '[]'::jsonb,
  status        text NOT NULL DEFAULT 'pending',     -- pending | approved | rejected
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS kb_review_queue_context_idx ON public.kb_review_queue (context_id);
CREATE INDEX IF NOT EXISTS kb_review_queue_status_idx  ON public.kb_review_queue (status);

-- Lock all four to the service role only (RLS on, no policies). The dashboard
-- only ever sees them through the backend's redacted serializers.
ALTER TABLE public.agents          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_tasks     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_runs      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.kb_review_queue ENABLE ROW LEVEL SECURITY;

-- ── migrations for existing deployments (idempotent) ─────────────────────────
-- Run these if the tables already exist from an earlier version.
ALTER TABLE public.agent_tasks ADD COLUMN IF NOT EXISTS run_token            text;
ALTER TABLE public.agents      ADD COLUMN IF NOT EXISTS notify_enabled       boolean NOT NULL DEFAULT false;
ALTER TABLE public.agents      ADD COLUMN IF NOT EXISTS notify_connector_ids jsonb NOT NULL DEFAULT '[]'::jsonb;
