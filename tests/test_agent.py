import json
import os
import struct
import tempfile
import unittest
import zlib
from datetime import datetime
from pathlib import Path
from unittest import mock

import make_content
from generator import aicontent, checks, config, feedback, ideas, llm, plan, telegram

ROOT = Path(__file__).resolve().parent.parent
CFG = config.load(ROOT / "config.json")


def idea(**kw):
    d = {"kind": "tip", "title": "Plan a calm start to the day", "title_en": "Calm mornings",
         "angle": "A short routine makes mornings easier.", "points": ["Prepare the night before", "Keep it simple"]}
    d.update(kw)
    return d


def png(path, w=1080, h=1080):
    def chunk(t, b):
        return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b) & 0xFFFFFFFF)
    raw = b"\x00" + b"\xff\xff\xff" * w
    data = zlib.compress(raw * h)
    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                           + chunk(b"IDAT", data) + chunk(b"IEND", b""))


GOOD_COPY = {
    "slides": [{"heading": "Evening routine", "lines": ["Pack the bag at night", "Lay out clothes"]}],
    "caption": "A calm morning starts the night before.\nSee the first comment for more.",
    "first_comment": "More ideas here",
}


class ConfigTests(unittest.TestCase):
    def test_defaults_are_neutral_english(self):
        cfg = config.load(ROOT / "does-not-exist.json")
        self.assertEqual(cfg["language"], "English")
        self.assertEqual(cfg["model"], "claude-sonnet-5-5")
        self.assertEqual(cfg["required_script"], "")

    def test_override_merges_nested(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text(json.dumps({"cadence": {"post_hour": 8}, "kinds": {"quote": "Quotes"}}))
            cfg = config.load(p)
        self.assertEqual(cfg["cadence"]["post_hour"], 8)
        self.assertEqual(cfg["cadence"]["spacing_days"], 4)
        self.assertEqual(list(cfg["kinds"]), ["quote"])

    def test_broken_json_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text("{nope")
            with self.assertRaises(ValueError):
                config.load(p)


class CleanIdeaTests(unittest.TestCase):
    def test_good_idea_passes(self):
        self.assertIsNotNone(ideas.clean_idea(idea(), CFG)[0])

    def test_rejections(self):
        cases = [idea(kind="video"), idea(title="x"), idea(points=["one"]),
                 idea(angle="This will cure sleep problems"), idea(points=["a", "see https://x.com"]),
                 idea(title="A 100% guarantee"), idea(points=["visit example.com now", "b"])]
        for c in cases:
            self.assertIsNone(ideas.clean_idea(c, CFG)[0], c)

    def test_required_script_and_blocked_patterns_come_from_config(self):
        cfg = dict(CFG, required_script=r"[\u0600-\u06ff]", blocked_patterns=[r"\bfree money\b"])
        self.assertIsNone(ideas.clean_idea(idea(), cfg)[0])  # English title lacks the required script
        cfg2 = dict(CFG, blocked_patterns=[r"\broutine\b"])
        self.assertIsNone(ideas.clean_idea(idea(), cfg2)[0])


class ProposeTests(unittest.TestCase):
    def test_ids_dedupe_and_drops(self):
        raw = {"ideas": [idea(), idea(), idea(title_en="Second idea"), idea(title_en="Bad", angle="cure it")]}
        out, rejected = ideas.propose("g", [], [], [], "2026-11-15", call=lambda *a, **k: raw, cfg=CFG)
        self.assertEqual([i["id"] for i in out], ["20261115-1", "20261115-2"])
        self.assertEqual(len(rejected), 2)

    def test_nothing_usable_raises(self):
        with self.assertRaises(llm.LLMError):
            ideas.propose("g", [], [], [], "2026-11-15", call=lambda *a, **k: {"ideas": [idea(kind="x")]}, cfg=CFG)

    def test_second_batch_on_the_same_day_gets_new_ids(self):
        log = [{"id": "20261115-1", "kind": "tip", "title_en": "Old one", "status": "rejected", "decided_on": ""},
               {"id": "20261115-2", "kind": "tip", "title_en": "Old two", "status": "rejected", "decided_on": ""}]
        raw = {"ideas": [idea(title_en="Brand new A"), idea(title_en="Brand new B")]}
        out, _ = ideas.propose("g", [], log, [], "2026-11-15", call=lambda *a, **k: raw, cfg=CFG)
        self.assertEqual([i["id"] for i in out], ["20261115-3", "20261115-4"])


class ApprovalTests(unittest.TestCase):
    def batch(self):
        return [dict(idea(), id="20261115-1"), dict(idea(title_en="Tip number two"), id="20261115-2"),
                dict(idea(kind="myth", title_en="Myth three"), id="20261115-3")]

    def decide(self, **kw):
        d = {"approve": set(), "hold": set(), "reject": set()}
        d.update(kw)
        return d

    def test_payload_round_trip_and_tamper(self):
        body = ideas.issue_body(self.batch(), "2026-11-15", CFG)
        self.assertEqual(len(ideas.parse_payload(body, CFG)), 3)
        with self.assertRaises(ValueError):
            ideas.parse_payload("no payload", CFG)
        with self.assertRaises(ValueError):
            ideas.parse_payload(body.replace("ideas-payload:", "ideas-payload:AAAA"), CFG)

    def test_payload_is_revalidated(self):
        evil = [dict(idea(angle="This will cure everything"), id="20261115-1")]
        import base64
        body = f"<!-- ideas-payload:{base64.b64encode(json.dumps(evil).encode()).decode()} -->"
        with self.assertRaises(ValueError):
            ideas.parse_payload(body, CFG)

    def test_notes_are_the_lines_after_the_command(self):
        self.assertEqual(ideas.parse_notes("/approve 1\nSimple words\nShort"), "Simple words\nShort")
        self.assertEqual(ideas.parse_notes("/approve 1"), "")
        self.assertEqual(len(ideas.parse_notes("/approve 1\n" + "x" * 2000)), 600)

    def test_command_parsing(self):
        P = ideas.parse_command
        self.assertEqual(P("/approve 1,3", 5), {"approve": {1, 3}, "hold": set(), "reject": {2, 4, 5}})
        self.assertEqual(P("/approve 1,3 hold 2 reject 4", 5), {"approve": {1, 3}, "hold": {2}, "reject": {4, 5}})
        self.assertEqual(P("/approve hold 2, 4\nthanks", 5)["hold"], {2, 4})
        self.assertEqual(P("/approve none", 3), {"approve": set(), "hold": set(), "reject": {1, 2, 3}})
        for bad in ("/approve", "/approve 6", "approve 1", "/approve 1; rm -rf /", "", "/approve 1 hold 1",
                    "/approve hold", "/approve 1 maybe 2", "/approve $(id)"):
            with self.assertRaises(ValueError, msg=bad):
                P(bad, 5)

    def test_apply_writes_plan_files_and_log(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "plan.csv").write_text("kind,link,status,generated_on,idea\ntip,,done,2026-10-20,x\n")
            chosen, held = ideas.apply_approval(self.batch(), self.decide(approve={1, 3}, reject={2}), d / "plan.csv",
                                                d / "ideas", d / "ideas" / "log.csv", "2026-11-15", "be brief")
            self.assertEqual((len(chosen), len(held)), (2, 0))
            rows = plan.read_plan(d / "plan.csv")
            self.assertEqual([(r["kind"], r["status"], r["idea"]) for r in rows[1:]],
                             [("tip", "pending", "20261115-1"), ("myth", "pending", "20261115-3")])
            saved = json.loads((d / "ideas" / "20261115-1.json").read_text())
            self.assertEqual(saved["note"], "be brief")
            self.assertFalse((d / "ideas" / "20261115-2.json").exists())
            log = ideas.read_log(d / "ideas" / "log.csv")
            self.assertEqual([r["status"] for r in log], ["approved", "rejected", "approved"])

    def test_held_ideas_return_first_in_the_next_batch_and_expire(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "plan.csv").write_text("kind,link,status,generated_on,idea\n")
            log = d / "ideas" / "log.csv"
            _, held = ideas.apply_approval(self.batch(), self.decide(approve={1}, hold={2}, reject={3}),
                                           d / "plan.csv", d / "ideas", log, "2026-11-15")
            self.assertEqual([h["id"] for h in held], ["20261115-2"])
            back = ideas.load_held(log, d / "ideas" / "held", "2026-11-22", CFG)
            self.assertEqual([h["id"] for h in back], ["20261115-2"])
            fresh = {"ideas": [idea(title_en=f"New idea {k}") for k in range(5)]}
            out, _ = ideas.propose("g", [], ideas.read_log(log), [], "2026-11-22", n=5,
                                   call=lambda *a, **k: fresh, held=back, cfg=CFG)
            self.assertEqual(len(out), 5)  # 1 held + 4 new
            self.assertEqual(out[0]["id"], "20261115-2")
            self.assertEqual(len({i["id"] for i in out}), 5)
            self.assertIn("held from an earlier batch", ideas.issue_body(out, "2026-11-22", CFG))
            ideas.apply_approval(out, self.decide(approve={1}, reject={2, 3, 4, 5}), d / "plan.csv", d / "ideas", log, "2026-11-22")
            self.assertFalse((d / "ideas" / "held" / "20261115-2.json").exists())
            self.assertEqual(ideas.load_held(log, d / "ideas" / "held", "2026-11-29", CFG), [])

    def test_third_hold_rejects(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "plan.csv").write_text("kind,link,status,generated_on,idea\n")
            log = d / "ideas" / "log.csv"
            one = [dict(idea(), id="20261101-1")]
            for day in ("2026-11-01", "2026-11-08", "2026-11-15"):
                ideas.apply_approval(one, self.decide(hold={1}), d / "plan.csv", d / "ideas", log, day)
            self.assertEqual(ideas.load_held(log, d / "ideas" / "held", "2026-11-22", CFG), [])
            self.assertEqual(ideas.read_log(log)[-1]["status"], "rejected")


class AiContentTests(unittest.TestCase):
    def test_disclaimer_hashtags_and_link_come_from_config(self):
        cfg = dict(CFG, disclaimer="Not advice.", hashtags="#tips #daily", default_link="https://example.org/more")
        out = aicontent.write("g", idea(id="x"), call=lambda *a, **k: GOOD_COPY, cfg=cfg)
        self.assertTrue(out["caption"].endswith("Not advice.\n#tips #daily"))
        self.assertTrue(out["first_comment"].endswith("https://example.org/more"))

    def test_no_link_no_disclaimer_when_unset(self):
        cfg = dict(CFG, default_link="", disclaimer="", hashtags="")
        out = aicontent.write("g", idea(id="x"), call=lambda *a, **k: GOOD_COPY, cfg=cfg)
        self.assertEqual(out["first_comment"], "More ideas here")
        self.assertEqual(out["caption"], GOOD_COPY["caption"])

    def test_link_in_first_comment_is_rejected_by_validate(self):
        self.assertTrue(aicontent.validate(dict(GOOD_COPY, first_comment="See https://example.com"), CFG))
        self.assertTrue(aicontent.validate(dict(GOOD_COPY, first_comment="See example.com/x"), CFG))

    def test_link_written_by_the_model_is_stripped_and_added_once(self):
        copy = dict(GOOD_COPY, first_comment="More ideas ➡ https://other.example/tips")
        out = aicontent.write("g", idea(id="x"), call=lambda *a, **k: copy, cfg=dict(CFG, default_link="https://example.org"))
        self.assertEqual(out["first_comment"].count("http"), 1)
        self.assertTrue(out["first_comment"].endswith("https://example.org"))

    def test_model_hashtag_lines_are_removed(self):
        copy = dict(GOOD_COPY, caption="Nice text.\n#random #tags")
        out = aicontent.write("g", idea(id="x"), call=lambda *a, **k: copy, cfg=dict(CFG, hashtags="#mine"))
        self.assertNotIn("#random", out["caption"])
        self.assertIn("#mine", out["caption"])

    def test_claims_are_blocked_then_retry_fails(self):
        bad = dict(GOOD_COPY, caption="This will cure your problems")
        calls = []
        def call(*a, **k):
            calls.append(1)
            return bad
        with self.assertRaises(llm.LLMError):
            aicontent.write("g", idea(id="x"), call=call, cfg=CFG)
        self.assertEqual(len(calls), 3)

    def test_retry_can_succeed(self):
        replies = iter([dict(GOOD_COPY, caption="100% guaranteed"), GOOD_COPY])
        self.assertIn("slides", aicontent.write("g", idea(id="x"), call=lambda *a, **k: next(replies), cfg=CFG))

    def test_links_in_text_and_overlong_text_flagged(self):
        self.assertTrue(aicontent.validate(dict(GOOD_COPY, caption="See https://x.com"), CFG))
        self.assertTrue(aicontent.validate(dict(GOOD_COPY, slides=[{"heading": "H", "lines": ["x" * 200]}]), CFG))

    def test_feedback_is_fenced_in_the_prompt(self):
        _, user = aicontent.build_prompt("g", idea(id="x"), CFG, "make it shorter")
        self.assertIn("<feedback>\nmake it shorter\n</feedback>", user)


class FeedbackTests(unittest.TestCase):
    NOW = datetime(2026, 11, 12, 15, 0)

    def test_claude_reply_is_validated(self):
        call = lambda *a, **k: {"first_post": "2026-11-13 00:00", "copy_notes": "shorter"}
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}):
            r = feedback.interpret("post on 13 Nov 12am, shorter", self.NOW, call=call)
        self.assertEqual((r.first_post, r.copy_notes, r.via), ("2026-11-13 00:00", "shorter", "claude"))

    def test_past_or_garbage_dates_are_dropped(self):
        for bad in ("2026-11-01 10:00", "tomorrow", "2026-13-45 99:99"):
            call = lambda *a, bad=bad, **k: {"first_post": bad, "copy_notes": ""}
            with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}):
                r = feedback.interpret("x", self.NOW, call=call)
            self.assertEqual(r.first_post, "")
            self.assertIn("ignored", r.copy_notes)

    def test_without_key_the_date_line_still_works(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            r = feedback.interpret("please\ndate: 2026-11-20 09:30\nthanks", self.NOW)
        self.assertEqual((r.first_post, r.via), ("2026-11-20 09:30", "date-line"))

    def test_api_failure_falls_back(self):
        def boom(*a, **k):
            raise llm.LLMError("down")
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}):
            r = feedback.interpret("date: 2026-11-20", self.NOW, call=boom)
        self.assertEqual(r.first_post, "2026-11-20 12:00")

    def test_empty(self):
        self.assertEqual(feedback.interpret("  ", self.NOW).via, "none")


class SyncDraftsTests(unittest.TestCase):
    def test_append_only_merge_keeps_both_sets_of_rows(self):
        import sync_drafts as sd
        base, main, mine = b"h\r\nA\r\n", b"h\r\nA\r\nB\r\n", b"h\r\nA\r\nC\r\n"
        self.assertEqual(sd.append_only_merge(base, mine, main), b"h\r\nA\r\nB\r\nC\r\n")

    def test_append_only_merge_refuses_edited_rows(self):
        import sync_drafts as sd
        self.assertIsNone(sd.append_only_merge(b"h\nA\n", b"h\nA2\nC\n", b"h\nA\nB\n"))


class ClientTests(unittest.TestCase):
    class R:
        def __init__(self, code, body=None):
            self.status_code, self._b = code, body or {}
        def json(self):
            return self._b

    def test_returns_tool_input_uses_auto_choice_and_configured_model(self):
        seen = {}
        def post(url, headers, json, timeout):
            seen.update(json=json, headers=headers)
            return self.R(200, {"content": [{"type": "tool_use", "name": "t", "input": {"a": 1}}]})
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sekret"}):
            self.assertEqual(llm.call_json("s", "u", {"type": "object"}, tool_name="t", post=post), {"a": 1})
        self.assertIn("`t` tool", seen["json"]["system"])
        self.assertEqual(seen["json"]["tool_choice"], {"type": "auto"})
        self.assertEqual(seen["json"]["model"], "claude-sonnet-5-5")
        self.assertEqual(seen["json"]["tools"][0]["name"], "t")
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k", "CLAUDE_MODEL": "other-model"}):
            llm.call_json("s", "u", {}, tool_name="t", post=post)
        self.assertEqual(seen["json"]["model"], "other-model")

    def test_retries_then_succeeds_and_hard_errors_stop_without_key(self):
        codes = iter([429, 200])
        def post(*a, **k):
            return self.R(next(codes), {"content": [{"type": "tool_use", "name": "submit", "input": {}}]})
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "SECRETKEY"}):
            self.assertEqual(llm.call_json("s", "u", {}, post=post, sleep=lambda s: None), {})
            with self.assertRaises(llm.LLMError) as cm:
                llm.call_json("s", "u", {}, post=lambda *a, **k: self.R(401), sleep=lambda s: None)
        self.assertNotIn("SECRETKEY", str(cm.exception))

    def test_no_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(llm.LLMError):
                llm.call_json("s", "u", {})


class TelegramTests(unittest.TestCase):
    def test_noop_without_config(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(telegram.send("hi"))

    def test_sends_and_never_leaks_token(self):
        class R:
            status_code = 403
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "TOKEN123", "TELEGRAM_CHAT_ID": "9"}):
            sent = {}
            ok = telegram.send("hello", post=lambda url, **k: sent.update(url=url, data=k["data"]) or type("R", (), {"status_code": 200})())
            self.assertTrue(ok)
            self.assertEqual(sent["data"]["chat_id"], "9")
            with self.assertRaises(RuntimeError) as cm:
                telegram.send("x", post=lambda *a, **k: R())
            import requests
            def boom(*a, **k):
                raise requests.ConnectionError("https://api.telegram.org/botTOKEN123/sendMessage")
            with self.assertRaises(RuntimeError) as cm2:
                telegram.send("x", post=boom)
        self.assertNotIn("TOKEN123", str(cm.exception))
        self.assertNotIn("TOKEN123", str(cm2.exception))
        self.assertNotIn("TOKEN123", repr(cm2.exception.__cause__))


class PlanTests(unittest.TestCase):
    def test_next_slots_respect_lead_and_spacing(self):
        now = datetime(2026, 11, 12, 15, 0)
        rows = [{"post_date": "2026-11-20 12:00"}]
        self.assertEqual(plan.next_slots(rows, now, 4, 3, 12, 2), ["2026-11-24 12:00", "2026-11-28 12:00"])
        self.assertEqual(plan.next_slots([], now, 4, 3, 12, 1), ["2026-11-16 12:00"])

    def test_append_rows_keeps_existing_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.csv"
            p.write_bytes(b"post_type,post_content\nA,b\n")
            plan.append_rows(p, [{"post_type": "x", "post_content": "y"}])
            self.assertEqual(p.read_bytes(), b"post_type,post_content\nA,b\nx,y\n")


class EndToEndTests(unittest.TestCase):
    """`make_content next` for an approved tip: copy from a fake Claude, cards from a fake renderer."""

    def test_draft_rows_plan_and_summary(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "ideas").mkdir()
            (d / "ideas" / "20261115-1.json").write_text(json.dumps(dict(idea(), note="calm tone")))
            (d / "plan.csv").write_text("kind,link,status,generated_on,idea\ntip,,pending,,20261115-1\n")
            (d / "content.csv").write_text("post_type,post_content,post_comment,post_date,media_url\n")
            fb = d / "fb.txt"
            fb.write_text("post on 20 Nov 9:30am")
            prompts = []

            def fake_shot(jobs, work):
                for out, w, h, doc in jobs:
                    png(out, w, h)

            def fake_call(system, user, schema, tool_name="submit", **k):
                prompts.append(user)
                if tool_name == "submit_settings":
                    return {"first_post": "2099-11-20 09:30", "copy_notes": "shorter"}
                return GOOD_COPY

            args = ["next", "--media-dir", str(d / "media"), "--content", str(d / "content.csv"),
                    "--plan", str(d / "plan.csv"), "--ideas-dir", str(d / "ideas"), "--summary", str(d / "sum.md"),
                    "--feedback-file", str(fb), "--branch", "b"]
            with mock.patch("generator.render.screenshot_all", fake_shot), \
                 mock.patch("generator.llm.call_json", fake_call), \
                 mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k", "GEN_NOW": "2026-11-12 10:00"}):
                rc = make_content.main(args)
            self.assertEqual(rc, 0)
            rows = plan.read_rows(d / "content.csv")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["post_type"], "images")
            self.assertEqual(rows[0]["post_date"], "2099-11-20 09:30")
            self.assertEqual(len(rows[0]["media_url"].split("|")), 2)
            self.assertEqual(plan.read_plan(d / "plan.csv")[0]["status"], "done")
            self.assertTrue(any("calm tone" in p and "shorter" in p for p in prompts))
            summary = (d / "sum.md").read_text(encoding="utf-8")
            self.assertIn("How your feedback was applied", summary)
            self.assertIn("2099-11-20 09:30", summary)

    def test_empty_backlog_and_full_queue_exit_codes(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "plan.csv").write_text("kind,link,status,generated_on,idea\n")
            (d / "content.csv").write_text("post_type,post_content,post_comment,post_date,media_url\n")
            base = ["next", "--content", str(d / "content.csv"), "--plan", str(d / "plan.csv"), "--summary", str(d / "s.md")]
            self.assertEqual(make_content.main(base), make_content.EXIT_EMPTY)
            (d / "content.csv").write_text("post_type,post_content,post_comment,post_date,media_url\n"
                                           "text,a,,2099-01-01 10:00,\ntext,b,,2099-01-02 10:00,\n")
            self.assertEqual(make_content.main(base + ["--respect-queue"]), make_content.EXIT_SKIP)


class CheckDraftTests(unittest.TestCase):
    def test_wrong_size_and_past_date_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            png(Path(d) / "a.png", 100, 100)
            row = {"post_type": "images", "post_content": "hi", "post_comment": "", "post_date": "2000-01-01 10:00",
                   "media_url": "a.png|b.png"}
            errs = checks.check_draft([row], datetime(2026, 1, 1), root=d, cfg=CFG)
            self.assertEqual(len(errs), 3)


if __name__ == "__main__":
    unittest.main()
