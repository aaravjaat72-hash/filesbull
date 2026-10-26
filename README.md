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
- Vercel webhook endpoint: `/api/webhook`.

The bot must only be used for files you own or are authorized to distribute. Telegram protected content may prevent both copying and linking.

## Setup

1. Create a Supabase project and run `schema.sql` in its SQL Editor.
2. Create a bot through BotFather. Do not put the token in the repository.
3. Find your Telegram numeric user ID and put it in `ADMIN_USER_IDS`.
4. Deploy this repository to Vercel.
5. Add these Vercel Environment Variables for Production:

   - `BOT_TOKEN`
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_ROLE_KEY`
   - `ADMIN_USER_IDS`

6. After deployment, set the Telegram webhook once in a browser or shell:

   `https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=https://<your-vercel-domain>/api/webhook`

7. Add the bot as an administrator to each channel, then send the bot:

   `/addchannel @yourchannel`

8. Forward authorized files into a configured indexing channel, or send files directly to the bot from an admin account.

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
