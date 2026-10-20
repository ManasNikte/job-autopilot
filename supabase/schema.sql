-- Job Autopilot online dashboard: database schema for Supabase (run once in the SQL editor).
--
-- Security model
--   * Every table has Row Level Security: a signed-in user can only read and write rows where
--     user_id = auth.uid(). There is no shared or public data.
--   * The local Mac connects OUT with the user's own session (no service key ever leaves Supabase).
--   * The website can only queue a short list of commands (check constraint); the Mac decides what
--     they mean. Nothing here can run arbitrary code on anyone's computer.
--   * Only summaries are stored: counts, job titles/companies/status, and email-update summaries.
--     Resumes, cover letters, the master prompt, and email text never leave the user's machine.

-- ---------------------------------------------------------------- profiles
create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  display_name text check (char_length(display_name) <= 60),
  marketing_opt_in boolean not null default false,          -- the separate, unchecked sign-up box
  opt_in_at timestamptz,
  created_at timestamptz not null default now()
);

-- Create the profile row from the sign-up form's metadata (name + opt-in choice).
create or replace function public.handle_new_user() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  insert into public.profiles (id, display_name, marketing_opt_in, opt_in_at)
  values (new.id,
          left(coalesce(new.raw_user_meta_data->>'display_name', ''), 60),
          coalesce((new.raw_user_meta_data->>'marketing_opt_in')::boolean, false),
          case when coalesce((new.raw_user_meta_data->>'marketing_opt_in')::boolean, false) then now() end)
  on conflict (id) do nothing;
  return new;
end $$;
drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created after insert on auth.users
  for each row execute function public.handle_new_user();

-- ---------------------------------------------------------------- machines (one per computer)
create table if not exists public.machines (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  name text not null check (char_length(name) <= 80),
  platform text check (char_length(platform) <= 20),
  app_version text check (char_length(app_version) <= 20),
  last_seen timestamptz,
  created_at timestamptz not null default now()
);
create index if not exists machines_user on public.machines(user_id);

-- ---------------------------------------------------------------- latest progress snapshot per machine
create table if not exists public.snapshots (
  machine_id uuid primary key references public.machines(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  data jsonb not null check (pg_column_size(data) < 200000),
  updated_at timestamptz not null default now()
);

-- ---------------------------------------------------------------- job list (metadata only)
create table if not exists public.jobs (
  machine_id uuid not null references public.machines(id) on delete cascade,
  job_id text not null check (char_length(job_id) <= 200),
  user_id uuid not null references auth.users(id) on delete cascade,
  company text, title text, location text, status text, match_score int,
  salary text, posted_at timestamptz, applied_at timestamptz, url text,
  updated_at timestamptz not null default now(),
  primary key (machine_id, job_id)
);
create index if not exists jobs_user on public.jobs(user_id, updated_at desc);

-- ---------------------------------------------------------------- email updates (summaries only)
create table if not exists public.email_updates (
  machine_id uuid not null references public.machines(id) on delete cascade,
  event_id bigint not null,
  user_id uuid not null references auth.users(id) on delete cascade,
  received_at timestamptz, company text, role text, event text, summary text,
  deadline text, job_id text, new_status text, state text,
  primary key (machine_id, event_id)
);
create index if not exists email_updates_user on public.email_updates(user_id, received_at desc);

-- ---------------------------------------------------------------- commands from the website to the Mac
create table if not exists public.commands (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  machine_id uuid not null references public.machines(id) on delete cascade,
  kind text not null check (kind in ('run', 'process', 'mail', 'resume')),
  status text not null default 'pending' check (status in ('pending', 'claimed', 'done', 'failed', 'expired')),
  result text check (char_length(result) <= 500),
  created_at timestamptz not null default now(),
  claimed_at timestamptz, finished_at timestamptz
);
create index if not exists commands_pending on public.commands(machine_id, status, created_at);

-- ---------------------------------------------------------------- Row Level Security: owner-only
alter table public.profiles      enable row level security;
alter table public.machines      enable row level security;
alter table public.snapshots     enable row level security;
alter table public.jobs          enable row level security;
alter table public.email_updates enable row level security;
alter table public.commands      enable row level security;

drop policy if exists "own profile" on public.profiles;
create policy "own profile" on public.profiles for all
  using (id = auth.uid()) with check (id = auth.uid());

do $$
declare t text;
begin
  foreach t in array array['machines', 'snapshots', 'jobs', 'email_updates', 'commands'] loop
    execute format('drop policy if exists "own rows" on public.%I', t);
    execute format('create policy "own rows" on public.%I for all to authenticated
                    using (user_id = auth.uid()) with check (user_id = auth.uid())', t);
  end loop;
end $$;

-- A queued command must target one of the user's own machines.
create or replace function public.command_machine_is_mine() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  if not exists (select 1 from public.machines m where m.id = new.machine_id and m.user_id = new.user_id) then
    raise exception 'unknown machine';
  end if;
  return new;
end $$;
drop trigger if exists commands_machine_check on public.commands;
create trigger commands_machine_check before insert on public.commands
  for each row execute function public.command_machine_is_mine();

-- At most 30 commands per user per hour (stops a stolen session or a stuck button from spamming a laptop).
create or replace function public.command_rate_limit() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  if (select count(*) from public.commands c where c.user_id = new.user_id
        and c.created_at > now() - interval '1 hour') >= 30 then
    raise exception 'too many requests; try again later';
  end if;
  return new;
end $$;
drop trigger if exists commands_rate_limit on public.commands;
create trigger commands_rate_limit before insert on public.commands
  for each row execute function public.command_rate_limit();

-- "Delete my data" on the website removes every row the user owns; deleting the account itself
-- removes everything through the on-delete-cascade foreign keys.
create or replace function public.delete_my_account() returns void
language plpgsql security definer set search_path = public as $$
begin
  delete from auth.users where id = auth.uid();
end $$;
revoke all on function public.delete_my_account() from public, anon;
grant execute on function public.delete_my_account() to authenticated;

-- Live updates on the website when the Mac pushes a new snapshot or finishes a command.
alter publication supabase_realtime add table public.snapshots;
alter publication supabase_realtime add table public.commands;
