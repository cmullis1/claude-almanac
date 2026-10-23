"""Regenerate tests/fixtures/transcripts: a synthetic Claude Code history for the Almanac tests.

Everything here is invented (projects, prompts, titles, paths). Nothing comes from a real
user. Run `python tests/fixtures/generate_transcripts.py` to rewrite the folder; the output is
fully deterministic, so a regenerate that changes any file means this script changed.

Shape: 3 projects, 12 main sessions across the 60 days before 2026-10-18, human prompts, two
model families, Bash/Edit/Write/Read/Skill/Agent tool calls, one subagent transcript, and one
forked duplicate (session 8 repeats an assistant message of session 7 under the same message.id).

Every event lands between 11:30 and 12:29 UTC so the local calendar day is the same in any
timezone from UTC-11 to UTC+11.
"""

import json
import os
import shutil
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "transcripts")
TODAY = datetime(2026, 10, 18, tzinfo=timezone.utc)
OPUS, SONNET = "claude-opus-5-5", "claude-sonnet-5"

PROJECTS = {
    "RG": ("D--Example-Robot-Garden", "D:\\Example\\Robot Garden"),
    "TH": ("D--Example-Tax-Helper", "D:\\Example\\Tax Helper"),
    "PF": ("D--Example-Pixel-Forge", "D:\\Example\\Pixel Forge"),
}

# (project, days before today, model, custom title or None, prompts, tools used on each turn)
SESSIONS = [
    ("RG", 58, OPUS, "Water the seedlings scheduler",
     ["Plan a watering schedule for the greenhouse robots",
      "Add a moisture sensor reading to the planner",
      "Write tests for the schedule overlap rules",
      "Ship it, run the deploy script for the greenhouse"],
     ["Skill:superpowers:brainstorming", "Write", "Bash:pytest", "Bash:npm"]),
    ("TH", 51, SONNET, None,
     ["Parse the mileage CSV and total it by month",
      "Handle blank rows in the mileage file",
      "Explain the standard deduction rounding"],
     ["Read", "Edit", "Bash:python"]),
    ("PF", 47, OPUS, "Sprite atlas packer",
     ["Pack the character sprites into a single atlas",
      "Trim transparent borders before packing",
      "Why does the atlas have a one pixel seam",
      "Add a padding option"],
     ["Read", "Edit", "Agent:Explore", "Edit"]),
    ("RG", 40, SONNET, None,
     ["Rename the gardener class to Tender",
      "Update the imports after the rename",
      "Run the linter and fix what it finds"],
     ["Edit", "Edit", "Bash:ruff"]),
    ("TH", 33, OPUS, "Quarterly estimate calculator",
     ["Build a quarterly estimated tax calculator",
      "Add the safe harbor rule",
      "Show the result as a small table",
      "Write the readme section for the calculator"],
     ["Write", "Edit", "Edit", "Write"]),
    ("PF", 27, SONNET, "Palette swap shader",
     ["Write a palette swap shader for the hero sprite",
      "The shader flickers on the first frame, fix it",
      "Add a second palette for the night scene"],
     ["Write", "Edit", "Edit"]),
    ("RG", 21, OPUS, "Fleet dashboard",
     ["Sketch a dashboard for the robot fleet status",
      "Add a battery level column",
      "Group the rows by greenhouse bay"],
     ["Skill:superpowers:brainstorming", "Write", "Edit"]),
    ("RG", 20, OPUS, "Fleet dashboard, second pass",
     ["Continue the dashboard and add sorting",
      "Sorting breaks when the battery is unknown, handle that"],
     ["Edit", "Edit"]),
    ("TH", 14, OPUS, "Receipt importer",
     ["Import receipts from a folder of PDFs",
      "Group the receipts by vendor",
      "Prepare the build and deploy the importer to the home server",
      "The deploy script exits with code 2, look into it"],
     ["Bash:python", "Edit", "Bash:rsync", "Read"]),
    ("PF", 9, SONNET, None,
     ["Export animations as GIFs from the editor",
      "Cap the GIF palette at 64 colors",
      "Add a progress bar to the export dialog"],
     ["Read", "Edit", "Edit"]),
    ("RG", 4, OPUS, "Irrigation planner release",
     ["Cut a release of the irrigation planner",
      "Update the changelog with this week's fixes",
      "Deploy the release notes page to the garden wiki",
      "Add a rollback note to the deploy checklist"],
     ["Skill:release-notes", "Edit", "Bash:git", "Edit"]),
    ("TH", 1, SONNET, "Deploy checklist for filing season",
     ["Draft the filing season checklist",
      "Add a step to deploy the checklist as a static page",
      "Trim the checklist to one page"],
     ["Write", "Bash:npm", "Edit"]),
]

BASH = {
    "pytest": "cd D:/Example && pytest -q tests/",
    "npm": "npm run deploy -- --target staging",
    "python": "python tools/run.py --verbose",
    "ruff": "ruff check . --fix",
    "rsync": "rsync -av dist/ homeserver:/srv/site/",
    "git": "git status && git log --oneline -5",
}

counter = {"msg": 0, "tool": 0, "uuid": 0}


def nxt(key):
    counter[key] += 1
    return counter[key]


def stamp(day_offset, minute, second=0):
    t = TODAY - timedelta(days=day_offset) + timedelta(hours=11, minutes=30 + minute, seconds=second)
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def uuid():
    return "00000000-0000-4000-8000-%012d" % nxt("uuid")


def usage():
    n = counter["msg"]
    return {
        "input_tokens": 1200 + 13 * n,
        "output_tokens": 300 + 29 * n,
        "cache_read_input_tokens": 20000 + 411 * n,
        "cache_creation_input_tokens": 800 + 7 * n + (500 if n % 3 == 0 else 0),
        "cache_creation": {
            "ephemeral_5m_input_tokens": 800 + 7 * n,
            "ephemeral_1h_input_tokens": 500 if n % 3 == 0 else 0,
        },
    }


def tool_input(spec, project_cwd, n):
    name, _, arg = spec.partition(":")
    base = project_cwd.replace("\\", "/")
    if name == "Bash":
        return "Bash", {"command": BASH[arg], "description": "Run " + arg}
    if name == "Edit":
        return "Edit", {"file_path": f"{base}/src/module_{n % 4}.py", "old_string": "x = 1",
                        "new_string": "x = 2\ny = 3\nz = x + y"}
    if name == "Write":
        return "Write", {"file_path": f"{base}/docs/note_{n % 3}.md", "content": "# Note\n\nSome text\nmore text\n"}
    if name == "Read":
        return "Read", {"file_path": f"{base}/src/module_{n % 4}.py"}
    if name == "Skill":
        return "Skill", {"skill": arg}
    if name == "Agent":
        return "Agent", {"subagent_type": arg, "description": "Look around", "prompt": "Find the entry point"}
    raise ValueError(spec)


def assistant(session_id, cwd, ts, model, content, sidechain=False):
    nxt("msg")
    rec = {
        "type": "assistant", "uuid": uuid(), "timestamp": ts, "sessionId": session_id, "cwd": cwd,
        "entrypoint": "cli", "isSidechain": sidechain,
        "message": {"id": "msg_%04d" % counter["msg"], "role": "assistant", "model": model,
                    "content": content, "usage": usage()},
    }
    return rec


def user(session_id, cwd, ts, content, human=False, sidechain=False):
    rec = {"type": "user", "uuid": uuid(), "timestamp": ts, "sessionId": session_id, "cwd": cwd,
           "entrypoint": "cli", "isSidechain": sidechain, "message": {"role": "user", "content": content}}
    if human:
        rec["origin"] = {"kind": "human"}
    return rec


def write_jsonl(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=True, separators=(",", ":")) + "\n")


def session_id(i):
    return "a1b2c3d4-%04d-4000-8000-%012d" % (i, i)


def build_session(i, spec):
    proj, offset, model, title, prompts, tools = spec
    sid = session_id(i)
    dirname, cwd = PROJECTS[proj]
    recs = []
    if title:
        recs.append({"type": "custom-title", "customTitle": title, "sessionId": sid})
    recs.append({"type": "file-history-snapshot", "messageId": "snap-%d" % i, "snapshot": {}})
    last_assistant = None
    for turn, (prompt, tool_spec) in enumerate(zip(prompts, tools)):
        m = turn * 12
        recs.append(user(sid, cwd, stamp(offset, m), prompt, human=True))
        tname, tinput = tool_input(tool_spec, cwd, i + turn)
        tid = "toolu_%03d_%d" % (i, turn)
        recs.append(assistant(sid, cwd, stamp(offset, m + 1), model, [
            {"type": "text", "text": "On it."},
            {"type": "tool_use", "id": tid, "name": tname, "input": tinput},
        ]))
        failed = i == 9 and turn == 3
        recs.append(user(sid, cwd, stamp(offset, m + 1, 30), [
            {"type": "tool_result", "tool_use_id": tid, "content": "exit 2" if failed else "ok", "is_error": failed},
        ]))
        last_assistant = assistant(sid, cwd, stamp(offset, m + 3), model, [{"type": "text", "text": "Done."}])
        recs.append(last_assistant)
    if i == 2:
        recs.append(user(sid, cwd, stamp(offset, 40), "<command-name>/clear</command-name>"))
    if i == 4:
        recs.append(user(sid, cwd, stamp(offset, 41), "[Request interrupted by user]"))
    return sid, dirname, recs, last_assistant


def main():
    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    finished = {}
    for idx, spec in enumerate(SESSIONS, start=1):
        sid, dirname, recs, last = build_session(idx, spec)
        if idx == 8:
            # A fork repeats the parent's last assistant message verbatim (same message.id).
            fork_src = finished[7]
            recs.insert(1, json.loads(json.dumps(fork_src)))
        finished[idx] = last
        write_jsonl(os.path.join(OUT, dirname, sid + ".jsonl"), recs)
        if idx == 11:
            sub = []
            dirname_, cwd = PROJECTS[spec[0]]
            sub.append(user(sid, cwd, stamp(spec[1], 20), "Find where releases are tagged", sidechain=True))
            t1 = assistant(sid, cwd, stamp(spec[1], 21), spec[2], [
                {"type": "tool_use", "id": "toolu_sub_1", "name": "Read",
                 "input": {"file_path": cwd.replace("\\", "/") + "/RELEASING.md"}}], sidechain=True)
            sub.append(t1)
            sub.append(assistant(sid, cwd, stamp(spec[1], 23), spec[2], [
                {"type": "tool_use", "id": "toolu_sub_2", "name": "Bash", "input": {"command": BASH["git"]}}],
                sidechain=True))
            sub.append(assistant(sid, cwd, stamp(spec[1], 25), spec[2], [{"type": "text", "text": "Tags live in git."}],
                                 sidechain=True))
            write_jsonl(os.path.join(OUT, dirname_, sid, "subagents", "agent-explore01.jsonl"), sub)


if __name__ == "__main__":
    main()
