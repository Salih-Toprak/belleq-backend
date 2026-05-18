# Belleq Platform API

Control plane backend for Belleq. Manages customer AWS environments and containers. Auth and database handled by Supabase.

## Local Development

```bash
cp .env.example .env
# Fill in Supabase + AWS credentials

# Option A: Docker
docker compose up --build

# Option B: Direct
pip install -r requirements.txt
uvicorn main:app --reload --port 8080
```

## Supabase Setup

### 1. Create Tables

Run this SQL in the Supabase SQL Editor:

```sql
-- Profiles (auto-created on signup via trigger)
create table profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  email text,
  full_name text,
  plan text default 'free',
  created_at timestamptz default now()
);

-- Auto-create profile on signup
create or replace function handle_new_user()
returns trigger as $$
begin
  insert into public.profiles (id, email, full_name)
  values (new.id, new.email, new.raw_user_meta_data->>'full_name');
  return new;
end;
$$ language plpgsql security definer;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function handle_new_user();

-- Environments
create table environments (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id),
  name text not null,
  status text default 'provisioning',
  aws_instance_id text,
  aws_region text default 'eu-west-1',
  public_ip text,
  master_api_key text,
  master_port int default 9000,
  error_message text,
  created_at timestamptz default now(),
  ready_at timestamptz
);

-- Containers
create table containers (
  id uuid primary key default gen_random_uuid(),
  environment_id uuid not null references environments(id),
  user_id uuid not null references auth.users(id),
  name text not null,
  container_name text not null,
  api_key text not null,
  port int not null,
  status text default 'running',
  created_at timestamptz default now()
);

-- Audit log
create table audit_logs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id),
  action text not null,
  resource_id text,
  detail text,
  created_at timestamptz default now()
);

-- RLS policies
alter table profiles enable row level security;
alter table environments enable row level security;
alter table containers enable row level security;
alter table audit_logs enable row level security;

create policy "Users read own profile" on profiles for select using (auth.uid() = id);
create policy "Users read own environments" on environments for select using (auth.uid() = user_id);
create policy "Users read own containers" on containers for select using (auth.uid() = user_id);
create policy "Users read own audit logs" on audit_logs for select using (auth.uid() = user_id);
```

### 2. Get Credentials

From your Supabase project dashboard:
- **Settings → API**: `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` (service_role key, not anon)
- **Settings → API → JWT Settings**: `SUPABASE_JWT_SECRET`

## Environment Variables

| Variable | Description |
|---|---|
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_SERVICE_KEY` | Supabase service_role key (bypasses RLS) |
| `SUPABASE_JWT_SECRET` | JWT secret from Supabase settings |
| `AWS_ACCESS_KEY_ID` | AWS IAM access key |
| `AWS_SECRET_ACCESS_KEY` | AWS IAM secret key |
| `AWS_REGION` | Default region. Default: `eu-west-1` |
| `AWS_AMI_ID` | Amazon Linux 2 AMI ID |
| `AWS_INSTANCE_TYPE` | EC2 instance type. Default: `t3.medium` |
| `AWS_KEY_PAIR_NAME` | EC2 key pair (optional) |
| `AWS_SECURITY_GROUP_ID` | SG allowing TCP 9000 from platform |
| `INTERNAL_POLL_INTERVAL` | Poll interval seconds. Default: `15` |
| `INTERNAL_POLL_TIMEOUT` | Poll timeout seconds. Default: `600` |
| `CORS_ORIGINS` | Comma-separated allowed origins |

## API Overview

### Auth (Supabase handles register/login — this backend only verifies tokens)
- `GET /auth/me` — Current user profile

### Environments
- `GET /environments` — List user's environments
- `POST /environments/provision` — Launch new AWS environment
- `GET /environments/{id}` — Environment details
- `GET /environments/{id}/status` — Polling endpoint
- `DELETE /environments/{id}` — Terminate environment

### Containers
- `POST /environments/{id}/containers/provision` — Create container
- `GET /environments/{id}/containers` — List containers
- `DELETE /environments/{id}/containers/{cid}` — Remove container

### Proxy
- `GET /environments/{id}/proxy/stats` — Aggregate stats
- `GET /environments/{id}/proxy/docs` — Aggregate docs
- `POST /environments/{id}/proxy/sync` — Trigger sync
- `GET /environments/{id}/proxy/sources` — List sources
- `POST /environments/{id}/proxy/sources` — Create source

### Health
- `GET /health` — Platform health check

## Deployment

```bash
# On a t3.medium EC2 (eu-west-1)
sudo yum update -y && sudo yum install -y docker git
sudo systemctl start docker && sudo systemctl enable docker
sudo usermod -aG docker ec2-user
# Re-login

git clone <repo> belleq-platform && cd belleq-platform
cp .env.example .env  # fill in values
docker compose up -d --build
```

## AWS IAM Permissions

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "ec2:RunInstances",
      "ec2:DescribeInstances",
      "ec2:TerminateInstances",
      "ec2:CreateTags"
    ],
    "Resource": "*"
  }]
}
```
