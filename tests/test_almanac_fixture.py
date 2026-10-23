"""The Almanac builds the same page from the synthetic fixture every time.

Eyes scenarios compare screenshots, so the HTML must not depend on the clock, the machine or the
order the operating system lists files in.
"""

import glob
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "transcripts")
NOW = "2026-10-18T12:00:00"

# Sum of input, output, cache-read and cache-write tokens over every distinct assistant message.
# The forked session repeats one message of its parent, so a build that double counts is higher.
EXPECTED_TOKENS = 3_467_960
EXPECTED_SESSIONS = 12
EXPECTED_FILES = 13


@pytest.fixture
def projects(tmp_path):
    """A private copy of the fixture with the parent session listed before its fork."""
    dest = tmp_path / "transcripts"
    shutil.copytree(FIXTURE, dest)
    parent = glob.glob(str(dest / "*" / "a1b2c3d4-0007-*.jsonl"))[0]
    fork = glob.glob(str(dest / "*" / "a1b2c3d4-0008-*.jsonl"))[0]
    os.utime(parent, (1_700_000_000, 1_700_000_000))
    os.utime(fork, (1_700_000_100, 1_700_000_100))
    return str(dest)


def build(projects_dir, out, now=NOW, tz=None):
    env = dict(os.environ)
    if tz:
        env["TZ"] = tz
    subprocess.run(
        [sys.executable, os.path.join(ROOT, "almanac.py"), "--projects-dir", projects_dir,
         "--now", now, "--out", str(out), "--no-open"],
        check=True, capture_output=True, text=True, cwd=ROOT, env=env,
    )
    return out.read_bytes()


def data_of(html_bytes):
    html = html_bytes.decode("utf-8")
    m = re.search(r"const DATA = (\{.*?\});\r?\nconst FAMS", html, re.S)
    return json.loads(m.group(1))


def test_two_builds_are_byte_identical(projects, tmp_path):
    first = build(projects, tmp_path / "a.html")
    second = build(projects, tmp_path / "b.html")
    assert first == second


def test_now_fixes_today_and_built_time(projects, tmp_path):
    data = data_of(build(projects, tmp_path / "a.html"))
    assert data["today"] == "2026-10-18"
    from datetime import datetime
    assert data["generated"] == int(datetime.fromisoformat(NOW).timestamp() * 1000)


def test_forked_duplicate_message_is_counted_once(projects, tmp_path):
    data = data_of(build(projects, tmp_path / "a.html"))
    assert len(data["sessions"]) == EXPECTED_SESSIONS
    assert data["files"] == EXPECTED_FILES
    assert sum(s["tokens"] for s in data["sessions"]) == EXPECTED_TOKENS


def test_expected_tokens_matches_distinct_messages_in_fixture():
    """Guards the constant above against a fixture edit: recompute it straight from the files."""
    seen, total, repeats = set(), 0, 0
    for path in glob.glob(os.path.join(FIXTURE, "**", "*.jsonl"), recursive=True):
        with open(path, encoding="utf-8") as f:
            for line in f:
                rec = json.loads(line)
                if rec.get("type") != "assistant":
                    continue
                msg = rec["message"]
                if msg["id"] in seen:
                    repeats += 1
                    continue
                seen.add(msg["id"])
                u = msg["usage"]
                cc = u["cache_creation"]
                total += (u["input_tokens"] + u["output_tokens"] + u["cache_read_input_tokens"]
                          + cc["ephemeral_5m_input_tokens"] + cc["ephemeral_1h_input_tokens"])
    assert repeats == 1
    assert total == EXPECTED_TOKENS


def test_fixture_covers_the_dashboard_inputs(projects, tmp_path):
    data = data_of(build(projects, tmp_path / "a.html"))
    sessions = data["sessions"]
    assert {s["project"] for s in sessions} == {"Robot Garden", "Tax Helper", "Pixel Forge"}
    assert {u[1] for s in sessions for u in s["usage"]} == {"Opus", "Sonnet"}
    assert sum(s["subagents"] for s in sessions) == 1
    tools = {t for s in sessions for t in s["tools"]}
    assert {"Bash", "Edit", "Write", "Read", "Skill", "Agent"} <= tools
    assert sum(1 for s in sessions for p in s["prompts"] if "deploy" in p[1].lower()) >= 3


# POSIX TZ strings: the sign is inverted, so "XXX-11" is UTC+11 and "XXX11" is UTC-11. These are the
# far-east and far-west edges of the zones the fixture (all events 11:30 to 12:29 UTC) supports.
FAR_EAST, FAR_WEST = "XXX-11", "XXX11"


def local_clock(tz):
    """The local wall-clock hour the child Python reports for a fixed instant (12:00 UTC)."""
    env = dict(os.environ, TZ=tz)
    code = "from datetime import datetime; print(datetime.fromtimestamp(1_789_992_000).hour)"
    return int(subprocess.run([sys.executable, "-c", code], check=True, capture_output=True,
                              text=True, env=env).stdout)


def test_data_is_the_same_in_every_timezone(projects, tmp_path):
    east, west = local_clock(FAR_EAST), local_clock(FAR_WEST)
    if east == west:
        pytest.skip("setting TZ does not change the local zone of a child Python on this platform")
    assert (east, west) == (23, 1)  # UTC+11 and UTC-11 at 12:00 UTC
    aware_now = "2026-10-18T12:00:00+00:00"
    in_east = data_of(build(projects, tmp_path / "east.html", aware_now, FAR_EAST))
    in_west = data_of(build(projects, tmp_path / "west.html", aware_now, FAR_WEST))
    assert in_east == in_west
    assert in_east["today"] == "2026-10-18"
    assert len(in_east["sessions"]) == EXPECTED_SESSIONS
    # Day buckets are the UTC calendar days of the fixture's assistant messages in both zones.
    expected_days = set()
    for path in glob.glob(os.path.join(FIXTURE, "**", "*.jsonl"), recursive=True):
        with open(path, encoding="utf-8") as f:
            for line in f:
                rec = json.loads(line)
                if rec.get("type") == "assistant":
                    expected_days.add(rec["timestamp"][:10])
    assert {u[0] for s in in_east["sessions"] for u in s["usage"]} == expected_days


def test_aware_now_is_converted_to_local_time(projects, tmp_path):
    """23:30 UTC on the 18th is already the 19th at UTC+11: today must follow the local calendar."""
    if local_clock(FAR_EAST) == local_clock(FAR_WEST):
        pytest.skip("setting TZ does not change the local zone of a child Python on this platform")
    data = data_of(build(projects, tmp_path / "a.html", "2026-10-18T23:30:00Z", FAR_EAST))
    assert data["today"] == "2026-10-19"
    assert data["generated"] == 1_792_366_200_000  # 2026-10-18T23:30:00Z, the same instant
