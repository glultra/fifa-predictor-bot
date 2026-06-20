"""
selftest.py — Validate the bot's logic against the real fixtures, no Discord needed.

Run:  python selftest.py
It checks time parsing, placeholder detection, poll construction, and the
scheduler's posting decision by simulating different "current times".
"""
import os
import datetime as dt

# Provide dummy creds so bot.py imports without a real token.
os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("CHANNEL_ID", "123456789")
os.environ.setdefault("FIXTURES_FILE", "fixtures_2026.json")

import fixtures as fx
import bot

PASS, FAIL = "✅", "❌"
errors = 0


def check(label, cond):
    global errors
    print(f"  {PASS if cond else FAIL} {label}")
    if not cond:
        errors += 1


print("\n[1] Time parsing (venue local + UTC offset -> UTC)")
# 13:00 at UTC-6 == 19:00 UTC
ko = fx.parse_kickoff("2026-06-11", "13:00 UTC-6")
check("13:00 UTC-6 -> 19:00 UTC", ko == dt.datetime(2026, 6, 11, 19, 0, tzinfo=dt.timezone.utc))
# 20:00 at UTC-4 == 00:00 next day UTC
ko2 = fx.parse_kickoff("2026-07-19", "20:00 UTC-4")
check("20:00 UTC-4 rolls to next UTC day", ko2 == dt.datetime(2026, 7, 20, 0, 0, tzinfo=dt.timezone.utc))
check("garbage time -> None", fx.parse_kickoff("2026-06-11", "not a time") is None)

print("\n[2] Placeholder vs real-team detection")
for token in ["W73", "L101", "2A", "1E", "3A/B/C/D/F", "W100"]:
    check(f"{token!r} is placeholder", fx.is_placeholder(token))
for nation in ["Mexico", "South Korea", "Czech Republic", "Bosnia & Herzegovina", "USA"]:
    check(f"{nation!r} is real", not fx.is_placeholder(nation))

print("\n[3] Load + normalize the real fixture file")
raw = fx.load_raw("fixtures_2026.json")
ms = fx.build_matches(raw)
check("104 matches parsed", len(ms) == 104)
check("sorted by kick-off", all(ms[i].kickoff_utc <= ms[i + 1].kickoff_utc for i in range(len(ms) - 1)))
check("all kick-offs are timezone-aware UTC", all(m.kickoff_utc.tzinfo == dt.timezone.utc for m in ms))
real = [m for m in ms if m.is_real]
check("group-stage teams detected as real (>=48 matches)", len(real) >= 48)

print("\n[4] Poll construction")
sample = real[0]
now = sample.kickoff_utc - dt.timedelta(hours=10)
poll = bot.build_poll(sample, now)
ans = [a.text for a in poll.answers]
check("question mentions both teams", sample.team1 in poll.question and sample.team2 in poll.question)
check("3 answers: flagged Team1 / Draw / flagged Team2",
      len(ans) == 3
      and sample.team1 in ans[0] and ans[0] != sample.team1   # flag prefix present
      and ans[1].endswith("Draw")
      and sample.team2 in ans[2] and ans[2] != sample.team2)
check("every team has a flag mapped", all(t in bot.FLAGS for t in [sample.team1, sample.team2]))
check("poll closes at kick-off (~10h)", abs(poll.duration - dt.timedelta(hours=10)) < dt.timedelta(minutes=1))
# Duration clamping
near = sample.kickoff_utc - dt.timedelta(minutes=20)
check("duration clamped to >=1h when posting late", bot.build_poll(sample, near).duration >= dt.timedelta(hours=1))

print("\n[5] Scheduler decision (classify) at three simulated times")
lead = dt.timedelta(hours=bot.LEAD_HOURS)
m = real[0]
before_window = m.kickoff_utc - dt.timedelta(hours=bot.LEAD_HOURS + 5)
inside_window = m.kickoff_utc - dt.timedelta(hours=1)
after_kickoff = m.kickoff_utc + dt.timedelta(minutes=5)
check("too early -> wait", bot.classify(m, before_window, lead) == "wait")
check("inside lead window -> post", bot.classify(m, inside_window, lead) == "post")
check("after kick-off -> expire", bot.classify(m, after_kickoff, lead) == "expire")

placeholder_match = next((x for x in ms if not x.is_real), None)
if placeholder_match:
    t = placeholder_match.kickoff_utc - dt.timedelta(hours=1)
    check("placeholder knockout match -> wait (skipped)", bot.classify(placeholder_match, t, lead) == "wait")

print("\n[6] Full-tournament simulation (how many polls would fire)")
# Walk an imaginary clock minute-by-coarse-step from before the opener to after the final,
# counting how many DISTINCT real matches would be posted exactly once.
fired = set()
start = min(m.kickoff_utc for m in ms) - dt.timedelta(hours=bot.LEAD_HOURS + 1)
end = max(m.kickoff_utc for m in ms) + dt.timedelta(hours=1)
clock = start
while clock <= end:
    for m in ms:
        if m.seq in fired:
            continue
        if bot.classify(m, clock, lead) == "post":
            fired.add(m.seq)
    clock += dt.timedelta(minutes=30)
check("every confirmed-team match would post once", len(fired) == len(real))
print(f"     -> {len(fired)} polls would be posted for currently-known teams "
      f"(knockout matches post later, after refresh fills the teams in).")

print("\n" + ("ALL CHECKS PASSED " + PASS if errors == 0 else f"{errors} CHECK(S) FAILED {FAIL}"))
raise SystemExit(1 if errors else 0)