-- Optional: email the site operator when someone creates an account or connects a new computer.
-- Run after schema.sql. Uses Resend (free tier) through pg_net; the API key lives in Supabase Vault,
-- never in this file or in the repo. See supabase/README.md, step 6.
--
-- Before running, store two secrets once (SQL editor):
--   select vault.create_secret('re_your_resend_key', 'resend_api_key');
--   select vault.create_secret('you@example.com',    'operator_email');

create extension if not exists pg_net with schema extensions;

create or replace function public.notify_operator(subject text, body text) returns void
language plpgsql security definer set search_path = '' as $$
declare
  api_key text;
  to_addr text;
begin
  select decrypted_secret into api_key from vault.decrypted_secrets where name = 'resend_api_key';
  select decrypted_secret into to_addr from vault.decrypted_secrets where name = 'operator_email';
  if api_key is null or to_addr is null then
    return;
  end if;
  perform net.http_post(
    url     := 'https://api.resend.com/emails',
    headers := jsonb_build_object('Authorization', 'Bearer ' || api_key, 'Content-Type', 'application/json'),
    body    := jsonb_build_object('from', 'Job Autopilot <onboarding@resend.dev>', 'to', to_addr,
                                  'subject', subject, 'text', body)
  );
exception when others then
  null;  -- a notification must never block a sign-up or a connection
end $$;
revoke all on function public.notify_operator(text, text) from public, anon, authenticated;

create or replace function public.notify_new_profile() returns trigger
language plpgsql security definer set search_path = '' as $$
declare
  addr text;
begin
  select email into addr from auth.users where id = new.id;
  perform public.notify_operator(
    'New Job Autopilot account: ' || coalesce(nullif(new.display_name, ''), addr),
    'Name: ' || coalesce(new.display_name, '-') || E'\nEmail: ' || coalesce(addr, '-')
      || E'\nOpted in to updates: ' || case when new.marketing_opt_in then 'yes' else 'no' end);
  return new;
end $$;

create or replace function public.notify_new_machine() returns trigger
language plpgsql security definer set search_path = '' as $$
declare
  addr text;
  who text;
begin
  select u.email, p.display_name into addr, who
    from auth.users u left join public.profiles p on p.id = u.id where u.id = new.user_id;
  perform public.notify_operator(
    'Job Autopilot set up on a new computer: ' || coalesce(nullif(who, ''), addr),
    'Name: ' || coalesce(who, '-') || E'\nEmail: ' || coalesce(addr, '-')
      || E'\nComputer: ' || coalesce(new.name, '-'));
  return new;
end $$;

drop trigger if exists notify_new_profile on public.profiles;
create trigger notify_new_profile after insert on public.profiles
  for each row execute function public.notify_new_profile();

drop trigger if exists notify_new_machine on public.machines;
create trigger notify_new_machine after insert on public.machines
  for each row execute function public.notify_new_machine();
