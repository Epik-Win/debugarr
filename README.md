# DeBuggarr

DeBuggarr is a Discord bot for collecting bug reports, organizing them in a
voteable issue board, and gathering optional diagnostics from Plex, Seerr, and
Docker.

## Features

- Accept reports through Discord commands and an optional Seerr webhook.
- Track status, votes, and diagnostic bundles in a local SQLite database.
- Send private administrator notifications and support administrative actions.
- Expose a small authenticated HTTP API for integrations.
- Optionally collect logs from an explicit `KNOWN_CONTAINERS` allow-list.

## Run

Requires Python 3.12 and these packages:

```sh
python -m pip install discord.py plexapi aiohttp docker requests
python debuggarr.py --help
```

The bot reads a dotenv-style file (default: `/opt/debuggarr/.env`) and stores
its database and diagnostic bundles under `DATA_DIR` (default:
`/opt/debuggarr/data`). Set `DISCORD_BOT_TOKEN` before starting the bot.
Configure the Discord channel/admin IDs and any integration credentials in the
environment file; never commit that file or collected diagnostics.

Optional integrations use `SEERR_WEBHOOK_SECRET`, `API_SECRET`,
`LOCAL_AI_API_KEY`, `PLEX_TOKEN`, and `SEERR_API_KEY`. Plex TLS verification is
enabled by default; use `PLEX_CA_BUNDLE` for a private certificate authority.
Disabling verification with `PLEX_VERIFY_TLS=false` is discouraged.

Mounting the Docker socket grants the bot access to the Docker daemon and can
provide effective host-level control. Only enable log collection when that
trust boundary is acceptable, and keep `KNOWN_CONTAINERS` limited to the
containers it may inspect.

## License

MIT. See [LICENSE](LICENSE).
