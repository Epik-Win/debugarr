# Debugarr

Debugarr is a Discord bot for collecting bug reports, organizing them in a
voteable issue board, and gathering optional diagnostics from Plex, Seerr, and
Docker.

## What it does

- Accepts reports through Discord slash commands and an optional Seerr webhook.
- Tracks reports, status, votes, and diagnostic bundles in SQLite.
- Sends private administrator notifications and supports resolving or reopening
  reports.
- Provides an authenticated HTTP API for integrations such as Mediacheckarr.
- Can collect recent logs from an explicit `KNOWN_CONTAINERS` allow-list.

Plex, Seerr, local AI summaries, Docker log collection, and HTTP integrations
are optional. Configure only the integrations you intend to use.

## Requirements

- Python 3.12
- A Discord application and bot added to your server with the
  `applications.commands` and `bot` OAuth scopes
- The bot's access to the channels and features you enable

Install the Python dependencies:

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install discord.py plexapi aiohttp docker requests
```

## Configure and run

Create a private dotenv-style file, for example `.env`, with at least:

```dotenv
DISCORD_BOT_TOKEN=replace-with-your-bot-token
DISCORD_GUILD_ID=replace-with-your-server-id
DISCORD_ADMIN_USER_ID=replace-with-your-discord-user-id
DISCORD_MAIN_CHANNEL=general

# Optional Telegram admin bot
TELEGRAM_BOT_TOKEN=replace-with-a-dedicated-telegram-bot-token
TELEGRAM_ADMIN_CHAT_IDS=replace-with-your-telegram-chat-id
# Optional: add more report-only users/chats; defaults to Telegram admins
TELEGRAM_ALLOWED_CHAT_IDS=replace-with-authorized-chat-ids
```

Start the bot from the repository directory:

```sh
python debuggarr.py --env-file .env
```

The default environment file is `/opt/debuggarr/.env`; pass `--env-file` when
using another location. SQLite data and diagnostic bundles are stored under
`/opt/debuggarr/data` by default. Persist that directory across restarts. To
choose a different data directory, set `DATA_DIR` in the process environment
before starting Debugarr.

In Discord, use `/report-bug` to submit a report. Server administrators (and
the configured administrator ID) can use `/resolve-bug` and `/reopen-bug`.

When Telegram is configured, Debugarr also sends admin notifications there.
Use `/start` with the bot to see commands: `/report-bug Title | Description`,
`/bugs`, `/resolve-bug ID`, `/fixed-bug ID [description]`,
`/reopen-bug ID Reason`, and `/bug ACTION REVIEW_ID`. Telegram admins can
resolve or mark issues fixed, reopen issues, and decide Mediacheck integration
reviews.
`TELEGRAM_ADMIN_CHAT_IDS` is the admin allow-list;
`TELEGRAM_ALLOWED_CHAT_IDS` adds authorized users who may report/list bugs but
not perform admin actions. When a token is configured, the first message from
an unauthorized chat logs its numeric ID so it can be added to the allow-list.
Restart Debugarr after changing the list. This bot uses Telegram long polling;
remove any webhook configured for the same bot.

Use separate Telegram bot tokens for Debugarr and Onboardarr (and Checkrr if
its notifier is enabled), because these integrations poll Telegram updates.
Keep the token and chat IDs private.

## Optional integrations

| Integration | Settings |
| --- | --- |
| Seerr webhook | `SEERR_WEBHOOK_SECRET`, optionally `SEERR_WEBHOOK_PORT` and `SEERR_WEBHOOK_PATH` |
| Debuggarr HTTP API | `API_SECRET` |
| Plex diagnostics | `PLEX_URL`, `PLEX_TOKEN`; optionally `PLEX_CA_BUNDLE` |
| Seerr diagnostics | `SEERR_URL`, `SEERR_API_KEY` |
| Local AI summaries | `LOCAL_AI_BASE`, `LOCAL_AI_MODEL`, optionally `LOCAL_AI_API_KEY` |
| Docker log collection | `KNOWN_CONTAINERS`, optionally `LOG_CAPTURE_WINDOW_MINUTES` |

`KNOWN_CONTAINERS` is a comma-separated allow-list. Access to the Docker socket
can grant effective control over the host, so mount it only if that trust
boundary is acceptable and keep the allow-list narrow. Do not expose the HTTP
API or webhook outside a trusted network without appropriate network controls.

Keep tokens, API keys, webhook secrets, and collected diagnostics out of
version control.

## HTTP API

Open the interactive Swagger UI at `/api/docs`; the OpenAPI 3 document is
available at `/api/openapi.json`. The complete GitHub-readable reference,
including authentication and request examples, is in [docs/API.md](docs/API.md).
Run the documentation helper tests with `python -m unittest discover -s tests`.

## Docker image

Each push to `main` publishes a multi-architecture image to GitHub Container
Registry. Pull and run the published image with a mounted environment file and
persistent data directory:

```sh
docker pull ghcr.io/epik-win/debugarr:latest
docker run -d --name debuggarr \
  --restart unless-stopped \
  -e DATA_DIR=/data \
  -v "$PWD/.env:/run/secrets/debuggarr.env:ro" \
  -v debuggarr-data:/data \
  ghcr.io/epik-win/debugarr:latest
```

The image is also tagged with the source commit SHA for deployments that need
an immutable version. Keep the environment file and data volume outside the
container.

## License

MIT. See [LICENSE](LICENSE).
