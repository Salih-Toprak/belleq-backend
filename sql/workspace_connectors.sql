-- ============================================================================
-- workspace_connectors: durable, per-account home for MCP connectors.
--
-- Connectors live on ephemeral EC2 masters; this table is their permanent home
-- on the (static) backend, so they survive deleting all contexts / terminating
-- the host. Masters mirror every change here and are re-hydrated from here on
-- the next provision.
--
-- Secrets are stored ONLY as `secrets_encrypted` — the Fernet ciphertext the
-- master produced with the shared CREDENTIAL_ENCRYPTION_KEY. The backend never
-- decrypts it; no plaintext credential is ever stored here.
--
-- Run in the Supabase SQL Editor (Dashboard > SQL Editor > New query).
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.workspace_connectors (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id      text NOT NULL,
  connector_id      text NOT NULL,
  display_name      text NOT NULL DEFAULT '',
  transport         text NOT NULL DEFAULT 'streamable_http',
  url               text NOT NULL DEFAULT '',
  command           text NOT NULL DEFAULT '',
  args              jsonb NOT NULL DEFAULT '[]'::jsonb,
  enabled           boolean NOT NULL DEFAULT true,
  auth_status       text NOT NULL DEFAULT 'none',
  tool_count        integer NOT NULL DEFAULT 0,
  last_status       text NOT NULL DEFAULT 'unknown',
  secrets_encrypted text NOT NULL DEFAULT '{}',  -- Fernet ciphertext; never decrypted here
  metadata          jsonb NOT NULL DEFAULT '{}'::jsonb,
  added_at          timestamptz,
  updated_at        timestamptz,
  synced_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, connector_id)
);

CREATE INDEX IF NOT EXISTS workspace_connectors_workspace_idx
  ON public.workspace_connectors (workspace_id);

-- Lock the table to the service role only. RLS is enabled with NO policies, so
-- anon/authenticated clients (the browser) can never read it; the backend's
-- service key bypasses RLS. The dashboard only ever sees the redacted list
-- served by GET /connectors — never this table directly.
ALTER TABLE public.workspace_connectors ENABLE ROW LEVEL SECURITY;
