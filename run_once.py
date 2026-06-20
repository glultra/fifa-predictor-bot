"""
run_once.py — Single-shot poll poster for GitHub Actions (cron).

Wakes up, posts any polls due right now (Team 1 / Draw / Team 2 with country
flags), saves which ones it posted, and exits. Scheduled by
.github/workflows/poll.yml. Set DRY_RUN=1 to preview without connecting.
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


# --- Country flags --------------------------------------------------------- #
def _flag(cc: str) -> str:
    """2-letter ISO code -> regional-indicator flag emoji (e.g. 'DE' -> German flag)."""
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in cc.upper())


def _subdiv(tag: str) -> str:
    """Subdivision flag (e.g. 'gbeng' -> England) via tag sequence."""
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
    """'Germany' -> 'flag Germany' (falls back to a soccer ball if unmapped)."""
    return (FLAGS.get(team, "\u26BD") + " " + team)[:55]


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
    poll = discord.Poll(question="Who wins? \U0001F3C6", duration=duration)
    poll.add_answer(text=m.team1[:55], emoji="\U0001F534")   # red circle
    poll.add_answer(text="Draw", emoji="\U0001F91D")          # handshake
    poll.add_answer(text=m.team2[:55], emoji="\U0001F535")   # blue circle
    return poll


def build_context_line(m: fx.Match) -> str:
    f1 = FLAGS.get(m.team1, "")
    f2 = FLAGS.get(m.team2, "")
    matchup = (f1 + " " + m.team1 + "  vs  " + f2 + " " + m.team2).strip()
    bits = ["\U0001F3C6 **" + m.round + "**"]
    if m.group:
        bits.append(m.group)
    if m.ground:
        bits.append("\U0001F4CD " + m.ground)
    ko = m.kickoff_unix
    return ("**" + matchup + "**\n" + " \u00B7 ".join(bits)
            + "\n\U0001F550 Kick-off: <t:" + str(ko) + ":F> (<t:" + str(ko) + ":R>)")


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
            print("   - " + label(m.team1) + " vs " + label(m.team2)
                  + "  (kick-off " + m.kickoff_utc.strftime("%Y-%m-%d %H:%M") + " UTC)")
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