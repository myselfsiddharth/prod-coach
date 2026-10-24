"""Tests for scripts/log.py. Run: python3 -m unittest discover -s tests

Every test runs the script as a subprocess with its own temporary HOME, the way Cubbie runs it.
"""
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "log.py"
SAMPLE = ROOT / "assets" / "sample_log.json"

NOW = "2026-10-24T18:10"
# Seeds for the picker's first random draw: it explores when the draw is below 0.2.
EXPLORE_SEED = next(s for s in range(1000) if random.Random(s).random() < 0.2)
STEADY_SEED = next(s for s in range(1000) if random.Random(s).random() >= 0.2)


def settings(**over):
    base = {"goal": "gym", "minutes": 45, "coach": "mob_boss", "intensity": 2, "calls": True,
            "buddy": "Maya", "supportive": False, "updated": "2026-10-12T17:40"}
    base.update(over)
    return base


class LogTestCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        self.state_file = Path(self.home) / ".config" / "prod-coach" / "log.json"

    def run_log(self, *args):
        env = dict(os.environ, HOME=self.home)
        done = subprocess.run([sys.executable, "-I", str(SCRIPT)] + list(args), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(done.stderr, "", "the script should never print to stderr")
        self.assertLess(len(done.stdout), 8000)
        return done.returncode, done.stdout

    def ok(self, *args):
        code, out = self.run_log(*args)
        self.assertEqual(code, 0, out)
        return out

    def lines(self, out):
        return dict(line.split(": ", 1) for line in out.splitlines() if ": " in line and not line.startswith("receipt:"))

    def write_state(self, checkins=(), pushes=(), **over):
        """checkins: (at, result, text, category); pushes: (checkin number from 1, kind, coach, outcome)."""
        state = {"version": 1, "settings": settings(**over), "checkins": [], "pushes": []}
        for i, (at, result, text, category) in enumerate(checkins, 1):
            state["checkins"].append({"id": i, "at": at, "result": result, "text": text, "category": category})
        for j, (cid, kind, coach, outcome) in enumerate(pushes, 1):
            at = state["checkins"][cid - 1]["at"]
            state["pushes"].append({"id": j, "checkin_id": cid, "at": at, "kind": kind, "coach": coach,
                                    "explored": False, "outcome": outcome,
                                    "outcome_at": at if outcome else None})
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state))

    def read_state(self):
        return json.loads(self.state_file.read_text())

    def history(self, rows, **over):
        """rows: (category, kind, coach, outcome), one evening excuse and push each, on earlier days."""
        checkins, pushes = [], []
        for i, (category, kind, coach, outcome) in enumerate(rows):
            day = 1 + i // 20
            checkins.append(("2026-09-%02dT18:%02d" % (day, i % 20), "excuse", "excuse %d" % i, category))
            pushes.append((i + 1, kind, coach, outcome))
        self.write_state(checkins, pushes, **over)

    def excuse(self, *extra, text="too tired", category="tired", now=NOW, allowed="roast,shrink,followup"):
        return self.ok("checkin", "--now", now, "--result", "excuse", "--text", text, "--category", category,
                       "--allowed", allowed, *extra)


class SetupAndStats(LogTestCase):
    def test_setup_then_stats_on_empty_history(self):
        out = self.ok("setup", "--now", NOW, "--goal", "gym", "--minutes", "45", "--coach", "mob_boss",
                      "--intensity", "2", "--calls", "yes", "--buddy", "Maya", "--supportive", "no")
        self.assertIn("ok: settings saved", out)
        self.assertEqual(self.lines(out)["buddy"], "Maya")
        stats = self.lines(self.ok("stats", "--now", NOW))
        self.assertEqual(stats["streak"], "0")
        self.assertEqual(stats["week"], "done 0, skipped 0, excused 0")
        self.assertEqual(stats["last_checkin"], "none yet")
        self.assertIn("none", stats["top_excuse"])
        self.assertIn("not enough", stats["works_best"])

    def test_setup_again_keeps_history_and_unchanged_fields(self):
        self.ok("setup", "--now", NOW, "--goal", "gym", "--minutes", "45", "--coach", "mob_boss", "--buddy", "Maya")
        self.excuse()
        out = self.ok("setup", "--now", NOW, "--coach", "drill_sergeant", "--buddy", "none")
        values = self.lines(out)
        self.assertEqual((values["goal"], values["minutes"], values["coach"], values["buddy"]),
                         ("gym", "45", "drill_sergeant", "none"))
        self.assertEqual(len(self.read_state()["checkins"]), 1)

    def test_first_setup_needs_a_goal(self):
        code, out = self.run_log("setup", "--now", NOW, "--minutes", "30")
        self.assertEqual((code, out), (2, "error: --goal is required for the first setup\n"))

    def test_sensitive_goals_get_the_supportive_coach(self):
        out = self.ok("setup", "--now", NOW, "--goal", "eat three meals a day", "--coach", "mob_boss",
                      "--supportive", "no")
        values = self.lines(out)
        self.assertEqual((values["supportive"], values["coach"]), ("yes", "supportive"))
        self.assertIn("note:", out)
        gym = self.ok("setup", "--now", NOW, "--goal", "lift weights", "--coach", "mob_boss", "--supportive", "no")
        self.assertEqual(self.lines(gym)["supportive"], "no")


class Receipts(LogTestCase):
    def test_excuse_lists_earlier_excuses_newest_first_with_same_category_first(self):
        self.write_state([
            ("2026-10-20T18:00", "excuse", "too tired, long lab day", "tired"),
            ("2026-10-21T18:00", "excuse", "swamped with homework", "busy"),
            ("2026-10-22T18:00", "excuse", "so tired, didn't sleep", "tired"),
            ("2026-10-23T18:00", "excuse", "meeting ran over", "busy"),
        ])
        out = self.excuse(text="tired again")
        receipts = [line for line in out.splitlines() if line.startswith("receipt: ")]
        self.assertEqual(receipts, [
            'receipt: Thu Oct 22: "so tired, didn\'t sleep"',
            'receipt: Tue Oct 20: "too tired, long lab day"',
            'receipt: Fri Oct 23: "meeting ran over"',
        ])
        self.assertIn('receipts: 2 earlier "tired" excuses this week', out)

    def test_receipts_command_filters_and_limits(self):
        self.write_state([
            ("2026-10-20T18:00", "excuse", "first", "tired"),
            ("2026-10-21T18:00", "excuse", "second", "busy"),
            ("2026-10-22T18:00", "excuse", "third", "tired"),
        ])
        self.assertEqual(self.ok("receipts", "--limit", "2").splitlines(),
                         ['receipt: Thu Oct 22: "third"', 'receipt: Wed Oct 21: "second"'])
        self.assertEqual(self.ok("receipts", "--category", "tired").count("receipt:"), 2)
        self.assertIn("none yet", self.ok("receipts", "--category", "weather"))

    def test_words_are_stored_exactly_even_when_they_start_with_a_dash(self):
        self.write_state()
        self.excuse(text="-_- so tired  \n today")
        self.assertEqual(self.read_state()["checkins"][0]["text"], "-_- so tired today")
        self.assertIn('"-_- so tired today"', self.ok("receipts"))


class Streaks(LogTestCase):
    def days(self, *results):
        return [("2026-10-%02dT18:00" % (18 + i), r, "x" if r in ("excuse", "valid") else "", "tired" if r == "excuse" else None)
                for i, r in enumerate(results)]

    def test_valid_does_not_break_the_streak_and_excuse_does(self):
        self.write_state(self.days("done", "valid", "done"))
        self.assertEqual(self.lines(self.ok("stats", "--now", NOW))["streak"], "2")
        out = self.excuse(now="2026-10-22T18:00")
        self.assertEqual(self.lines(out)["streak"], "0 (was 2)")

    def test_an_excuse_that_was_followed_by_going_still_counts_as_done(self):
        self.write_state([("2026-10-23T18:00", "done", "", None), ("2026-10-24T18:00", "excuse", "later", "later"),
                          ("2026-10-24T19:30", "done", "", None)])
        self.assertEqual(self.lines(self.ok("stats", "--now", "2026-10-24T20:00"))["streak"], "2")

    def test_noreply_breaks_the_streak(self):
        self.write_state(self.days("done", "done"))
        out = self.ok("checkin", "--now", "2026-10-20T19:00", "--result", "noreply")
        self.assertEqual(self.lines(out)["streak"], "0 (was 2)")

    def test_week_counts_days(self):
        self.write_state(self.days("done", "excuse", "valid", "noreply"))
        week = self.lines(self.ok("stats", "--now", "2026-10-21T20:00"))["week"]
        self.assertEqual(week, "done 1, skipped 2, excused 1")


class Outcomes(LogTestCase):
    def setUp(self):
        super().setUp()
        self.write_state([("2026-10-24T18:00", "excuse", "tired", "tired")], [(1, "shrink", "mob_boss", None)])

    def test_done_within_three_hours_credits_the_push(self):
        out = self.ok("checkin", "--now", "2026-10-24T20:59", "--result", "done")
        self.assertIn("credited: push 1 (shrink, mob_boss)", out)
        self.assertEqual(self.read_state()["pushes"][0]["outcome"], "complied")

    def test_done_after_three_hours_does_not(self):
        out = self.ok("checkin", "--now", "2026-10-24T21:01", "--result", "done")
        self.assertNotIn("credited", out)
        self.assertEqual(self.read_state()["pushes"][0]["outcome"], "ignored")

    def test_a_new_excuse_marks_the_open_push_ignored(self):
        self.excuse(now="2026-10-24T19:00")
        self.assertEqual(self.read_state()["pushes"][0]["outcome"], "ignored")

    def test_valid_leaves_open_pushes_alone_and_adds_no_push(self):
        out = self.ok("checkin", "--now", "2026-10-24T19:00", "--result", "valid", "--text", "fever")
        self.assertNotIn("push:", out)
        state = self.read_state()
        self.assertEqual((len(state["pushes"]), state["pushes"][0]["outcome"]), (1, None))

    def test_outcome_command_overrides(self):
        self.assertEqual(self.ok("outcome", "--now", NOW, "--push", "1", "--result", "pushed_back"),
                         "ok: push 1 marked pushed_back\n")
        self.assertEqual(self.read_state()["pushes"][0]["outcome"], "pushed_back")
        code, out = self.run_log("outcome", "--now", NOW, "--push", "99", "--result", "complied")
        self.assertEqual((code, out), (2, "error: no push with that number\n"))

    def test_noreply_logs_a_silence_and_picks_a_push(self):
        out = self.ok("checkin", "--now", "2026-10-24T19:05", "--result", "noreply", "--allowed", "roast,shrink,followup")
        self.assertIn("push:", out)
        self.assertEqual(self.read_state()["checkins"][-1]["category"], "noreply")


class Picker(LogTestCase):
    def test_prefers_the_best_candidate_for_the_same_excuse_category(self):
        rows = ([("tired", "shrink", "mob_boss", "complied")] * 3 + [("tired", "roast", "mob_boss", "ignored")] * 3
                + [("tired", "followup", "mob_boss", "complied")] + [("tired", "followup", "mob_boss", "ignored")] * 2
                + [("busy", "roast", "mob_boss", "complied")] * 6)  # roast wins overall, but not after "tired"
        self.history(rows)
        out = self.excuse("--seed", str(STEADY_SEED))
        values = self.lines(out)
        self.assertEqual((values["push"], values["coach"]), ("shrink", "mob_boss"))
        self.assertIn('shrink worked 3 of 3 times after "tired" excuses', values["reason"])
        self.assertFalse(self.read_state()["pushes"][-1]["explored"])

    def test_falls_back_to_the_daypart_then_to_everything(self):
        rows = ([("busy", "shrink", "mob_boss", "complied")] * 3 + [("busy", "roast", "mob_boss", "ignored")] * 3
                + [("busy", "followup", "mob_boss", "ignored")] * 2)
        self.history(rows)  # nothing in the "weather" category, so the evening history decides
        values = self.lines(self.excuse("--seed", str(STEADY_SEED), category="weather"))
        self.assertEqual(values["push"], "shrink")
        self.assertIn("in the evening", values["reason"])

    def test_picks_the_least_tried_candidate_when_one_has_fewer_than_two_tries(self):
        self.history([("tired", "roast", "mob_boss", "complied")] * 3 + [("tired", "shrink", "mob_boss", "complied")] * 3
                     + [("tired", "followup", "mob_boss", "ignored")])
        values = self.lines(self.excuse("--seed", str(STEADY_SEED)))
        self.assertEqual(values["push"], "followup")
        self.assertEqual(values["reason"], "trying something new")
        self.assertTrue(self.read_state()["pushes"][-1]["explored"])

    def test_random_exploration_picks_the_least_tried_and_ties_go_to_the_lightest(self):
        self.history([("tired", "roast", "mob_boss", "complied")] * 4 + [("tired", "shrink", "mob_boss", "complied")] * 2
                     + [("tired", "followup", "mob_boss", "complied")] * 2)
        self.assertEqual(self.lines(self.excuse("--seed", str(EXPLORE_SEED)))["push"], "shrink")

    def test_rotating_coaches_learn_which_coach_works(self):
        rows = []
        for coach in ("mob_boss", "drill_sergeant", "hype_man", "disappointed_dad", "tycoon", "commentator"):
            works = "complied" if coach == "mob_boss" else "ignored"
            rows += [("tired", "shrink", coach, works)] * 2
            rows += [("tired", "roast", coach, "ignored")] * 2 + [("tired", "followup", coach, "ignored")] * 2
        self.history(rows, coach="auto")
        values = self.lines(self.excuse("--seed", str(STEADY_SEED)))
        self.assertEqual((values["push"], values["coach"]), ("shrink", "mob_boss"))
        self.assertIn("Mob Boss lands best with you", values["reason"])

    def test_calls_only_when_allowed_and_enabled(self):
        for seed in range(25):
            self.write_state()
            self.assertNotEqual(self.lines(self.excuse("--seed", str(seed)))["push"], "call")
        for seed in range(10):
            self.write_state(calls=False)
            self.assertNotEqual(self.lines(self.excuse("--seed", str(seed), allowed="call,shrink"))["push"], "call")
        self.write_state()
        self.assertEqual(self.lines(self.excuse("--seed", "1", allowed="call"))["push"], "call")

    def test_never_calls_twice_in_a_row_on_the_same_day(self):
        self.write_state([("2026-10-24T09:00", "excuse", "tired", "tired")], [(1, "call", "mob_boss", "ignored")])
        for seed in range(20):
            shutil.copy(str(self.state_file), str(self.state_file) + ".keep")
            self.assertNotEqual(self.lines(self.excuse("--seed", str(seed), allowed="call,shrink"))["push"], "call")
            shutil.move(str(self.state_file) + ".keep", str(self.state_file))
        # The next day a call is fine again.
        self.assertEqual(self.lines(self.excuse("--seed", "1", now="2026-10-25T18:10", allowed="call"))["push"], "call")

    def test_buddy_draft_needs_a_buddy(self):
        self.write_state(buddy=None)
        self.assertNotEqual(self.lines(self.excuse("--seed", "1", allowed="buddy_draft,shrink"))["push"], "buddy_draft")
        self.write_state()
        out = self.excuse("--seed", "1", allowed="buddy_draft")
        self.assertEqual((self.lines(out)["push"], self.lines(out)["buddy"]), ("buddy_draft", "Maya"))

    def test_supportive_goal_only_gets_shrink_or_followup_from_the_supportive_coach(self):
        for seed in range(20):
            self.write_state(supportive=True, coach="supportive")
            values = self.lines(self.excuse("--seed", str(seed), allowed="roast,shrink,followup,buddy_draft,call"))
            self.assertIn(values["push"], ("shrink", "followup"))
            self.assertEqual(values["coach"], "supportive")

    def test_something_is_always_picked(self):
        self.write_state()
        self.assertEqual(self.lines(self.excuse(allowed=""))["push"], "shrink")


class Safety(LogTestCase):
    def test_sample_mode_never_writes_anything(self):
        before = hashlib.sha256(SAMPLE.read_bytes()).hexdigest()
        self.ok("--sample", "stats", "--now", NOW)
        self.ok("checkin", "--sample", "--now", NOW, "--result", "excuse", "--text", "x", "--category", "tired")
        self.ok("--sample", "setup", "--now", NOW, "--goal", "reading")
        self.ok("--sample", "outcome", "--now", NOW, "--push", "1", "--result", "complied")
        self.ok("--sample", "forget", "--what", "all")
        self.assertEqual(os.listdir(self.home), [])
        self.assertEqual(hashlib.sha256(SAMPLE.read_bytes()).hexdigest(), before)

    def test_sample_history_is_realistic(self):
        stats = self.lines(self.ok("--sample", "stats", "--now", "2026-10-24T18:00"))
        self.assertEqual(stats["streak"], "3")
        self.assertIn("tired", stats["top_excuse"])
        self.assertIn("shrink", stats["works_best"])

    def test_corrupt_file_exits_4_and_is_kept_aside(self):
        self.state_file.parent.mkdir(parents=True)
        self.state_file.write_text("{not json")
        code, out = self.run_log("stats", "--now", NOW)
        self.assertEqual(code, 4)
        self.assertTrue(out.startswith("error: "))
        self.assertEqual([p.name.split("-")[0] for p in self.state_file.parent.iterdir()], ["log.json.bad"])
        self.ok("setup", "--now", NOW, "--goal", "gym")  # starts fresh afterwards

    def test_wrong_shape_counts_as_corrupt(self):
        self.state_file.parent.mkdir(parents=True)
        self.state_file.write_text(json.dumps({"version": 1, "settings": None, "checkins": [{"id": 1}], "pushes": []}))
        self.assertEqual(self.run_log("stats", "--now", NOW)[0], 4)

    def test_missing_setup_exits_3(self):
        for args in (["stats", "--now", NOW], ["receipts"], ["checkin", "--now", NOW, "--result", "done"],
                     ["outcome", "--now", NOW, "--push", "1", "--result", "complied"]):
            self.assertEqual(self.run_log(*args), (3, "error: no goal set up\n"))
        self.assertFalse(self.state_file.exists())

    def test_bad_input_exits_2_with_one_line(self):
        cases = [
            (["checkin", "--now", NOW, "--result", "maybe"], "error: --result must be done, excuse, valid or noreply"),
            (["checkin", "--now", NOW, "--result", "excuse"], "error: --text is required for excuse and valid"),
            (["checkin", "--result", "done"], "error: --now is required (YYYY-MM-DDTHH:MM, the student's local time)"),
            (["stats", "--now", "yesterday"], "error: --now must look like 2026-10-24T18:10"),
            (["setup", "--now", NOW, "--goal", "gym", "--intensity", "9"], "error: --intensity must be a whole number from 1 to 3"),
            (["setup", "--now", NOW, "--goal", "gym", "--calls", "maybe"], "error: --calls must be yes or no"),
            (["setup", "--now", NOW, "--goal", "gym", "--coach", "elvis"], None),
            (["checkin", "--now", NOW, "--result", "done", "--allowed", "nuke"], None),
            (["stats", "--bogus"], "error: unknown option --bogus"),
            (["dance"], None),
            ([], None),
        ]
        self.write_state()
        for args, expected in cases:
            code, out = self.run_log(*args)
            self.assertEqual(code, 2, args)
            self.assertEqual(len(out.splitlines()), 1, args)
            self.assertTrue(out.startswith("error: "), out)
            if expected:
                self.assertEqual(out.strip(), expected)

    def test_forget_excuses_removes_text_but_keeps_counts(self):
        self.write_state([("2026-10-22T18:00", "valid", "fever", None), ("2026-10-23T18:00", "excuse", "too tired", "tired"),
                          ("2026-10-24T19:00", "done", "", None)])
        self.state_file.with_name("log.json.bad-20260101T000000").write_text("old excuses")
        self.ok("forget", "--what", "excuses")
        state = self.read_state()
        self.assertEqual([c["text"] for c in state["checkins"]], ["", "", ""])
        self.assertEqual(len(state["checkins"]), 3)
        self.assertIn("none yet", self.ok("receipts"))
        self.assertEqual(self.lines(self.ok("stats", "--now", "2026-10-24T20:00"))["week"], "done 1, skipped 1, excused 1")
        self.assertEqual([p.name for p in self.state_file.parent.iterdir()], ["log.json"])

    def test_forget_all_deletes_everything(self):
        self.write_state([("2026-10-23T18:00", "excuse", "too tired", "tired")])
        self.state_file.with_name("log.json.bad-20260101T000000").write_text("old excuses")
        self.ok("forget", "--what", "all")
        self.assertEqual(list(self.state_file.parent.iterdir()), [])
        self.ok("forget", "--what", "all")  # nothing left is fine too
        self.assertEqual(self.run_log("stats", "--now", NOW)[0], 3)

    def test_every_output_stays_under_8000_characters(self):
        long_text = "w" * 400
        checkins = [("2026-10-%02dT18:%02d" % (1 + i // 40, i % 40), "excuse", long_text, "tired") for i in range(200)]
        self.write_state(checkins)
        for args in (["receipts", "--limit", "50"], ["stats", "--now", NOW],
                     ["checkin", "--now", NOW, "--result", "excuse", "--text", long_text, "--category", "tired"]):
            out = self.ok(*args)
            self.assertLess(len(out), 8000)
        self.assertTrue(self.ok("receipts", "--limit", "50").rstrip().endswith("note: output shortened"))

    def test_no_temp_files_are_left_behind(self):
        self.ok("setup", "--now", NOW, "--goal", "gym")
        self.excuse()
        self.assertEqual([p.name for p in self.state_file.parent.iterdir()], ["log.json"])


if __name__ == "__main__":
    unittest.main()
