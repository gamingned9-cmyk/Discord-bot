# Repeat-Message Discord Bot

Sends a message to a channel a set number of times, spaced out by a set
interval — you control the count and the interval with a command.

## 1. Create the bot on Discord

1. Go to https://discord.com/developers/applications and click **New Application**.
2. Give it a name, then open the **Bot** tab and click **Add Bot**.
3. Under **Privileged Gateway Intents**, turn ON **Message Content Intent**.
4. Click **Reset Token** / **Copy** to get your bot token — keep this secret.
5. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`
   - Bot Permissions: `Send Messages`, `Read Message History`
   - Copy the generated URL, open it in your browser, and invite the bot to your server.

## 2. Set up the project locally

You'll need Python 3.10+.

```bash
cd discord-bot
python -m venv venv
source venv/bin/activate      # on Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and paste in your bot token:

```bash
cp .env.example .env
```

Edit `.env`:
```
DISCORD_TOKEN=your_actual_token_here
DISCORD_PREFIX=!
```

## 3. Run it

```bash
python bot.py
```

You should see `Logged in as YourBotName` in the terminal.

## 4. Use it

In any channel the bot can see, type:

```
!repeat 10 60 Don't forget to vote!
```

This sends "Don't forget to vote!" 10 times, once every 60 seconds.

To stop a run early:

```
!stop
```

## 5. Hosting it so it stays online 24/7

Running `python bot.py` on your own machine only works while that machine
is on. Once you've confirmed it works locally, options to keep it running
all the time include:

- **Railway** (https://railway.app) — connect your GitHub repo, add
  `DISCORD_TOKEN` as an environment variable, deploy. Free tier available.
- **Replit** (https://replit.com) — good for quick testing, less reliable
  for long-term always-on bots without a paid plan.
- **A small VPS** (DigitalOcean, Linode, etc.) — run `python bot.py`
  inside a `screen`/`tmux` session or as a `systemd` service.

Whichever you choose, never commit your `.env` file or paste your token
anywhere public — treat it like a password.
