#!/usr/bin/env python3
"""Draft and idea tooling for the CSV Facebook Page Scheduler (all settings: config.json).

  python make_content.py ideas            # weekly: Claude proposes ideas -> issue body + Telegram text
  python make_content.py approve ...      # apply an owner's `/approve 1,3 hold 2 reject 4` comment
  python make_content.py next             # draft the next approved idea: cards + caption -> content.csv rows
  python make_content.py telegram-check   # optional: find your TELEGRAM_CHAT_ID and send a test message
  python make_content.py notify ...       # optional: send a Telegram message (no-op if not configured)

`next` takes the first `pending` topic in content-plan.csv, has Claude write the copy from the approved idea,
renders 1080x1080 cards, appends one `images` row to content.csv, marks the topic done and writes a review summary.
Exit codes: 0 = draft created, 3 = nothing to do (queue full / no change), 4 = backlog empty, 1 = error.
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

from generator import aicontent, checks, config, feedback, llm, plan, render, telegram
from generator import ideas as ideas_mod

EXIT_SKIP, EXIT_EMPTY = 3, 4


def render_cards(idea, content, media_dir, cfg, force=False):
    """Render the card slides for an AI-written post. Returns the PNG paths."""
    pages = render.card_pages(cfg["kinds"][idea["kind"]], idea["title"], content["slides"], cfg)
    media_dir = Path(media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)
    paths = [media_dir / f"{idea['kind']}-{idea['id']}-{name}.png" for name, _ in pages]
    existing = [p for p in paths if p.exists()]
    if existing and not force:
        raise FileExistsError(f"{', '.join(p.name for p in existing)} already exist (use --force to overwrite)")
    work = Path(tempfile.mkdtemp(prefix="gen-"))
    try:
        render.screenshot_all([(p, 1080, 1080, doc) for p, (_, doc) in zip(paths, pages)], work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return paths


def summary_md(idea, row, repo, branch, tz):
    base = f"https://github.com/{repo}/blob/{branch}/" if repo and branch else ""
    catchup = os.environ.get("CATCHUP_HOURS") or "6"
    out = [f"## Draft: {idea['kind']} post — {idea['title_en']}", "",
           "**Review, edit anything you like in the web editor, then merge to approve.** "
           "Close the PR (and set the topic to `skip` in `content-plan.csv`) to reject. "
           "Comment `/regenerate <notes>` to redraft it.", "",
           f"**Merge before {row['post_date']} ({tz}).** The publisher only catches up rows that are "
           f"at most {catchup} hours overdue; a later merge means the post is skipped.", "",
           f"Approved idea `{idea['id']}`: {idea['angle']}", "",
           f"### `images` post at **{row['post_date']}** ({tz})", "", "```text", row["post_content"], "```",
           f"First comment: {row['post_comment'] or '(none)'}", ""]
    out += [f"![{m}]({base}{m}?raw=true)" for m in row["media_url"].split("|") if base]
    out += ["", "_Written by Claude from the approved idea; the cards are rendered from the card template._", ""]
    return "\n".join(out)


def feedback_md(fb):
    lines = ["### How your feedback was applied", ""]
    lines.append(f"- First post set to **{fb.first_post}**." if fb.first_post else "- No date change.")
    if fb.copy_notes:
        lines.append(f"- Wording notes: {fb.copy_notes} — passed to the copywriter for this draft.")
    lines.append(f"- _(read via: {fb.via})_")
    return "\n".join(lines) + "\n"


def cmd_next(a):
    cfg = config.load()
    cad = cfg["cadence"]
    now = plan.parse_date(os.environ["GEN_NOW"]) if os.environ.get("GEN_NOW") else plan.now_local()
    rows = plan.read_rows(a.content)
    if a.respect_queue and plan.future_count(rows, now) >= cad["queue_below"]:
        print(f"SKIP: {plan.future_count(rows, now)} post(s) already scheduled in the future")
        return EXIT_SKIP
    plan_rows = plan.read_plan(a.plan)
    topic = plan.next_pending(plan_rows)
    if topic is None:
        print("EMPTY: no pending topic left in content-plan.csv")
        return EXIT_EMPTY
    if topic["kind"] not in cfg["kinds"]:
        print(f"Unsupported kind {topic['kind']!r} (config kinds: {list(cfg['kinds'])})", file=sys.stderr)
        return 1

    fb = feedback.Result()
    if a.feedback_file and Path(a.feedback_file).exists():
        fb = feedback.interpret(Path(a.feedback_file).read_text(encoding="utf-8"), now)
        print(f"Feedback read via {fb.via}: first_post={fb.first_post!r} notes={fb.copy_notes!r}")

    slot = a.first_post or fb.first_post
    if slot:
        if plan.parse_date(slot) is None:
            raise ValueError(f"--first-post {slot!r} is not 'YYYY-MM-DD HH:MM'")
        slot = plan.parse_date(slot).strftime("%Y-%m-%d %H:%M")
    else:
        slot = plan.next_slots(rows, now, cad["spacing_days"], cad["lead_days"], cad["post_hour"], 1)[0]

    idea_path = Path(a.ideas_dir) / f"{topic.get('idea', '')}.json"
    if not idea_path.is_file():
        raise ValueError(f"topic has no approved idea file ({idea_path})")
    saved = json.loads(idea_path.read_text(encoding="utf-8"))
    idea, why = ideas_mod.clean_idea(saved, cfg)
    if idea is None:
        raise ValueError(f"{idea_path} is not a valid idea: {why}")
    idea["id"] = topic["idea"]
    notes = "\n".join(x for x in (str(saved.get("note") or ""), fb.copy_notes) if x)
    content = aicontent.write(config.guidelines(cfg), idea, notes, link=topic.get("link") or None, cfg=cfg)
    imgs = render_cards(idea, content, a.media_dir, cfg, a.force)
    new_row = {"post_type": "images", "post_content": content["caption"], "post_comment": content["first_comment"],
               "post_date": slot, "media_url": "|".join(p.as_posix() for p in imgs)}

    errs = checks.check_draft([new_row], now, cfg=cfg)
    if errs:
        print("CHECKS FAILED:\n  " + "\n  ".join(errs), file=sys.stderr)
        return 1

    plan.append_rows(a.content, [new_row])
    topic["status"], topic["generated_on"] = "done", now.strftime("%Y-%m-%d")
    plan.write_plan(a.plan, plan_rows)

    summary = summary_md(idea, new_row, os.environ.get("GITHUB_REPOSITORY", ""), a.branch, plan.timezone_name())
    if fb.via != "none":
        summary += "\n" + feedback_md(fb)
    Path(a.summary).write_text(summary, encoding="utf-8")
    Path(a.summary).with_suffix(".dates").write_text(slot + "\n")
    Path(a.summary).with_suffix(".photo").write_text(imgs[0].as_posix())
    print(f"OK: drafted '{idea['title_en']}', post at {slot}")
    return 0


def cmd_ideas(a):
    """Weekly: ask Claude for post ideas; write the GitHub issue body and the Telegram text."""
    cfg = config.load()
    today = plan.now_local().strftime("%Y-%m-%d")
    plan_rows = plan.read_plan(a.plan)
    pending = sum(1 for r in plan_rows if r.get("status", "").strip() == "pending")
    if pending >= cfg["cadence"]["max_pending"]:
        print(f"SKIP: {pending} topics already pending (limit {cfg['cadence']['max_pending']})")
        return EXIT_SKIP
    if not llm.available():
        print("ERROR: ANTHROPIC_API_KEY is not set", file=sys.stderr)
        return 1
    recent = [r["post_content"].strip().splitlines()[0][:120] for r in plan.read_rows(a.content)[-15:]
              if r.get("post_content", "").strip()]
    held = ideas_mod.load_held(a.log, Path(a.ideas_dir) / "held", today, cfg)
    if held:
        print(f"Carrying over {len(held)} held idea(s): {[h['id'] for h in held]}")
    ideas, rejected = ideas_mod.propose(config.guidelines(cfg), plan_rows, ideas_mod.read_log(a.log), recent, today,
                                        a.count or cfg["cadence"]["ideas_per_batch"], held=held, cfg=cfg)
    if rejected:
        print("Dropped invalid ideas:", rejected)
    Path(a.issue_body).write_text(ideas_mod.issue_body(ideas, today, cfg), encoding="utf-8")
    Path(a.telegram_file).write_text(ideas_mod.telegram_text(ideas, "{ISSUE_URL}", today, cfg), encoding="utf-8")
    print(f"OK: {len(ideas)} ideas")
    return 0


def cmd_approve(a):
    """On /approve: record the decision, add approved ideas to the plan. The reply text goes to --result."""
    cfg = config.load()
    today = plan.now_local().strftime("%Y-%m-%d")
    try:
        ideas = ideas_mod.parse_payload(Path(a.issue_body).read_text(encoding="utf-8"), cfg)
        comment = Path(a.comment).read_text(encoding="utf-8")
        approved = ideas_mod.parse_command(comment, len(ideas))
        notes = ideas_mod.parse_notes(comment)
    except ValueError as e:
        Path(a.result).write_text(f"⚠️ {e}\n", encoding="utf-8")
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    chosen, held = ideas_mod.apply_approval(ideas, approved, a.plan, a.ideas_dir, a.log, today, notes)
    msg = []
    if chosen:
        msg += ["Approved — drafting starts now; you will get a pull request to review.", ""]
        msg += [f"- ✅ {i['title_en']} ({i['kind']})" for i in chosen]
        if notes:
            msg += ["", "Your instructions will be passed to the copywriter for these drafts."]
    if held:
        msg += ["", "Held — these come back in the next batch:", ""]
        msg += [f"- ⏸ {i['title_en']}" for i in held]
    n_rej = len(ideas) - len(chosen) - len(held)
    msg += ["", f"Rejected: {n_rej}."] if n_rej else []
    Path(a.result).write_text("\n".join(msg).strip() + "\n", encoding="utf-8")
    print(f"OK: approved {len(chosen)}, held {len(held)}, rejected {n_rej} of {len(ideas)}")
    return 0 if chosen else EXIT_SKIP  # 0 = something to draft; 3 = decisions recorded, nothing to draft


def cmd_tgcheck(a):
    """Diagnose Telegram: list the chats that messaged the bot, compare with TELEGRAM_CHAT_ID, send a test message."""
    try:
        found = telegram.chats()
        configured = os.environ.get("TELEGRAM_CHAT_ID", "")
        print("Chats that have messaged the bot (id, type, name):")
        for c in found:
            print(f"  {c[0]}  {c[1]}  {c[2]}" + ("   <-- matches TELEGRAM_CHAT_ID" if str(c[0]) == configured else ""))
        if not found:
            print("  (none: open the bot in Telegram, press Start, send a message, then run this again)")
        elif not any(str(c[0]) == configured for c in found):
            print("TELEGRAM_CHAT_ID matches none of them: set the secret to the id of your private chat above.")
        print("Test message:", "sent" if telegram.send("✅ Telegram notifications work") else "not configured")
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_notify(a):
    """Telegram message; never fails the workflow (a missing/broken notification must not block anything)."""
    text = Path(a.text_file).read_text(encoding="utf-8").strip()
    try:
        sent = telegram.send(text, a.photo if a.photo and Path(a.photo).is_file() else None)
    except RuntimeError as e:
        print(f"WARNING: {e}", file=sys.stderr)
        return 0
    print("Telegram message sent" if sent else "Telegram not configured; skipped")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("next", help="draft the next pending topic and schedule it")
    n.add_argument("--content", default="content.csv")
    n.add_argument("--plan", default="content-plan.csv")
    n.add_argument("--media-dir", default="media")
    n.add_argument("--force", action="store_true", help="overwrite existing media files")
    n.add_argument("--respect-queue", action="store_true",
                   help="skip when cadence.queue_below or more future posts are already scheduled")
    n.add_argument("--first-post", default="", help="'YYYY-MM-DD HH:MM' instead of the computed slot")
    n.add_argument("--feedback-file", default="", help="a /regenerate comment; Claude (or a 'date:' line) is read from it")
    n.add_argument("--ideas-dir", default="ideas")
    n.add_argument("--branch", default="")
    n.add_argument("--summary", default="draft-summary.md")
    n.set_defaults(fn=cmd_next)

    i = sub.add_parser("ideas", help="weekly: propose post ideas (Claude) for approval")
    i.add_argument("--content", default="content.csv")
    i.add_argument("--plan", default="content-plan.csv")
    i.add_argument("--log", default="ideas/log.csv")
    i.add_argument("--ideas-dir", default="ideas")
    i.add_argument("--count", type=int, default=0, help="ideas per batch (default: cadence.ideas_per_batch)")
    i.add_argument("--issue-body", default="ideas-issue.md")
    i.add_argument("--telegram-file", default="ideas-telegram.txt")
    i.set_defaults(fn=cmd_ideas)

    ap = sub.add_parser("approve", help="apply an /approve comment on an ideas issue")
    ap.add_argument("--plan", default="content-plan.csv")
    ap.add_argument("--log", default="ideas/log.csv")
    ap.add_argument("--ideas-dir", default="ideas")
    ap.add_argument("--issue-body", required=True)
    ap.add_argument("--comment", required=True)
    ap.add_argument("--result", default="approve-result.md")
    ap.set_defaults(fn=cmd_approve)

    tc = sub.add_parser("telegram-check", help="diagnose Telegram setup and send a test message")
    tc.set_defaults(fn=cmd_tgcheck)

    nt = sub.add_parser("notify", help="send a Telegram message (no-op if not configured)")
    nt.add_argument("--text-file", required=True)
    nt.add_argument("--photo", default="")
    nt.set_defaults(fn=cmd_notify)

    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except (ValueError, FileExistsError, FileNotFoundError, RuntimeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
