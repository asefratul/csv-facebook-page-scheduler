"""Stage 1 of the agent: propose post ideas, publish them for approval, record the decision.

Flow: `propose` (Claude) -> `issue_body` (a GitHub issue with a hidden, validated payload) -> the owner comments
`/approve 1,3` -> `apply_approval` writes ideas/<id>.json, adds `pending` rows to content-plan.csv and logs all
decisions in ideas/log.csv. The model's output is always re-validated here; it never writes anything itself.
"""
import base64
import csv
import json
import re
from pathlib import Path

from . import checks, config, llm
from .plan import read_plan, write_plan

LOG_FIELDS = ["id", "kind", "title_en", "status", "decided_on"]
PAYLOAD = re.compile(r"<!-- ideas-payload:([A-Za-z0-9+/=]+) -->")
MAX_HOLDS = 3  # an idea held this many times is rejected instead of coming back again


def schema(cfg):
    return {
        "type": "object",
        "properties": {"ideas": {"type": "array", "minItems": 1, "maxItems": 8, "items": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(cfg["kinds"])},
                "title": {"type": "string", "description": f"Short hook title in {cfg['language']}, max 80 chars"},
                "title_en": {"type": "string", "description":
                             "English title for the reviewer (identical to title if the post language is English)"},
                "angle": {"type": "string", "description": "1-2 sentences: what the post says and why readers care"},
                "points": {"type": "array", "items": {"type": "string"}, "description":
                           "2-6 short English key points the post will make"},
            },
            "required": ["kind", "title", "title_en", "angle", "points"],
        }}},
        "required": ["ideas"],
    }


def clean_idea(d, cfg=None):
    """Return (idea, None) or (None, reason). Pure validation; trims and bounds everything."""
    cfg = cfg or config.load()
    try:
        idea = {"kind": d["kind"], "title": str(d["title"]).strip(), "title_en": str(d["title_en"]).strip(),
                "angle": str(d["angle"]).strip(), "points": [str(x).strip() for x in d.get("points", []) if str(x).strip()]}
    except (KeyError, TypeError, ValueError, AttributeError):
        return None, "malformed"
    if idea["kind"] not in cfg["kinds"]:
        return None, f"unknown kind {idea['kind']!r}"
    if not 3 <= len(idea["title"]) <= 90 or checks.check_text("title", idea["title"], cfg):
        return None, "title missing/too long/wrong script"
    if not 3 <= len(idea["title_en"]) <= 120 or not 3 <= len(idea["angle"]) <= 500:
        return None, "title_en/angle length"
    if not 2 <= len(idea["points"]) <= 6:
        return None, "needs 2-6 points"
    if any(len(x) > 220 for x in idea["points"]):
        return None, "point too long"
    text = " ".join([idea["title"], idea["title_en"], idea["angle"], *idea["points"]])
    if checks.check_claims("idea", text, cfg) or checks.URL.search(text):
        return None, "claim or link"
    return idea, None


def read_log(path):
    p = Path(path)
    if not p.exists():
        return []
    with open(p, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def append_log(path, rows):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    with open(p, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS, lineterminator="\n")
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in LOG_FIELDS})


def build_prompt(guidelines, plan_rows, log_rows, recent_captions, n, today, cfg):
    planned = [f"- {r.get('kind')} ({r.get('status')})" + (f" idea {r['idea']}" if r.get("idea") else "") for r in plan_rows]
    decided = [f"- [{r['status']}] {r['kind']}: {r['title_en']}" for r in log_rows[-40:]]  # held ones return on their own
    system = (f"You are the content strategist for {cfg['brand_name']}'s Facebook page. Propose post ideas that "
              "readers will find useful and that follow the guidelines exactly. Reply only through the tool.\n\n"
              "GUIDELINES\n" + guidelines)
    user = (f"Today is {today}. Propose {n} distinct post ideas for the coming weeks.\n"
            f"- Mix the formats ({', '.join(cfg['kinds'])}).\n"
            "- Do not repeat or closely mirror anything already planned, decided or posted.\n"
            f"- `title` is the hook for the cover slide, in {cfg['language']}. Points are short English facts/steps.\n\n"
            "ALREADY PLANNED:\n" + ("\n".join(planned) or "(none)") +
            "\n\nPREVIOUS IDEAS AND DECISIONS:\n" + ("\n".join(decided) or "(none)") +
            "\n\nRECENT POSTS (opening lines):\n" + ("\n".join(f"- {c}" for c in recent_captions) or "(none)"))
    return system, user


def propose(guidelines, plan_rows, log_rows, recent_captions, today, n=5, call=None, held=(), cfg=None):
    """Returns (ideas with ids, rejected-reasons). Raises llm.LLMError if nothing usable came back."""
    cfg = cfg or config.load()
    held = list(held)[:n]
    n_new = max(n - len(held), 2)  # always at least 2 fresh ideas next to the ones held back
    system, user = build_prompt(guidelines, plan_rows, log_rows, recent_captions, n_new, today, cfg)
    raw = (call or llm.call_json)(system, user, schema(cfg), tool_name="submit_ideas", max_tokens=3000)
    titles = {r["title_en"].lower() for r in log_rows} | {h["title_en"].lower() for h in held}
    ideas, rejected = [], []
    for d in raw.get("ideas", [])[:n_new + 3]:
        idea, why = clean_idea(d, cfg)
        if idea and idea["title_en"].lower() in titles:
            idea, why = None, "repeats an earlier idea"
        if idea is None:
            rejected.append(why)
            continue
        titles.add(idea["title_en"].lower())
        ideas.append(idea)
    ideas = ideas[:n_new]
    if not ideas:
        raise llm.LLMError(f"no usable ideas (rejected: {rejected})")
    day = today.replace("-", "")
    used = {h["id"] for h in held} | {r["id"] for r in log_rows}  # ids stay unique even for two batches on one day
    nxt = 1
    for idea in ideas:
        while f"{day}-{nxt}" in used:
            nxt += 1
        idea["id"] = f"{day}-{nxt}"
        used.add(idea["id"])
    return held + ideas, rejected


def _is_held(idea, today):
    return not idea["id"].startswith(today.replace("-", ""))


def issue_body(ideas, today, cfg=None):
    cfg = cfg or config.load()
    out = [f"## Post ideas — {today}", "",
           "Reply with one comment to decide (only the repository owner's comment counts):", "",
           "- `/approve 1,3` approves 1 and 3 and rejects the rest",
           f"- `/approve 1,3 hold 2` also holds 2: it comes back in the next batch (after {MAX_HOLDS} holds it is dropped)",
           "- `/approve 1 hold 2,4 reject 3,5` sets each one explicitly; unlisted ideas are rejected",
           "- `/approve none` rejects the whole batch",
           "- Lines after the first line are instructions for the drafts of the ideas you approve in that comment "
           "(same for all of them), e.g. `Keep the language very simple`", ""]
    for n, i in enumerate(ideas, start=1):
        tag = " · ↩ held from an earlier batch" if _is_held(i, today) else ""
        hook = f" · Hook: {i['title']}" if i["title"] != i["title_en"] else ""
        out += [f"### {n}. {i['title_en']}", f"**{cfg['kinds'][i['kind']]}**{hook}{tag}", "", i["angle"], ""]
        out += [f"- {p}" for p in i["points"]] + [""]
    payload = base64.b64encode(json.dumps(ideas, ensure_ascii=False).encode()).decode()
    out.append(f"<!-- ideas-payload:{payload} -->")
    return "\n".join(out) + "\n"


def telegram_text(ideas, issue_url, today, cfg=None):
    cfg = cfg or config.load()
    lines = [f"\U0001f9e0 {len(ideas)} new post ideas ({today})", ""]
    lines += [f"{n}. {i['title_en']} — {cfg['kinds'][i['kind']]}" + (" (held)" if _is_held(i, today) else "")
              for n, i in enumerate(ideas, start=1)]
    lines += ["", "Decide by commenting here, e.g. /approve 1,3 hold 2 reject 4 (unlisted = rejected):", issue_url]
    return "\n".join(lines)


def parse_payload(body, cfg=None):
    """Ideas stored in an issue body, re-validated. Raises ValueError if the payload is missing or damaged."""
    m = PAYLOAD.search(body or "")
    if not m:
        raise ValueError("no ideas payload in the issue")
    try:
        raw = json.loads(base64.b64decode(m.group(1)).decode())
        if not isinstance(raw, list):
            raise TypeError
    except Exception:
        raise ValueError("ideas payload is damaged") from None
    ideas = []
    for d in raw:
        idea, why = clean_idea(d, cfg)
        if idea is None or not re.fullmatch(r"\d{8}-\d{1,2}", str(d.get("id", ""))):
            raise ValueError(f"invalid idea in payload: {why or 'bad id'}")
        idea["id"] = d["id"]
        ideas.append(idea)
    return ideas


def parse_command(comment, count):
    """'/approve 1,3 hold 2 reject 4' -> {'approve': {1,3}, 'hold': {2}, 'reject': {4, ...unlisted}}.
    Numbers without a keyword are approved; '/approve none' rejects the whole batch. Raises ValueError."""
    text = (comment or "").strip()
    first = text.splitlines()[0].strip() if text else ""
    usage = "use `/approve 1,3`, optionally with `hold 2` and `reject 4` (unlisted ideas are rejected), or `/approve none`"
    m = re.fullmatch(r"/approve\s+(.+)", first, re.IGNORECASE)
    if not m:
        raise ValueError(usage)
    rest = m.group(1).strip()
    out = {"approve": set(), "hold": set(), "reject": set()}
    if rest.lower() != "none":
        seg = r"(?:(hold|reject)\s+)?(\d+(?:\s*,\s*\d+)*)"
        if not re.fullmatch(rf"(?:\s*{seg})+", rest, re.IGNORECASE):
            raise ValueError(usage)
        for kw, nums in re.findall(seg, rest, re.IGNORECASE):
            out[(kw or "approve").lower()] |= {int(x) for x in re.findall(r"\d+", nums)}
    listed = [n for v in out.values() for n in v]
    bad = sorted({n for n in listed if not 1 <= n <= count})
    if bad:
        raise ValueError(f"no idea number(s) {bad}; this batch has 1-{count}")
    if len(listed) != len(set(listed)):
        raise ValueError("an idea number is in more than one state")
    out["reject"] |= set(range(1, count + 1)) - set(listed)
    return out


def parse_notes(comment, limit=600):
    """Lines after the /approve line: instructions for the drafts of the ideas approved in this comment."""
    lines = (comment or "").strip().splitlines()[1:]
    return "\n".join(x.rstrip() for x in lines).strip()[:limit]


def latest_status(log_rows):
    status = {}
    for r in log_rows:
        status[r["id"]] = r["status"]
    return status


def load_held(log_path, held_dir, today, cfg=None):
    """Held ideas that come back in the next batch (re-validated). Ideas held MAX_HOLDS times are rejected."""
    log = read_log(log_path)
    held_dir = Path(held_dir)
    holds = {}
    for r in log:
        if r["status"] == "held":
            holds[r["id"]] = holds.get(r["id"], 0) + 1
    out, dropped = [], []
    for iid, st in latest_status(log).items():
        f = held_dir / f"{iid}.json"
        if st != "held" or not f.is_file():
            continue
        idea, _ = clean_idea(json.loads(f.read_text(encoding="utf-8")), cfg)
        if idea is None or holds[iid] >= MAX_HOLDS:
            f.unlink()
            dropped.append({"id": iid, "kind": (idea or {}).get("kind", ""), "title_en": (idea or {}).get("title_en", iid),
                            "status": "rejected", "decided_on": today})
            continue
        idea["id"] = iid
        out.append(idea)
    if dropped:
        append_log(log_path, dropped)
    return out


def apply_approval(ideas, decisions, plan_path, ideas_dir, log_path, today, notes=""):
    """Write approved ideas + plan rows, park held ideas in ideas/held/, log every decision.
    `decisions` is parse_command's dict. Returns (approved, held) idea lists."""
    ideas_dir = Path(ideas_dir)
    held_dir = ideas_dir / "held"
    ideas_dir.mkdir(parents=True, exist_ok=True)
    plan_rows = read_plan(plan_path)
    log, chosen, held = [], [], []
    for n, idea in enumerate(ideas, start=1):
        state = "approved" if n in decisions["approve"] else "held" if n in decisions["hold"] else "rejected"
        log.append({"id": idea["id"], "kind": idea["kind"], "title_en": idea["title_en"],
                    "status": state, "decided_on": today})
        stale = held_dir / f"{idea['id']}.json"
        if state == "held":
            held_dir.mkdir(parents=True, exist_ok=True)
            stale.write_text(json.dumps(idea, ensure_ascii=False, indent=2) + "\n", "utf-8")
            held.append(idea)
            continue
        stale.unlink(missing_ok=True)
        if state == "rejected":
            continue
        saved = dict(idea, note=notes) if notes else idea  # the note is read back when the draft is written
        (ideas_dir / f"{idea['id']}.json").write_text(json.dumps(saved, ensure_ascii=False, indent=2) + "\n", "utf-8")
        plan_rows.append({"kind": idea["kind"], "link": "", "status": "pending", "generated_on": "", "idea": idea["id"]})
        chosen.append(idea)
    write_plan(plan_path, plan_rows)
    append_log(log_path, log)
    return chosen, held
