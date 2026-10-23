"""Claude Almanac - turn your local Claude Code history into a one-page dashboard.

Reads the session transcripts Claude Code keeps under ~/.claude/projects and writes
a single self-contained HTML file. Nothing leaves your machine.

    python almanac.py                  # writes claude-almanac.html and opens it
    python almanac.py --out report.html --no-open
    python almanac.py --now 2026-10-18T12:00:00   # pin "today" and the build time
    python almanac.py --projects-dir tests/fixtures/transcripts   # the invented sample history
"""

import argparse
import json
import os
import re
import sys
import time
import webbrowser
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime

# $ per million tokens: (input, output, cache-read multiplier of input).
# Cache writes are 1.25x input (5-minute TTL) and 2x input (1-hour TTL).
PRICES = {
    "claude-fable-5-1": (10.0, 50.0, 0.025),
    "claude-mythos-5-1": (10.0, 50.0, 0.025),
    "claude-fable-5": (10.0, 50.0, 0.1),
    "claude-opus-5-5": (4.0, 20.0, 0.05),
    "claude-opus-5": (5.0, 25.0, 0.1),
    "claude-opus-4-8": (5.0, 25.0, 0.1),
    "claude-opus-4-7": (5.0, 25.0, 0.1),
    "claude-opus-4-6": (5.0, 25.0, 0.1),
    "claude-sonnet-5-5": (2.0, 10.0, 0.1),
    "claude-sonnet-5": (2.0, 10.0, 0.1),
    "claude-sonnet-4-6": (3.0, 15.0, 0.1),
    "claude-haiku-5-5": (0.10, 0.50, 0.1),
    "claude-haiku-4-5": (1.0, 5.0, 0.1),
}
FAMILY_FALLBACK = {
    "Fable": (10.0, 50.0, 0.1),
    "Opus": (5.0, 25.0, 0.1),
    "Sonnet": (3.0, 15.0, 0.1),
    "Haiku": (1.0, 5.0, 0.1),
}
IDLE_CAP_MS = 5 * 60 * 1000  # gaps longer than this don't count as active time
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
CMD_RE = re.compile(r"<command-name>\s*/?([^<\s]+)\s*</command-name>")
REMINDER_RE = re.compile(r"<system-reminder>.*?(?:</system-reminder>|$)", re.S)


def family(model):
    m = (model or "").lower()
    for name in ("fable", "mythos", "opus", "sonnet", "haiku"):
        if name in m:
            return "Fable" if name == "mythos" else name.capitalize()
    return None


def price_for(model):
    base = re.sub(r"-\d{8}$", "", model or "")
    if base in PRICES:
        return PRICES[base]
    return FAMILY_FALLBACK.get(family(model), (5.0, 25.0, 0.1))


def cost_of(model, inp, cw5, cw1, cr, out):
    pin, pout, read_mult = price_for(model)
    if re.sub(r"-\d{8}$", "", model) == "claude-haiku-5-5" and inp + cw5 + cw1 + cr > 100_000:
        pin, pout = 0.50, 2.50  # long-prompt tier
    actual = (inp * pin + cw5 * pin * 1.25 + cw1 * pin * 2 + cr * pin * read_mult + out * pout) / 1e6
    uncached = ((inp + cw5 + cw1 + cr) * pin + out * pout) / 1e6
    return actual, uncached


def ts_ms(s):
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return None


def bash_name(cmd):
    for seg in re.split(r"&&|\|\||;|\||\n", cmd or ""):
        toks = seg.strip().split()
        while toks and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]):
            toks = toks[1:]
        while toks and toks[0] in ("do", "then", "else", "time", "sudo", "!"):
            toks = toks[1:]
        if not toks or toks[0] in ("cd", "export", "set", "source", ".", "for", "while", "if", "done", "fi", "true"):
            continue
        name = toks[0].strip("\"'()")
        name = re.split(r"[\\/]", name)[-1]
        return name[:30] or None
    return None


def text_of(content):
    if isinstance(content, list):
        content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(content, str):
        return ""
    # Hosts can prepend context blocks to the user's own words; keep only what the user typed.
    return REMINDER_RE.sub("", content)


def parse_file(job):
    """Pull the raw facts out of one transcript. Dedup and bucketing happen in the parent."""
    path, session_id, is_main = job
    out = {
        "path": path, "sid": session_id, "main": is_main,
        "msgs": [], "tools": [], "prompts": [], "cmds": [], "interrupts": [], "errors": [],
        "times": [], "title": None, "cwd": None, "relocated": None, "entry": None,
    }
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return out
    with f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            t = r.get("type")
            if t == "custom-title":
                out["title"] = r.get("customTitle") or out["title"]
                continue
            if t == "relocated":
                out["relocated"] = r.get("relocatedCwd") or out["relocated"]
                continue
            if t not in ("user", "assistant", "system"):
                continue
            ms = ts_ms(r.get("timestamp", ""))
            if ms is None:
                continue
            if out["cwd"] is None and r.get("cwd"):
                out["cwd"] = r["cwd"]
            if out["entry"] is None and r.get("entrypoint"):
                out["entry"] = r["entrypoint"]
            out["times"].append(ms)
            msg = r.get("message") or {}

            if t == "assistant":
                u = msg.get("usage") or {}
                cc = u.get("cache_creation") or {}
                cw1 = cc.get("ephemeral_1h_input_tokens", 0) or 0
                cw5 = cc.get("ephemeral_5m_input_tokens")
                if cw5 is None:
                    cw5 = max((u.get("cache_creation_input_tokens") or 0) - cw1, 0)
                model = msg.get("model") or ""
                if model and not model.startswith("<"):
                    out["msgs"].append((
                        msg.get("id") or r.get("uuid"), ms, model,
                        u.get("input_tokens") or 0, cw5, cw1, u.get("cache_read_input_tokens") or 0,
                        u.get("output_tokens") or 0,
                    ))
                for b in msg.get("content") or []:
                    if not isinstance(b, dict) or b.get("type") != "tool_use":
                        continue
                    name, inp = b.get("name") or "?", b.get("input") or {}
                    detail, lines = None, 0
                    if name == "Bash" or name == "PowerShell":
                        detail = bash_name(inp.get("command"))
                    elif name == "Skill":
                        detail = inp.get("skill")
                    elif name in ("Agent", "Task"):
                        detail = inp.get("subagent_type") or "general-purpose"
                    elif name in EDIT_TOOLS:
                        detail = inp.get("file_path") or inp.get("notebook_path")
                        if name == "Write":
                            lines = (inp.get("content") or "").count("\n") + 1
                        elif name == "Edit":
                            lines = (inp.get("new_string") or "").count("\n") + 1
                        elif name == "MultiEdit":
                            lines = sum((e.get("new_string") or "").count("\n") + 1 for e in inp.get("edits") or [])
                    out["tools"].append((b.get("id"), name, detail, lines))

            elif t == "user" and not r.get("isSidechain"):
                content = msg.get("content")
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                            out["errors"].append(b.get("tool_use_id"))
                text = text_of(content).strip()
                if not text:
                    continue
                uid = r.get("uuid")
                m = CMD_RE.search(text)
                if m:
                    out["cmds"].append((uid, "/" + m.group(1).lstrip("/")))
                    continue
                if text.startswith("[Request interrupted"):
                    out["interrupts"].append(uid)
                    continue
                origin = r.get("origin")
                if r.get("isMeta"):
                    continue
                if isinstance(origin, dict):
                    if origin.get("kind") != "human":
                        continue
                elif text.startswith("<") or r.get("promptSource") == "system":
                    continue
                if isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in content
                ):
                    continue
                out["prompts"].append((uid, ms, text[:600]))
    return out


def find_jobs(root):
    jobs = []
    for proj in os.listdir(root):
        pdir = os.path.join(root, proj)
        if not os.path.isdir(pdir):
            continue
        for name in os.listdir(pdir):
            full = os.path.join(pdir, name)
            if name.endswith(".jsonl") and os.path.isfile(full):
                jobs.append((full, name[:-6], True))
            elif os.path.isdir(full):
                for dp, _, files in os.walk(full):
                    for fn in files:
                        if fn.endswith(".jsonl"):
                            jobs.append((os.path.join(dp, fn), name, False))
    return jobs


def project_of(cwd, home):
    if not cwd:
        return "(unknown)", "(unknown)"
    p = cwd.rstrip("\\/") or cwd
    low = p.lower().replace("/", "\\")
    i = low.find("\\.claude\\worktrees\\")
    if i >= 0:
        p = p[:i]
    if "scratch-workspaces" in low:
        return "No folder (scratch)", "(scratch)"
    if p.lower() == home.lower().rstrip("\\/"):
        return "~ (home)", p
    base = re.split(r"[\\/]", p)[-1]
    return (base or p), p


def merge_spans(spans):
    """Merge [start, end) millisecond spans into sorted, non-overlapping [start, end] seconds."""
    merged = []
    for a, b in sorted(spans):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [[a // 1000, -(-b // 1000)] for a, b in merged]


def local_day(ms):
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")


def build(root, now=None):
    if now is None:
        now = datetime.now()
    home = os.path.expanduser("~")
    jobs = find_jobs(root)
    # Main transcripts first, oldest first, so the original copy of a forked/resumed message wins.
    jobs.sort(key=lambda j: (not j[2], os.path.getmtime(j[0])))
    with ProcessPoolExecutor() as ex:
        results = list(ex.map(parse_file, jobs, chunksize=16))

    seen_msg, seen_tool, seen_uuid, seen_err = set(), set(), set(), set()
    sessions = {}

    def sess(sid):
        if sid not in sessions:
            sessions[sid] = {
                "id": sid, "title": None, "cwd": None, "relocated": None, "entry": None,
                "start": None, "end": None, "active": 0, "agentSecs": 0, "subagents": 0,
                "spans": [], "prompts": [], "cmds": Counter(),
                "usage": defaultdict(lambda: [0.0, 0, 0.0]),
                "tools": Counter(), "skills": Counter(), "agents": Counter(), "mcp": Counter(),
                "bash": Counter(), "files": Counter(), "lines": 0, "toolCalls": 0, "errors": 0,
                "interrupts": 0, "tokens": 0,
            }
        return sessions[sid]

    for r in results:
        s = sess(r["sid"])
        if r["main"]:
            s["title"] = r["title"] or s["title"]
            s["cwd"] = s["cwd"] or r["cwd"]
            s["relocated"] = r["relocated"] or s["relocated"]
            s["entry"] = s["entry"] or r["entry"]
        else:
            s["subagents"] += 1
            s["cwd"] = s["cwd"] or r["cwd"]

        times = sorted(r["times"])
        if times:
            s["start"] = min(s["start"] or times[0], times[0])
            s["end"] = max(s["end"] or times[-1], times[-1])
        # Each event keeps the session "active" until the next one, for at most IDLE_CAP_MS.
        for a, b in zip(times, times[1:]):
            gap = min(b - a, IDLE_CAP_MS)
            if r["main"]:
                s["active"] += gap / 1000
                if gap > 0:
                    s["spans"].append((a, a + gap))
            else:
                s["agentSecs"] += gap / 1000

        for mid, ms, model, inp, cw5, cw1, cr, out_t in r["msgs"]:
            if mid in seen_msg:
                continue
            seen_msg.add(mid)
            fam = family(model) or "Other"
            actual, uncached = cost_of(model, inp, cw5, cw1, cr, out_t)
            u = s["usage"][(local_day(ms), fam)]
            u[0] += actual
            u[1] += inp + cw5 + cw1 + cr + out_t
            u[2] += uncached
            s["tokens"] += inp + cw5 + cw1 + cr + out_t

        for tid, name, detail, lines in r["tools"]:
            if tid in seen_tool:
                continue
            seen_tool.add(tid)
            s["toolCalls"] += 1
            if name.startswith("mcp__"):
                server = name.split("__")[1] if name.count("__") >= 2 else name
                server = re.sub(r"^[0-9a-f]{8}-[0-9a-f-]{27}$", "connector", server)
                s["mcp"][server] += 1
                s["tools"]["MCP: " + server] += 1
            else:
                s["tools"][name] += 1
            if not detail:
                continue
            if name in ("Bash", "PowerShell"):
                s["bash"][detail] += 1
            elif name == "Skill":
                s["skills"][detail] += 1
            elif name in ("Agent", "Task"):
                s["agents"][detail] += 1
            elif name in EDIT_TOOLS:
                s["files"][detail.replace("\\", "/")] += 1
                s["lines"] += lines

        for tid in r["errors"]:
            if tid not in seen_err:
                seen_err.add(tid)
                s["errors"] += 1
        for uid in r["interrupts"]:
            if uid not in seen_uuid:
                seen_uuid.add(uid)
                s["interrupts"] += 1
        for uid, cmd in r["cmds"]:
            if uid not in seen_uuid:
                seen_uuid.add(uid)
                s["cmds"][cmd] += 1
        if r["main"]:
            for uid, ms, text in r["prompts"]:
                if uid in seen_uuid:
                    continue
                seen_uuid.add(uid)
                s["prompts"].append((ms, text))

    out = []
    for s in sessions.values():
        if not s["start"] or (not s["prompts"] and not s["usage"]):
            continue
        cwd = s["relocated"] or s["cwd"]
        pname, ppath = project_of(cwd, home)
        s["prompts"].sort()
        title = s["title"]
        if not title:
            first = next((p[1] for p in s["prompts"]), "")
            title = (first.splitlines() or ["Untitled session"])[0][:90] or "Untitled session"
        out.append({
            "id": s["id"], "title": title, "project": pname, "path": ppath,
            "cwd": cwd or "", "entry": s["entry"] or "",
            "start": s["start"], "end": s["end"], "active": round(s["active"]),
            "agentSecs": round(s["agentSecs"]), "subagents": s["subagents"],
            "spans": merge_spans(s["spans"]),
            "prompts": s["prompts"], "cmds": dict(s["cmds"]),
            "usage": [[d, f, round(v[0], 4), v[1], round(v[2], 4)] for (d, f), v in s["usage"].items()],
            "tools": dict(s["tools"]), "skills": dict(s["skills"]), "agents": dict(s["agents"]),
            "mcp": dict(s["mcp"]), "bash": dict(s["bash"]),
            "files": dict(s["files"].most_common(150)),
            "lines": s["lines"], "toolCalls": s["toolCalls"], "errors": s["errors"],
            "interrupts": s["interrupts"], "tokens": s["tokens"],
        })
    out.sort(key=lambda x: x["start"], reverse=True)
    return {
        "generated": int(now.timestamp() * 1000),
        "today": now.strftime("%Y-%m-%d"),
        "files": len(jobs),
        "sessions": out,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--projects-dir", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--out", default="claude-almanac.html")
    ap.add_argument("--no-open", action="store_true", help="don't open the page when done")
    ap.add_argument("--now", help="ISO datetime to use as today and the build time (for reproducible output); a zone offset or Z is converted to local time")
    args = ap.parse_args()
    try:
        now = datetime.fromisoformat(args.now) if args.now else None
        if now is not None and now.tzinfo is not None:
            # An aware instant becomes local wall-clock time, the zone day buckets use, so "today" can't disagree with them.
            now = now.astimezone().replace(tzinfo=None)
    except ValueError:
        sys.exit(f"--now needs an ISO datetime like 2026-10-18T12:00:00, got {args.now!r}")

    if not os.path.isdir(args.projects_dir):
        sys.exit(f"No Claude Code transcripts found at {args.projects_dir}")
    t0 = time.time()
    data = build(args.projects_dir, now)
    template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "almanac_template.html")
    with open(template_path, encoding="utf-8") as f:
        template = f.read()
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    blob = blob.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    html = template.replace("/*__DATA__*/null", blob)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    n = len(data["sessions"])
    print(f"Read {data['files']} transcripts -> {n} sessions in {time.time() - t0:.1f}s")
    print(f"Wrote {os.path.abspath(args.out)} ({len(html) / 1e6:.1f} MB)")
    if not args.no_open:
        webbrowser.open("file:///" + os.path.abspath(args.out).replace("\\", "/"))


if __name__ == "__main__":
    main()
