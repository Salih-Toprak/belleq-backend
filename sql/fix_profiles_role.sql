-- ============================================================================
-- Idempotent fix: ensure public.profiles exists and has a `role` column.
-- Safe to run repeatedly. Run in Supabase SQL Editor.
-- ============================================================================

-- 1. Make sure the table exists at all
CREATE TABLE IF NOT EXISTS public.profiles (
  id         uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  created_at timestamptz DEFAULT now()
);

-- 2. Add the role column if it's missing
ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS role text;

-- 3. Add created_at if it's missing (older partial tables)
ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT now();

-- 4. Add the role CHECK constraint (drop first so re-running doesn't error)
ALTER TABLE public.profiles
  DROP CONSTRAINT IF EXISTS profiles_role_check;
ALTER TABLE public.profiles
  ADD CONSTRAINT profiles_role_check
  CHECK (role IS NULL OR role IN ('free', 'pro', 'team', 'enterprise', 'admin'));

-- 5. Promote your account to admin (replace the email)
INSERT INTO public.profiles (id, role)
SELECT id, 'admin'
FROM auth.users
WHERE email = 'YOUR_EMAIL_HERE'
ON CONFLICT (id) DO UPDATE SET role = 'admin';

-- 6. Verify
SELECT u.email, p.role
FROM public.profiles p
JOIN auth.users u ON u.id = p.id
WHERE u.email = 'YOUR_EMAIL_HERE';
