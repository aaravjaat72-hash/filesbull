-- filesbull: subject category column (optional; the bot works without it using a fallback). Run once in Supabase SQL Editor.
alter table public.books add column if not exists category text;
create index if not exists books_category_idx on public.books (category);
