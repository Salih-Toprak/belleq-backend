-- ============================================================================
-- Public launch waitlist
-- Run this in the Supabase SQL Editor (Dashboard > SQL Editor > New query).
--
-- Anyone (anonymous visitors) may INSERT their email to join the waitlist.
-- Nobody can SELECT/READ the list with the anon key — only the service role
-- (backend / Supabase dashboard) can read it. This keeps the email list private
-- while letting the marketing site collect signups with just the anon key.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.waitlist (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email      text NOT NULL UNIQUE,
  name       text,
  source     text,                          -- which CTA / page they came from
  referrer   text,
  created_at timestamptz DEFAULT now()
);

ALTER TABLE public.waitlist ENABLE ROW LEVEL SECURITY;

-- Allow anonymous + authenticated visitors to add themselves.
DROP POLICY IF EXISTS "anyone can join the waitlist" ON public.waitlist;
CREATE POLICY "anyone can join the waitlist"
  ON public.waitlist
  FOR INSERT
  TO anon, authenticated
  WITH CHECK (true);

-- No SELECT/UPDATE/DELETE policies => the anon key cannot read or modify rows.
-- The service-role key (backend) bypasses RLS, so you can always read the list
-- from the Supabase dashboard or an admin endpoint.

-- Helpful index for sorting signups by time.
CREATE INDEX IF NOT EXISTS waitlist_created_at_idx ON public.waitlist (created_at DESC);
