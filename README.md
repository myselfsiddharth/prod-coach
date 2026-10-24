# Prod Coach (Cubbie Skill)

A coach that remembers every excuse you've made, quotes it back, and learns which kind of push actually gets you
moving. Pick a goal and a coach (Drill Sergeant, Mob Boss, Disappointed Dad...). Cubbie checks in at your times.

## The student moment

It's 6 pm. You said you'd go to the gym. You text back "too tired, long lab day."

> **Mob Boss:** Third "tired" this week. Tuesday: "too tired, long lab day." Thursday: "so tired, didn't sleep."
> We had an arrangement. Ten minutes, 7:30. Deal?

Every excuse is saved word for word on your side, so the receipts are exact and never guessed.

## Install

Text this repo's link to Cubbie and reply "yes". No keys, no sign-in, nothing to set up.

## What it does

- **Check-ins in a voice you pick.** Six comic archetypes plus a gentle Supportive coach, at the intensity you choose.
- **Receipts.** Past excuses quoted back exactly, with the day. Streaks and weekly counts come from a log, not memory.
- **Learns what works on you.** Each push (roast, a smaller version, a follow-up, a call, a text to a buddy that you
  send) is scored by whether you actually went afterwards, and the next one is picked from that.

## How it works

```
 student (iMessage)
        │  texts
        ▼
 ┌──────────────┐  reads   ┌───────────┐  runs   ┌──────────────────┐  reads/writes  ┌───────────────────────┐
 │    Cubbie    │ ───────► │ SKILL.md  │ ──────► │ scripts/log.py   │ ─────────────► │ ~/.config/prod-coach/ │
 │ (reminders,  │          │ + persona │         │ (stdlib Python)  │                │ log.json              │
 │ calls, memory│ ◄─────── │   voices  │ ◄────── │ key: value lines │                └───────────────────────┘
 └──────────────┘  replies └───────────┘ output  └──────────────────┘
```

Cubbie does what only Cubbie can (scheduled check-ins, phone calls, memory, talking to the student). The script
does what must be exact and identical across conversations: logging, receipts, streaks, and choosing the next push.
Scripts can't call Cubbie, so Cubbie passes in the student's local time and their words as arguments.

### The push picker, in plain words

1. The student's settings decide which pushes are allowed (`call` needs calls turned on, `buddy_draft` needs a
   buddy) and which coaches are in play (the one they picked, or all six if they chose "surprise me").
2. For each kind of push and each coach, the picker looks at how often it was followed by the student actually
   going, within 3 hours. It uses the situation that matches best: the same excuse category ("tired") if there are
   at least 2 tries, otherwise the same time of day, otherwise everything.
3. It picks the best smoothed success rate, `(worked + 1) / (tries + 2)`, so one lucky try doesn't decide.
4. One time in five, or whenever something has fewer than 2 tries, it tries the least-tried option instead, so it
   keeps learning. It says so ("trying something new").
5. It never calls twice in a row on the same day. Goals about eating, weight, body image or mental health only get
   the Supportive coach and the gentlest pushes (a smaller version, a follow-up). This is enforced in the script, not
   just in the instructions.

Kind and coach are learned separately. With six coaches and five kinds there are 30 combinations; scoring each
dimension on its own means it starts to learn after a handful of check-ins rather than after sixty.

## Privacy

All data stays in the student's own Cubbie sandbox (`~/.config/prod-coach/log.json`). Nothing is sent anywhere and the
script has no network access. "Forget my excuses" blanks the saved words and keeps the counts; "forget everything"
deletes the file. Cubbie only ever texts its own student: for a buddy it drafts a message and the student sends it.

## Hard lines

Every coach, every intensity: no jokes about body, weight, appearance, eating, race, religion, gender, sexuality,
disability, mental health, family or money. No slurs, threats or sexual content. The roast is aimed at the excuse,
never the person. Personas are archetypes, not real people.

## Tests

```bash
python3 -m unittest discover -s tests          # 38 tests, standard library only, about 12 seconds
python3 scripts/log.py --sample stats --now 2026-10-24T18:00       # try it on a bundled two-week history
python3 scripts/log.py --sample checkin --now 2026-10-24T18:10 --result excuse --text "too tired" --category tired
```

They run the script exactly as Cubbie does (a subprocess with a throwaway `HOME`) and cover: setup and stats on an
empty history; receipts order and exact wording; streaks (sick days don't break them, excuses do, an excuse followed by
going still counts); the 3-hour credit window; the picker preferring what works in the same situation, exploring the
least-tried option, never calling unless allowed or twice in a row, and the supportive restrictions; `--sample`
writing nothing; corrupt files (exit 4, set aside), missing setup (exit 3) and bad input (exit 2); forgetting; and
every output staying under 8,000 characters.

Verified on Python 3.9.6 (the minimum) and 3.14.7. The Skill validator from the Cubbie Hackathon Kit reports OK with
no warnings:

```bash
python3 ../cubbie-hackathon-kit/skills/cubbie-skill-creator/scripts/validate_cubbie_skill.py prod-coach
```

## Tested on Cubbie

_Not run yet._ The 13-step conversation checklist (setup, a persona's voice, receipts, a sick day, a buddy
draft, forgetting) is run on a real Cubbie account by a teammate, and the pass/fail results go here.

## Limitations

- One goal per student at a time. Changing the goal keeps the history.
- The excuse category (tired, busy, later, weather, other) is chosen by Cubbie from the student's words, so it is
  only as good as that call. Receipts are exact either way.
- The picker needs a couple of weeks of check-ins before its choices are more than a good guess.
- Times are the student's local time as passed in by Cubbie. There is no time-zone logic in the script.
- Calls depend on what Cubbie can do for a student; if a call isn't possible, Cubbie falls back to a text.

## Team

Built at the Cubbie Hackathon (DevHacks 2026) at ASU. Mascot: Sagu, a small cactus.
