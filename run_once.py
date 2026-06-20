"""
run_once.py — Single-shot poll poster for GitHub Actions (cron).

Unlike bot.py (which runs forever), this wakes up, posts any polls that are due
right now, saves which ones it posted, and exits. GitHub Actions runs it on a
schedule (e.g. every 15 minutes). Because polls go out hours before kick-off,
the exact minute doesn't matter — any run inside the lead window posts the poll.

Config comes from environment variables (set as GitHub Actions *secrets*):
  DISCORD_TOKEN   (required)
  CHANNEL_ID      (required)
  LEAD_HOURS, CLOSE_AT_KICKOFF, POLL_HOURS, SKIP_PLACEHOLDERS  (optional)
Set DRY_RUN=1 to print what *would* be posted without connecting to Discord.
"""
from __future__ import annotations

import os
import json
import asyncio
import datetime as dt
from pathlib import Path

import fixtures as fx


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


TOKEN = os.environ.get("DISCORD_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
LEAD_HOURS = float(os.environ.get("LEAD_HOURS", "12"))
CLOSE_AT_KICKOFF = _bool("CLOSE_AT_KICKOFF", True)
POLL_HOURS = float(os.environ.get("POLL_HOURS", "12"))
SKIP_PLACEHOLDERS = _bool("SKIP_PLACEHOLDERS", True)
STATE_FILE = os.environ.get("STATE_FILE", "posted.json")
FIXTURES_FILE = os.environ.get("FIXTURES_FILE")  # unset -> fetch live from openfootball
DRY_RUN = _bool("DRY_RUN", False)

MIN_POLL = dt.timedelta(hours=1)
MAX_POLL = dt.timedelta(days=32)


def load_state() -> set:
    p = Path(STATE_FILE)
    if p.exists():
        try:
            return set(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, ValueError):
            pass
    return set()


def save_state(state: set) -> None:
    Path(STATE_FILE).write_text(json.dumps(sorted(state)), encoding="utf-8")


def classify(m: fx.Match, now: dt.datetime, lead: dt.timedelta) -> str:
    if now >= m.kickoff_utc:
        return "expire"
    if SKIP_PLACEHOLDERS and not m.is_real:
        return "wait"
    if m.kickoff_utc - now <= lead:
        return "post"
    return "wait"


def build_poll(discord, m: fx.Match, now: dt.datetime):
    duration = (m.kickoff_utc - now) if CLOSE_AT_KICKOFF else dt.timedelta(hours=POLL_HOURS)
    duration = max(MIN_POLL, min(duration, MAX_POLL))
    poll = discord.Poll(question=f"{m.team1} vs {m.team2} — who wins?"[:300], duration=duration)
    poll.add_answer(text=m.team1[:55], emoji="\U0001F170\uFE0F")
    poll.add_answer(text="Draw", emoji="\U0001F91D")
    poll.add_answer(text=m.team2[:55], emoji="\U0001F171\uFE0F")
    return poll


def build_context_line(m: fx.Match) -> str:
    bits = ["\U0001F3C6 **" + m.round + "**"]
    if m.group:
        bits.append(m.group)
    if m.ground:
        bits.append("\U0001F4CD " + m.ground)
    ko = m.kickoff_unix
    return " \u00B7 ".join(bits) + "\n\U0001F550 Kick-off: <t:" + str(ko) + ":F> (<t:" + str(ko) + ":R>)\nCast your vote \U0001F447"


def select(matches, posted, now, lead):
    to_post, expired = [], []
    for m in matches:
        if m.seq in posted:
            continue
        action = classify(m, now, lead)
        if action == "expire":
            expired.append(m)
        elif action == "post":
            to_post.append(m)
    return to_post, expired


async def main() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    raw = fx.load_raw(FIXTURES_FILE)
    matches = fx.build_matches(raw)
    posted = load_state()
    lead = dt.timedelta(hours=LEAD_HOURS)

    to_post, expired = select(matches, posted, now, lead)

    changed = False
    for m in expired:
        posted.add(m.seq)
        changed = True

    if DRY_RUN:
        print("[DRY RUN] now (UTC) = " + now.strftime("%Y-%m-%d %H:%M"))
        print("[DRY RUN] would post " + str(len(to_post)) + " poll(s):")
        for m in to_post:
            print("   - " + m.team1 + " vs " + m.team2 + "  (kick-off " + m.kickoff_utc.strftime("%Y-%m-%d %H:%M") + " UTC)")
        print("[DRY RUN] " + str(len(expired)) + " past match(es) marked done.")
        if changed:
            save_state(posted)
        return

    if not to_post:
        if changed:
            save_state(posted)
        print("No polls due. (" + str(len(expired)) + " past match(es) recorded.)")
        return

    if not TOKEN or not CHANNEL_ID:
        raise SystemExit("DISCORD_TOKEN and CHANNEL_ID must be set.")

    import discord
    client = discord.Client(intents=discord.Intents.default())
    counter = {"n": 0}

    @client.event
    async def on_ready():
        try:
            channel = client.get_channel(int(CHANNEL_ID)) or await client.fetch_channel(int(CHANNEL_ID))
            for m in to_post:
                await channel.send(content=build_context_line(m), poll=build_poll(discord, m, now))
                posted.add(m.seq)
                counter["n"] += 1
                print("Posted: " + m.team1 + " vs " + m.team2)
        finally:
            save_state(posted)
            await client.close()

    await client.start(TOKEN)
    print("Done. Posted " + str(counter["n"]) + " poll(s).")


if __name__ == "__main__":
    asyncio.run(main())