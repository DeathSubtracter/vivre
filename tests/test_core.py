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
    def test_every_example_button_has_a_valid_saved_rule_and_skips_the_model(self):
        import re
        html = open(os.path.join(os.path.dirname(__file__), "..", "static", "index.html")).read()
        chips = re.findall(r'data-rule="([^"]+)"', html)
        self.assertEqual(len(chips), 6)
        with mock.patch.object(meta, "compile_rule") as model:
            for chip in chips:
                self.assertTrue(server.compile_valid(chip, None), chip)
        model.assert_not_called()
        self.assertEqual(server.saved_rule("Tell me if you hear clapping.")["sound_labels"], ["Clapping"])
        self.assertIsNone(server.saved_rule("Tell me if a dog barks"))

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


class SecondDeviceTests(unittest.TestCase):
    def test_alerts_reach_the_log_and_the_view_has_no_frames_or_costs(self):
        S = session(0)
        with mock.patch.object(meta, "check_frame", return_value=yes(True)), mock.patch.object(server, "start_sam"), \
             mock.patch.object(meta, "confirm", return_value={"confirm": True, "explanation": "Why"}):
            out = server.run_check(S, b"j", 1, 3)
        self.assertEqual(out["new_alerts"][0]["id"], 1)
        view = server.watch_view(S)
        self.assertEqual(view["log"][0]["say"], "Why")
        self.assertEqual(set(view), {"cards", "states", "log"})
        self.assertNotIn("j", str(view["cards"]))


class HearingTests(unittest.TestCase):
    def test_visitor_data_is_cleaned(self):
        self.assertEqual(server.parse_heard('{"ms":12,"scores":{"Clapping":0.8}}'), {"ms": 12, "scores": {"Clapping": 0.8}})
        for bad in ("nope", "[]", '{"scores":5}', '{"scores":{"Clapping":"x"}}'):
            self.assertIsNone(server.parse_heard(bad), bad)
        self.assertEqual(server.parse_heard('{"scores":{"Clapping":9}}')["scores"], {})

    def test_labels_are_limited_to_the_recognizer_list(self):
        s = server.validate_spec({**SpecTests.good, "sound_labels": ["Clapping", "Made up"]}, "s")
        self.assertEqual(s["sound_labels"], ["Clapping"])
        self.assertEqual(server.validate_spec({**SpecTests.good, "sound_labels": "Clapping"}, "s")["sound_labels"], [])

    def test_the_recognizer_fires_the_rule_without_asking_spark(self):
        S = session(0)
        S["specs"][0].update(sound_labels=["Clapping"], uses_sound=True)
        S["heard"] = {"ms": 5, "scores": {"Clapping": 0.7, "Speech": 0.1}}
        with mock.patch.object(meta, "check_frame") as spark, mock.patch.object(server, "start_sam"), \
             mock.patch.object(server, "explain_later") as later:
            out = server.run_check(S, b"j", 1, 3)
        spark.assert_not_called()
        self.assertEqual(out["cards"][0]["status"], "alert")
        self.assertIn("clapping", out["new_alerts"][0]["say"])
        later.assert_called_once()
        self.assertEqual(out["stages"]["listen"]["top"], "Clapping 70%")

    def test_a_low_score_stays_quiet_and_other_rules_still_use_spark(self):
        S = session(0)
        S["specs"].append({"title": "T", "subjects": [], "question": "Q2?", "duration_s": 0})
        S["specs"][0].update(sound_labels=["Clapping"], uses_sound=True)
        S["timers"].append(RuleTimer(0)); S["cards"].append(dict(S["cards"][0])); S["expl"].append("")
        S["heard"] = {"ms": 5, "scores": {"Clapping": 0.1}}
        with mock.patch.object(meta, "check_frame", return_value=yes(False)) as spark, mock.patch.object(server, "start_sam"):
            out = server.run_check(S, b"j", 1, 3)
        self.assertEqual(spark.call_args.args[0], ["Q2?"])
        self.assertEqual([c["status"] for c in out["cards"]], ["ok", "ok"])

    def test_a_clap_scored_as_hands_counts_but_a_weak_one_does_not(self):
        spec = {"sound_labels": ["Clapping"]}
        self.assertEqual(server.heard_match(spec, {"scores": {"Hands": 0.8}}), ("Clapping", 0.8))
        self.assertIsNone(server.heard_match(spec, {"scores": {"Hands": 0.3}}))
        self.assertIsNone(server.heard_match({"sound_labels": ["Bark"]}, {"scores": {"Hands": 0.9}}))

    def test_alert_stays_up_for_a_short_sound_and_can_fire_again_later(self):
        S = session(0)
        S["specs"][0].update(sound_labels=["Clapping"], uses_sound=True)
        with mock.patch.object(server, "start_sam"), mock.patch.object(server, "explain_later"):
            S["heard"] = {"ms": 1, "scores": {"Clapping": 0.9}}
            server.run_check(S, b"j", 1, 3)
            S["heard"] = {"ms": 1, "scores": {}}
            self.assertEqual(server.run_check(S, b"j", 1, 6)["cards"][0]["status"], "alert")
            self.assertEqual(server.run_check(S, b"j", 1, 20)["cards"][0]["status"], "alert")
            server.run_check(S, b"j", 1, 34)
            self.assertEqual(server.run_check(S, b"j", 1, 37)["cards"][0]["status"], "ok")
            S["heard"] = {"ms": 1, "scores": {"Clapping": 0.9}}
            self.assertEqual(len(server.run_check(S, b"j", 1, 40)["new_alerts"]), 1)

    def test_a_second_burst_after_a_quiet_gap_is_a_second_alert(self):
        S = session(0)
        S["specs"][0].update(sound_labels=["Clapping"], uses_sound=True)
        hit, quiet = {"ms": 1, "scores": {"Hands": 0.9}}, {"ms": 1, "scores": {}}
        def check(heard, t):
            S["heard"] = heard
            return server.run_check(S, b"j", 1, t)
        with mock.patch.object(server, "start_sam"), mock.patch.object(server, "explain_later"):
            self.assertEqual(len(check(hit, 3)["new_alerts"]), 1)
            self.assertEqual(check(hit, 6)["new_alerts"], [])     # one long burst stays one incident
            self.assertEqual(check(quiet, 9)["cards"][0]["status"], "alert")
            self.assertEqual(check(hit, 10)["new_alerts"], [])    # too soon after the first
            check(quiet, 11)
            second = check(hit, 14)
            self.assertEqual([a["id"] for a in second["new_alerts"]], [2])
            self.assertEqual(second["cards"][0]["status"], "alert")
            self.assertEqual(len(second["log"]), 2)

    def test_a_camera_rule_still_fires_only_once_until_it_clears(self):
        S = session(0)
        with mock.patch.object(meta, "check_frame", return_value=yes(True)), mock.patch.object(server, "start_sam"), \
             mock.patch.object(meta, "confirm", return_value={"confirm": True, "explanation": "Why"}) as wonder:
            for t in (3, 6, 30): server.run_check(S, b"j", 1, t)
        self.assertEqual(wonder.call_count, 1)

    def test_wonder_explains_afterward_and_failure_keeps_the_alert(self):
        class NowPool:
            def submit(self, fn): fn()
        S = session(0)
        S["frames"].append(b"j")
        alert = {"say": "Heard clapping (80% sure)."}
        with mock.patch.object(server, "compile_pool", NowPool()), \
             mock.patch.object(meta, "confirm", return_value={"confirm": True, "explanation": "Someone is clapping."}):
            server.explain_later(S, 0, S["specs"][0], alert)
        self.assertEqual(alert["say"], "Someone is clapping.")
        self.assertEqual(S["stages"]["confirm"]["state"], "ok")
        alert = {"say": "Heard clapping (80% sure)."}
        with mock.patch.object(server, "compile_pool", NowPool()), mock.patch.object(meta, "confirm", side_effect=ValueError("x")):
            server.explain_later(S, 0, S["specs"][0], alert)
        self.assertEqual(alert["say"], "Heard clapping (80% sure).")
        self.assertEqual(S["stages"]["confirm"]["state"], "unavailable")

    def test_a_wanted_sound_fires_at_once_with_no_model_call(self):
        S = session(0)
        S["specs"].append({"title": "T", "subjects": [], "question": "Q2?", "duration_s": 0})
        S["specs"][0].update(sound_labels=["Clapping"], uses_sound=True)
        S["timers"].append(RuleTimer(0)); S["cards"].append(dict(S["cards"][0])); S["expl"].append("")
        S["last"] = [{"yes": False, "note": "n"}]
        S["heard"] = {"ms": 1, "scores": {"Hands": 0.9}}
        with mock.patch.object(meta, "check_frame") as spark, mock.patch.object(server, "start_sam") as sam, \
             mock.patch.object(server, "explain_later"):
            out = server.run_check(S, b"j", 1, 3, fast=True)
        spark.assert_not_called(); sam.assert_not_called()
        self.assertEqual(out["cards"][0]["status"], "alert")
        self.assertEqual(out["new_alerts"][0]["at"], 3000)
        self.assertEqual(out["skipped"], 0)

    def test_the_browser_is_told_every_label_and_bar(self):
        self.assertEqual(server.listen_bars({"sound_labels": ["Clapping"]}),
                         {"Clapping": server.HEARD_MIN, "Hands": 0.5, "Applause": 0.25})
        self.assertEqual(server.listen_bars({"sound_labels": []}), {})

    def test_without_the_recognizer_spark_decides(self):
        S = session(0)
        S["specs"][0].update(sound_labels=["Clapping"], uses_sound=True)
        with mock.patch.object(meta, "check_frame", return_value=yes(False)) as spark, mock.patch.object(server, "start_sam"):
            server.run_check(S, b"j", 1, 3)
        spark.assert_called_once()


class SpeechRuleTests(unittest.TestCase):
    def setUp(self):
        self.S = session(0)
        self.S["specs"][0]["uses_sound"] = True
        self.S["audio"] = b"RIFFxxxxWAVE"

    def test_alerts_at_once_and_a_second_call_after_a_gap_alerts_again(self):
        S = self.S
        def check(answer, t):
            with mock.patch.object(meta, "check_frame", return_value=yes(answer)), mock.patch.object(server, "start_sam"), \
                 mock.patch.object(server, "explain_later") as later:
                out = server.run_check(S, b"j", 1, t)
            return out, later
        first, later = check(True, 3)
        self.assertEqual(first["cards"][0]["status"], "alert")
        later.assert_called_once()
        self.assertEqual(check(True, 6)[0]["new_alerts"], [])    # still the same call
        self.assertEqual(check(False, 9)[0]["cards"][0]["status"], "alert")
        second = check(True, 12)[0]
        self.assertEqual([a["id"] for a in second["new_alerts"]], [2])

    def test_the_camera_can_never_block_a_sound_rule(self):
        s = server.validate_spec({**SpecTests.good, "uses_sound": True, "conditions": [{"subject": "cup", "min": 1}]}, "s")
        self.assertEqual((s["subjects"], s["conditions"]), ([], []))

    def test_wonder_disagreeing_is_shown_not_hidden(self):
        class NowPool:
            def submit(self, fn): fn()
        self.S["frames"].append(b"j")
        alert = {"say": "Heard a call"}
        with mock.patch.object(server, "compile_pool", NowPool()), \
             mock.patch.object(meta, "confirm", return_value={"confirm": False, "explanation": "Only music."}):
            server.explain_later(self.S, 0, self.S["specs"][0], alert)
        self.assertIn("couldn't confirm", alert["say"])
        self.assertIn("Only music.", alert["say"])


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
