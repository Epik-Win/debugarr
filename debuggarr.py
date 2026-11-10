#!/usr/bin/env python3
"""DeBuggarr: Discord bug-report bot with auto log/Plex diagnostics collection."""
from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import re
import sqlite3
import sys
import hashlib
import urllib.parse
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import discord
from discord.ext import commands
from api_docs import load_openapi_document, swagger_ui_html

DEFAULT_ENV_FILE = Path("/opt/debuggarr/.env")
DEFAULT_DATA_DIR = Path(os.environ.get("DATA_DIR", "/opt/debuggarr/data"))
BUG_EMOJI_UP = "\U0001F44D"
BUG_EMOJI_DOWN = "\U0001F44E"


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ[key] = value


def telegram_api_call(token: str, method: str, payload: Dict[str, Any]) -> Any:
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=urllib.parse.urlencode(payload).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=35) as response:
            result = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Telegram {method} request failed with HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Telegram {method} request failed: {exc.reason}") from None
    if not result.get("ok"):
        raise RuntimeError(f"Telegram {method} request failed: {result}")
    return result.get("result")


def parse_chat_ids(value: str) -> set[int]:
    chat_ids = set()
    for item in value.split(","):
        item = item.strip()
        if item:
            try:
                chat_ids.add(int(item))
            except ValueError as exc:
                raise ValueError(f"Invalid Telegram chat ID: {item}") from exc
    return chat_ids


def utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def format_mediacheck_review(review_id: str, title: str, media_path: str,
                             evidence: Dict[str, Any]) -> str:
    claimed_title = (
        evidence.get("claimed_movie")
        or evidence.get("claimed_episode")
        or title
    )
    safe_path = media_path[:350].replace("`", "'")
    lines = [
        f"⚠️ **Media identity review** — `{review_id}`",
        f"**Title:** {claimed_title[:150]}",
        f"**File:** `{safe_path}`",
        "",
        "**Why this was flagged**",
    ]

    expected_runtime = evidence.get("expected_runtime_minutes")
    actual_runtime = evidence.get("file_runtime_minutes")
    if expected_runtime is not None and actual_runtime is not None:
        lines.extend([
            f"The file is **{float(actual_runtime):.1f} minutes** long, but "
            f"Radarr lists the movie as **{float(expected_runtime):.0f} minutes**.",
            f"The allowed runtime difference is "
            f"{float(evidence.get('runtime_tolerance_percent', 25)):.0f}%.",
            "This quick duration check triggered the review; audio transcription "
            "and subtitle comparison were not run.",
        ])
    elif evidence.get("match_percent") is not None:
        provider = str(evidence.get("reference_provider") or "external subtitle")
        lines.extend([
            f"The middle audio sample matched **{float(evidence['match_percent']):.1f}%** "
            "of the subtitle's four-word phrases "
            f"(review threshold: {float(evidence.get('threshold_percent', 0)):.1f}%).",
            f"Subtitle reference: **{provider}**.",
        ])
        if evidence.get("audio_sample_minutes") is not None:
            lines.append(
                f"Audio checked: **{float(evidence['audio_sample_minutes']):.0f} minutes** "
                "from the middle of the file."
            )
    else:
        finding = str(evidence.get("finding") or "The identity check needs review.")
        lines.append(finding[:500])

    lines.extend([
        "",
        "**Diagnostic evidence (complete)**",
        "```json",
        json.dumps(evidence, indent=2, ensure_ascii=False),
        "```",
        "",
        "**What should I do?** Reply with one command:",
        f"`!bug replace {review_id}` — quarantine this file and search for a replacement.",
        f"`!bug accept {review_id}` — keep this file and mark the review accepted.",
    ])
    return "\n".join(lines)


def format_integration_review(review_id: str, source: str, title: str,
                              summary: str, details: Dict[str, Any],
                              actions: List[Dict[str, str]]) -> str:
    lines = [
        f"⚠️ **{source[:80]} review** — `{review_id}`",
        f"**Title:** {title[:200]}",
        "",
        "**What needs attention**",
        summary[:1000],
        "",
        "**Diagnostic evidence (complete)**",
        "```json",
        json.dumps(details, indent=2, ensure_ascii=False),
        "```",
        "",
        "**Reply with one command:**",
    ]
    lines.extend(
        f"`!bug {action['command']} {review_id}` — {action['description']}"
        for action in actions
    )
    return "\n".join(lines)


def parse_iso(value: Optional[str]) -> Optional[datetime.datetime]:
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except ValueError:
        return None


# --- Persistence -----------------------------------------------------------

def db_path() -> Path:
    return DEFAULT_DATA_DIR / "debuggarr.db"


def bundles_dir(bug_id: str) -> Path:
    return DEFAULT_DATA_DIR / "bundles" / bug_id


def get_connection() -> sqlite3.Connection:
    DEFAULT_DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path()), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _add_column_if_missing(conn: sqlite3.Connection, column: str, coltype: str) -> None:
    try:
        conn.execute(f"ALTER TABLE bugs ADD COLUMN {column} {coltype}")
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            raise


def init_db() -> None:
    conn = get_connection()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bugs (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                reporter_discord_id TEXT,
                reporter_label TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                container_hint TEXT,
                bundle_path TEXT,
                diagnostics_status TEXT NOT NULL DEFAULT 'pending',
                message_id TEXT,
                channel_id TEXT,
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                upvote_user_ids TEXT NOT NULL DEFAULT '[]',
                downvote_user_ids TEXT NOT NULL DEFAULT '[]'
            )
            """
        )
        # Added later than the original schema - migrate existing DBs in place.
        _add_column_if_missing(conn, "fix_description", "TEXT")
        _add_column_if_missing(conn, "fixed_at", "TEXT")
        _add_column_if_missing(conn, "admin_dm_message_id", "TEXT")
        _add_column_if_missing(conn, "seerr_issue_id", "TEXT")
        _add_column_if_missing(conn, "reopen_reason", "TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dm_reminders (
                message_id TEXT PRIMARY KEY,
                expires_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mediacheck_reviews (
                review_id TEXT PRIMARY KEY,
                media_path TEXT NOT NULL,
                title TEXT NOT NULL,
                evidence TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                decision TEXT,
                decided_by TEXT,
                created_at TEXT NOT NULL,
                decided_at TEXT,
                dm_message_id TEXT,
                result TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS integration_reviews (
                review_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                details TEXT NOT NULL,
                actions TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                decision TEXT,
                decided_by TEXT,
                created_at TEXT NOT NULL,
                decided_at TEXT,
                dm_message_id TEXT,
                result TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS integration_notifications (
                source TEXT NOT NULL,
                notification_id TEXT NOT NULL,
                message_ids TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                PRIMARY KEY (source, notification_id)
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def notification_code(source: str, notification_id: str) -> str:
    """Short code the admin types to dismiss an integration notification."""
    return "N" + hashlib.sha1(f"{source}/{notification_id}".encode()).hexdigest()[:6].upper()


def add_dm_reminder(message_id: str, expires_at: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO dm_reminders (message_id, expires_at) VALUES (?, ?)",
            (message_id, expires_at),
        )
        conn.commit()
    finally:
        conn.close()


def pop_expired_dm_reminders(now_iso: str) -> List[str]:
    conn = get_connection()
    try:
        rows = conn.execute("SELECT message_id FROM dm_reminders WHERE expires_at <= ?", (now_iso,)).fetchall()
        ids = [row[0] for row in rows]
        conn.execute("DELETE FROM dm_reminders WHERE expires_at <= ?", (now_iso,))
        conn.commit()
        return ids
    finally:
        conn.close()


def delete_bug(bug_id: str) -> None:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM bugs WHERE id = ?", (bug_id,))
        conn.commit()
    finally:
        conn.close()


def insert_bug(bug: Dict[str, Any]) -> None:
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO bugs (
                id, source, reporter_discord_id, reporter_label, title, description,
                status, created_at, upvote_user_ids, downvote_user_ids, diagnostics_status
            ) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, '[]', '[]', 'pending')
            """,
            (
                bug["id"], bug["source"], bug.get("reporter_discord_id"),
                bug["reporter_label"], bug["title"], bug["description"], bug["created_at"],
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_bug(bug_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM bugs WHERE id = ?", (bug_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_bug_by_message_id(message_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM bugs WHERE message_id = ?", (message_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_bug_by_seerr_issue_id(seerr_issue_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM bugs WHERE seerr_issue_id = ?", (seerr_issue_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_seerr_issue_status(seerr_url: str, api_key: str, seerr_issue_id: str, status: str) -> bool:
    """Sync a status change back to Seerr (the reverse of the webhook path -
    Seerr resolving an issue already flows into DeBuggarr; this is what makes
    !bug fixed/reopen flow the other way). `status` is Seerr's own "resolved"
    or "open". Best-effort: failures are logged, never raised, since this
    should never block the local status change from taking effect.
    """
    if not api_key or not seerr_issue_id:
        return False
    try:
        import requests
        resp = requests.post(
            f"{seerr_url}/api/v1/issue/{seerr_issue_id}/{status}",
            headers={"X-Api-Key": api_key},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:
        print(f"Failed to update Seerr issue {seerr_issue_id} to '{status}': {exc}")
        return False


def list_open_bugs_ordered() -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM bugs WHERE status = 'open' ORDER BY created_at ASC"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def update_bug_fields(bug_id: str, **fields: Any) -> None:
    if not fields:
        return
    conn = get_connection()
    try:
        columns = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [bug_id]
        conn.execute(f"UPDATE bugs SET {columns} WHERE id = ?", values)
        conn.commit()
    finally:
        conn.close()


def apply_vote(bug_id: str, user_id: str, direction: str) -> None:
    bug = get_bug(bug_id)
    if not bug:
        return
    up = set(json.loads(bug["upvote_user_ids"]))
    down = set(json.loads(bug["downvote_user_ids"]))
    if direction == "up":
        up.add(user_id)
        down.discard(user_id)
    else:
        down.add(user_id)
        up.discard(user_id)
    update_bug_fields(
        bug_id,
        upvote_user_ids=json.dumps(sorted(up)),
        downvote_user_ids=json.dumps(sorted(down)),
    )


def remove_vote(bug_id: str, user_id: str, direction: str) -> None:
    bug = get_bug(bug_id)
    if not bug:
        return
    up = set(json.loads(bug["upvote_user_ids"]))
    down = set(json.loads(bug["downvote_user_ids"]))
    if direction == "up":
        up.discard(user_id)
    else:
        down.discard(user_id)
    update_bug_fields(
        bug_id,
        upvote_user_ids=json.dumps(sorted(up)),
        downvote_user_ids=json.dumps(sorted(down)),
    )


def new_bug_id() -> str:
    for _ in range(5):
        candidate = uuid.uuid4().hex[:8]
        if not get_bug(candidate):
            return candidate
    return uuid.uuid4().hex[:12]


# --- Diagnostics: AI container classification + docker logs ----------------

def classify_containers_with_ai(
    base_url: str,
    model: str,
    known_containers: List[str],
    title: str,
    description: str,
    request_timeout: int = 30,
    retry_interval_seconds: int = 15,
    max_wait_seconds: int = 7200,
) -> List[str]:
    """Ask the local AI which containers are related. If the AI is unreachable
    (down/restarting/cold), this blocks and retries every `retry_interval_seconds`
    until it responds or `max_wait_seconds` elapses, rather than giving up on
    the first failure. Safe to call from a background thread (asyncio.to_thread).
    """
    try:
        import requests
    except Exception:
        print("requests not available; skipping AI container classification")
        return []

    api_key = os.getenv("LOCAL_AI_API_KEY", "").strip()
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    prompt = (
        "You are triaging a bug report for a self-hosted media server. "
        f"Known Docker containers: {', '.join(known_containers)}.\n\n"
        f"Bug title: {title}\n"
        f"Bug description: {description}\n\n"
        "Reply with ONLY a JSON array (no prose, no markdown) containing the 1-3 "
        "container names from the list above that are most likely related to this bug. "
        "If none seem related, reply with an empty array []."
    )

    import time
    deadline = time.time() + max_wait_seconds
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = requests.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 150,
                    "temperature": 0,
                },
                timeout=request_timeout,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            match = re.search(r"\[.*?\]", content, re.DOTALL)
            if not match:
                return []
            names = json.loads(match.group(0))
            if not isinstance(names, list):
                return []
            known_set = set(known_containers)
            return [n for n in names if isinstance(n, str) and n in known_set][:3]
        except requests.exceptions.RequestException as exc:
            if time.time() >= deadline:
                print(f"Local AI still unreachable after {max_wait_seconds}s; giving up (last error: {exc})")
                return []
            print(f"Local AI unreachable (attempt {attempt}: {exc}); retrying in {retry_interval_seconds}s")
            time.sleep(retry_interval_seconds)
        except Exception as exc:
            # Response came back but was malformed - not an availability problem, don't retry.
            print(f"AI container classification failed to parse response: {exc}")
            return []


def capture_container_logs(container_name: str, since_minutes: int, dest_path: Path) -> bool:
    try:
        import docker
    except Exception:
        print("docker SDK not available; skipping log capture")
        return False

    client = None
    try:
        client = docker.DockerClient(base_url="unix://var/run/docker.sock")
        container = client.containers.get(container_name)
        since = datetime.datetime.utcnow() - datetime.timedelta(minutes=since_minutes)
        raw = container.logs(since=since, timestamps=True, tail=5000)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_text(raw.decode("utf-8", errors="replace"))
        return True
    except Exception as exc:
        print(f"Failed to capture logs for container '{container_name}': {exc}")
        return False
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


# --- Diagnostics: Plex metadata (Seerr-reported issues) ---------------------

def capture_plex_data(plex_url: str, plex_token: str, media_title: str, reporter_label: str, dest_path: Path) -> bool:
    if not plex_url or not plex_token or not media_title:
        return False
    try:
        from plexapi.server import PlexServer
        import requests as req
    except Exception:
        print("plexapi not available; skipping Plex data capture")
        return False

    try:
        session = req.Session()
        session.verify = os.getenv("PLEX_CA_BUNDLE") or os.getenv(
            "PLEX_VERIFY_TLS", "true",
        ).lower() not in ("0", "false", "no", "off")
        server = PlexServer(plex_url, plex_token, session=session)

        data: Dict[str, Any] = {"query_title": media_title, "reporter": reporter_label, "matches": []}
        for section in server.library.sections():
            if section.type not in ("movie", "show"):
                continue
            try:
                results = section.search(title=media_title)
            except Exception:
                results = []
            for item in results:
                match: Dict[str, Any] = {
                    "title": item.title,
                    "type": item.type,
                    "ratingKey": item.ratingKey,
                    "media": [],
                }
                for media in getattr(item, "media", []) or []:
                    for part in getattr(media, "parts", []) or []:
                        match["media"].append({
                            "file": getattr(part, "file", None),
                            "container": getattr(media, "container", None),
                            "videoCodec": getattr(media, "videoCodec", None),
                            "audioCodec": getattr(media, "audioCodec", None),
                            "resolution": getattr(media, "videoResolution", None),
                            "bitrate": getattr(media, "bitrate", None),
                            "duration": getattr(media, "duration", None),
                        })
                data["matches"].append(match)

        try:
            data["active_sessions"] = [
                {
                    "title": getattr(s, "title", None),
                    "usernames": getattr(s, "usernames", None),
                    "player_state": getattr(s.players[0], "state", None) if getattr(s, "players", None) else None,
                }
                for s in server.sessions()
            ]
        except Exception:
            data["active_sessions"] = []

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_text(json.dumps(data, indent=2, default=str))
        return True
    except Exception as exc:
        print(f"Failed to capture Plex data for '{media_title}': {exc}")
        return False


# --- Embeds ------------------------------------------------------------------

FIXED_BUG_RETENTION = datetime.timedelta(days=7)

CHANNEL_TOPIC_TEXT = (
    "Bug tracker: /report-bug here, or click ⚠️ Report an Issue on a title in Seerr for "
    "movie/show problems. \U0001F44D/\U0001F44E to vote. Fixed bugs turn green and auto-remove after 7 days."
)
CHANNEL_INTRO_EMBED_TITLE = "\U0001F4CC What is this channel for?"


def build_channel_intro_embed() -> discord.Embed:
    return discord.Embed(
        title=CHANNEL_INTRO_EMBED_TITLE,
        description=(
            "This is where bugs and issues for this server get tracked.\n\n"
            "**Report a bug:**\n"
            "• `/report-bug` right here — for anything server/app related.\n"
            "• For a movie/show playback problem, open the title in Seerr and click the ⚠️ icon "
            "instead — that one routes Plex details straight to the admin.\n\n"
            "**Voting:** every report gets 👍/👎 reactions so others can weigh in.\n\n"
            "**Status colors:** 🔴 red = open, 🟢 green = fixed (auto-removed here after 7 days), "
            "⚪ grey = resolved.\n\n"
            "This message is pinned and self-maintained by the bot — it'll always be here."
        ),
        color=discord.Color.blue(),
    )


def build_bug_embed(bug: Dict[str, Any]) -> discord.Embed:
    up_count = len(json.loads(bug["upvote_user_ids"]))
    down_count = len(json.loads(bug["downvote_user_ids"]))
    status = bug["status"]

    if status == "fixed":
        title = f"✅ FIXED — {bug['title']}"
        color = discord.Color.green()
    elif status == "resolved":
        title = f"✅ RESOLVED — {bug['title']}"
        color = discord.Color.dark_grey()
    else:
        title = f"🐛 {bug['title']}"
        color = discord.Color.red()

    embed = discord.Embed(title=title, description=bug["description"], color=color)

    if status == "fixed" and bug.get("fix_description"):
        embed.add_field(name="Fix", value=bug["fix_description"], inline=False)

    if status == "open" and bug.get("reopen_reason"):
        embed.add_field(name="Reopened — Why", value=bug["reopen_reason"], inline=False)

    embed.add_field(name="Reported by", value=bug["reporter_label"], inline=True)
    source_label = {
        "seerr": "Seerr issue",
        "telegram": "Telegram report",
    }.get(bug["source"], "Discord report")
    embed.add_field(name="Source", value=source_label, inline=True)
    embed.add_field(name="Bug ID", value=f"`{bug['id']}`", inline=True)

    diagnostics_status = bug.get("diagnostics_status") or "pending"
    done_text = "✅ diagnostics collected"
    if bug.get("container_hint"):
        done_text += f" ({bug['container_hint']})"

    diagnostics_text = {
        "pending": "🔍 collecting diagnostics…",
        "done": done_text,
        "no_logs": "⚠️ no related container identified",
        "failed": "⚠️ diagnostics collection failed",
    }.get(diagnostics_status, diagnostics_status)

    footer = f"{BUG_EMOJI_UP} {up_count}   {BUG_EMOJI_DOWN} {down_count}   •   {diagnostics_text}"

    if status == "fixed":
        fixed_at = parse_iso(bug.get("fixed_at"))
        if fixed_at:
            removal_date = (fixed_at + FIXED_BUG_RETENTION).strftime("%Y-%m-%d")
            footer += f"   •   will be removed from this channel around {removal_date}"

    embed.set_footer(text=footer)

    created_at = parse_iso(bug["created_at"])
    if created_at:
        embed.timestamp = created_at
    return embed


def bug_to_api_dict(bug: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(bug)
    try:
        result["upvotes"] = json.loads(bug.get("upvote_user_ids") or "[]")
    except Exception:
        result["upvotes"] = []
    try:
        result["downvotes"] = json.loads(bug.get("downvote_user_ids") or "[]")
    except Exception:
        result["downvotes"] = []
    result.pop("upvote_user_ids", None)
    result.pop("downvote_user_ids", None)
    return result


# --- Discord bot ---------------------------------------------------------

class BugReportModal(discord.ui.Modal, title="Report a Bug"):
    bug_title = discord.ui.TextInput(label="Short summary", max_length=100, placeholder="e.g. Sonarr won't grab new episodes")
    bug_description = discord.ui.TextInput(
        label="Describe the bug",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        placeholder="What happened? What did you expect instead? Steps to reproduce, if any.",
    )

    def __init__(self, client: "DeBuggarrClient") -> None:
        super().__init__()
        self._client = client

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("✅ Your bug report has been posted.", ephemeral=True)
        await self._client.create_and_post_bug(
            interaction=interaction,
            source="slash",
            reporter_discord_id=str(interaction.user.id),
            reporter_label=interaction.user.display_name,
            title=str(self.bug_title.value).strip(),
            description=str(self.bug_description.value).strip(),
        )


class DeBuggarrClient(commands.Bot):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        intents = kwargs.pop("intents", discord.Intents.default())
        super().__init__(command_prefix="!", intents=intents, **kwargs)

        self._main_channel_ref = os.getenv("DISCORD_MAIN_CHANNEL", "general").strip()
        self._admin_user_id = os.getenv("DISCORD_ADMIN_USER_ID", "").strip()
        self._admin_username = os.getenv("DISCORD_ADMIN_USERNAME", "").strip()
        self._telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self._telegram_admin_chat_ids = parse_chat_ids(os.getenv("TELEGRAM_ADMIN_CHAT_IDS", ""))
        configured_chat_ids = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS")
        self._telegram_allowed_chat_ids = parse_chat_ids(
            configured_chat_ids if configured_chat_ids is not None
            else os.getenv("TELEGRAM_ADMIN_CHAT_IDS", "")
        )
        self._telegram_offset = 0

        self._local_ai_base = os.getenv("LOCAL_AI_BASE", "http://llama-litellm:4000/v1").strip()
        self._local_ai_model = os.getenv("LOCAL_AI_MODEL", "Apex").strip()
        self._local_ai_retry_interval = int(os.getenv("LOCAL_AI_RETRY_INTERVAL_SECONDS", "15"))
        self._local_ai_max_wait = int(os.getenv("LOCAL_AI_MAX_WAIT_SECONDS", "7200"))
        self._known_containers = [
            c.strip() for c in os.getenv("KNOWN_CONTAINERS", "").split(",") if c.strip()
        ]
        self._log_window_minutes = int(os.getenv("LOG_CAPTURE_WINDOW_MINUTES", "30"))

        self._plex_url = os.getenv("PLEX_URL", "").strip()
        self._plex_token = os.getenv("PLEX_TOKEN", "").strip()

        self._seerr_webhook_path = os.getenv("SEERR_WEBHOOK_PATH", "/debuggarr/seerr-webhook").strip()
        self._seerr_webhook_port = int(os.getenv("SEERR_WEBHOOK_PORT", "8095"))
        self._seerr_webhook_secret = os.getenv("SEERR_WEBHOOK_SECRET", "").strip()
        self._api_secret = os.getenv("API_SECRET", "").strip()

        self._seerr_url = os.getenv("SEERR_URL", "http://seerr:5055").strip().rstrip("/")
        self._seerr_api_key = os.getenv("SEERR_API_KEY", "").strip()

        self._bottom_check_interval = int(os.getenv("BUG_BOTTOM_CHECK_INTERVAL_SECONDS", "300"))
        self._bottom_grace = datetime.timedelta(hours=24)

    # --- helpers -----------------------------------------------------------

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            return False
        if self._is_admin_author(interaction.user):
            return True
        if interaction.user.id == interaction.guild.owner_id:
            return True
        perms = getattr(interaction.user, "guild_permissions", None)
        return bool(perms and perms.administrator)

    def _is_admin_author(self, author: Any) -> bool:
        if self._admin_user_id and str(author.id) == self._admin_user_id:
            return True
        if self._admin_username and author.name.lower() == self._admin_username.lower():
            return True
        return False

    def _resolve_main_channel(self, guild: Optional[discord.Guild]) -> Optional[discord.TextChannel]:
        if guild is None:
            return None
        ref = self._main_channel_ref
        for ch in guild.text_channels:
            if str(ch.id) == ref or ch.name == ref or ch.name.replace("-", "_") == ref.replace("-", "_"):
                return ch
        return None

    async def _resolve_admin_user(self) -> Optional[discord.User]:
        if not self._admin_user_id:
            print("DeBuggarr: no DISCORD_ADMIN_USER_ID configured; skipping admin DM")
            return None
        try:
            return await self.fetch_user(int(self._admin_user_id))
        except Exception as exc:
            print(f"Could not fetch admin user by id: {exc}")
            return None

    async def _dm_admin(self, text: str) -> Optional[discord.Message]:
        """Send a DM and return its first message for existing callers."""
        messages = await self._dm_admin_all(text)
        return messages[0] if messages else None

    async def _dm_admin_all(self, text: str) -> List[discord.Message]:
        """Send a possibly multi-message DM and return every sent message."""
        await self._telegram_notify_admins(text)
        user = await self._resolve_admin_user()
        if user is None:
            return []

        sent_messages: List[discord.Message] = []
        try:
            dm_channel = user.dm_channel or await user.create_dm()
            for i in range(0, len(text), 2000):
                sent_messages.append(
                    await dm_channel.send(text[i:i + 2000], suppress_embeds=True)
                )
        except Exception as exc:
            print(f"Failed to DM admin: {exc}")
            for sent in sent_messages:
                try:
                    await sent.delete()
                except Exception as cleanup_exc:
                    print(f"Failed to remove partial admin DM after send failure: {cleanup_exc}")
            return []
        return sent_messages

    async def _telegram_send(self, chat_id: int, text: str) -> None:
        if not self._telegram_token:
            return
        for start in range(0, len(text), 4000):
            await asyncio.to_thread(
                telegram_api_call,
                self._telegram_token,
                "sendMessage",
                {"chat_id": chat_id, "text": text[start:start + 4000]},
            )

    async def _telegram_notify_admins(self, text: str) -> None:
        if not self._telegram_token:
            return
        for chat_id in self._telegram_admin_chat_ids:
            try:
                await self._telegram_send(chat_id, text)
            except Exception as exc:
                print(f"Failed to send Telegram admin notification: {exc}")

    async def _telegram_poll_loop(self) -> None:
        if not self._telegram_token:
            return
        await self.wait_until_ready()

        print("Telegram bot polling enabled")
        observed_chat_ids = set()
        while True:
            try:
                updates = await asyncio.to_thread(
                    telegram_api_call,
                    self._telegram_token,
                    "getUpdates",
                    {"offset": self._telegram_offset, "timeout": 25},
                )
                for update in updates or []:
                    self._telegram_offset = max(
                        self._telegram_offset, int(update.get("update_id", 0)) + 1,
                    )
                    message = update.get("message") or update.get("edited_message")
                    if not message or not message.get("text"):
                        continue
                    chat_id = int(message.get("chat", {}).get("id", 0))
                    if chat_id not in self._telegram_allowed_chat_ids:
                        if chat_id not in observed_chat_ids:
                            print(f"Telegram chat ID observed: {chat_id}; authorize it in TELEGRAM_ADMIN_CHAT_IDS or TELEGRAM_ALLOWED_CHAT_IDS")
                            observed_chat_ids.add(chat_id)
                        continue
                    try:
                        await self._handle_telegram_message(message)
                    except Exception as exc:
                        print(f"Telegram command failed: {exc}")
                        await self._telegram_send(chat_id, "The command failed. Check the Debugarr logs.")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"Telegram polling failed: {exc}")
                await asyncio.sleep(10)

    async def _handle_telegram_message(self, message: Dict[str, Any]) -> None:
        chat_id = int(message["chat"]["id"])
        parts = message["text"].strip().split(maxsplit=1)
        if not parts:
            return
        command = parts[0].split("@", 1)[0].lower()
        argument = parts[1].strip() if len(parts) > 1 else ""

        if command in ("/start", "/help"):
            await self._telegram_send(chat_id, (
                "Debugarr commands:\n"
                "/report-bug Title | Description\n"
                "/bugs — list open reports\n"
                "/resolve-bug ID — admin only\n"
                "/fixed-bug ID [description] — admin only\n"
                "/reopen-bug ID Reason — admin only\n"
                "/bug ACTION REVIEW_ID — decide an integration review (admin only)"
            ))
            return

        if command == "/report-bug":
            if chat_id not in self._telegram_allowed_chat_ids:
                return
            if "|" not in argument:
                await self._telegram_send(chat_id, "Usage: /report-bug Title | Description")
                return
            title, description = (part.strip() for part in argument.split("|", 1))
            if not title or not description or len(title) > 100 or len(description) > 1000:
                await self._telegram_send(chat_id, "Add a title (up to 100 characters) and description (up to 1000 characters).")
                return
            guild_id = int(os.getenv("DISCORD_GUILD_ID", "0") or 0)
            guild = self.get_guild(guild_id) if guild_id else None
            channel = self._resolve_main_channel(guild)
            if channel is None:
                await self._telegram_send(chat_id, "Debugarr could not find the configured Discord report channel.")
                return
            bug_id = new_bug_id()
            insert_bug({
                "id": bug_id,
                "source": "telegram",
                "reporter_discord_id": f"telegram:{message.get('from', {}).get('id', chat_id)}",
                "reporter_label": message.get("from", {}).get("username") or f"Telegram user {chat_id}",
                "title": title,
                "description": description,
                "created_at": utcnow_iso(),
            })
            await self._reassert_bug_order(channel)
            asyncio.create_task(self._collect_slash_diagnostics(bug_id))
            asyncio.create_task(self._notify_admin_new_bug(bug_id, guild_id))
            asyncio.create_task(self._reconcile_everything())
            await self._telegram_send(chat_id, f"Report `{bug_id}` was added to the bug board.")
            return

        if command == "/bugs":
            bugs = list_open_bugs_ordered()[:10]
            if not bugs:
                await self._telegram_send(chat_id, "There are no open bug reports.")
                return
            await self._telegram_send(chat_id, "\n".join(
                f"{bug['id']} — {bug['title']}" for bug in bugs
            ))
            return

        if chat_id not in self._telegram_admin_chat_ids:
            await self._telegram_send(chat_id, "This command is limited to configured Telegram administrators.")
            return

        if command == "/resolve-bug":
            bug = get_bug(argument)
            if not bug:
                await self._telegram_send(chat_id, f"No bug found with ID `{argument}`.")
                return
            update_bug_fields(bug["id"], status="resolved", resolved_at=utcnow_iso())
            await self._refresh_embed(bug["id"])
            await self._telegram_send(chat_id, f"Bug `{bug['id']}` marked resolved.")
            return

        if command == "/fixed-bug":
            bug_args = argument.split(maxsplit=1)
            if not bug_args:
                await self._telegram_send(chat_id, "Usage: /fixed-bug ID [fix description]")
                return
            bug = get_bug(bug_args[0])
            if not bug:
                await self._telegram_send(chat_id, f"No bug found with ID `{bug_args[0]}`.")
                return
            fix_description = bug_args[1].strip() if len(bug_args) > 1 else ""
            update_bug_fields(
                bug["id"],
                status="fixed",
                fix_description=fix_description or None,
                fixed_at=utcnow_iso(),
            )
            await self._refresh_embed(bug["id"])
            await self._retire_bug_dm(bug)
            await self._sync_seerr_status(bug, "resolved")
            asyncio.create_task(self._reconcile_everything())
            await self._telegram_send(chat_id, f"Bug `{bug['id']}` marked fixed.")
            return

        if command == "/reopen-bug":
            reopen_args = argument.split(maxsplit=1)
            if len(reopen_args) != 2 or not reopen_args[1].strip():
                await self._telegram_send(chat_id, "Usage: /reopen-bug ID Reason")
                return
            bug = get_bug(reopen_args[0])
            if not bug:
                await self._telegram_send(chat_id, f"No bug found with ID `{reopen_args[0]}`.")
                return
            await self._reopen_bug(bug, reason=reopen_args[1].strip())
            await self._telegram_send(chat_id, f"Bug `{bug['id']}` reopened.")
            return

        if command == "/bug":
            bug_args = argument.split(maxsplit=2)
            if len(bug_args) < 2:
                await self._telegram_send(chat_id, "Usage: /bug ACTION REVIEW_ID")
                return
            await self._telegram_decide_review(chat_id, bug_args[0].lower(), bug_args[1].upper())
            return

        await self._telegram_send(chat_id, "Unknown command. Send /help for available commands.")

    async def _telegram_decide_review(self, chat_id: int, action: str, review_id: str) -> None:
        conn = get_connection()
        try:
            review = conn.execute(
                "SELECT * FROM integration_reviews WHERE review_id = ?", (review_id,),
            ).fetchone()
            if review:
                if review["status"] != "pending":
                    await self._telegram_send(chat_id, f"Review `{review_id}` was already {review['status']}.")
                    return
                allowed_actions = json.loads(review["actions"])
                if action not in {item.get("command") for item in allowed_actions}:
                    choices = ", ".join(item["command"] for item in allowed_actions)
                    await self._telegram_send(chat_id, f"Choose one of: {choices}")
                    return
                cursor = conn.execute(
                    "UPDATE integration_reviews SET status='decided', decision=?, decided_by=?, "
                    "decided_at=? WHERE review_id=? AND status='pending'",
                    (action, f"telegram:{chat_id}", utcnow_iso(), review_id),
                )
                conn.commit()
                if cursor.rowcount:
                    await self._telegram_send(chat_id, f"Recorded `{action}` for review `{review_id}`.")
                else:
                    await self._telegram_send(chat_id, f"Review `{review_id}` was already decided.")
                return
            legacy_review = conn.execute(
                "SELECT * FROM mediacheck_reviews WHERE review_id = ?", (review_id,),
            ).fetchone()
            if legacy_review:
                if legacy_review["status"] != "pending":
                    await self._telegram_send(chat_id, f"Review `{review_id}` was already {legacy_review['status']}.")
                    return
                if action not in ("replace", "accept", "confirm", "deny"):
                    await self._telegram_send(chat_id, "Choose `replace` or `accept` for this review.")
                    return
                decision = "confirmed" if action in ("replace", "confirm") else "denied"
                cursor = conn.execute(
                    "UPDATE mediacheck_reviews SET status=?, decision=?, decided_by=?, "
                    "decided_at=? WHERE review_id=? AND status='pending'",
                    (decision, decision, f"telegram:{chat_id}", utcnow_iso(), review_id),
                )
                conn.commit()
                if cursor.rowcount:
                    await self._telegram_send(chat_id, f"Recorded `{action}` for review `{review_id}`.")
                else:
                    await self._telegram_send(chat_id, f"Review `{review_id}` was already decided.")
                return
        finally:
            conn.close()
        await self._telegram_send(chat_id, f"No pending integration review found with ID `{review_id}`.")

    async def _delete_admin_dm_messages(self, stored_ids: Optional[str]) -> bool:
        if not stored_ids:
            return True
        try:
            decoded = json.loads(stored_ids)
            message_ids = decoded if isinstance(decoded, list) else [decoded]
        except (json.JSONDecodeError, TypeError):
            message_ids = [stored_ids]
        user = await self._resolve_admin_user()
        if user is None:
            return False
        try:
            dm_channel = user.dm_channel or await user.create_dm()
        except Exception as exc:
            print(f"Failed to open admin DM to clean up resolved review: {exc}")
            return False

        success = True
        for message_id in message_ids:
            try:
                message = await dm_channel.fetch_message(int(message_id))
                await message.delete()
            except discord.NotFound:
                continue
            except Exception as exc:
                print(f"Failed to delete resolved review DM {message_id}: {exc}")
                success = False
        return success

    async def _delete_legacy_mediacheck_crash_notice(
        self, source: str, notification_id: str,
    ) -> bool:
        if source != "mediacheck" or not notification_id.startswith("crash-"):
            return True
        command = notification_id.removeprefix("crash-")
        if command not in {"hook", "watch", "scan", "reviews"}:
            return True
        user = await self._resolve_admin_user()
        if user is None:
            return False
        try:
            dm_channel = user.dm_channel or await user.create_dm()
            async for message in dm_channel.history(limit=100):
                if (message.author.id == self.user.id
                        and f"`mediacheck {command}` crashed:" in message.content):
                    await message.delete()
        except Exception as exc:
            print(f"Failed to remove an older mediacheck {command} crash DM: {exc}")
            return False
        return True

    DM_REMINDER_TTL = datetime.timedelta(days=1)
    DM_HELP_HINT = "\n\n💡 Send `!bug help` for the full command list."

    async def _send_temporary(self, destination: Any, text: str) -> Optional[discord.Message]:
        """The admin DM shows only what still needs attention: a bug's DM is
        deleted once it is fixed or removed, integration notifications when
        their service resolves them, and replies to commands (confirmations,
        the `!bug help` reference) are transient - sent through here and
        auto-deleted after DM_REMINDER_TTL by `_cleanup_expired_dm_reminders`.
        """
        try:
            msg = await destination.send(text)
            expires_at = (datetime.datetime.now(datetime.timezone.utc) + self.DM_REMINDER_TTL).isoformat()
            add_dm_reminder(str(msg.id), expires_at)
            return msg
        except Exception as exc:
            print(f"Failed to send temporary message: {exc}")
            return None

    async def _send_temporary_help(self, destination: Any, prefix: str = "") -> Optional[discord.Message]:
        text = f"{prefix}\n\n{self.BUG_COMMAND_USAGE}" if prefix else self.BUG_COMMAND_USAGE
        return await self._send_temporary(destination, text)

    async def _retire_bug_dm(self, bug: Dict[str, Any]) -> None:
        """Delete a bug's admin DM notification once it no longer needs attention."""
        if bug.get("admin_dm_message_id") and await self._delete_admin_dm_messages(bug["admin_dm_message_id"]):
            update_bug_fields(bug["id"], admin_dm_message_id=None)

    async def _cleanup_expired_dm_reminders(self) -> None:
        expired_ids = pop_expired_dm_reminders(utcnow_iso())
        if not expired_ids:
            return
        user = await self._resolve_admin_user()
        if user is None:
            return
        try:
            dm_channel = user.dm_channel or await user.create_dm()
        except Exception as exc:
            print(f"Failed to open admin DM for reminder cleanup: {exc}")
            return
        removed = 0
        for message_id in expired_ids:
            try:
                msg = await dm_channel.fetch_message(int(message_id))
                await msg.delete()
                removed += 1
            except Exception:
                pass
        if removed:
            print(f"DeBuggarr: removed {removed} expired command reminder(s) from admin DM")

    async def _reconcile_everything(self) -> None:
        """Run after every bug-mutating event (created, status changed,
        edited, removed) as well as on the periodic timer: keeps the
        bug-reports channel showing only currently-tracked (open) or
        recently-fixed bugs. The admin DM is kept tidy separately (see
        `_send_temporary`).
        """
        try:
            await self._scan_and_reconcile_channel()
        except Exception as exc:
            print(f"Channel reconciliation failed: {exc}")

    async def _delete_bug_message(self, bug: Dict[str, Any]) -> None:
        if not bug.get("message_id") or not bug.get("channel_id"):
            return
        try:
            channel = self.get_channel(int(bug["channel_id"])) or await self.fetch_channel(int(bug["channel_id"]))
            message = await channel.fetch_message(int(bug["message_id"]))
            await message.delete()
        except Exception as exc:
            print(f"Failed to delete message for bug {bug['id']}: {exc}")

    async def _sync_seerr_status(self, bug: Dict[str, Any], status: str) -> None:
        seerr_issue_id = bug.get("seerr_issue_id")
        if not seerr_issue_id:
            return
        ok = await asyncio.to_thread(
            update_seerr_issue_status, self._seerr_url, self._seerr_api_key, seerr_issue_id, status,
        )
        if ok:
            print(f"DeBuggarr: synced bug {bug['id']} status to Seerr issue {seerr_issue_id} ({status})")

    async def _reopen_bug(self, bug: Dict[str, Any], reason: Optional[str] = None, sync_to_seerr: bool = True) -> None:
        """Clear fixed-state fields, record the reason, and refresh the bug.

        Sync the reopened status to Seerr unless this call handles a Seerr
        webhook that already changed the issue there.
        """
        update_bug_fields(
            bug["id"], status="open", fix_description=None, fixed_at=None,
            resolved_at=None, reopen_reason=reason,
        )
        await self._refresh_embed(bug["id"])
        await self._reassert_bug_order()
        if sync_to_seerr:
            asyncio.create_task(self._sync_seerr_status(bug, "open"))
        asyncio.create_task(self._reconcile_everything())


    # --- admin PM commands -----------------------------------------------------

    BUG_COMMAND_USAGE = (
        "**DeBuggarr admin commands** (send these directly to me in DM):\n"
        "`!bug <choice> <review-id>` — choose one of the actions listed in an integration review DM\n"
        "`!bug replace <review-id>` / `!bug accept <review-id>` — mediacheck: replace or keep a reviewed import\n"
        "`!bug confirm <review-id>` / `!bug deny <review-id>` — backward-compatible mediacheck review choices\n"
        "`!bug remove <id>` — delete a bug entirely (message + record)\n"
        "`!bug fixed <id> [fix description]` — mark fixed (turns green), auto-removed from the bug channel after 7 days\n"
        "`!bug reopen <id> [reason]` — put a fixed/resolved bug back to open\n"
        "`!bug edit <id> <new description>` — replace the bug's description text\n"
        "`!bug dismiss <code>` — remove a service notification (e.g. speedtest, MusicAI) from this DM\n\n"
        "**Slash commands** (in the server, not DM):\n"
        "`/reopen-bug <id> <reason>` — same as `!bug reopen`, but the reason is required and shown on the bug's card\n"
        "`/resolve-bug <id>` — mark a bug resolved (grey banner)"
    )

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.guild is not None:
            return
        if not self._is_admin_author(message.author):
            return

        content = (message.content or "").strip()
        if not content.lower().startswith("!bug"):
            return

        try:
            await self._handle_admin_bug_command(message, content)
        except Exception as exc:
            print(f"Admin bug command failed: {exc}")
            await message.channel.send(f"Something went wrong: {exc}")

    async def _dismiss_notification(self, channel: Any, code: str) -> None:
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT source, notification_id, message_ids FROM integration_notifications WHERE status = 'active'"
            ).fetchall()
        finally:
            conn.close()
        row = next((r for r in rows if notification_code(r["source"], r["notification_id"]) == code), None)
        if row is None:
            await self._send_temporary(channel, f"No active notification with code `{code}`.")
            return
        if not await self._delete_admin_dm_messages(row["message_ids"]):
            await self._send_temporary(channel, f"Couldn't delete notification `{code}`; try again shortly.")
            return
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE integration_notifications SET status = 'resolved', resolved_at = ? "
                "WHERE source = ? AND notification_id = ?",
                (utcnow_iso(), row["source"], row["notification_id"]),
            )
            conn.commit()
        finally:
            conn.close()
        await self._send_temporary(channel, f"🧹 Dismissed {row['source']} notification `{code}`.")

    async def _handle_admin_bug_command(self, message: discord.Message, content: str) -> None:
        parts = content.split(maxsplit=3)
        if len(parts) < 3 or parts[1].lower() == "help":
            await self._send_temporary_help(message.channel)
            return

        action = parts[1].lower()
        bug_id = parts[2].strip()
        rest = parts[3].strip() if len(parts) > 3 else ""

        if action == "dismiss":
            await self._dismiss_notification(message.channel, bug_id.upper())
            return

        conn = get_connection()
        try:
            integration_review = conn.execute(
                "SELECT * FROM integration_reviews WHERE review_id = ?",
                (bug_id.upper(),),
            ).fetchone()
        finally:
            conn.close()
        if integration_review:
            if integration_review["status"] != "pending":
                await self._send_temporary(message.channel,
                    f"Review `{bug_id.upper()}` was already {integration_review['status']}."
                )
                return
            allowed_actions = json.loads(integration_review["actions"])
            selected = next(
                (item for item in allowed_actions if item.get("command") == action),
                None,
            )
            if selected is None:
                choices = ", ".join(
                    f"`!bug {item['command']} {bug_id.upper()}`"
                    for item in allowed_actions
                )
                await self._send_temporary(message.channel,
                    f"That action isn't available for review `{bug_id.upper()}`. Use {choices}."
                )
                return
            conn = get_connection()
            try:
                cursor = conn.execute(
                    """
                    UPDATE integration_reviews
                    SET status = 'decided', decision = ?, decided_by = ?, decided_at = ?
                    WHERE review_id = ? AND status = 'pending'
                    """,
                    (action, str(message.author.id), utcnow_iso(), bug_id.upper()),
                )
                conn.commit()
                if cursor.rowcount == 0:
                    await self._send_temporary(message.channel,
                        f"Review `{bug_id.upper()}` was already decided."
                    )
                    return
            finally:
                conn.close()
            await self._send_temporary(message.channel,
                f"✅ Recorded `{action}` for {integration_review['source']} "
                f"review `{bug_id.upper()}`. The connected service will apply the decision."
            )
            return

        if action in ("confirm", "deny", "replace", "accept"):
            decision = "confirmed" if action in ("confirm", "replace") else "denied"
            conn = get_connection()
            try:
                review = conn.execute(
                    "SELECT * FROM mediacheck_reviews WHERE review_id = ?", (bug_id.upper(),)
                ).fetchone()
                if not review:
                    await self._send_temporary(message.channel, f"No pending mediacheck review found with ID `{bug_id}`.")
                    return
                if review["status"] != "pending":
                    await self._send_temporary(message.channel,
                        f"Review `{bug_id.upper()}` was already {review['status']}."
                    )
                    return
                cursor = conn.execute(
                    """
                    UPDATE mediacheck_reviews
                    SET status = ?, decision = ?, decided_by = ?, decided_at = ?
                    WHERE review_id = ? AND status = 'pending'
                    """,
                    (decision, decision, str(message.author.id), utcnow_iso(), bug_id.upper()),
                )
                if cursor.rowcount == 0:
                    await self._send_temporary(message.channel, f"Review `{bug_id.upper()}` was already decided.")
                    return
                conn.commit()
            finally:
                conn.close()

            if decision == "confirmed":
                response = (
                    f"✅ Replacement approved for review `{bug_id.upper()}`. Mediacheck will quarantine this file, "
                    "blocklist its release when known, and ask the *arr to search for a replacement."
                )
            else:
                response = (
                    f"✅ Review `{bug_id.upper()}` accepted. The file will be kept and marked as "
                    "reviewed so this unchanged import is not flagged again."
                )
            await self._send_temporary(message.channel, response)
            return

        bug = get_bug(bug_id)
        if not bug:
            await self._send_temporary(message.channel, f"No bug found with ID `{bug_id}`.")
            return

        if action == "remove":
            await self._delete_bug_message(bug)
            await self._retire_bug_dm(bug)
            delete_bug(bug_id)
            await self._send_temporary(message.channel, f"🗑️ Removed bug `{bug_id}`.")
            asyncio.create_task(self._reconcile_everything())

        elif action == "fixed":
            update_bug_fields(bug_id, status="fixed", fix_description=rest or None, fixed_at=utcnow_iso())
            await self._refresh_embed(bug_id)
            await self._retire_bug_dm(bug)
            confirmation = f"✅ Marked bug `{bug_id}` as fixed."
            if rest:
                confirmation += f" Fix noted: {rest}"
            confirmation += " It will be removed from the bug channel in 7 days (reporters can re-issue with /report-bug if it recurs)."
            if bug.get("seerr_issue_id"):
                confirmation += " Also marking the Seerr issue resolved."
            await self._send_temporary(message.channel, confirmation)
            asyncio.create_task(self._sync_seerr_status(bug, "resolved"))
            asyncio.create_task(self._reconcile_everything())

        elif action == "reopen":
            await self._reopen_bug(bug, reason=rest or None)
            confirmation = f"🔁 Reopened bug `{bug_id}`."
            if rest:
                confirmation += f" Reason noted: {rest}"
            if bug.get("seerr_issue_id"):
                confirmation += " Also reopening the Seerr issue."
            await self._send_temporary(message.channel, confirmation)

        elif action == "edit":
            if not rest:
                await self._send_temporary(message.channel, "Usage: `!bug edit <id> <new description>`")
                return
            update_bug_fields(bug_id, description=rest)
            await self._refresh_embed(bug_id)
            await self._send_temporary(message.channel, f"✏️ Updated description for bug `{bug_id}`.")

        else:
            await self._send_temporary_help(message.channel, prefix=f"Unknown action `{action}`.")

    # --- fixed-bug auto-cleanup -------------------------------------------------

    async def _fixed_bug_cleanup_loop(self) -> None:
        while True:
            try:
                await self._cleanup_expired_fixed_bugs()
            except Exception as exc:
                print(f"Fixed-bug cleanup failed: {exc}")
            await asyncio.sleep(3600)

    async def _cleanup_expired_fixed_bugs(self) -> None:
        conn = get_connection()
        try:
            rows = [dict(row) for row in conn.execute("SELECT * FROM bugs WHERE status = 'fixed'").fetchall()]
        finally:
            conn.close()

        now = datetime.datetime.now(datetime.timezone.utc)
        for bug in rows:
            await self._retire_bug_dm(bug)
            fixed_at = parse_iso(bug.get("fixed_at"))
            if not fixed_at or now - fixed_at < FIXED_BUG_RETENTION:
                continue
            await self._delete_bug_message(bug)
            update_bug_fields(bug["id"], status="archived", message_id=None)
            print(f"DeBuggarr: archived expired fixed bug {bug['id']} (removed from channel)")

        conn = get_connection()
        try:
            archived = [dict(row) for row in conn.execute(
                "SELECT * FROM bugs WHERE status = 'archived' AND admin_dm_message_id IS NOT NULL"
            ).fetchall()]
        finally:
            conn.close()
        for bug in archived:
            await self._retire_bug_dm(bug)

        if rows:
            asyncio.create_task(self._reconcile_everything())

    def _jump_url(self, guild_id: int, channel_id: str, message_id: str) -> str:
        return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"

    async def _refresh_embed(self, bug_id: str) -> None:
        bug = get_bug(bug_id)
        if not bug or not bug.get("message_id") or not bug.get("channel_id"):
            return
        try:
            channel = self.get_channel(int(bug["channel_id"])) or await self.fetch_channel(int(bug["channel_id"]))
            message = await channel.fetch_message(int(bug["message_id"]))
            await message.edit(embed=build_bug_embed(bug))
        except Exception as exc:
            print(f"Failed to refresh embed for bug {bug_id}: {exc}")

    # --- bug creation --------------------------------------------------------

    async def create_and_post_bug(
        self,
        interaction: discord.Interaction,
        source: str,
        reporter_discord_id: Optional[str],
        reporter_label: str,
        title: str,
        description: str,
    ) -> None:
        channel = self._resolve_main_channel(interaction.guild)
        if channel is None:
            print(f"DeBuggarr: could not resolve main channel '{self._main_channel_ref}'")
            return

        bug_id = new_bug_id()
        insert_bug({
            "id": bug_id,
            "source": source,
            "reporter_discord_id": reporter_discord_id,
            "reporter_label": reporter_label,
            "title": title,
            "description": description,
            "created_at": utcnow_iso(),
        })

        # Repost open bugs in order and create this bug's channel message.
        await self._reassert_bug_order(channel)

        asyncio.create_task(self._collect_slash_diagnostics(bug_id))
        asyncio.create_task(self._notify_admin_new_bug(bug_id, interaction.guild.id if interaction.guild else 0))
        asyncio.create_task(self._reconcile_everything())

    async def post_seerr_bug(self, title: str, description: str, media_title: str, reporter_label: str, guild_id: int, seerr_issue_id: str = "") -> None:
        channel = None
        for guild in self.guilds:
            if guild.id == guild_id:
                channel = self._resolve_main_channel(guild)
                break
        if channel is None:
            print(f"DeBuggarr: could not resolve main channel '{self._main_channel_ref}' for Seerr bug")
            return

        bug_id = new_bug_id()
        insert_bug({
            "id": bug_id,
            "source": "seerr",
            "reporter_discord_id": None,
            "reporter_label": reporter_label,
            "title": title,
            "description": description,
            "created_at": utcnow_iso(),
        })
        if seerr_issue_id:
            update_bug_fields(bug_id, seerr_issue_id=seerr_issue_id)

        # Repost open bugs in order and create this bug's channel message.
        await self._reassert_bug_order(channel)

        asyncio.create_task(self._collect_seerr_diagnostics(bug_id, media_title))
        asyncio.create_task(self._notify_admin_new_bug(bug_id, guild_id))
        asyncio.create_task(self._reconcile_everything())

    async def _collect_slash_diagnostics(self, bug_id: str) -> None:
        bug = get_bug(bug_id)
        if not bug:
            return

        containers = await asyncio.to_thread(
            classify_containers_with_ai,
            self._local_ai_base, self._local_ai_model, self._known_containers,
            bug["title"], bug["description"],
            30, self._local_ai_retry_interval, self._local_ai_max_wait,
        )

        if not containers:
            update_bug_fields(bug_id, diagnostics_status="no_logs")
            await self._refresh_embed(bug_id)
            return

        bundle = bundles_dir(bug_id)
        captured: List[str] = []
        for container in containers:
            dest = bundle / f"{container}.log"
            ok = await asyncio.to_thread(capture_container_logs, container, self._log_window_minutes, dest)
            if ok:
                captured.append(container)

        update_bug_fields(
            bug_id,
            container_hint=", ".join(captured) if captured else None,
            bundle_path=str(bundle) if captured else None,
            diagnostics_status="done" if captured else "failed",
        )
        await self._refresh_embed(bug_id)

    async def _collect_seerr_diagnostics(self, bug_id: str, media_title: str) -> None:
        bug = get_bug(bug_id)
        if not bug:
            return
        bundle = bundles_dir(bug_id)
        dest = bundle / "plex_data.json"
        ok = await asyncio.to_thread(
            capture_plex_data, self._plex_url, self._plex_token, media_title, bug["reporter_label"], dest,
        )
        update_bug_fields(
            bug_id,
            bundle_path=str(bundle) if ok else None,
            diagnostics_status="done" if ok else "failed",
        )
        await self._refresh_embed(bug_id)

    async def _notify_admin_new_bug(self, bug_id: str, guild_id: int) -> None:
        bug = get_bug(bug_id)
        if not bug:
            return

        jump_url = self._jump_url(guild_id, bug["channel_id"], bug["message_id"]) if bug.get("channel_id") else ""
        text = (
            f"🐛 New bug reported ({bug['source']})\n"
            f"ID: `{bug['id']}`\n"
            f"By: {bug['reporter_label']}\n"
            f"Title: {bug['title']}\n"
            f"Description: {bug['description']}\n"
            + (f"Link: {jump_url}\n" if jump_url else "")
            + self.DM_HELP_HINT
        )
        dm_message = await self._dm_admin(text)
        if dm_message is not None:
            update_bug_fields(bug_id, admin_dm_message_id=str(dm_message.id))

    # --- reactions -----------------------------------------------------------

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if self.user and payload.user_id == self.user.id:
            return
        emoji = str(payload.emoji)
        if emoji not in (BUG_EMOJI_UP, BUG_EMOJI_DOWN):
            return
        bug = get_bug_by_message_id(str(payload.message_id))
        if not bug:
            return
        apply_vote(bug["id"], str(payload.user_id), "up" if emoji == BUG_EMOJI_UP else "down")
        await self._refresh_embed(bug["id"])

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        if self.user and payload.user_id == self.user.id:
            return
        emoji = str(payload.emoji)
        if emoji not in (BUG_EMOJI_UP, BUG_EMOJI_DOWN):
            return
        bug = get_bug_by_message_id(str(payload.message_id))
        if not bug:
            return
        remove_vote(bug["id"], str(payload.user_id), "up" if emoji == BUG_EMOJI_UP else "down")
        await self._refresh_embed(bug["id"])

    # --- keep bugs pinned to the bottom of the channel, in order -------------

    async def _repost_all_open_bugs(self, channel: discord.TextChannel, open_bugs: List[Dict[str, Any]]) -> None:
        """Delete every currently-tracked message for these bugs (if any) and
        resend them fresh, back-to-back, in creation order."""
        for bug in open_bugs:
            if bug.get("message_id"):
                try:
                    old = await channel.fetch_message(int(bug["message_id"]))
                    await old.delete()
                except Exception:
                    pass

            new_msg = await channel.send(embed=build_bug_embed(bug))
            try:
                await new_msg.add_reaction(BUG_EMOJI_UP)
                await new_msg.add_reaction(BUG_EMOJI_DOWN)
            except Exception:
                pass
            update_bug_fields(bug["id"], message_id=str(new_msg.id), channel_id=str(channel.id))

        print(f"DeBuggarr: (re)posted {len(open_bugs)} open bug(s) in order in #{channel.name}")

    async def _bottom_check_loop(self) -> None:
        # This task is created (in setup_hook) before the gateway's READY
        # event necessarily fires, so self.guilds can still be empty on the
        # very first tick - wait for it, or the intro/reconcile checks below
        # would silently no-op once and then not retry for a full interval.
        await self.wait_until_ready()
        while True:
            try:
                await self._ensure_channel_intro()
            except Exception as exc:
                print(f"Channel intro check failed: {exc}")
            try:
                await self._reconcile_everything()
            except Exception as exc:
                print(f"Reconciliation pass failed: {exc}")
            try:
                await self._enforce_bottom_position()
            except Exception as exc:
                print(f"Bottom-position check failed: {exc}")
            try:
                await self._cleanup_expired_dm_reminders()
            except Exception as exc:
                print(f"DM reminder cleanup failed: {exc}")
            await asyncio.sleep(self._bottom_check_interval)

    def _extract_bug_id_from_message(self, message: discord.Message) -> Optional[str]:
        if not message.embeds:
            return None
        for field in message.embeds[0].fields:
            if field.name == "Bug ID":
                return str(field.value).strip("`")
        return None

    async def _scan_and_reconcile_channel(self) -> None:
        """Walk the ENTIRE bug channel (not just messages this process already
        knows about) so the bot self-heals from anything that leaves a stray
        message behind: a bug removed/archived by a means that didn't clean up
        its message, a fixed bug past its 7-day retention window, or multiple
        message copies of the same bug id left over from some interrupted
        repost. Anything not currently a legitimate open bug, or a fixed bug
        still within its retention window, gets deleted; if more than one
        message exists for a bug that IS legitimate, only the one the database
        already considers canonical (its recorded message_id) is kept.
        """
        if not self.guilds:
            return
        channel = self._resolve_main_channel(self.guilds[0])
        if channel is None:
            return

        messages_by_bug_id: Dict[str, List[discord.Message]] = {}
        async for msg in channel.history(limit=None):
            if self.user is None or msg.author.id != self.user.id:
                continue
            bug_id = self._extract_bug_id_from_message(msg)
            if bug_id is None:
                continue
            messages_by_bug_id.setdefault(bug_id, []).append(msg)

        removed = 0
        for bug_id, messages in messages_by_bug_id.items():
            bug = get_bug(bug_id)
            should_be_visible = False
            if bug:
                if bug["status"] == "open":
                    should_be_visible = True
                elif bug["status"] == "fixed":
                    fixed_at = parse_iso(bug.get("fixed_at"))
                    if fixed_at and datetime.datetime.now(datetime.timezone.utc) - fixed_at < FIXED_BUG_RETENTION:
                        should_be_visible = True

            keep_id: Optional[str] = None
            if should_be_visible:
                canonical = next((m for m in messages if str(m.id) == str(bug.get("message_id"))), None)
                if canonical:
                    keep_id = str(canonical.id)
                else:
                    newest = max(messages, key=lambda m: m.id)
                    keep_id = str(newest.id)
                    update_bug_fields(bug_id, message_id=keep_id, channel_id=str(channel.id))

            for msg in messages:
                if str(msg.id) == keep_id:
                    continue
                try:
                    await msg.delete()
                    removed += 1
                except Exception as exc:
                    print(f"Failed to remove stray/duplicate bug message {msg.id} (bug {bug_id}): {exc}")

            if not should_be_visible and bug and bug.get("message_id"):
                update_bug_fields(bug_id, message_id=None)

        if removed:
            print(f"DeBuggarr: channel scan removed {removed} stray/expired/duplicate bug message(s) from #{channel.name}")

    async def _ensure_channel_intro(self) -> None:
        """A new member exploring channels for the first time - or returning
        after a long time away - should immediately understand what this
        channel is and how to use it, without having to scroll or ask.
        Discord has no "unremovable message" primitive, so this uses the two
        real mechanisms that get closest: the channel Topic (plain-text
        metadata shown under the channel name on every visit, not a message
        at all, so ordinary message deletion can't touch it - only someone
        with Manage Channels can change it) and a pinned embed with the fuller
        explanation, which self-heals here if it's ever missing (unpinned,
        deleted, or this is the first run).
        """
        if not self.guilds:
            return
        channel = self._resolve_main_channel(self.guilds[0])
        if channel is None:
            return

        try:
            if channel.topic != CHANNEL_TOPIC_TEXT:
                await channel.edit(topic=CHANNEL_TOPIC_TEXT, reason="DeBuggarr channel intro")
        except Exception as exc:
            print(f"Failed to set #{channel.name} topic: {exc}")

        def _is_intro(message: discord.Message) -> bool:
            return (
                self.user is not None
                and message.author.id == self.user.id
                and bool(message.embeds)
                and message.embeds[0].title == CHANNEL_INTRO_EMBED_TITLE
            )

        try:
            pins = [pinned async for pinned in channel.pins()]
        except Exception as exc:
            print(f"Failed to fetch pins in #{channel.name}: {exc}")
            pins = []

        if any(_is_intro(pinned) for pinned in pins):
            return  # already posted and pinned - nothing to do

        # Not currently pinned - either never posted, or it's sitting there
        # unpinned from an earlier attempt where pinning failed (missing
        # permission). Reuse that existing message instead of sending
        # another one every time this check runs.
        intro_message = None
        try:
            async for msg in channel.history(limit=500):
                if _is_intro(msg):
                    intro_message = msg
                    break
        except Exception as exc:
            print(f"Failed to search #{channel.name} history for an existing intro message: {exc}")

        if intro_message is None:
            try:
                intro_message = await channel.send(embed=build_channel_intro_embed())
            except Exception as exc:
                print(f"Failed to post channel intro in #{channel.name}: {exc}")
                return

        try:
            await intro_message.pin(reason="DeBuggarr channel intro")
            print(f"DeBuggarr: pinned the channel intro in #{channel.name}")
        except Exception as exc:
            print(f"Channel intro posted but still can't pin it in #{channel.name} (missing Manage Messages?): {exc}")

    async def _enforce_bottom_position(self) -> None:
        """Periodic check: once something else has sat below the bug block
        for longer than the grace period, bump the whole block back to the
        very bottom of the channel."""
        if not self.guilds:
            return
        channel = self._resolve_main_channel(self.guilds[0])
        if channel is None:
            return

        open_bugs = list_open_bugs_ordered()
        if not open_bugs:
            return

        tracked_ids = {b["message_id"] for b in open_bugs if b.get("message_id")}

        last_message = None
        async for msg in channel.history(limit=1):
            last_message = msg
        if last_message is None:
            return

        if str(last_message.id) in tracked_ids:
            return

        age = discord.utils.utcnow() - last_message.created_at
        if age < self._bottom_grace:
            return

        await self._repost_all_open_bugs(channel, open_bugs)

    async def _reassert_bug_order(self, channel: Optional[discord.TextChannel] = None) -> bool:
        """Every open bug should have exactly one message, contiguous and in
        creation order, wherever that block currently sits in the channel.
        Run at startup and right after a new bug is reported (in addition to
        the periodic bottom-of-channel check above, which handles bumping the
        whole block back down after something else has sat below it for the
        grace period). If the order/contiguity is wrong - including a brand
        new bug that has no message yet - wipe and resend every open bug in
        order. Returns True if a repost happened.
        """
        if channel is None:
            if not self.guilds:
                return False
            channel = self._resolve_main_channel(self.guilds[0])
            if channel is None:
                return False

        open_bugs = list_open_bugs_ordered()
        if not open_bugs:
            return False

        if any(not bug.get("message_id") for bug in open_bugs):
            correctly_ordered = False
        else:
            expected_ids = [str(bug["message_id"]) for bug in open_bugs]
            history_limit = max(100, len(open_bugs) * 5 + 20)
            recent = [msg async for msg in channel.history(limit=history_limit)]
            recent.reverse()  # oldest -> newest
            recent_ids = [str(msg.id) for msg in recent]
            # The bug messages must appear as a contiguous, correctly-ordered
            # run somewhere in that history.
            correctly_ordered = "|".join(expected_ids) in "|".join(recent_ids)

        if correctly_ordered:
            return False

        await self._repost_all_open_bugs(channel, open_bugs)
        return True

    # --- Seerr webhook ---------------------------------------------------------

    async def _start_http_api(self) -> None:
        try:
            from aiohttp import web
        except Exception:
            print("aiohttp not available; HTTP API/Seerr webhook not started")
            return

        async def seerr_handle(request: "web.Request") -> "web.Response":
            print(f"Seerr webhook: received request from {request.remote}")
            if self._seerr_webhook_secret:
                auth = request.headers.get("Authorization") or request.headers.get("X-Webhook-Secret")
                if auth != self._seerr_webhook_secret:
                    print("Seerr webhook: rejected (missing/incorrect Authorization header)")
                    return web.Response(status=403, text="Forbidden")
            try:
                payload = await request.json()
            except Exception as exc:
                print(f"Seerr webhook: invalid JSON body: {exc}")
                return web.Response(status=400, text="Invalid JSON")

            if not isinstance(payload, dict):
                print("Seerr webhook: ignored (payload is not a JSON object)")
                return web.json_response({"status": "ignored"}, status=202)

            notification_type = str(payload.get("notification_type", "")).upper()
            print(f"Seerr webhook: notification_type={notification_type or '<none>'} payload_keys={list(payload.keys())}")
            if notification_type and "ISSUE" not in notification_type:
                print(f"Seerr webhook: ignored (notification_type '{notification_type}' is not an issue event)")
                return web.json_response({"status": "ignored"}, status=202)

            media = payload.get("media") or {}
            issue = payload.get("issue") or {}
            seerr_issue_id = str(issue.get("issue_id") or "").strip()

            # Seerr fires a separate webhook event for every lifecycle change on
            # the SAME issue (created, resolved, reopened, commented, ...), not
            # just when it's first reported. Only ISSUE_CREATED should create a
            # new bug here - anything else is an update to one already posted,
            # matched by Seerr's own issue id, and must never spawn a duplicate.
            if notification_type and notification_type != "ISSUE_CREATED":
                existing = get_bug_by_seerr_issue_id(seerr_issue_id) if seerr_issue_id else None
                if not existing:
                    print(f"Seerr webhook: ignored ({notification_type} for an issue DeBuggarr never posted)")
                    return web.json_response({"status": "ignored"}, status=202)
                if notification_type == "ISSUE_RESOLVED" and existing["status"] == "open":
                    update_bug_fields(existing["id"], status="fixed", fix_description="Marked resolved in Seerr.", fixed_at=utcnow_iso())
                    await self._refresh_embed(existing["id"])
                    await self._retire_bug_dm(existing)
                    print(f"Seerr webhook: bug {existing['id']} marked fixed (resolved in Seerr)")
                    # This branch only runs on the actual open->fixed transition
                    # (guarded above), so a repeat ISSUE_RESOLVED for an
                    # already-fixed bug takes the "else" path below and never
                    # re-sends this - one notification per real status delta.
                    user = await self._resolve_admin_user()
                    if user is not None:
                        await self._send_temporary(
                            user.dm_channel or await user.create_dm(),
                            f"✅ Bug `{existing['id']}` ({existing['title']}) was automatically marked fixed — resolved in Seerr.",
                        )
                    asyncio.create_task(self._reconcile_everything())
                elif notification_type == "ISSUE_REOPENED" and existing["status"] != "open":
                    await self._reopen_bug(existing, reason="Reopened in Seerr.", sync_to_seerr=False)
                    print(f"Seerr webhook: bug {existing['id']} reopened (reopened in Seerr)")
                    # Same delta guard as above, mirrored: only the actual
                    # transition back to open notifies, never a repeat
                    # ISSUE_REOPENED on an already-open bug.
                    reopened_dm = await self._dm_admin(f"🔁 Bug `{existing['id']}` ({existing['title']}) was automatically reopened — reopened in Seerr.{self.DM_HELP_HINT}")
                    if reopened_dm is not None:
                        update_bug_fields(existing["id"], admin_dm_message_id=str(reopened_dm.id))
                else:
                    print(f"Seerr webhook: ignored ({notification_type} on bug {existing['id']}, no action needed)")
                return web.json_response({"status": "updated"})

            if seerr_issue_id and get_bug_by_seerr_issue_id(seerr_issue_id):
                print(f"Seerr webhook: ignored (duplicate ISSUE_CREATED for issue {seerr_issue_id})")
                return web.json_response({"status": "duplicate"}, status=202)

            title = str(payload.get("subject") or media.get("title") or "Seerr issue reported")
            description = str(
                payload.get("message")
                or issue.get("issue_type")
                or "No description provided."
            )
            media_title = str(media.get("title") or payload.get("subject") or "")
            reporter_label = str(
                issue.get("reportedBy_username")
                or issue.get("reportedBy_email")
                or "Seerr user"
            )

            guild_id_env = os.getenv("DISCORD_GUILD_ID", "").strip()
            guild_id = int(guild_id_env) if guild_id_env else (self.guilds[0].id if self.guilds else 0)

            if not guild_id:
                print("Seerr webhook: no guild resolved; dropping event")
                return web.json_response({"status": "no_guild"}, status=202)

            print(f"Seerr webhook: accepted issue '{title}' reported by {reporter_label}")
            asyncio.create_task(self.post_seerr_bug(title, description, media_title, reporter_label, guild_id, seerr_issue_id))
            return web.json_response({"status": "accepted"})

        # --- REST API: everything the "!bug" DM commands can do, plus a
        # one-way channel for a local AI helper to notify the admin, so an
        # external process (no Discord client of its own) can drive this bot.

        @web.middleware
        async def api_auth_middleware(request: "web.Request", handler):
            if request.path.startswith("/api/"):
                if request.path in ("/api/docs", "/api/openapi.json"):
                    return await handler(request)
                if not self._api_secret:
                    return web.json_response({"error": "API_SECRET is not configured on the server"}, status=503)
                auth = request.headers.get("Authorization") or request.headers.get("X-API-Key")
                if auth != self._api_secret:
                    return web.json_response({"error": "forbidden"}, status=403)
            return await handler(request)

        async def api_list_bugs(request: "web.Request") -> "web.Response":
            status_filter = request.query.get("status")
            conn = get_connection()
            try:
                if status_filter:
                    rows = conn.execute("SELECT * FROM bugs WHERE status = ? ORDER BY created_at ASC", (status_filter,)).fetchall()
                else:
                    rows = conn.execute("SELECT * FROM bugs ORDER BY created_at ASC").fetchall()
            finally:
                conn.close()
            return web.json_response({"bugs": [bug_to_api_dict(dict(row)) for row in rows]})

        async def api_docs(request: "web.Request") -> "web.Response":
            return web.Response(text=swagger_ui_html(), content_type="text/html")

        async def api_openapi(request: "web.Request") -> "web.Response":
            try:
                document = load_openapi_document(self._seerr_webhook_path)
            except (OSError, ValueError, KeyError) as exc:
                print(f"Could not load the OpenAPI document: {exc}")
                return web.json_response({"error": "openapi_document_unavailable"}, status=500)
            return web.json_response(document)

        async def api_get_bug(request: "web.Request") -> "web.Response":
            bug = get_bug(request.match_info["bug_id"])
            if not bug:
                return web.json_response({"error": "not_found"}, status=404)
            return web.json_response(bug_to_api_dict(bug))

        async def api_list_bundle(request: "web.Request") -> "web.Response":
            bug_id = request.match_info["bug_id"]
            bug = get_bug(bug_id)
            if not bug:
                return web.json_response({"error": "not_found"}, status=404)
            bundle = bundles_dir(bug_id)
            if not bundle.exists():
                return web.json_response({"files": []})
            files = [{"name": p.name, "size": p.stat().st_size} for p in sorted(bundle.iterdir()) if p.is_file()]
            return web.json_response({"files": files})

        async def api_get_bundle_file(request: "web.Request") -> "web.Response":
            bug_id = request.match_info["bug_id"]
            filename = request.match_info["filename"]
            if "/" in filename or "\\" in filename or ".." in filename:
                return web.json_response({"error": "invalid_filename"}, status=400)
            bug = get_bug(bug_id)
            if not bug:
                return web.json_response({"error": "not_found"}, status=404)
            file_path = bundles_dir(bug_id) / filename
            if not file_path.is_file():
                return web.json_response({"error": "not_found"}, status=404)
            try:
                content = file_path.read_text(errors="replace")
            except Exception as exc:
                return web.json_response({"error": str(exc)}, status=500)
            return web.Response(text=content, content_type="text/plain")

        async def api_mark_fixed(request: "web.Request") -> "web.Response":
            bug_id = request.match_info["bug_id"]
            bug = get_bug(bug_id)
            if not bug:
                return web.json_response({"error": "not_found"}, status=404)
            try:
                payload = await request.json()
            except Exception:
                payload = {}
            description = str((payload or {}).get("description") or "").strip() or None
            update_bug_fields(bug_id, status="fixed", fix_description=description, fixed_at=utcnow_iso())
            await self._refresh_embed(bug_id)
            await self._retire_bug_dm(bug)
            asyncio.create_task(self._sync_seerr_status(bug, "resolved"))
            asyncio.create_task(self._reconcile_everything())
            return web.json_response(bug_to_api_dict(get_bug(bug_id)))

        async def api_reopen(request: "web.Request") -> "web.Response":
            bug_id = request.match_info["bug_id"]
            bug = get_bug(bug_id)
            if not bug:
                return web.json_response({"error": "not_found"}, status=404)
            try:
                payload = await request.json()
            except Exception:
                payload = {}
            reason = str((payload or {}).get("reason") or "").strip() or None
            await self._reopen_bug(bug, reason=reason)
            return web.json_response(bug_to_api_dict(get_bug(bug_id)))

        async def api_edit(request: "web.Request") -> "web.Response":
            bug_id = request.match_info["bug_id"]
            if not get_bug(bug_id):
                return web.json_response({"error": "not_found"}, status=404)
            try:
                payload = await request.json()
            except Exception:
                payload = {}
            description = str((payload or {}).get("description") or "").strip()
            if not description:
                return web.json_response({"error": "description is required"}, status=400)
            update_bug_fields(bug_id, description=description)
            await self._refresh_embed(bug_id)
            return web.json_response(bug_to_api_dict(get_bug(bug_id)))

        async def api_remove(request: "web.Request") -> "web.Response":
            bug_id = request.match_info["bug_id"]
            bug = get_bug(bug_id)
            if not bug:
                return web.json_response({"error": "not_found"}, status=404)
            await self._delete_bug_message(bug)
            await self._retire_bug_dm(bug)
            delete_bug(bug_id)
            asyncio.create_task(self._reconcile_everything())
            return web.json_response({"status": "removed"})

        async def api_notify(request: "web.Request") -> "web.Response":
            try:
                payload = await request.json()
            except Exception:
                return web.json_response({"error": "invalid_json"}, status=400)
            text = str((payload or {}).get("message") or "").strip()
            if not text:
                return web.json_response({"error": "message is required"}, status=400)
            # Optional "source" labels scripts other than the local AI (e.g. mediacheck).
            source = str((payload or {}).get("source") or "").strip()
            notification_id = str((payload or {}).get("notification_id") or "").strip()
            # A transient notification (e.g. "speed is back to normal") deletes
            # itself after DM_REMINDER_TTL; it can still be dismissed sooner.
            transient = bool((payload or {}).get("transient"))
            if notification_id and (
                not source or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", notification_id)
            ):
                return web.json_response(
                    {"error": "notification_id requires a source and a valid identifier"},
                    status=400,
                )
            # Every notification is tracked so the admin can `!bug dismiss` it;
            # callers that never resolve their own get a one-off id.
            explicit_id = bool(notification_id)
            store_source = source or "local-ai"
            if not notification_id:
                notification_id = f"auto-{uuid.uuid4().hex[:12]}"
            if explicit_id:
                conn = get_connection()
                try:
                    row = conn.execute(
                        "SELECT status, message_ids FROM integration_notifications "
                        "WHERE source = ? AND notification_id = ?",
                        (store_source, notification_id),
                    ).fetchone()
                    if (row and row["status"] == "active"
                            and json.loads(row["message_ids"] or "[]")):
                        return web.json_response({
                            "status": "sent", "notification_id": notification_id,
                        })
                finally:
                    conn.close()
            conn = get_connection()
            try:
                conn.execute(
                    """
                    INSERT INTO integration_notifications
                        (source, notification_id, status, created_at)
                    VALUES (?, ?, 'active', ?)
                    ON CONFLICT(source, notification_id) DO UPDATE SET
                        message_ids = '[]', status = 'active',
                        created_at = excluded.created_at, resolved_at = NULL
                    """,
                    (store_source, notification_id, utcnow_iso()),
                )
                conn.commit()
            finally:
                conn.close()
            label = f"🛠️ **{source}:**" if source else "🤖 **Local AI:**"
            dismiss = f"\n\n🧹 `!bug dismiss {notification_code(store_source, notification_id)}` removes this message."
            dm_messages = await self._dm_admin_all(f"{label} {text}{dismiss}")
            if not dm_messages:
                return web.json_response({"error": "failed_to_send"}, status=502)
            message_ids = [str(message.id) for message in dm_messages]
            if transient:
                expires_at = (datetime.datetime.now(datetime.timezone.utc) + self.DM_REMINDER_TTL).isoformat()
                for message_id in message_ids:
                    add_dm_reminder(message_id, expires_at)
            conn = get_connection()
            try:
                conn.execute(
                    """
                    UPDATE integration_notifications SET message_ids = ?
                    WHERE source = ? AND notification_id = ? AND status = 'active'
                    """,
                    (json.dumps(message_ids), store_source, notification_id),
                )
                conn.commit()
            finally:
                conn.close()
            result = {"status": "sent", "message_id": message_ids[0]}
            if explicit_id:
                result["notification_id"] = notification_id
            return web.json_response(result)

        async def api_resolve_notification(request: "web.Request") -> "web.Response":
            source = request.match_info["source"]
            notification_id = request.match_info["notification_id"]
            conn = get_connection()
            try:
                row = conn.execute(
                    "SELECT message_ids FROM integration_notifications "
                    "WHERE source = ? AND notification_id = ?",
                    (source, notification_id),
                ).fetchone()
            finally:
                conn.close()
            if not row:
                if not await self._delete_legacy_mediacheck_crash_notice(
                    source, notification_id,
                ):
                    return web.json_response(
                        {"error": "failed_to_delete_legacy_notification"}, status=502,
                    )
                return web.json_response({
                    "status": "resolved", "notification_found": False,
                })
            if not await self._delete_admin_dm_messages(row["message_ids"]):
                return web.json_response({"error": "failed_to_delete_notification"}, status=502)
            conn = get_connection()
            try:
                conn.execute(
                    """
                    UPDATE integration_notifications
                    SET status = 'resolved', resolved_at = ?
                    WHERE source = ? AND notification_id = ?
                    """,
                    (utcnow_iso(), source, notification_id),
                )
                conn.commit()
            finally:
                conn.close()
            return web.json_response({"status": "resolved"})

        async def api_create_integration_review(request: "web.Request") -> "web.Response":
            try:
                payload = await request.json()
            except Exception:
                return web.json_response({"error": "invalid_json"}, status=400)
            if not isinstance(payload, dict):
                return web.json_response({"error": "expected_json_object"}, status=400)

            source = str(payload.get("source") or "").strip()
            review_id = str(payload.get("review_id") or "").strip().upper()
            title = str(payload.get("title") or "").strip()
            summary = str(payload.get("summary") or "").strip()
            details = payload.get("details")
            actions = payload.get("actions")
            if not re.fullmatch(r"[a-z][a-z0-9_-]{1,39}", source):
                return web.json_response({"error": "invalid_source"}, status=400)
            if not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{2,63}", review_id):
                return web.json_response({"error": "invalid_review_id"}, status=400)
            if not title or len(title) > 300 or not summary or len(summary) > 2000:
                return web.json_response(
                    {"error": "title and summary are required and must be within limits"},
                    status=400,
                )
            if not isinstance(details, dict) or len(json.dumps(details)) > 100_000:
                return web.json_response(
                    {"error": "details must be a JSON object no larger than 100 KB"},
                    status=400,
                )
            if not isinstance(actions, list) or not 2 <= len(actions) <= 5:
                return web.json_response(
                    {"error": "actions must contain between two and five choices"},
                    status=400,
                )
            normalized_actions = []
            for item in actions:
                if not isinstance(item, dict):
                    return web.json_response({"error": "each action must be an object"}, status=400)
                command = str(item.get("command") or "").strip().lower()
                description = str(item.get("description") or "").strip()
                if (not re.fullmatch(r"[a-z][a-z0-9_-]{0,23}", command)
                        or command == "help"
                        or not description or len(description) > 300):
                    return web.json_response({"error": "invalid action command or description"}, status=400)
                normalized_actions.append({"command": command, "description": description})
            commands = [item["command"] for item in normalized_actions]
            if len(set(commands)) != len(commands):
                return web.json_response({"error": "action commands must be unique"}, status=400)

            conn = get_connection()
            try:
                row = conn.execute(
                    "SELECT * FROM integration_reviews WHERE review_id = ?", (review_id,)
                ).fetchone()
                if row:
                    if row["source"] != source:
                        return web.json_response({"error": "review_id_conflict"}, status=409)
                    if row["status"] != "pending" or row["dm_message_id"]:
                        return web.json_response({"status": row["status"], "review_id": review_id})
                else:
                    conn.execute(
                        """
                        INSERT INTO integration_reviews
                            (review_id, source, title, summary, details, actions, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (review_id, source, title, summary, json.dumps(details),
                         json.dumps(normalized_actions), utcnow_iso()),
                    )
                    conn.commit()
            finally:
                conn.close()

            text = format_integration_review(
                review_id, source, title, summary, details, normalized_actions,
            )
            dm_messages = await self._dm_admin_all(text)
            if not dm_messages:
                return web.json_response({"error": "failed_to_send"}, status=502)

            conn = get_connection()
            try:
                conn.execute(
                    "UPDATE integration_reviews SET dm_message_id = ? "
                    "WHERE review_id = ? AND status = 'pending'",
                    (json.dumps([str(message.id) for message in dm_messages]), review_id),
                )
                conn.commit()
            finally:
                conn.close()
            return web.json_response({
                "status": "pending", "review_id": review_id, "source": source,
            })

        async def api_list_integration_reviews(request: "web.Request") -> "web.Response":
            source = request.query.get("source", "").strip()
            status = request.query.get("status", "decided").strip()
            if not re.fullmatch(r"[a-z][a-z0-9_-]{1,39}", source):
                return web.json_response({"error": "valid source is required"}, status=400)
            if status not in ("decided", "completed"):
                return web.json_response({"error": "status must be decided or completed"}, status=400)
            conn = get_connection()
            try:
                rows = conn.execute(
                    """
                    SELECT review_id, source, title, summary, details, actions, status,
                           decision, decided_by, created_at, decided_at, result
                    FROM integration_reviews
                    WHERE source = ? AND status = ?
                    ORDER BY created_at ASC
                    """,
                    (source, status),
                ).fetchall()
            finally:
                conn.close()
            reviews = []
            for row in rows:
                item = dict(row)
                item["details"] = json.loads(item["details"])
                item["actions"] = json.loads(item["actions"])
                reviews.append(item)
            return web.json_response({"reviews": reviews})

        async def api_complete_integration_review(request: "web.Request") -> "web.Response":
            review_id = request.match_info["review_id"].upper()
            try:
                payload = await request.json()
            except Exception:
                return web.json_response({"error": "invalid_json"}, status=400)
            result = str((payload or {}).get("result") or "").strip()
            if not result:
                return web.json_response({"error": "result is required"}, status=400)
            conn = get_connection()
            try:
                row = conn.execute(
                    "SELECT status, dm_message_id FROM integration_reviews WHERE review_id = ?",
                    (review_id,),
                ).fetchone()
            finally:
                conn.close()
            if not row:
                return web.json_response({"error": "not_found"}, status=404)
            if row["status"] not in ("decided", "completed"):
                return web.json_response({"error": "review_not_decided"}, status=409)
            if not await self._delete_admin_dm_messages(row["dm_message_id"]):
                return web.json_response({"error": "failed_to_delete_review_dm"}, status=502)
            if row["status"] == "decided":
                conn = get_connection()
                try:
                    conn.execute(
                        """
                        UPDATE integration_reviews SET status = 'completed', result = ?
                        WHERE review_id = ? AND status = 'decided'
                        """,
                        (result[:1500], review_id),
                    )
                    conn.commit()
                finally:
                    conn.close()
            return web.json_response({"status": "completed"})

        async def api_withdraw_integration_review(request: "web.Request") -> "web.Response":
            """The sender retracts a review the admin hasn't answered yet
            (e.g. it was raised by an outage, not a real finding): delete its
            DM and close it."""
            review_id = request.match_info["review_id"].upper()
            try:
                payload = await request.json()
            except Exception:
                return web.json_response({"error": "invalid_json"}, status=400)
            result = str((payload or {}).get("result") or "").strip()
            if not result:
                return web.json_response({"error": "result is required"}, status=400)
            conn = get_connection()
            try:
                row = conn.execute(
                    "SELECT status, dm_message_id FROM integration_reviews WHERE review_id = ?",
                    (review_id,),
                ).fetchone()
            finally:
                conn.close()
            if not row:
                return web.json_response({"error": "not_found"}, status=404)
            if row["status"] == "completed":
                return web.json_response({"status": "completed"})
            if row["status"] != "pending":
                return web.json_response({"error": "review_already_decided"}, status=409)
            if not await self._delete_admin_dm_messages(row["dm_message_id"]):
                return web.json_response({"error": "failed_to_delete_review_dm"}, status=502)
            conn = get_connection()
            try:
                conn.execute(
                    "UPDATE integration_reviews SET status = 'completed', result = ? "
                    "WHERE review_id = ? AND status = 'pending'",
                    (result[:1500], review_id),
                )
                conn.commit()
            finally:
                conn.close()
            return web.json_response({"status": "completed"})

        async def api_create_mediacheck_review(request: "web.Request") -> "web.Response":
            try:
                payload = await request.json()
            except Exception:
                return web.json_response({"error": "invalid_json"}, status=400)
            if not isinstance(payload, dict):
                return web.json_response({"error": "expected_json_object"}, status=400)

            review_id = str(payload.get("review_id") or "").strip().upper()
            media_path = str(payload.get("media_path") or "").strip()
            title = str(payload.get("title") or "").strip()
            evidence = payload.get("evidence")
            if not re.fullmatch(r"MC-[A-F0-9]{8}", review_id):
                return web.json_response({"error": "invalid_review_id"}, status=400)
            if not media_path or len(media_path) > 1000 or not title or len(title) > 300:
                return web.json_response({"error": "valid media_path and title are required"}, status=400)
            if not isinstance(evidence, dict):
                return web.json_response({"error": "evidence must be a JSON object"}, status=400)

            conn = get_connection()
            try:
                row = conn.execute(
                    "SELECT * FROM mediacheck_reviews WHERE review_id = ?", (review_id,)
                ).fetchone()
                if row and row["status"] != "pending":
                    return web.json_response({"error": "review_already_decided"}, status=409)
                if row and row["dm_message_id"]:
                    return web.json_response({"status": "pending", "review_id": review_id})
                if not row:
                    conn.execute(
                        """
                        INSERT INTO mediacheck_reviews
                            (review_id, media_path, title, evidence, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (review_id, media_path, title, json.dumps(evidence), utcnow_iso()),
                    )
                    conn.commit()
            finally:
                conn.close()

            text = format_mediacheck_review(review_id, title, media_path, evidence)
            dm_messages = await self._dm_admin_all(text)
            if not dm_messages:
                return web.json_response({"error": "failed_to_send"}, status=502)

            conn = get_connection()
            try:
                conn.execute(
                    "UPDATE mediacheck_reviews SET dm_message_id = ? "
                    "WHERE review_id = ? AND status = 'pending'",
                    (json.dumps([str(message.id) for message in dm_messages]), review_id),
                )
                conn.commit()
            finally:
                conn.close()
            return web.json_response({"status": "pending", "review_id": review_id})

        async def api_list_mediacheck_decisions(request: "web.Request") -> "web.Response":
            conn = get_connection()
            try:
                rows = conn.execute(
                    """
                    SELECT review_id, media_path, title, evidence, status, decision,
                           decided_by, created_at, decided_at, result
                    FROM mediacheck_reviews
                    WHERE status IN ('confirmed', 'denied')
                    ORDER BY created_at ASC
                    """
                ).fetchall()
            finally:
                conn.close()
            return web.json_response({
                "reviews": [
                    {**dict(row), "evidence": json.loads(row["evidence"])}
                    for row in rows
                ]
            })

        async def api_complete_mediacheck_review(request: "web.Request") -> "web.Response":
            review_id = request.match_info["review_id"].upper()
            try:
                payload = await request.json()
            except Exception:
                return web.json_response({"error": "invalid_json"}, status=400)
            result = str((payload or {}).get("result") or "").strip()
            if not result:
                return web.json_response({"error": "result is required"}, status=400)
            conn = get_connection()
            try:
                row = conn.execute(
                    "SELECT status, dm_message_id FROM mediacheck_reviews WHERE review_id = ?",
                    (review_id,),
                ).fetchone()
            finally:
                conn.close()
            if not row:
                return web.json_response({"error": "not_found"}, status=404)
            if row["status"] not in ("confirmed", "denied", "completed"):
                return web.json_response({"error": "review_not_decided"}, status=409)
            if not await self._delete_admin_dm_messages(row["dm_message_id"]):
                return web.json_response({"error": "failed_to_delete_review_dm"}, status=502)
            if row["status"] != "completed":
                conn = get_connection()
                try:
                    conn.execute(
                        """
                        UPDATE mediacheck_reviews SET status = 'completed', result = ?
                        WHERE review_id = ? AND status IN ('confirmed', 'denied')
                        """,
                        (result[:1500], review_id),
                    )
                    conn.commit()
                finally:
                    conn.close()
            return web.json_response({"status": "completed"})

        app = web.Application(middlewares=[api_auth_middleware])
        app.router.add_post(self._seerr_webhook_path, seerr_handle)
        app.router.add_get("/api/docs", api_docs)
        app.router.add_get("/api/openapi.json", api_openapi)
        app.router.add_get("/api/bugs", api_list_bugs)
        app.router.add_get("/api/bugs/{bug_id}", api_get_bug)
        app.router.add_get("/api/bugs/{bug_id}/bundle", api_list_bundle)
        app.router.add_get("/api/bugs/{bug_id}/bundle/{filename}", api_get_bundle_file)
        app.router.add_post("/api/bugs/{bug_id}/fixed", api_mark_fixed)
        app.router.add_post("/api/bugs/{bug_id}/reopen", api_reopen)
        app.router.add_post("/api/bugs/{bug_id}/edit", api_edit)
        app.router.add_delete("/api/bugs/{bug_id}", api_remove)
        app.router.add_post("/api/notify", api_notify)
        app.router.add_post(
            "/api/notify/{source}/{notification_id}/resolve", api_resolve_notification,
        )
        app.router.add_post("/api/reviews", api_create_integration_review)
        app.router.add_get("/api/reviews", api_list_integration_reviews)
        app.router.add_post(
            "/api/reviews/{review_id}/complete", api_complete_integration_review,
        )
        app.router.add_post(
            "/api/reviews/{review_id}/withdraw", api_withdraw_integration_review,
        )
        app.router.add_post("/api/mediacheck/reviews", api_create_mediacheck_review)
        app.router.add_get("/api/mediacheck/reviews", api_list_mediacheck_decisions)
        app.router.add_post(
            "/api/mediacheck/reviews/{review_id}/complete", api_complete_mediacheck_review,
        )
        runner = web.AppRunner(app)

        async def _run() -> None:
            try:
                await runner.setup()
                site = web.TCPSite(runner, "0.0.0.0", self._seerr_webhook_port)
                await site.start()
                print(f"HTTP API + Seerr webhook listener started on port {self._seerr_webhook_port} (Seerr: {self._seerr_webhook_path}, API: /api/*)")
            except Exception as exc:
                print(f"Failed to start HTTP API/Seerr webhook listener: {exc}")

        asyncio.create_task(_run())

    # --- slash commands -------------------------------------------------------

    async def setup_hook(self) -> None:
        @self.tree.command(name="report-bug", description="Report a bug for the server/media admin to look into")
        async def report_bug(interaction: discord.Interaction) -> None:
            channel = self._resolve_main_channel(interaction.guild)
            if channel is not None and interaction.channel_id != channel.id:
                await interaction.response.send_message(
                    f"Please use /report-bug in {channel.mention}.", ephemeral=True,
                )
                return
            await interaction.response.send_modal(BugReportModal(self))

        @self.tree.command(name="resolve-bug", description="[Admin] Mark a bug report as resolved")
        @discord.app_commands.describe(bug_id="The short bug ID shown in the bug's embed footer/fields")
        async def resolve_bug(interaction: discord.Interaction, bug_id: str) -> None:
            if not self._is_admin(interaction):
                await interaction.response.send_message("Only the admin can resolve bugs.", ephemeral=True)
                return

            bug = get_bug(bug_id.strip())
            if not bug:
                await interaction.response.send_message(f"No bug found with ID `{bug_id}`.", ephemeral=True)
                return

            update_bug_fields(bug["id"], status="resolved", resolved_at=utcnow_iso())
            await self._refresh_embed(bug["id"])
            await interaction.response.send_message(f"Bug `{bug['id']}` marked resolved.", ephemeral=True)

        @self.tree.command(name="reopen-bug", description="[Admin] Reopen a bug, with a required reason")
        @discord.app_commands.describe(
            bug_id="The short bug ID shown in the bug's embed footer/fields",
            reason="Why this bug is being reopened (shown on the bug's card)",
        )
        async def reopen_bug(interaction: discord.Interaction, bug_id: str, reason: str) -> None:
            if not self._is_admin(interaction):
                await interaction.response.send_message("Only the admin can reopen bugs.", ephemeral=True)
                return

            reason = reason.strip()
            if not reason:
                await interaction.response.send_message("A reason is required to reopen a bug.", ephemeral=True)
                return

            bug = get_bug(bug_id.strip())
            if not bug:
                await interaction.response.send_message(f"No bug found with ID `{bug_id}`.", ephemeral=True)
                return

            await self._reopen_bug(bug, reason=reason)
            confirmation = f"🔁 Bug `{bug['id']}` reopened. Reason: {reason}"
            if bug.get("seerr_issue_id"):
                confirmation += " (also reopening the Seerr issue)"
            await interaction.response.send_message(confirmation, ephemeral=True)

        try:
            guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()
            if guild_id:
                guild = discord.Object(id=int(guild_id))
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                print(f"Synced {len(synced)} slash commands to guild {guild_id}: {[c.name for c in synced]}")
            else:
                synced = await self.tree.sync()
                print(f"Synced {len(synced)} global slash commands")
        except Exception as exc:
            print(f"Slash command sync failed: {exc}")

        asyncio.create_task(self._start_http_api())
        asyncio.create_task(self._bottom_check_loop())
        asyncio.create_task(self._fixed_bug_cleanup_loop())
        asyncio.create_task(self._reassert_bug_order())
        if self._telegram_token:
            asyncio.create_task(self._telegram_poll_loop())


async def run_bot() -> None:
    intents = discord.Intents.default()
    intents.guilds = True

    discord_app_id = os.getenv("DISCORD_CLIENT_ID", "").strip()
    client_kwargs: Dict[str, Any] = {"intents": intents}
    if discord_app_id:
        try:
            client_kwargs["application_id"] = int(discord_app_id)
        except ValueError:
            pass

    client = DeBuggarrClient(**client_kwargs)
    await client.start(os.environ["DISCORD_BOT_TOKEN"])


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DeBuggarr: bug-report Discord bot with auto diagnostics collection")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    return parser


def main() -> None:
    args = build_argument_parser().parse_args()
    load_env_file(args.env_file)
    print(f"Loaded env file: {args.env_file}")
    print(f"DISCORD_BOT_TOKEN present: {bool(os.getenv('DISCORD_BOT_TOKEN'))}")
    print(f"DISCORD_ADMIN_USER_ID present: {bool(os.getenv('DISCORD_ADMIN_USER_ID'))}")

    init_db()

    if not os.getenv("DISCORD_BOT_TOKEN"):
        raise SystemExit("DISCORD_BOT_TOKEN is required (set it in debuggarr/.env)")

    asyncio.run(run_bot())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"DeBuggarr failed: {exc}", file=sys.stderr)
        raise
