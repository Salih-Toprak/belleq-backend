-- ============================================================================
-- profiles table for RBAC
-- Run this in the Supabase SQL Editor (Dashboard > SQL Editor > New query)
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.profiles (
  id        uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  role      text CHECK (role IN ('free', 'pro', 'team', 'enterprise', 'admin')),
  created_at timestamptz DEFAULT now()
);

-- Allow the service-role key (used by the backend) full access
ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;

-- Policy: users can read their own profile
CREATE POLICY "Users can read own profile"
  ON public.profiles FOR SELECT
  USING (auth.uid() = id);

-- Policy: service role (backend) can do everything via service key
-- (Supabase service key bypasses RLS by default, so no extra policy needed)

-- Optional: auto-create a profile row when a new user signs up
-- This trigger creates an empty profile (role = NULL) so the frontend
-- redirects them to the plan-selection page.
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS trigger AS $$
BEGIN
  INSERT INTO public.profiles (id)
  VALUES (NEW.id)
  ON CONFLICT (id) DO NOTHING;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Drop existing trigger if present, then recreate
DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();
