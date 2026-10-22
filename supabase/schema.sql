-- Job Autopilot online dashboard: database schema for Supabase (run once in the SQL editor).
--
-- Security model
--   * Every table has Row Level Security: a signed-in user can only read and write rows where
--     user_id = auth.uid(), plus read the rows of an owner who added their confirmed email
--     (account_emails). There is no public data.
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
-- City the user chose to show the site operator (Settings → Online dashboard; blank keeps it private).
alter table public.machines add column if not exists city text check (char_length(city) <= 80);

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

-- ---------------------------------------------------------------- extra sign-in emails
-- An owner can let other email addresses sign in to the same dashboard (for example a personal and a
-- university address). Each address is its own confirmed account with its own password; it sees the
-- owner's data and can send requests to the owner's computers, but can't connect computers or add emails.
create table if not exists public.account_emails (
  owner_id uuid not null references auth.users(id) on delete cascade,
  email text not null check (email = lower(email) and char_length(email) between 3 and 254 and email like '%_@_%'),
  created_at timestamptz not null default now(),
  primary key (owner_id, email)
);
alter table public.account_emails enable row level security;

-- Owners whose data the signed-in user may see: themselves, plus anyone who added their confirmed email.
create or replace function public.viewable_owner_ids() returns setof uuid
language sql stable security definer set search_path = public as $$
  select auth.uid()
  union
  select a.owner_id from public.account_emails a
  join auth.users u on u.id = auth.uid() and u.email_confirmed_at is not null   -- only a confirmed address
  where a.email = lower(u.email) and a.owner_id <> auth.uid()
$$;
revoke all on function public.viewable_owner_ids() from public, anon;
grant execute on function public.viewable_owner_ids() to authenticated;

drop policy if exists "owner manages" on public.account_emails;
create policy "owner manages" on public.account_emails for all to authenticated
  using (owner_id = auth.uid()) with check (owner_id = auth.uid());
drop policy if exists "member sees and leaves" on public.account_emails;
create policy "member sees and leaves" on public.account_emails for select to authenticated
  using (email = lower(auth.jwt() ->> 'email'));
drop policy if exists "member leaves" on public.account_emails;
create policy "member leaves" on public.account_emails for delete to authenticated
  using (email = lower(auth.jwt() ->> 'email'));

create or replace function public.account_email_limit() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  if (select count(*) from public.account_emails where owner_id = new.owner_id) >= 5 then
    raise exception 'at most 5 extra emails';
  end if;
  return new;
end $$;
drop trigger if exists account_email_limit on public.account_emails;
create trigger account_email_limit before insert on public.account_emails
  for each row execute function public.account_email_limit();

-- ---------------------------------------------------------------- policies
-- Read: your rows and those of owners who added your email. Write: only your own rows, except that a
-- request (command) may be queued for an owner who shared with you.
do $$
declare t text;
begin
  foreach t in array array['machines', 'snapshots', 'jobs', 'email_updates', 'commands'] loop
    execute format('drop policy if exists "own rows" on public.%I', t);
    execute format('drop policy if exists "read shared" on public.%I', t);
    execute format('drop policy if exists "write own" on public.%I', t);
    execute format('drop policy if exists "update own" on public.%I', t);
    execute format('drop policy if exists "delete own" on public.%I', t);
    execute format('create policy "read shared" on public.%I for select to authenticated
                    using (user_id in (select public.viewable_owner_ids()))', t);
    execute format('create policy "update own" on public.%I for update to authenticated
                    using (user_id = auth.uid()) with check (user_id = auth.uid())', t);
    execute format('create policy "delete own" on public.%I for delete to authenticated
                    using (user_id = auth.uid())', t);
    if t = 'commands' then
      execute format('create policy "write own" on public.%I for insert to authenticated
                      with check (user_id in (select public.viewable_owner_ids()))', t);
    else
      execute format('create policy "write own" on public.%I for insert to authenticated
                      with check (user_id = auth.uid())', t);
    end if;
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

-- Progress rows (snapshot, jobs, email updates) must belong to one of the writer's own machines, so nobody
-- can claim another computer's rows by guessing its id.
create or replace function public.row_machine_is_mine() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  if not exists (select 1 from public.machines m where m.id = new.machine_id and m.user_id = new.user_id) then
    raise exception 'unknown machine';
  end if;
  return new;
end $$;
do $$
declare t text;
begin
  foreach t in array array['snapshots', 'jobs', 'email_updates'] loop
    execute format('drop trigger if exists row_machine_check on public.%I', t);
    execute format('create trigger row_machine_check before insert or update on public.%I
                    for each row execute function public.row_machine_is_mine()', t);
  end loop;
end $$;

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

-- ---------------------------------------------------------------- operator stats
-- Applications in the last N days on one machine, from its snapshot's 14-day series (counts only).
create or replace function public.applied_in_days(snap jsonb, days int) returns int
language sql stable set search_path = public as $$
  select coalesce(sum((e->>'n')::int), 0)::int
  from jsonb_array_elements(coalesce(snap->'series', '[]'::jsonb)) e
  where (e->>'day')::date > current_date - days
$$;

-- Site-wide counts for the person who runs the site ("active" = applied in the last 14 days), shown in the footer of their local dashboard.
-- The operators table has row-level security and no policies, so nobody can read or change it through
-- the API; add yourself once in the SQL editor (see supabase/README.md). Everyone else gets null.
create table if not exists public.operators (
  user_id uuid primary key references auth.users(id) on delete cascade
);
alter table public.operators enable row level security;

create or replace function public.operator_stats() returns jsonb
language plpgsql stable security definer set search_path = public as $$
begin
  if not exists (select 1 from public.operators where user_id = auth.uid()) then
    return null;
  end if;
  return jsonb_build_object(
    -- confirmed accounts, not counting extra sign-in emails (the same person)
    'users', (select count(*) from auth.users u
              where u.email_confirmed_at is not null
                and not exists (select 1 from public.account_emails a
                                where a.email = lower(u.email) and a.owner_id <> u.id)),
    -- active: applied to at least one job in the last 14 days
    'active', (select count(distinct s.user_id) from public.snapshots s
               where public.applied_in_days(s.data, 14) > 0),
    'connected', (select count(distinct m.user_id) from public.machines m),
    'as_of', now());
end $$;
revoke all on function public.operator_stats() from public, anon;
grant execute on function public.operator_stats() to authenticated;

-- One row per person for the operator: account details, each computer, and progress counts.
-- Exactly what the privacy note lists. Never company names, job titles, links, assessment or email content,
-- or the greeting (it can name companies): only numbers, role-family labels, and run times.
create or replace function public.operator_users() returns jsonb
language plpgsql stable security definer set search_path = public as $$
begin
  if not exists (select 1 from public.operators where user_id = auth.uid()) then
    return null;
  end if;
  return coalesce((select jsonb_agg(row_to_json(x)::jsonb order by x.applied_week desc, x.applied_total desc)
  from (
    select u.email, p.display_name as name, u.email_confirmed_at as joined, u.last_sign_in_at as last_sign_in,
           coalesce(p.marketing_opt_in, false) as opt_in, p.opt_in_at,
           (select coalesce(jsonb_agg(a.email order by a.created_at), '[]'::jsonb)
              from public.account_emails a where a.owner_id = u.id) as other_emails,
           (select m.city from public.machines m where m.user_id = u.id and coalesce(m.city, '') <> ''
             order by m.last_seen desc nulls last limit 1) as city,
           (select count(*) from public.machines m where m.user_id = u.id) as computers,
           (select max(m.last_seen) from public.machines m where m.user_id = u.id) as last_seen,
           coalesce((select sum((s.data->'kpis'->>'applied_total')::int) from public.snapshots s where s.user_id = u.id), 0) as applied_total,
           coalesce((select sum(public.applied_in_days(s.data, 7)) from public.snapshots s where s.user_id = u.id), 0) as applied_week,
           coalesce((select sum(public.applied_in_days(s.data, 14)) from public.snapshots s where s.user_id = u.id), 0) as applied_14d,
           (select count(*) from public.commands c where c.user_id = u.id and c.created_at > now() - interval '30 days') as requests_30d,
           (select max(c.created_at) from public.commands c where c.user_id = u.id) as last_request,
           (select coalesce(jsonb_agg(jsonb_build_object(
               'name', m.name, 'platform', m.platform, 'app_version', m.app_version, 'city', m.city,
               'last_seen', m.last_seen, 'connected', m.created_at, 'synced', s.updated_at,
               'kpis', s.data->'kpis', 'pipeline', s.data->'pipeline', 'series', s.data->'series',
               'families', (select coalesce(jsonb_agg(jsonb_build_object('fam', f->>'fam', 'n', f->'n', 'pct', f->'pct')), '[]'::jsonb)
                              from jsonb_array_elements(coalesce(s.data->'families'->'slices', '[]'::jsonb)) f),
               'time_saved_hours', s.data->'time_saved'->'hours',
               'last_run', case when s.data ? 'last_run' and jsonb_typeof(s.data->'last_run') = 'object' then jsonb_build_object(
                   'status', s.data->'last_run'->'status', 'started_at', s.data->'last_run'->'started_at',
                   'finished_at', s.data->'last_run'->'finished_at', 'source', s.data->'last_run'->'source') end,
               'next_run', s.data->'next_run',
               'queued', s.data->'background'->'queued', 'paused_until', s.data->'background'->'paused_until',
               'open_items', s.data->'open_items'
             ) order by m.last_seen desc nulls last), '[]'::jsonb)
             from public.machines m left join public.snapshots s on s.machine_id = m.id
             where m.user_id = u.id) as machines
    from auth.users u left join public.profiles p on p.id = u.id
    where u.email_confirmed_at is not null
      and not exists (select 1 from public.account_emails a where a.email = lower(u.email) and a.owner_id <> u.id)
  ) x), '[]'::jsonb);
end $$;
revoke all on function public.operator_users() from public, anon;
grant execute on function public.operator_users() to authenticated;

-- New sign-ups and newly connected computers since a time, for the operator's own computer to email them
-- (Job Autopilot sends the email through the operator's own Gmail; nothing here sends mail).
create or replace function public.operator_events(since timestamptz) returns jsonb
language plpgsql stable security definer set search_path = public as $$
begin
  if not exists (select 1 from public.operators where user_id = auth.uid()) then
    return null;
  end if;
  return jsonb_build_object(
    'accounts', coalesce((select jsonb_agg(jsonb_build_object(
        'email', u.email, 'name', p.display_name, 'opt_in', coalesce(p.marketing_opt_in, false),
        'at', u.email_confirmed_at) order by u.email_confirmed_at)
      from auth.users u left join public.profiles p on p.id = u.id
      where u.email_confirmed_at > since), '[]'::jsonb),
    'computers', coalesce((select jsonb_agg(jsonb_build_object(
        'email', u.email, 'name', p.display_name, 'computer', m.name, 'at', m.created_at) order by m.created_at)
      from public.machines m join auth.users u on u.id = m.user_id left join public.profiles p on p.id = m.user_id
      where m.created_at > since), '[]'::jsonb),
    'as_of', now());
end $$;
revoke all on function public.operator_events(timestamptz) from public, anon;
grant execute on function public.operator_events(timestamptz) to authenticated;

-- Live updates on the website when the Mac pushes a new snapshot or finishes a command.
do $$
declare t text;
begin
  foreach t in array array['snapshots', 'commands'] loop
    if not exists (select 1 from pg_publication_tables
                   where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = t) then
      execute format('alter publication supabase_realtime add table public.%I', t);
    end if;
  end loop;
end $$;
