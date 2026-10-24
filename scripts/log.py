#!/usr/bin/env python3
"""Prod Coach log: the student's check-ins, excuses and pushes, plus the push picker.

Python 3.9+, standard library only, no network. State lives in $HOME/.config/prod-coach/log.json.
Output is short `key: value` lines for Cubbie to read.

Exit codes: 0 ok; 1 unexpected failure; 2 bad input; 3 no goal set up yet; 4 the log could not be read.
"""
import argparse
import calendar as cal
import datetime as dt
import html
import json
import os
import random
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VERSION = 1
MAX_OUTPUT = 7800  # characters; Cubbie reads under 8,000
MAX_TEXT = 300  # characters of an excuse kept word for word
CREDIT_WINDOW = dt.timedelta(hours=3)
EXPLORE_P = 0.2
MIN_TRIES = 2

EXIT_FAILED, EXIT_USAGE, EXIT_NO_GOAL, EXIT_BAD_LOG = 1, 2, 3, 4

KINDS = ("roast", "shrink", "followup", "buddy_draft", "call")  # lightest first
SUPPORTIVE_KINDS = ("shrink", "followup")
COACHES = {
    "drill_sergeant": "Drill Sergeant",
    "hype_man": "Hype Man",
    "disappointed_dad": "Disappointed Dad",
    "mob_boss": "Mob Boss",
    "tycoon": "Tycoon Showman",
    "commentator": "Sports Commentator",
    "supportive": "Supportive",
}
ROTATION = tuple(c for c in COACHES if c != "supportive")
RESULTS = ("done", "excuse", "valid", "noreply")
OUTCOMES = ("complied", "ignored", "pushed_back")
CATEGORIES = ("tired", "busy", "later", "weather", "other")
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
FREE_TEXT_FLAGS = ("--text", "--goal", "--buddy")  # values may start with "-", so bind them with "="

# Goals about eating, weight, body image or mental health always get the supportive coach.
SENSITIVE_GOAL = re.compile(
    r"\b(eat\w*|diet\w*|calor\w*|binge\w*|purg\w*|anorex\w*|bulimi\w*|fasting|skinny|body image"
    r"|(lose|losing|gain|gaining) (weight|fat)|weight loss|mental health|anxi\w*|depress\w*"
    r"|therap\w*|self[- ]harm)\b",
    re.IGNORECASE,
)

State = Dict[str, Any]


class ExitError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def usage_error(message: str) -> ExitError:
    return ExitError(EXIT_USAGE, message)


# ---------------------------------------------------------------- time

def parse_stamp(text: str) -> dt.datetime:
    return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M")


def stamp(when: dt.datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M")


def day_label(when: dt.datetime) -> str:
    return "%s %s %d" % (DAYS[when.weekday()], MONTHS[when.month - 1], when.day)


def daypart(when: dt.datetime) -> str:
    if 5 <= when.hour < 12:
        return "morning"
    if 12 <= when.hour < 17:
        return "afternoon"
    if 17 <= when.hour < 22:
        return "evening"
    return "night"


# ---------------------------------------------------------------- argument parsing

class Parser(argparse.ArgumentParser):
    """Reports every problem as one `error:` line with exit code 2."""

    def error(self, message: str) -> None:
        message = re.sub(r"^argument [^:]+: ", "", message)
        if message.startswith("the following arguments are required"):
            names = message.split(": ", 1)[1]
            if names.strip() == "command":
                raise usage_error("no command given (setup, checkin, outcome, receipts, stats or forget)")
            raise usage_error("missing " + names.replace(", ", " and "))
        if message.startswith("unrecognized arguments"):
            raise usage_error("unknown option " + message.split(": ", 1)[1])
        found = re.match(r"invalid choice: '?([^' ]*)'?", message)
        if found:
            raise usage_error("unknown command '%s' (setup, checkin, outcome, receipts, stats or forget)"
                              % found.group(1))
        raise usage_error(message)


def one_of(flag: str, options: Tuple[str, ...]):
    names = ", ".join(options[:-1]) + " or " + options[-1]

    def convert(value: str) -> str:
        if value not in options:
            raise argparse.ArgumentTypeError("%s must be %s" % (flag, names))
        return value
    return convert


def yes_no(flag: str):
    def convert(value: str) -> bool:
        if value.lower() not in ("yes", "no"):
            raise argparse.ArgumentTypeError("%s must be yes or no" % flag)
        return value.lower() == "yes"
    return convert


def whole_number(flag: str, low: int, high: int):
    def convert(value: str) -> int:
        try:
            number = int(value)
        except ValueError:
            number = low - 1
        if not low <= number <= high:
            raise argparse.ArgumentTypeError("%s must be a whole number from %d to %d" % (flag, low, high))
        return number
    return convert


def clean_text(value: str, limit: int) -> str:
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value)
    return " ".join(value.split())[:limit]


def parse_now(value: str) -> dt.datetime:
    try:
        return parse_stamp(value)
    except ValueError:
        raise argparse.ArgumentTypeError("--now must look like 2026-10-24T18:10")


def parse_coach(value: str) -> str:
    if value != "auto" and value not in COACHES:
        raise argparse.ArgumentTypeError("--coach must be auto or one of " + ", ".join(COACHES))
    return value


def parse_allowed(value: str) -> List[str]:
    kinds = [k.strip() for k in value.split(",") if k.strip()]
    if any(k not in KINDS for k in kinds):
        raise argparse.ArgumentTypeError("--allowed must be a comma list of " + ", ".join(KINDS))
    return kinds


def parse_month(value: str) -> dt.date:
    try:
        return dt.datetime.strptime(value, "%Y-%m").date()
    except ValueError:
        raise argparse.ArgumentTypeError("--month must look like 2026-10")


def parse_goal(value: str) -> str:
    goal = clean_text(value, 80)
    if not goal:
        raise argparse.ArgumentTypeError("--goal can't be empty")
    return goal


def parse_buddy(value: str) -> Optional[str]:
    name = clean_text(value, 40)
    return None if name.lower() in ("", "none") else name


def build_parser() -> argparse.ArgumentParser:
    common = Parser(add_help=False)
    common.add_argument("--now", type=parse_now, default=argparse.SUPPRESS, help="local time, YYYY-MM-DDTHH:MM")
    common.add_argument("--sample", action="store_true", default=argparse.SUPPRESS,
                        help="use the bundled sample history and save nothing")
    common.add_argument("--seed", type=whole_number("--seed", 0, 2**31 - 1), default=argparse.SUPPRESS,
                        help="make the picker deterministic (tests only)")

    parser = Parser(prog="log.py", description="Prod Coach log.", allow_abbrev=False)
    parser.add_argument("--now", type=parse_now, default=None)
    parser.add_argument("--sample", action="store_true", default=False)
    parser.add_argument("--seed", type=whole_number("--seed", 0, 2**31 - 1), default=None)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)

    def add(name: str, help_text: str):
        return commands.add_parser(name, parents=[common], help=help_text, allow_abbrev=False)

    setup = add("setup", "create or update the goal and settings")
    setup.add_argument("--goal", type=parse_goal)
    setup.add_argument("--minutes", type=whole_number("--minutes", 1, 600))
    setup.add_argument("--coach", type=parse_coach)
    setup.add_argument("--intensity", type=whole_number("--intensity", 1, 3))
    setup.add_argument("--calls", type=yes_no("--calls"))
    setup.add_argument("--buddy", type=parse_buddy, default=argparse.SUPPRESS)
    setup.add_argument("--supportive", type=yes_no("--supportive"))

    checkin = add("checkin", "log a check-in answer and get the next push")
    checkin.add_argument("--result", required=True,
                         type=one_of("--result", RESULTS))
    checkin.add_argument("--text")
    checkin.add_argument("--category", type=one_of("--category", CATEGORIES), default="other")
    checkin.add_argument("--allowed", type=parse_allowed, default=["roast", "shrink", "followup"])

    outcome = add("outcome", "record how a push landed")
    outcome.add_argument("--push", required=True, type=whole_number("--push", 1, 10**9))
    outcome.add_argument("--result", required=True, type=one_of("--result", OUTCOMES))

    receipts = add("receipts", "past excuses, newest first")
    receipts.add_argument("--category", type=one_of("--category", CATEGORIES))
    receipts.add_argument("--limit", type=whole_number("--limit", 1, 50), default=3)

    add("stats", "streak, week, top excuse, what works")

    calendar = add("calendar", "write a month-by-month calendar page of your check-ins")
    calendar.add_argument("--month", type=parse_month, help="newest month to show, YYYY-MM (default: this month)")
    calendar.add_argument("--months", type=whole_number("--months", 1, 6), default=1,
                          help="how many months to show, newest first")
    calendar.add_argument("--out", help="where to save the page (default: next to the log)")

    forget = add("forget", "forget excuse text or everything")
    forget.add_argument("--what", required=True, type=one_of("--what", ("excuses", "all")))
    return parser


def normalize_argv(argv: List[str]) -> List[str]:
    """Bind free-text values to their flag so words that start with "-" are not mistaken for options."""
    out: List[str] = []
    i = 0
    while i < len(argv):
        if argv[i] in FREE_TEXT_FLAGS and i + 1 < len(argv):
            out.append("%s=%s" % (argv[i], argv[i + 1]))
            i += 2
        else:
            out.append(argv[i])
            i += 1
    return out


# ---------------------------------------------------------------- state

def state_path() -> Path:
    return Path(os.path.expanduser("~")) / ".config" / "prod-coach" / "log.json"


def sample_path() -> Path:
    return Path(__file__).resolve().parent.parent / "assets" / "sample_log.json"


def empty_state() -> State:
    return {"version": VERSION, "settings": None, "checkins": [], "pushes": []}


def check_shape(state: Any) -> None:
    """Raise ValueError unless `state` looks like a log this script wrote."""
    try:
        if not isinstance(state, dict) or state.get("version") != VERSION:
            raise ValueError("version")
        if state.get("settings") is not None and not isinstance(state["settings"], dict):
            raise ValueError("settings")
        for c in state["checkins"]:
            parse_stamp(c["at"])
            if c["result"] not in RESULTS or not isinstance(c["id"], int):
                raise ValueError("checkin")
        for p in state["pushes"]:
            parse_stamp(p["at"])
            if p["kind"] not in KINDS or not isinstance(p["id"], int) or not isinstance(p["checkin_id"], int):
                raise ValueError("push")
            if p.get("outcome") not in (None,) + OUTCOMES:
                raise ValueError("outcome")
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(str(exc))


def set_aside(path: Path) -> None:
    bad = path.with_name("log.json.bad-" + time.strftime("%Y%m%dT%H%M%S"))
    n = 1
    while bad.exists():
        bad = path.with_name("%s-%d" % (bad.name, n))
        n += 1
    os.replace(str(path), str(bad))


def write_atomic(path: Path, state: State) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="log.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class Log:
    """The student's state plus where it is saved; in sample mode nothing is ever saved."""

    def __init__(self, sample: bool):
        self.sample = sample
        self.path = sample_path() if sample else state_path()
        self.state = self._load()

    def _load(self) -> State:
        if not self.path.exists():
            if self.sample:
                raise ExitError(EXIT_BAD_LOG, "the sample history is missing")
            return empty_state()
        try:
            with open(str(self.path), encoding="utf-8") as handle:
                state = json.load(handle)
            check_shape(state)
            return state
        except ValueError:  # includes JSONDecodeError and UnicodeDecodeError
            if self.sample:
                raise ExitError(EXIT_BAD_LOG, "the sample history could not be read")
            set_aside(self.path)
            raise ExitError(EXIT_BAD_LOG, "the log could not be read, so it was set aside and history starts fresh")

    def save(self) -> None:
        if not self.sample:
            write_atomic(self.path, self.state)

    @property
    def settings(self) -> Dict[str, Any]:
        """The goal and settings; exits with code 3 if the student has not set up a goal yet."""
        if not self.state["settings"]:
            raise ExitError(EXIT_NO_GOAL, "no goal set up")
        return self.state["settings"]

    def next_id(self, key: str) -> int:
        return max([e["id"] for e in self.state[key]] or [0]) + 1


def delete_log_files() -> None:
    path = state_path()
    if path.exists():
        path.unlink()
    for bad in sorted(path.parent.glob("log.json.bad-*")) + [path.parent / "calendar.html"]:
        if bad.exists():
            bad.unlink()


# ---------------------------------------------------------------- history maths

def by_newest(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(entries, key=lambda e: (e["at"], e["id"]), reverse=True)


def day_statuses(checkins: List[Dict[str, Any]]) -> Dict[dt.date, str]:
    """One status per day: done beats valid (sick or emergency) beats skipped (excuse or no reply).

    A student who gave an excuse at 6 and went at 8 still did it, and one who then said they were sick was not
    skipping, so the later report wins."""
    rank = {"skipped": 0, "valid": 1, "done": 2}
    names = {"done": "done", "excuse": "skipped", "noreply": "skipped", "valid": "valid"}
    days: Dict[dt.date, str] = {}
    for c in checkins:
        day, status = parse_stamp(c["at"]).date(), names[c["result"]]
        if day not in days or rank[status] > rank[days[day]]:
            days[day] = status
    return days


def streak_of(checkins: List[Dict[str, Any]]) -> int:
    days = day_statuses(checkins)
    count = 0
    for day in sorted(days, reverse=True):
        if days[day] == "done":
            count += 1
        elif days[day] == "skipped":
            break
    return count


def week_line(checkins: List[Dict[str, Any]], now: dt.datetime) -> str:
    first = now.date() - dt.timedelta(days=6)
    counts = {"done": 0, "skipped": 0, "valid": 0}
    for day, status in day_statuses(checkins).items():
        if first <= day <= now.date():
            counts[status] += 1
    return "week: done %d, skipped %d, excused %d" % (counts["done"], counts["skipped"], counts["valid"])


def receipt_line(c: Dict[str, Any]) -> str:
    return 'receipt: %s: "%s"' % (day_label(parse_stamp(c["at"])), c["text"])


def receipts_for(state: State, category: Optional[str], exclude_id: Optional[int] = None) -> List[Dict[str, Any]]:
    found = [c for c in by_newest(state["checkins"])
             if c["result"] == "excuse" and c.get("text") and c["id"] != exclude_id]
    if category:
        found = [c for c in found if c.get("category") == category]
    return found


# ---------------------------------------------------------------- push picker

def resolved_rows(state: State) -> List[Dict[str, Any]]:
    """Pushes with a known outcome, tagged with the excuse category and daypart they happened in."""
    category_of = {c["id"]: c.get("category") for c in state["checkins"]}
    return [{"kind": p["kind"], "coach": p["coach"], "won": p["outcome"] == "complied",
             "category": category_of.get(p["checkin_id"]), "part": daypart(parse_stamp(p["at"]))}
            for p in state["pushes"] if p.get("outcome") in OUTCOMES]


def tally(rows: List[Dict[str, Any]], field: str, value: str, category: str, part: str) -> Tuple[int, int, str]:
    """(wins, tries, scope) for one kind or coach, from the most specific scope with enough tries."""
    mine = [r for r in rows if r[field] == value]
    for scope, same in (("category", lambda r: r["category"] == category),
                        ("daypart", lambda r: r["part"] == part)):
        chosen = [r for r in mine if same(r)]
        if len(chosen) >= MIN_TRIES:
            return sum(r["won"] for r in chosen), len(chosen), scope
    return sum(r["won"] for r in mine), len(mine), "all"


def choose(options: List[str], rows: List[Dict[str, Any]], field: str, category: str, part: str,
           explore: bool) -> Tuple[str, bool, Tuple[int, int, str]]:
    """Pick one kind or coach. Returns (choice, explored, tally). Ties go to the lighter option (list order)."""
    if len(options) == 1:
        return options[0], False, tally(rows, field, options[0], category, part)
    tries = {o: sum(1 for r in rows if r[field] == o) for o in options}
    if explore or min(tries.values()) < MIN_TRIES:
        pick = min(options, key=lambda o: (tries[o], options.index(o)))
        return pick, True, tally(rows, field, pick, category, part)
    scored = {o: tally(rows, field, o, category, part) for o in options}
    pick = max(options, key=lambda o: ((scored[o][0] + 1) / (scored[o][1] + 2), -options.index(o)))
    return pick, False, scored[pick]


def eligible(settings: Dict[str, Any], state: State, allowed: List[str], now: dt.datetime) -> Tuple[List[str], List[str]]:
    kinds = [k for k in KINDS if k in allowed]
    if settings.get("supportive"):
        kinds = [k for k in kinds if k in SUPPORTIVE_KINDS] or list(SUPPORTIVE_KINDS)
    if not settings.get("calls"):
        kinds = [k for k in kinds if k != "call"]
    if not settings.get("buddy"):
        kinds = [k for k in kinds if k != "buddy_draft"]
    last = by_newest(state["pushes"])[:1]
    if last and last[0]["kind"] == "call" and parse_stamp(last[0]["at"]).date() == now.date():
        kinds = [k for k in kinds if k != "call"]  # never two calls in a row on the same day
    if settings.get("supportive"):
        coaches = ["supportive"]
    elif settings.get("coach") in COACHES:
        coaches = [settings["coach"]]
    else:
        coaches = list(ROTATION)
    return kinds or ["shrink"], coaches


def explain(kind: str, coach: str, kind_tally: Tuple[int, int, str], coach_tally: Tuple[int, int, str],
            rotating: bool, category: str, part: str) -> str:
    wins, tries, scope = kind_tally
    if wins == 0:
        return "nothing has worked yet, so %s is just the lightest option" % kind
    where = {"category": ' after "%s" excuses' % category if category != "noreply" else " after no-reply check-ins",
             "daypart": " in the " + part, "all": ""}[scope]
    text = "%s worked %d of %d times%s" % (kind, wins, tries, where)
    if rotating and coach_tally[0] > 0:
        text += "; %s lands best with you (%d of %d)" % (COACHES[coach], coach_tally[0], coach_tally[1])
    return text


def pick_push(state: State, settings: Dict[str, Any], category: str, allowed: List[str],
              now: dt.datetime, seed: Optional[int]) -> Dict[str, Any]:
    rng = random.Random(seed)
    explore = rng.random() < EXPLORE_P
    kinds, coaches = eligible(settings, state, allowed, now)
    rows, part = resolved_rows(state), daypart(now)
    kind, kind_explored, kind_tally = choose(kinds, rows, "kind", category, part, explore)
    coach, coach_explored, coach_tally = choose(coaches, rows, "coach", category, part, explore)
    explored = kind_explored or coach_explored
    reason = "trying something new" if explored else explain(
        kind, coach, kind_tally, coach_tally, len(coaches) > 1, category, part)
    return {"kind": kind, "coach": coach, "explored": explored, "reason": reason}


# ---------------------------------------------------------------- commands

def need_now(args: argparse.Namespace) -> dt.datetime:
    if args.now is None:
        raise usage_error("--now is required (YYYY-MM-DDTHH:MM, the student's local time)")
    return args.now


def settings_lines(s: Dict[str, Any]) -> List[str]:
    return ["goal: %s" % s["goal"], "minutes: %d" % s["minutes"], "coach: %s" % s["coach"],
            "intensity: %d" % s["intensity"], "calls: %s" % ("yes" if s["calls"] else "no"),
            "buddy: %s" % (s["buddy"] or "none"), "supportive: %s" % ("yes" if s["supportive"] else "no")]


def cmd_setup(log: Log, args: argparse.Namespace) -> List[str]:
    now = need_now(args)
    current = log.state["settings"]
    if current is None and args.goal is None:
        raise usage_error("--goal is required for the first setup")
    s = dict(current or {"goal": "", "minutes": 30, "coach": "auto", "intensity": 2, "calls": False,
                         "buddy": None, "supportive": False})
    for key in ("goal", "minutes", "coach", "intensity", "calls", "supportive"):
        if getattr(args, key) is not None:
            s[key] = getattr(args, key)
    if hasattr(args, "buddy"):
        s["buddy"] = args.buddy
    lines = ["ok: settings saved"]
    forced = bool(SENSITIVE_GOAL.search(s["goal"])) and not s["supportive"]
    if forced or s["coach"] == "supportive":
        s["supportive"] = True
        if forced:
            lines.append("note: this goal is about eating, weight, body or mental health, so the coach is supportive")
    if s["supportive"]:
        s["coach"] = "supportive"
    elif s["coach"] == "supportive":
        s["coach"] = "auto"
    s["updated"] = stamp(now)
    log.state["settings"] = s
    log.save()
    return lines + settings_lines(s) + (["note: sample mode, nothing was saved"] if log.sample else [])


def resolve_open_pushes(state: State, result: str, now: dt.datetime) -> Optional[Dict[str, Any]]:
    """Close pushes still waiting on an outcome; returns the push a `done` credits as complied, if any."""
    open_pushes = by_newest([p for p in state["pushes"] if p.get("outcome") is None])
    credited = None
    if result == "done" and open_pushes:
        newest = open_pushes[0]
        if dt.timedelta(0) <= now - parse_stamp(newest["at"]) <= CREDIT_WINDOW:
            credited = newest
    if result != "valid":
        for p in open_pushes:
            p["outcome"] = "complied" if p is credited else "ignored"
            p["outcome_at"] = stamp(now)
    return credited


def cmd_checkin(log: Log, args: argparse.Namespace) -> List[str]:
    now = need_now(args)
    settings, state = log.settings, log.state
    text = clean_text(args.text or "", MAX_TEXT)
    if args.result in ("excuse", "valid") and not text:
        raise usage_error("--text is required for excuse and valid")
    category = args.category if args.result == "excuse" else ("noreply" if args.result == "noreply" else None)

    streak_before = streak_of(state["checkins"])
    credited = resolve_open_pushes(state, args.result, now)
    entry = {"id": log.next_id("checkins"), "at": stamp(now), "result": args.result, "text": text,
             "category": category}
    state["checkins"].append(entry)
    streak_now = streak_of(state["checkins"])

    lines = ["streak: %d%s" % (streak_now, " (was %d)" % streak_before if streak_now < streak_before else ""),
             week_line(state["checkins"], now)]
    if args.result == "done":
        if credited:
            lines.append("credited: push %d (%s, %s) worked" % (credited["id"], credited["kind"], credited["coach"]))
        log.save()
        return lines
    if args.result == "valid":
        lines.append("note: sick or emergency, not counted against the student")
        log.save()
        return lines

    week_ago = now - dt.timedelta(days=7)
    same = [c for c in state["checkins"] if c.get("category") == category and c["id"] != entry["id"]
            and week_ago < parse_stamp(c["at"]) <= now]
    if same and category == "noreply":
        lines.append("receipts: %d earlier %s this week" % (len(same), "silence" if len(same) == 1 else "silences"))
    elif same:
        lines.append('receipts: %d earlier "%s" %s this week'
                     % (len(same), category, "excuse" if len(same) == 1 else "excuses"))
    past = receipts_for(state, None, exclude_id=entry["id"])
    past = [c for c in past if c.get("category") == category] + [c for c in past if c.get("category") != category]
    lines += [receipt_line(c) for c in past[:3]]

    push = pick_push(state, settings, category, args.allowed, now, args.seed)
    push_id = log.next_id("pushes")
    state["pushes"].append({"id": push_id, "checkin_id": entry["id"], "at": stamp(now), "kind": push["kind"],
                            "coach": push["coach"], "explored": push["explored"], "outcome": None,
                            "outcome_at": None})
    log.save()
    lines += ["push: " + push["kind"], "coach: " + push["coach"], "push_id: %d" % push_id]
    if push["kind"] == "buddy_draft":
        lines.append("buddy: " + settings["buddy"])
    return lines + ["reason: " + push["reason"]]


def cmd_outcome(log: Log, args: argparse.Namespace) -> List[str]:
    now = need_now(args)
    _ = log.settings
    for p in log.state["pushes"]:
        if p["id"] == args.push:
            p["outcome"], p["outcome_at"] = args.result, stamp(now)
            log.save()
            return ["ok: push %d marked %s" % (args.push, args.result)]
    raise usage_error("no push with that number")


def cmd_receipts(log: Log, args: argparse.Namespace) -> List[str]:
    _ = log.settings
    found = receipts_for(log.state, args.category)[:args.limit]
    if not found:
        return ["receipts: none yet" if not args.category else 'receipts: none yet for "%s"' % args.category]
    return [receipt_line(c) for c in found]


def best_push(rows: List[Dict[str, Any]]) -> Optional[str]:
    """The push (and coach) with the best smoothed success rate among those tried at least twice."""
    for fields in (("kind", "coach"), ("kind",)):
        groups: Dict[Tuple[str, ...], List[bool]] = {}
        for r in rows:
            groups.setdefault(tuple(r[f] for f in fields), []).append(r["won"])
        ranked = sorted(((sum(w) + 1) / (len(w) + 2), len(w), key) for key, w in groups.items() if len(w) >= MIN_TRIES)
        if ranked:
            _, tries, key = ranked[-1]
            wins = sum(groups[key])
            who = " with %s" % COACHES[key[1]] if len(key) > 1 else ""
            return "%s%s (%d of %d)" % (key[0], who, wins, tries)
    return None


def cmd_stats(log: Log, args: argparse.Namespace) -> List[str]:
    now = need_now(args)
    s, state = log.settings, log.state
    lines = ["goal: %s, %d min" % (s["goal"], s["minutes"]), "streak: %d" % streak_of(state["checkins"]),
             week_line(state["checkins"], now)]
    recent = [c for c in by_newest(state["checkins"])
              if c["result"] == "excuse" and now - dt.timedelta(days=14) < parse_stamp(c["at"]) <= now]
    if recent:
        counts: Dict[str, int] = {}
        for c in recent:
            counts[c.get("category") or "other"] = counts.get(c.get("category") or "other", 0) + 1
        top = max(counts, key=lambda k: (counts[k], -[c.get("category") or "other" for c in recent].index(k)))
        example = next((c["text"] for c in recent if (c.get("category") or "other") == top and c.get("text")), "")
        lines.append("top_excuse: %s, %d time%s in 14 days%s" % (
            top, counts[top], "" if counts[top] == 1 else "s", ' (latest: "%s")' % example if example else ""))
    else:
        lines.append("top_excuse: none in the last 14 days")
    lines.append("works_best: " + (best_push(resolved_rows(state)) or "not enough pushes tried yet"))
    last = by_newest(state["checkins"])[:1]
    lines.append("last_checkin: %s %s, %s" % (day_label(parse_stamp(last[0]["at"])), last[0]["at"][11:], last[0]["result"])
                 if last else "last_checkin: none yet")
    return lines


def cmd_forget(args: argparse.Namespace) -> List[str]:
    if args.sample:
        return ["note: sample mode, nothing was changed"]
    if args.what == "all":
        delete_log_files()
        return ["ok: everything forgotten"]
    log = Log(sample=False)
    for c in log.state["checkins"]:
        if c["result"] in ("excuse", "valid"):
            c["text"] = ""
    if log.path.exists():
        log.save()
    for old in sorted(log.path.parent.glob("log.json.bad-*")) + [log.path.parent / "calendar.html"]:
        if old.exists():  # set-aside logs and the saved calendar page both hold the old words
            old.unlink()
    return ["ok: excuse text forgotten, counts kept"]


CALENDAR_STYLE = """
  :root{--bg:#F2F6F1;--card:#fff;--ink:#16241C;--mute:#5d6e63;--line:#d9e3da;--done:#1E7A4C;--skip:#E8357C;--excuse:#FFC93C;--off:#e9efe9}
  @media (prefers-color-scheme:dark){:root{--bg:#0F1A14;--card:#17261D;--ink:#EAF3EC;--mute:#9db0a3;--line:#294034;--done:#2FA36A;--skip:#E8357C;--excuse:#FFC93C;--off:#1d2f24}}
  *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif}
  main{max-width:720px;margin:0 auto;padding:24px 16px 48px}h1{margin:0 0 4px;font-size:1.6rem}h2{margin:28px 0 10px;font-size:1.15rem}
  .sub{color:var(--mute);margin:0 0 18px}.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px}
  .tile{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:12px 14px}.tile b{display:block;font-size:1.7rem}
  .tile span{color:var(--mute);font-size:.85rem}.grid{display:grid;grid-template-columns:repeat(7,1fr);gap:6px}
  .dow{color:var(--mute);font-size:.75rem;text-align:center}.day{aspect-ratio:1;border-radius:12px;background:var(--off);padding:5px 7px;
  font-size:.8rem;display:flex;flex-direction:column;justify-content:space-between;color:var(--ink)}.day i{font-style:normal;font-size:1.1rem;font-weight:700;text-align:right}
  .blank{background:none}.future{opacity:.45}.today{outline:2px solid var(--ink)}
  .done{background:var(--done);color:#fff}.skipped{background:var(--skip);color:#fff}.valid{background:var(--excuse);color:#16241C}
  .legend{display:flex;gap:14px;flex-wrap:wrap;color:var(--mute);font-size:.85rem;margin-top:10px}.legend em{font-style:normal;display:inline-block;width:12px;height:12px;border-radius:4px;margin-right:6px;vertical-align:-1px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:6px 14px}.card p{margin:8px 0}.bar{display:flex;align-items:center;gap:8px;margin:8px 0}
  .bar span{min-width:64px}.bar div{height:12px;border-radius:6px;background:var(--skip)}footer{margin-top:28px;color:var(--mute);font-size:.8rem}
"""


def best_streak_of(checkins: List[Dict[str, Any]]) -> int:
    days = day_statuses(checkins)
    best = run = 0
    for day in sorted(days):
        if days[day] == "done":
            run += 1
            best = max(best, run)
        elif days[day] == "skipped":
            run = 0
    return best


def month_counts(days: Dict[dt.date, str], year: int, month: int) -> Dict[str, int]:
    counts = {"done": 0, "skipped": 0, "valid": 0}
    for day, status in days.items():
        if (day.year, day.month) == (year, month):
            counts[status] += 1
    return counts


def month_html(state: State, days: Dict[dt.date, str], year: int, month: int, today: dt.date) -> str:
    esc = lambda text: html.escape(text, quote=True)
    marks = {"done": "\u2713", "skipped": "\u2715", "valid": "\u2013"}
    labels = {"done": "went", "skipped": "skipped", "valid": "excused (sick or emergency)"}
    by_day: Dict[dt.date, List[str]] = {}
    for c in sorted(state["checkins"], key=lambda e: (e["at"], e["id"])):
        when = parse_stamp(c["at"])
        line = "%s %s%s" % (c["at"][11:], c["result"], ": " + c["text"] if c.get("text") else "")
        by_day.setdefault(when.date(), []).append(line)
    cells = ['<div class="dow">%s</div>' % d for d in ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")]
    for week in cal.Calendar(firstweekday=6).monthdatescalendar(year, month):
        for day in week:
            if day.month != month:
                cells.append('<div class="day blank"></div>')
                continue
            status = days.get(day)
            classes = ["day"] + ([status] if status else []) + (["future"] if day > today else []) + (["today"] if day == today else [])
            title = esc("%s: %s\n%s" % (day_label(dt.datetime.combine(day, dt.time())), labels.get(status, "no check-in"),
                                       "\n".join(by_day.get(day, []))))
            cells.append('<div class="%s" title="%s"><span>%s</span><i>%s</i></div>'
                         % (" ".join(classes), title, day.day, marks.get(status, "")))
    counts = month_counts(days, year, month)
    excuses = [c for c in by_newest(state["checkins"]) if c["result"] == "excuse" and c.get("text")
               and (parse_stamp(c["at"]).year, parse_stamp(c["at"]).month) == (year, month)]
    kinds: Dict[str, int] = {}
    for c in excuses:
        kinds[c.get("category") or "other"] = kinds.get(c.get("category") or "other", 0) + 1
    bars = "".join('<div class="bar"><span>%s</span><div style="width:%dpx"></div> %d</div>'
                   % (esc(k), 40 * n, n) for k, n in sorted(kinds.items(), key=lambda kv: -kv[1])) or "<p>No excuses logged.</p>"
    receipts = "".join("<p>%s: \u201c%s\u201d</p>" % (esc(day_label(parse_stamp(c["at"]))), esc(c["text"]))
                       for c in excuses[:8]) or "<p>No receipts this month.</p>"
    done, skipped = counts["done"], counts["skipped"]
    rate = "%d%%" % round(100 * done / (done + skipped)) if done + skipped else "\u2013"
    return ('<h2>%s %d</h2><div class="tiles"><div class="tile"><b>%d</b><span>days you went</span></div>'
            '<div class="tile"><b>%d</b><span>days skipped</span></div><div class="tile"><b>%d</b><span>excused</span></div>'
            '<div class="tile"><b>%s</b><span>went, of days with a check-in</span></div></div>'
            '<div class="grid" style="margin-top:12px">%s</div>'
            '<h2>Excuses by kind</h2><div class="card">%s</div><h2>Receipts</h2><div class="card">%s</div>'
            % (cal.month_name[month], year, done, skipped, counts["valid"], rate, "".join(cells), bars, receipts))


def render_calendar(state: State, settings: Dict[str, Any], newest: dt.date, count: int, now: dt.datetime) -> str:
    days = day_statuses(state["checkins"])
    months, year, month = [], newest.year, newest.month
    for _ in range(count):
        months.append((year, month))
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    best = best_push(resolved_rows(state))
    body = "".join(month_html(state, days, y, m, now.date()) for y, m in months)
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Prod Coach calendar</title><style>%s</style></head><body><main>'
            '<h1>\U0001F335 %s</h1><p class="sub">%d min goal. Updated %s.</p>'
            '<div class="tiles"><div class="tile"><b>%d</b><span>day streak</span></div>'
            '<div class="tile"><b>%d</b><span>best streak</span></div>'
            '<div class="tile"><b style="font-size:1rem">%s</b><span>what works on you</span></div></div>%s'
            '<div class="legend"><span><em style="background:var(--done)"></em>went</span>'
            '<span><em style="background:var(--skip)"></em>skipped</span>'
            '<span><em style="background:var(--excuse)"></em>excused</span>'
            '<span><em style="background:var(--off)"></em>no check-in</span></div>'
            '<footer>Saved on your own Cubbie. Hover a day for the words you used.</footer></main></body></html>'
            % (CALENDAR_STYLE, html.escape(settings["goal"]), settings["minutes"], html.escape(day_label(now)),
               streak_of(state["checkins"]), best_streak_of(state["checkins"]),
               html.escape(best or "not enough pushes tried yet"), body))


def cmd_calendar(log: Log, args: argparse.Namespace) -> List[str]:
    now = need_now(args)
    settings, state = log.settings, log.state
    newest = (args.month or now.date()).replace(day=1)
    page = render_calendar(state, settings, newest, args.months, now)
    lines = []
    if log.sample and not args.out:
        lines.append("note: sample mode, nothing was saved")
    else:
        path = Path(args.out).expanduser() if args.out else state_path().parent / "calendar.html"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="calendar.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(page)
            os.replace(tmp, str(path))
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        lines += ["ok: calendar saved", "file: %s" % path]
    counts = month_counts(day_statuses(state["checkins"]), newest.year, newest.month)
    return lines + ["month: %s %d" % (cal.month_name[newest.month], newest.year),
                    "days: went %d, skipped %d, excused %d" % (counts["done"], counts["skipped"], counts["valid"]),
                    "streak: %d" % streak_of(state["checkins"]),
                    "best_streak: %d" % best_streak_of(state["checkins"])]


COMMANDS = {"calendar": cmd_calendar, "setup": cmd_setup, "checkin": cmd_checkin, "outcome": cmd_outcome, "receipts": cmd_receipts,
            "stats": cmd_stats}


def emit(lines: List[str]) -> None:
    out: List[str] = []
    size = 0
    for line in lines:
        if size + len(line) + 1 > MAX_OUTPUT:
            out.append("note: output shortened")
            break
        out.append(line)
        size += len(line) + 1
    sys.stdout.write("\n".join(out) + "\n")


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        args = build_parser().parse_args(normalize_argv(argv))
        if args.command == "forget":
            lines = cmd_forget(args)
        else:
            lines = COMMANDS[args.command](Log(args.sample), args)
        code = 0
    except ExitError as exc:
        lines, code = ["error: " + exc.message], exc.code
    except OSError:
        lines, code = ["error: could not read or save the log"], EXIT_FAILED
    except Exception:  # never show a stack trace to the student
        lines, code = ["error: something went wrong"], EXIT_FAILED
    emit(lines)
    return code


if __name__ == "__main__":
    sys.exit(main())
