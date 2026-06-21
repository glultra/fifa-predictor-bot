"""
run_once.py — World Cup 2026 poll + prediction-game bot (GitHub Actions cron).

Each run it does two jobs:
  1. POST polls for matches kicking off within the lead window (Team1/Draw/Team2).
  2. SCORE finished matches: read who voted for the winning option, give each
     correct voter +1 point, then post an updated leaderboard.

Voting uses Discord native polls (everyone sees the live tally). Scoring reads
each poll's voters via the API after the result is known. State (which polls
were posted, which were scored, and everyone's points) lives in posted.json,
which the workflow commits back to the repo so it survives between runs.

Set DRY_RUN=1 to preview the plan without connecting to Discord.
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
POINTS_FOR_WINNER = int(os.environ.get("POINTS_FOR_WINNER", "1"))
LEADERBOARD_SIZE = int(os.environ.get("LEADERBOARD_SIZE", "20"))

MIN_POLL = dt.timedelta(hours=1)
MAX_POLL = dt.timedelta(days=32)
# Discord polls last >= 1 hour, so only post if the match is at least this far
# away (guarantees the poll closes by kick-off). Closer matches are skipped.
MIN_LEAD = dt.timedelta(minutes=float(os.environ.get("MIN_LEAD_MINUTES", "60")))

TEAM1_ICON = "\U0001F534"   # red circle
DRAW_ICON = "\U0001F91D"    # handshake
TEAM2_ICON = "\U0001F535"   # blue circle


# --- Country flags (used in the message line, which renders everywhere) ---- #
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


# --- State ----------------------------------------------------------------- #
def load_state() -> dict:
    p = Path(STATE_FILE)
    data = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            data = {}
    if isinstance(data, list):  # migrate old format (just a list of seqs)
        data = {"polls": {}, "scored": list(data), "skip": list(data)}
    data.setdefault("polls", {})     # seq(str) -> poll message_id(int)
    data.setdefault("scored", [])    # seqs already scored
    data.setdefault("skip", [])      # seqs handled without a poll (expired/late)
    data.setdefault("points", {})    # user_id(str) -> points(int)
    data.setdefault("names", {})     # user_id(str) -> display name
    return data


def save_state(data: dict) -> None:
    data["scored"] = sorted(set(data["scored"]))
    data["skip"] = sorted(set(data["skip"]))
    Path(STATE_FILE).write_text(json.dumps(data, ensure_ascii=False, indent=0), encoding="utf-8")


# --- Match result helpers (pure, tested in selftest) ----------------------- #
def winner_from_ft(ft) -> str | None:
    """[2,0] -> 'team1', [1,1] -> 'draw', [0,3] -> 'team2', missing -> None."""
    if not isinstance(ft, (list, tuple)) or len(ft) != 2:
        return None
    a, b = ft[0], ft[1]
    if a > b:
        return "team1"
    if b > a:
        return "team2"
    return "draw"


def result_map(raw: dict) -> dict:
    """seq -> 'team1'/'team2'/'draw'/None, keyed by original index (matches Match.seq)."""
    out = {}
    for i, mt in enumerate(raw.get("matches", [])):
        sc = mt.get("score") or {}
        out[i] = winner_from_ft(sc.get("ft")) if isinstance(sc, dict) else None
    return out


def correct_text(m: fx.Match, winner: str) -> str | None:
    """The poll answer text that is correct for a given winner."""
    if winner == "team1":
        return m.team1[:55]
    if winner == "team2":
        return m.team2[:55]
    if winner == "draw":
        return "Draw"
    return None


def render_leaderboard(points: dict, names: dict, limit: int = 20) -> str:
    if not points:
        return "\U0001F3C6 **PREDICTION LEADERBOARD**\n\nNo predictions scored yet."
    ranked = sorted(points.items(), key=lambda kv: (-kv[1], names.get(kv[0], "").lower()))
    medals = {0: "\U0001F947", 1: "\U0001F948", 2: "\U0001F949"}
    lines = ["\U0001F3C6 **PREDICTION LEADERBOARD** \U0001F3C6", ""]
    for idx, (uid, pts) in enumerate(ranked[:limit]):
        rank = medals.get(idx, f"**{idx + 1}.**")
        name = names.get(uid, f"User {uid}")
        unit = "pt" if pts == 1 else "pts"
        lines.append(f"{rank} {name} — **{pts}** {unit}")
    return "\n".join(lines)


# --- Posting decision ------------------------------------------------------ #
def classify(m: fx.Match, now: dt.datetime, lead: dt.timedelta) -> str:
    remaining = m.kickoff_utc - now
    if remaining < MIN_LEAD:
        return "expire"  # kicked off, or too close to close by kick-off -> skip
    if SKIP_PLACEHOLDERS and not m.is_real:
        return "wait"
    if remaining <= lead:
        return "post"
    return "wait"


def build_poll(discord, m: fx.Match, now: dt.datetime):
    duration = (m.kickoff_utc - now) if CLOSE_AT_KICKOFF else dt.timedelta(hours=POLL_HOURS)
    duration = max(MIN_POLL, min(duration, MAX_POLL))
    poll = discord.Poll(question="Who wins? \U0001F3C6", duration=duration)
    poll.add_answer(text=m.team1[:55], emoji=TEAM1_ICON)
    poll.add_answer(text="Draw", emoji=DRAW_ICON)
    poll.add_answer(text=m.team2[:55], emoji=TEAM2_ICON)
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


def plan(matches, state, now, lead, results):
    handled = set(int(s) for s in state["polls"]) | set(state["skip"])
    to_post, to_skip = [], []
    for m in matches:
        if m.seq in handled:
            continue
        action = classify(m, now, lead)
        if action == "expire":
            to_skip.append(m)
        elif action == "post":
            to_post.append(m)
    scored = set(state["scored"])
    to_score = []
    for s_str in state["polls"]:
        seq = int(s_str)
        if seq in scored:
            continue
        if results.get(seq) is not None:
            to_score.append(seq)
    return to_post, to_skip, to_score


async def main() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    raw = fx.load_raw(FIXTURES_FILE)
    matches = fx.build_matches(raw)
    by_seq = {m.seq: m for m in matches}
    results = result_map(raw)
    state = load_state()
    lead = dt.timedelta(hours=LEAD_HOURS)

    to_post, to_skip, to_score = plan(matches, state, now, lead, results)
    for m in to_skip:
        state["skip"].append(m.seq)

    if DRY_RUN:
        print("[DRY RUN] now (UTC) = " + now.strftime("%Y-%m-%d %H:%M"))
        print("[DRY RUN] would POST " + str(len(to_post)) + " poll(s):")
        for m in to_post:
            print("   - " + m.team1 + " vs " + m.team2)
        print("[DRY RUN] would SCORE " + str(len(to_score)) + " finished match(es):")
        for seq in to_score:
            m = by_seq.get(seq)
            if m:
                print("   - " + m.team1 + " vs " + m.team2 + " -> winner: " + str(results.get(seq)))
        print("[DRY RUN] " + str(len(to_skip)) + " match(es) skipped (past/too late).")
        save_state(state)
        return

    if not to_post and not to_score:
        save_state(state)
        print("Nothing to post or score. (" + str(len(to_skip)) + " skipped.)")
        return

    if not TOKEN or not CHANNEL_ID:
        raise SystemExit("DISCORD_TOKEN and CHANNEL_ID must be set.")

    import discord
    client = discord.Client(intents=discord.Intents.default())
    tally = {"posted": 0, "scored": 0}

    @client.event
    async def on_ready():
        try:
            channel = client.get_channel(int(CHANNEL_ID)) or await client.fetch_channel(int(CHANNEL_ID))

            # 1) Post new polls
            for m in to_post:
                msg = await channel.send(content=build_context_line(m), poll=build_poll(discord, m, now))
                state["polls"][str(m.seq)] = msg.id
                tally["posted"] += 1
                print("Posted: " + m.team1 + " vs " + m.team2)

            # 2) Score finished matches
            newly = []
            for seq in to_score:
                m = by_seq.get(seq)
                winner = results.get(seq)
                if m is None or winner is None:
                    state["scored"].append(seq)
                    continue
                want = correct_text(m, winner)
                try:
                    msg = await channel.fetch_message(state["polls"][str(seq)])
                except discord.DiscordException as e:
                    print("Could not fetch poll for " + m.team1 + " vs " + m.team2 + ": " + str(e))
                    state["scored"].append(seq)
                    continue
                poll = getattr(msg, "poll", None)
                if poll is not None:
                    ans = next((a for a in poll.answers if a.text == want), None)
                    if ans is not None:
                        async for user in ans.voters():
                            if getattr(user, "bot", False):
                                continue
                            uid = str(user.id)
                            state["points"][uid] = state["points"].get(uid, 0) + POINTS_FOR_WINNER
                            state["names"][uid] = getattr(user, "display_name", None) or user.name
                state["scored"].append(seq)
                newly.append(m)
                tally["scored"] += 1
                print("Scored: " + m.team1 + " vs " + m.team2 + " (winner " + winner + ")")

            # 3) Post leaderboard if anything was scored
            if newly:
                summary = "\U0001F4E3 **Results are in!** " + ", ".join(
                    f"{m.team1} vs {m.team2}" for m in newly)
                board = render_leaderboard(state["points"], state["names"], LEADERBOARD_SIZE)
                await channel.send(summary + "\n\n" + board)
        finally:
            save_state(state)
            await client.close()

    await client.start(TOKEN)
    print(f"Done. Posted {tally['posted']} poll(s), scored {tally['scored']} match(es).")


if __name__ == "__main__":
    asyncio.run(main())
