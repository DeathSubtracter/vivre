"""Run with: .venv/bin/python -m unittest discover tests   (stubbed models, no API calls, no cost)"""
import os, sys, tempfile, threading, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MODEL_API_KEY"] = "test-only"
os.environ["SPEND_FILE"] = os.path.join(tempfile.mkdtemp(), "spend.json")

import meta, server
from budget import Budget
from ratelimit import RateLimiter
from timers import RuleTimer


class TimerTests(unittest.TestCase):
    def test_goes_ok_watch_due_after_duration(self):
        r = RuleTimer(10)
        self.assertEqual(r.step(False, 0), ("ok", 0.0))
        self.assertEqual(r.step(True, 3), ("watch", 0))
        self.assertEqual(r.step(True, 9), ("watch", 6))
        self.assertEqual(r.step(True, 13), ("due", 10))

    def test_one_stray_no_keeps_the_clock_two_reset_it(self):
        r = RuleTimer(10)
        r.step(True, 0)
        self.assertEqual(r.step(False, 3), ("watch", 3))
        self.assertEqual(r.step(True, 6), ("watch", 6))
        r.step(False, 9)
        self.assertEqual(r.step(False, 12), ("ok", 0.0))
        self.assertIsNone(r.since)

    def test_a_no_on_the_due_check_does_not_fire(self):
        r = RuleTimer(5)
        r.step(True, 0)
        self.assertEqual(r.step(False, 6), ("watch", 6))

    def test_alert_stays_until_clearly_over_and_can_fire_again(self):
        r = RuleTimer(0)
        self.assertEqual(r.step(True, 0)[0], "due")
        r.confirmed()
        self.assertEqual(r.step(True, 3)[0], "alert")
        self.assertEqual(r.step(False, 6)[0], "alert")
        self.assertEqual(r.step(False, 9)[0], "ok")
        self.assertEqual(r.step(True, 12)[0], "due")

    def test_rejected_waits_before_asking_wonder_again(self):
        r = RuleTimer(0)
        r.step(True, 0)
        r.rejected(0)
        self.assertEqual(r.step(True, 3)[0], "watch")
        self.assertEqual(r.step(True, 16)[0], "due")


class SpecTests(unittest.TestCase):
    good = {"title": "Cup", "subjects": ["cup"], "question": "Is a cup on the table?", "duration_s": 10}

    def test_good_spec(self):
        self.assertEqual(server.validate_spec(self.good, "s")["duration_s"], 10)

    def test_bad_specs_rejected(self):
        for bad in (None, [], "x", {"question": 5}, {"question": "no"}, {**self.good, "duration_s": "soon"}):
            self.assertIsNone(server.validate_spec(bad, "s"), bad)

    def test_limits_are_enforced(self):
        s = server.validate_spec({**self.good, "duration_s": 9999, "subjects": ["a", "b", "c", "d", 5, "x" * 99]}, "s")
        self.assertEqual(s["duration_s"], 120)
        self.assertEqual(len(s["subjects"]), 3)
        self.assertEqual(server.validate_spec({**self.good, "duration_s": -4}, "s")["duration_s"], 0)


class ParseTests(unittest.TestCase):
    def test_count_boxes_on_documented_shape(self):
        one = "<|box;x1=1;y1=2;x2=3;y2=4;w=9;h=9|><|mask;x=0;y=0;data=1,1,~a|>"
        self.assertEqual(meta.count_boxes(""), 0)
        self.assertEqual(meta.count_boxes(f"<0f>0{one},2{one}"), 2)

    def test_jparse_survives_bad_replies(self):
        for bad in (None, "", "no json", "{broken", "[1,2]"):
            self.assertIsNone(meta.jparse(bad))
        self.assertEqual(meta.jparse('text {"a":1} text'), {"a": 1})


class CompileTests(unittest.TestCase):
    good = {"question": "Is a cup there?", "subjects": ["cup"], "duration_s": 1}

    def test_a_bad_reply_gets_one_retry(self):
        with mock.patch.object(meta, "compile_rule", side_effect=[None, self.good]) as c:
            self.assertEqual(server.compile_valid("s", None)["question"], "Is a cup there?")
        self.assertEqual(c.call_count, 2)

    def test_gives_up_after_two_bad_replies(self):
        with mock.patch.object(meta, "compile_rule", return_value=None) as c:
            self.assertIsNone(server.compile_valid("s", None))
        self.assertEqual(c.call_count, 2)


class RetryTests(unittest.TestCase):
    class Status(Exception):
        def __init__(self, code): self.status_code = code

    def test_404s_are_retried_until_one_works(self):
        replies = [self.Status(404), self.Status(404), "ok"]
        def call():
            r = replies.pop(0)
            if isinstance(r, Exception): raise r
            return r
        with mock.patch.object(meta.time, "sleep"):
            self.assertEqual(meta.try_past_404(call), "ok")

    def test_404s_stop_eventually(self):
        call = mock.Mock(side_effect=self.Status(404))
        with mock.patch.object(meta.time, "sleep"), self.assertRaises(self.Status):
            meta.try_past_404(call)
        self.assertEqual(call.call_count, meta.NOT_FOUND_TRIES)

    def test_other_errors_are_not_retried(self):
        call = mock.Mock(side_effect=self.Status(401))
        with self.assertRaises(self.Status):
            meta.try_past_404(call)
        self.assertEqual(call.call_count, 1)


class BudgetTests(unittest.TestCase):
    def test_each_cap_trips(self):
        b = Budget(0.5, 2, 3)
        self.assertIsNone(b.blocked("a"))
        self.assertIn("session", b.blocked("a", 0.5))
        b.charge("a", 2)
        self.assertIn("today", b.blocked("a"))
        self.assertIsNone(b.blocked("b"))
        b.charge("b", 1)
        self.assertIn("used up", b.blocked("c"))

    def test_total_survives_restart_and_bad_file_stops_spending(self):
        p = os.path.join(tempfile.mkdtemp(), "s.json")
        Budget(1, 1, 5, p).charge("a", 4)
        self.assertEqual(Budget(1, 1, 5, p).total, 4)
        open(p, "w").write("garbage")
        self.assertIsNotNone(Budget(1, 1, 5, p).blocked("a"))


class RateTests(unittest.TestCase):
    def test_window(self):
        r = RateLimiter()
        self.assertTrue(r.allow("k", 2, 60, now=0))
        self.assertTrue(r.allow("k", 2, 60, now=1))
        self.assertFalse(r.allow("k", 2, 60, now=2))
        self.assertTrue(r.allow("k", 2, 60, now=61))


def session(need=6):
    spec = {"title": "T", "subjects": [], "question": "Q?", "duration_s": need}
    return {"specs": [spec], "timers": [RuleTimer(need)], "expl": [""], "t0": 0, "seen": 0, "last": None, "last_call": 0,
            "last_tick": 0, "n": 0, "led": meta.Ledger(), "ip": "x", "frames": __import__("collections").deque(maxlen=3),
            "counts": {}, "seen_at": {}, "boxes": {}, "audio": None, "sam_pending": 0, "sam_ok": None, "sam_ms": None, "lock": threading.Lock(), "out": None,
            "cards": [{"status": "ok", "held": 0, "need": need, "note": ""}], "stages": {}}


def yes(v): return [{"yes": v, "note": "n"}]


class CheckFlowTests(unittest.TestCase):
    def test_alert_flow_with_wonder_confirming(self):
        S = session()
        with mock.patch.object(meta, "check_frame", return_value=yes(True)), \
             mock.patch.object(meta, "confirm", return_value={"confirm": True, "explanation": "Why"}) as c:
            self.assertEqual(server.run_check(S, b"j", 1, 3)["cards"][0]["status"], "watch")
            self.assertEqual(server.run_check(S, b"j", 1, 6)["cards"][0]["held"], 3)
            out = server.run_check(S, b"j", 1, 9)
            self.assertEqual(out["cards"][0]["status"], "alert")
            self.assertEqual(out["new_alerts"][0]["say"], "Why")
            self.assertEqual(server.run_check(S, b"j", 1, 12)["new_alerts"], [])
            self.assertEqual(c.call_count, 1)

    def test_wonder_saying_no_prevents_alert(self):
        S = session(0)
        with mock.patch.object(meta, "check_frame", return_value=yes(True)), \
             mock.patch.object(meta, "confirm", return_value={"confirm": False, "explanation": "Nope"}):
            out = server.run_check(S, b"j", 1, 3)
        self.assertEqual(out["cards"][0]["status"], "ok")
        self.assertEqual(out["new_alerts"], [])

    def test_wonder_failure_alerts_and_says_so(self):
        S = session(0)
        with mock.patch.object(meta, "check_frame", return_value=yes(True)), \
             mock.patch.object(meta, "confirm", side_effect=ValueError("bad")):
            out = server.run_check(S, b"j", 1, 3)
        self.assertEqual(out["cards"][0]["status"], "alert")
        self.assertEqual(out["stages"]["confirm"]["state"], "unavailable")

    def test_spark_failure_is_visible_and_freezes_timers(self):
        S = session()
        with mock.patch.object(meta, "check_frame", return_value=yes(True)):
            server.run_check(S, b"j", 1, 3)
        with mock.patch.object(meta, "check_frame", side_effect=RuntimeError("down")):
            out = server.run_check(S, b"j", 1, 6)
        self.assertTrue(out["problem"])
        self.assertEqual(S["timers"][0].misses, 0)

    def test_unusable_reply_is_visible(self):
        S = session()
        with mock.patch.object(meta, "check_frame", return_value=None):
            out = server.run_check(S, b"j", 1, 3)
        self.assertTrue(out["problem"])
        self.assertEqual(out["cards"][0]["status"], "ok")

    def test_still_frame_skips_the_model(self):
        S = session()
        with mock.patch.object(meta, "check_frame", return_value=yes(False)) as c:
            server.run_check(S, b"j", 1, 3)
            out = server.run_check(S, b"j", 0, 6)
            self.assertEqual(c.call_count, 1)
            self.assertTrue(out["reused"])
            self.assertEqual(out["skipped"], 1)
            server.run_check(S, b"j", 0, 16)  # too long since a real look
            self.assertEqual(c.call_count, 2)

    def test_sam_failure_shows_and_check_continues(self):
        S = session()
        S["specs"][0]["subjects"] = ["cup"]
        pool = lambda: __import__("concurrent.futures").futures.ThreadPoolExecutor(max_workers=2)
        with mock.patch.object(meta, "segment", return_value=None), \
             mock.patch.object(meta, "check_frame", return_value=yes(False)):
            server.start_sam(S, b"j")
            server.sam_pool.shutdown(wait=True)
            server.sam_pool = pool()
            out = server.run_check(S, b"j", 1, 3)
            server.sam_pool.shutdown(wait=True)  # finish inside the stub so nothing reaches the network
            server.sam_pool = pool()
        self.assertEqual(out["stages"]["see"]["state"], "unavailable")


class SamDecidesTests(unittest.TestCase):
    def test_boxes_are_scaled_to_the_frame(self):
        one = "<|box;x1=160;y1=120;x2=480;y2=360;w=640;h=480;c=0.9|><|mask;x=0;y=0;data=1,1,~a|>"
        self.assertEqual(meta.parse_boxes(f"<0f>0{one}"), [[0.25, 0.25, 0.75, 0.75]])
        self.assertEqual(meta.parse_boxes(""), [])

    def test_conditions_are_validated(self):
        s = server.validate_spec({**SpecTests.good, "conditions": [
            {"subject": "cup", "min": 1}, {"subject": "ghost", "min": 1}, {"subject": "cup", "max": "no"}, "x",
            {"subject": "cup", "max": 99}]}, "s")
        self.assertEqual(s["conditions"], [{"subject": "cup", "min": 1, "max": None}])
        self.assertFalse(s["uses_sound"])

    def test_sam_can_overrule_spark(self):
        S = session(0)
        S["specs"][0]["conditions"] = [{"subject": "cup", "min": 1, "max": None}]
        S["counts"], S["seen_at"], S["boxes"] = {"cup": 0}, {"cup": 3}, {"cup": []}
        with mock.patch.object(meta, "check_frame", return_value=yes(True)), \
             mock.patch.object(meta, "confirm") as c, mock.patch.object(server, "start_sam"):
            out = server.run_check(S, b"j", 1, 3)
        self.assertEqual(out["cards"][0]["status"], "ok")
        self.assertIn("SAM counts 0 cup", out["cards"][0]["note"])
        self.assertTrue(out["stages"]["see"]["overruled"])
        c.assert_not_called()

    def test_stale_counts_decide_nothing(self):
        spec = {"conditions": [{"subject": "person", "min": None, "max": 0}]}
        self.assertIsNone(server.unmet_condition(spec, {"person": 1}, {"person": 0}, server.FRESH_S + 1))
        self.assertIn("person", server.unmet_condition(spec, {"person": 1}, {"person": 0}, 1))

    def test_fresh_boxes_are_returned_and_stale_ones_dropped(self):
        S = session()
        S["counts"], S["seen_at"], S["boxes"] = {"cup": 1, "pot": 1}, {"cup": 100, "pot": 1}, {"cup": [[0, 0, 1, 1]], "pot": [[0, 0, 1, 1]]}
        with mock.patch.object(meta, "check_frame", return_value=yes(False)), mock.patch.object(server, "start_sam"):
            out = server.run_check(S, b"j", 1, 101)
        self.assertEqual([b["label"] for b in out["boxes"]], ["cup"])


class SoundTests(unittest.TestCase):
    def test_sound_rule_gets_the_clip_and_is_never_skipped_as_still(self):
        S = session()
        S["specs"][0]["uses_sound"], S["audio"] = True, b"RIFFxxxxWAVE"
        with mock.patch.object(meta, "check_frame", return_value=yes(False)) as c, mock.patch.object(server, "start_sam"):
            server.run_check(S, b"j", 1, 3)
            server.run_check(S, b"j", 0, 6)
        self.assertEqual(c.call_count, 2)
        self.assertEqual(c.call_args.kwargs["audio"], b"RIFFxxxxWAVE")

    def test_other_rules_never_send_sound(self):
        S = session()
        S["audio"] = b"RIFFxxxxWAVE"
        with mock.patch.object(meta, "check_frame", return_value=yes(False)) as c, mock.patch.object(server, "start_sam"):
            server.run_check(S, b"j", 1, 3)
        self.assertIsNone(c.call_args.kwargs["audio"])

    def test_motion_rule_sends_two_frames(self):
        S = session()
        S["specs"][0]["uses_motion"] = True
        with mock.patch.object(meta, "check_frame", return_value=yes(False)) as c, mock.patch.object(server, "start_sam"):
            server.run_check(S, b"a", 1, 3)
            server.run_check(S, b"b", 1, 6)
        self.assertEqual(c.call_args.args[1], [b"a", b"b"])


if __name__ == "__main__":
    unittest.main()
