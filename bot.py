"""
bot.py — FIFA World Cup 2026 auto-poll bot.

Posts a native Discord poll (Team 1 / Draw / Team 2) into a channel a set number
of hours before each match kicks off. Everyone sees the live vote tally; the poll
closes automatically at kick-off. Fully hands-off once running.

Setup:  copy .env.example -> .env, fill in the values, then `python bot.py`.
"""
from __future__ import annotations

import os
import json
import logging
import datetime as dt
from pathlib import Path

import discord
from discord.ext import tasks
from dotenv import load_dotenv

import fixtures as fx

# --------------------------------------------------------------------------- #
# Configuration (from environment / .env)
# --------------------------------------------------------------------------- #
load_dotenv()


def _get_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise SystemExit(f"Missing required environment variable: {name}")
    return val


TOKEN = _require("DISCORD_TOKEN")
CHANNEL_ID = int(_require("CHANNEL_ID"))

LEAD_HOURS = float(os.environ.get("LEAD_HOURS", "12"))          # post this long before kick-off
CLOSE_AT_KICKOFF = _get_bool("CLOSE_AT_KICKOFF", True)          # poll closes at kick-off
POLL_HOURS = float(os.environ.get("POLL_HOURS", "12"))         # used only if CLOSE_AT_KICKOFF=false
SKIP_PLACEHOLDERS = _get_bool("SKIP_PLACEHOLDERS", True)        # wait until knockout teams are known
FIXTURES_FILE = os.environ.get("FIXTURES_FILE", "fixtures_2026.json")
STATE_FILE = os.environ.get("STATE_FILE", "posted.json")
CHECK_SECONDS = int(os.environ.get("CHECK_SECONDS", "60"))     # how often to check the schedule
REFRESH_HOURS = float(os.environ.get("REFRESH_HOURS", "6"))    # re-download fixtures this often (0=never)

DISCORD_MIN_POLL = dt.timedelta(hours=1)        # Discord minimum poll duration
DISCORD_MAX_POLL = dt.timedelta(days=32)        # Discord maximum poll duration

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("wcbot")

# --------------------------------------------------------------------------- #
# State (which matches we've already posted) — survives restarts
# --------------------------------------------------------------------------- #
def load_state() -> set[int]:
    p = Path(STATE_FILE)
    if p.exists():
        try:
            return set(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, ValueError):
            log.warning("State file %s was unreadable; starting fresh.", STATE_FILE)
    return set()


def save_state(state: set[int]) -> None:
    Path(STATE_FILE).write_text(json.dumps(sorted(state)), encoding="utf-8")


posted: set[int] = load_state()
matches: list[fx.Match] = []
_last_refresh: dt.datetime | None = None

intents = discord.Intents.default()  # no privileged intents required
client = discord.Client(intents=intents)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def reload_matches() -> None:
    global matches
    raw = fx.load_raw(FIXTURES_FILE)
    matches = fx.build_matches(raw)
    real = sum(m.is_real for m in matches)
    log.info("Loaded %d fixtures (%d with confirmed teams).", len(matches), real)


def _flag(cc: str) -> str:
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in cc.upper())


def _subdiv(tag: str) -> str:
    return "\U0001F3F4" + "".join(chr(0xE0000 + ord(c)) for c in tag) + "\U000E007F"


_ISO = {
    "Algeria": "DZ", "Argentina": "AR", "Australia": "AU", "Austria": "AT", "Belgium": "BE",
    "Bosnia & Herzegovina": "BA", "Brazil": "BR", "Canada": "CA", "Cape Verde": "CV",
    "Colombia": "CO", "Croatia": "HR", "Curaçao": "CW", "Czech Republic": "CZ", "DR Congo": "CD",
    "Ecuador": "EC", "Egypt": "EG", "France": "FR", "Germany": "DE", "Ghana": "GH", "Haiti": "HT",
    "Iran": "IR", "Iraq": "IQ", "Ivory Coast": "CI", "Japan": "JP", "Jordan": "JO", "Mexico": "MX",
    "Morocco": "MA", "Netherlands": "NL", "New Zealand": "NZ", "Norway": "NO", "Panama": "PA",
    "Paraguay": "PY", "Portugal": "PT", "Qatar": "QA", "Saudi Arabia": "SA", "Senegal": "SN",
    "South Africa": "ZA", "South Korea": "KR", "Spain": "ES", "Sweden": "SE", "Switzerland": "CH",
    "Tunisia": "TN", "Turkey": "TR", "USA": "US", "Uruguay": "UY", "Uzbekistan": "UZ",
}
FLAGS = {name: _flag(cc) for name, cc in _ISO.items()}
FLAGS["England"] = _subdiv("gbeng")
FLAGS["Scotland"] = _subdiv("gbsct")


def label(team: str) -> str:
    return (FLAGS.get(team, "\u26BD") + " " + team)[:55]


def build_poll(m: fx.Match, now: dt.datetime) -> discord.Poll:
    if CLOSE_AT_KICKOFF:
        duration = m.kickoff_utc - now
    else:
        duration = dt.timedelta(hours=POLL_HOURS)
    # Clamp to Discord's allowed range.
    duration = max(DISCORD_MIN_POLL, min(duration, DISCORD_MAX_POLL))

    question = f"{m.team1} vs {m.team2} — who wins?"[:300]
    poll = discord.Poll(question=question, duration=duration)
    poll.add_answer(text=label(m.team1))
    poll.add_answer(text="\U0001F91D Draw")
    poll.add_answer(text=label(m.team2))
    return poll


def build_context_line(m: fx.Match) -> str:
    f1 = FLAGS.get(m.team1, "")
    f2 = FLAGS.get(m.team2, "")
    matchup = f"{f1} {m.team1}  vs  {f2} {m.team2}".strip()
    bits = [f"🏆 **{m.round}**"]
    if m.group:
        bits.append(m.group)
    if m.ground:
        bits.append(f"📍 {m.ground}")
    header = " · ".join(bits)
    ko = m.kickoff_unix
    # <t:UNIX:F> renders in each viewer's own timezone; :R is a "in 5 hours" countdown.
    return f"**{matchup}**\n{header}\n🕐 Kick-off: <t:{ko}:F> (<t:{ko}:R>)\nCast your vote 👇"


def classify(m: fx.Match, now: dt.datetime, lead: dt.timedelta) -> str:
    """Decide what to do with a match right now (pure function, easy to test).

    Returns one of:
      "expire" -> kick-off has passed; record it and never poll
      "post"   -> inside the lead window with confirmed teams; post the poll
      "wait"   -> not yet (too early, or teams still placeholders)
    """
    if now >= m.kickoff_utc:
        return "expire"
    if SKIP_PLACEHOLDERS and not m.is_real:
        return "wait"
    if m.kickoff_utc - now <= lead:
        return "post"
    return "wait"


async def get_channel() -> discord.abc.Messageable | None:
    ch = client.get_channel(CHANNEL_ID)
    if ch is None:
        try:
            ch = await client.fetch_channel(CHANNEL_ID)
        except discord.DiscordException as e:
            log.error("Cannot access channel %s: %s", CHANNEL_ID, e)
            return None
    return ch


async def maybe_refresh(now: dt.datetime) -> None:
    """Re-download fixtures periodically so knockout teams fill in automatically."""
    global _last_refresh
    due = _last_refresh is None or (now - _last_refresh) >= dt.timedelta(hours=REFRESH_HOURS)
    if not due:
        return
    try:
        if REFRESH_HOURS > 0 and FIXTURES_FILE:
            fx.fetch_and_save(FIXTURES_FILE)
        reload_matches()
    except Exception as e:  # network hiccup etc. — keep the previously loaded data
        log.warning("Fixture refresh failed (using cached data): %s", e)
    finally:
        _last_refresh = now


# --------------------------------------------------------------------------- #
# Scheduler
# --------------------------------------------------------------------------- #
@tasks.loop(seconds=CHECK_SECONDS)
async def scheduler() -> None:
    now = dt.datetime.now(dt.timezone.utc)

    if REFRESH_HOURS > 0:
        await maybe_refresh(now)

    channel = await get_channel()
    if channel is None:
        return

    lead = dt.timedelta(hours=LEAD_HOURS)
    for m in matches:
        if m.seq in posted:
            continue

        action = classify(m, now, lead)

        if action == "expire":
            posted.add(m.seq)
            save_state(posted)
            continue
        if action == "wait":
            continue

        # action == "post"
        try:
            poll = build_poll(m, now)
            await channel.send(content=build_context_line(m), poll=poll)
            posted.add(m.seq)
            save_state(posted)
            log.info(
                "Posted poll: %s vs %s  (kick-off %s UTC)",
                m.team1, m.team2, m.kickoff_utc.strftime("%Y-%m-%d %H:%M"),
            )
        except discord.DiscordException as e:
            # Don't mark as posted -> will retry on the next loop.
            log.error("Failed to post %s vs %s: %s", m.team1, m.team2, e)


@scheduler.before_loop
async def _before() -> None:
    await client.wait_until_ready()


@client.event
async def on_ready() -> None:
    log.info("Logged in as %s (id: %s)", client.user, client.user.id)
    try:
        reload_matches()
    except Exception as e:
        log.error("Could not load fixtures on startup: %s", e)
    global _last_refresh
    _last_refresh = dt.datetime.now(dt.timezone.utc)
    if not scheduler.is_running():
        scheduler.start()
    log.info(
        "Scheduler running. Posting %.0fh before kick-off; checking every %ds.",
        LEAD_HOURS, CHECK_SECONDS,
    )


if __name__ == "__main__":
    client.run(TOKEN, log_handler=None)