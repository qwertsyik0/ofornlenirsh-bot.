# @ofornlenirsh_bot

Telegram bot for styling already-written posts. It does not rewrite wording or add phrases. It only applies Telegram entities, decorative Unicode, and custom/premium emoji from packs added by the user.

## Run on Render

Build: `pip install -r requirements.txt`

Start: `uvicorn app:app --host 0.0.0.0 --port $PORT`

Required environment variable: `BOT_TOKEN`.

Optional: `BOT_USERNAME`, `WEBHOOK_URL`, `WEBHOOK_SECRET`, `DB_PATH`, `LOG_LEVEL`.

Never commit the bot token to GitHub.