# Telegram Book Search Bot

A GitHub/Vercel-ready Telegram bot for authorized book/file catalogs. It indexes documents posted in configured Telegram channels, supports keyword search, sends the original message when Telegram permits copying, and falls back to a source-message link when direct delivery fails.

## Current MVP

- Multiple active source channels in Supabase.
- Automatic indexing of new channel posts containing documents, videos, or audio.
- Admin uploads in the bot chat are indexed as `Admin uploads`.
- Hindi/English/mixed-language detection from caption and filename.
- Search by title, caption, filename, source channel, and keywords.
- Direct `copyMessage` delivery with link fallback.
- Admin commands: `/addchannel @channelusername`, `/channels`, `/stats`.
- Owner-controlled sub-admins: `/addadmin`, `/removeadmin`.
- Required join gate: `/requirechannel @channelusername` and `/requiredchannels`.
- Optional AI ranking with `OPENAI_API_KEY`; AI only ranks indexed database records.
- Delivery logs in `delivery_logs` for admin reporting.
- Private backup fallback with `/setbackupchannel CHAT_ID`.
- Private audit log chat with `/setlogchat CHAT_ID`.
- Reports with `/report` and metadata correction with `/rename BOOK_ID New title`.
- Vercel webhook endpoint: `/api/webhook`.

The bot must only be used for files you own or are authorized to distribute. Telegram protected content may prevent both copying and linking.

## Setup

1. Create a Supabase project and run `schema.sql` in its SQL Editor.
   If the original tables already exist, also run `alter table public.books add column if not exists content_text text;` and create the new tables from the latest schema.
2. Create a bot through BotFather. Do not put the token in the repository.
3. Find your Telegram numeric user ID and put it in `ADMIN_USER_IDS`.
4. Deploy this repository to Vercel.
5. Add these Vercel Environment Variables for Production:

   - `BOT_TOKEN`
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_ROLE_KEY`
   - `ADMIN_USER_IDS`
   - Optional: `OPENAI_API_KEY`, `OPENAI_MODEL=gpt-5-mini`

6. After deployment, set the Telegram webhook once in a browser or shell:

   `https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=https://<your-vercel-domain>/api/webhook`

7. Add the bot as an administrator to each channel, then send the bot:

   `/addchannel @yourchannel`

8. Forward authorized files into a configured indexing channel, or send files directly to the bot from an admin account.

## Advanced setup

- Add a mandatory user channel with `/requirechannel @channelusername`; the bot must be able to check membership there.
- Add a sub-admin with `/addadmin TELEGRAM_NUMERIC_ID`. Only IDs in `ADMIN_USER_IDS` (the owner list) can add or remove sub-admins.
- The AI key is optional. Without it, deterministic keyword/content search still works. With it, the AI ranks only database results and cannot invent a file or source.
- Backup channels and audit-log groups should be added as a later controlled feature; never place their IDs or secrets in public code.

## Recommended captions

Use a caption to improve search quality, for example:

`SSC English | Objective English | grammar | vocabulary`

If the filename is poor, the MVP still indexes the caption and filename. Full PDF text extraction/OCR and AI-based classification should be added as a separate worker in the next phase because large scanned PDFs should not be processed synchronously inside a Vercel webhook request.

## Local test

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# export the variables from .env in your shell
python app.py
```

For production, keep `SUPABASE_SERVICE_ROLE_KEY` server-side only. Never commit `.env` or a real bot token.
