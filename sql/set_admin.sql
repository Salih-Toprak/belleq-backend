-- ============================================================================
-- Promote an account to admin by email.
-- Creates the profiles row if it doesn't exist yet (e.g. accounts that were
-- created before the profiles table / signup trigger existed).
-- Run in Supabase SQL Editor. Replace the email below.
-- ============================================================================

INSERT INTO public.profiles (id, role)
SELECT id, 'admin'
FROM auth.users
WHERE email = 'YOUR_EMAIL_HERE'
ON CONFLICT (id) DO UPDATE SET role = 'admin';

-- Verify it worked:
SELECT u.email, p.role
FROM public.profiles p
JOIN auth.users u ON u.id = p.id
WHERE u.email = 'YOUR_EMAIL_HERE';
