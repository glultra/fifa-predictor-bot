"""
fixtures.py — Load and normalize FIFA World Cup 2026 fixtures.

Data source: openfootball/worldcup.json (public domain / CC0, no API key).
https://github.com/openfootball/worldcup.json

Each raw match looks like:
    {"round": "Matchday 1", "date": "2026-06-11", "time": "13:00 UTC-6",
     "team1": "Mexico", "team2": "South Africa", "group": "Group A",
     "ground": "Mexico City", "score": {...}}

Knockout matches use placeholder team names until teams are decided, e.g.
"2A", "1E", "3A/B/C/D/F", "W73", "L101". We detect and (by default) skip those.
"""
from __future__ import annotations

import json
import re
import urllib.request
import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

OPENFOOTBALL_URL = (
    "https://raw.githubusercontent.com/openfootball/"
    "worldcup.json/master/2026/worldcup.json"
)

# A team token is a *placeholder* (not a real nation yet) if it looks like:
#   W73 / L101            -> winner/loser of match N
#   2A / 1E               -> group position
#   3A/B/C/D/F            -> ranked third-place slot
# Real nation names always contain at least one lowercase letter and never
# match these patterns (note: "USA" is all-caps but does NOT match, so it is
# correctly treated as a real team).
PLACEHOLDER_RE = re.compile(r"^([WL]\d+|\d[A-L](/[A-L]+)*)$")

# Kickoff time looks like "13:00 UTC-6" (venue local time + UTC offset).
TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*UTC([+-]\d{1,2})$")


def is_placeholder(team: str) -> bool:
    """True if `team` is a bracket placeholder rather than a real nation."""
    return bool(PLACEHOLDER_RE.match((team or "").strip()))


def parse_kickoff(date_str: str, time_str: str) -> Optional[dt.datetime]:
    """Combine '2026-06-11' + '13:00 UTC-6' into a timezone-aware UTC datetime."""
    m = TIME_RE.match((time_str or "").strip())
    if not m:
        return None
    hh, mm, off = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        year, month, day = (int(x) for x in date_str.split("-"))
    except (ValueError, AttributeError):
        return None
    venue_tz = dt.timezone(dt.timedelta(hours=off))
    local = dt.datetime(year, month, day, hh, mm, tzinfo=venue_tz)
    return local.astimezone(dt.timezone.utc)


@dataclass
class Match:
    seq: int                    # stable index in the source file -> used as state key
    team1: str
    team2: str
    kickoff_utc: dt.datetime    # timezone-aware UTC
    round: str
    group: Optional[str]
    ground: Optional[str]

    @property
    def is_real(self) -> bool:
        """True only when BOTH sides are decided nations (not placeholders)."""
        return not (is_placeholder(self.team1) or is_placeholder(self.team2))

    @property
    def kickoff_unix(self) -> int:
        return int(self.kickoff_utc.timestamp())


def load_raw(path: Optional[str]) -> dict:
    """Read fixtures from a local file if present, else fetch live from openfootball."""
    if path and Path(path).exists():
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return _fetch_json()


def fetch_and_save(path: str) -> dict:
    """Download the latest fixtures and overwrite the local snapshot."""
    raw_text = _fetch_text()
    Path(path).write_text(raw_text, encoding="utf-8")
    return json.loads(raw_text)


def _fetch_text() -> str:
    req = urllib.request.Request(
        OPENFOOTBALL_URL, headers={"User-Agent": "wc2026-poll-bot"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def _fetch_json() -> dict:
    return json.loads(_fetch_text())


def build_matches(raw: dict) -> list[Match]:
    """Turn raw JSON into a sorted list of Match objects (skips unparseable times)."""
    out: list[Match] = []
    for i, mt in enumerate(raw.get("matches", [])):
        kickoff = parse_kickoff(mt.get("date", ""), mt.get("time", ""))
        if kickoff is None:
            continue
        out.append(
            Match(
                seq=i,
                team1=(mt.get("team1") or "").strip(),
                team2=(mt.get("team2") or "").strip(),
                kickoff_utc=kickoff,
                round=(mt.get("round") or "").strip(),
                group=(mt.get("group") or None),
                ground=(mt.get("ground") or None),
            )
        )
    out.sort(key=lambda m: m.kickoff_utc)
    return out
