-- filesbull: user tracking table (run once in Supabase -> SQL Editor -> New query -> Run)
-- Safe to run more than once. Until it is run, the bot keeps the same data in bot_settings ("user:<id>").
create table if not exists public.bot_users (
  user_id bigint primary key,
  first_name text,
  last_name text,
  username text,
  first_seen timestamptz not null default now(),
  last_seen timestamptz not null default now(),
  verified boolean not null default false,
  searches integer not null default 0,
  files_received integer not null default 0
);
create index if not exists bot_users_last_seen_idx on public.bot_users (last_seen desc);
create index if not exists bot_users_first_seen_idx on public.bot_users (first_seen desc);
alter table public.bot_users enable row level security;

-- delivery log: who got which file, for which search
alter table public.delivery_logs add column if not exists username text;
alter table public.delivery_logs add column if not exists query text;
create index if not exists delivery_logs_user_idx on public.delivery_logs (user_id, created_at desc);
create index if not exists delivery_logs_created_idx on public.delivery_logs (created_at desc);

-- backup: find files not yet copied to the backup channel quickly
create index if not exists books_backup_msg_idx on public.books (backup_message_id);
