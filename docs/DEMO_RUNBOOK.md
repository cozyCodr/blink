# Blink — Demo Runbook (recording day)

The beats and the narration live in `DEMO_SCRIPT_2M30.md`. This file is the
operational half: what to run, what to say to Blink, what to do when a beat
misbehaves on camera. Follow it top to bottom.

Budget **2h30** end to end: 30 min prep, 45 min shooting, 45 min edit, 30 min
upload and Devpost. Everything is recorded against the live deployment.

---

## The holiday arc (the spine of the film)

One continuous story that chains notification, grounded search, the confirm
gate, a real calendar write, and a replan across multiple days. It replaces the
separate beats 3, 4 and 5 with one unbroken run, which is stronger television
and easier to shoot.

**The premise:** Monday to Friday, 06:00 to 18:00, is booked as work. A session
today did not happen. A public holiday lands in the plan window, and that day's
work block does not apply, so the missed work has somewhere to go.

### The holiday must be real, and Sept 1 is not one

Verified live against the deployment on 2026-08-31, three separate queries:

- **"public holiday September 1 2026 Zimbabwe"** returns, correctly, *"There is
  no public holiday in Zimbabwe on September 1, 2026"*, citing timeanddate.com
  and officeholidays.com.
- **Munhumutapa Day, Tuesday 15 September 2026** is real. Confirmed by
  officeholidays.com, zimbabweyp.com, iharare.com and Wikipedia across two
  independent queries. It is a newly established holiday.

So do **not** stage an invented holiday for tomorrow. Blink is built to refuse
to confirm what it cannot verify, which means on camera it would contradict you,
with sources. Use **15 September** and the beat is dramatic *and* true, which is
the only combination that survives judges reading the repository.

### The run, in order

1. **The notification arrives** on the locked phone about the session that did
   not happen. Tap it.
2. **"Reschedule what I missed."** Blink proposes. It has nowhere good to put
   the work, because Monday to Friday 06:00 to 18:00 is blocked.
3. **"The fifteenth is a public holiday here. Look it up and confirm."** Blink
   asks permission to search, naming the real query. Say yes on camera.
4. **The grounded answer lands** with real sources: Munhumutapa Day, Tuesday 15
   September. This is the beat where a search is not decoration, it changes the
   plan.
5. **"So I am not working that day. Clear my work block on the fifteenth."**
   Blink proposes the deletion, you confirm, and the **real Google Calendar
   event disappears** in the second tab.
6. **"Put the client project sessions in there."** Blink reads the now-open day
   through `get_capacity`'s free windows and places several sessions across real
   free time with `schedule_task_sessions`, reporting each one.

That last step is the P21-01 work shipped today. Before it, Blink could only
ever hold one session per task, so this beat was impossible.

### Seeding it

- Put the work blocks on the **real Google Calendar as single events**, 06:00 to
  18:00, one per weekday, **not as a recurring series**. Deleting one instance of
  a recurring event is a sharper edge than a demo needs. Then run
  `POST /v1/workspaces/{ws}/calendar/sync-google` so they arrive as hard
  constraints.
- Set the workspace timezone first by opening the web app once. A workspace with
  no timezone runs in UTC and every time on screen will be two hours out.
- Leave one session earlier today unstarted so step 1 has something true to be
  about.

### Where it can break

- **The searched fact must stay checkable.** If a source moves, re-run step 3
  before recording and read what actually comes back.
- **Step 5 needs the calendar scope connected.** Without it Blink correctly
  reports the plan changed and the calendar did not, which is honest and not the
  beat you want. Check the settings panel first.
- **Step 6 with no free time does nothing.** Confirm the work block on the 15th
  is really gone from the synced constraints before asking for placement.

---

## T-30 · Prep

**1. Seed real state.** An empty account demos nothing.

```bash
bash deployment/seed_demo.sh
```

Expect: 4 tasks, blocks planned, 2 milestones (Oct 15 / Dec 20), a utilization
percentage. If utilization comes back `None`, re-run once.

**2. Put one session a few minutes out**, so beat 1's notification is real.
Type into Blink: `put a 25 minute deep work block at <now + 8 minutes>`. Confirm
it. Then leave the phone alone.

**3. Two browser tabs, both visible before you hit record.** Tab A: Blink,
Nocturne, voice on, window at native size. Tab B: Google Calendar on week view,
signed into the same account. Beat 3 is worthless if the calendar is off screen
when the event moves.

**4. Phone capture.** iPhone by cable, QuickTime → File → New Movie Recording →
select the iPhone as the source. Full resolution, no bezel crop needed.

**5. Screen capture.** QuickTime screen recording or `Cmd+Shift+5`. Do not
resize the window mid-take.

**6. Silence everything else.** Slack, Mail, calendar alerts, Do Not Disturb on
the Mac but **not** on the phone.

**7. Have the Cloud Run logs tab open** already scrolled to live, for beat 6.

---

## Shooting order

Shoot **out of order**, easiest first, so a hard beat never eats the schedule.
Record each beat as its own file. One take per beat is fine; you are cutting
anyway. Narration is a **separate audio pass at the end** — do not read while
operating, it makes both worse. The judge said plainly she prefers a human
voice, so it is your voice over the picture; Blink's Charon voice is heard
in-app, which is a different thing.

### Take order: 3 → 4 → 5 → 2 → 1 → 6

Beat 3 first because it is the highest-scoring beat and the one most likely to
need a retake.

---

## Beat 3 · It changes your real calendar, and asks first (0:45–1:20)

Both tabs visible. **Two prompts, in this order.**

> **move my thesis block to Thursday at 2**

Verified live on 2026-08-31: this executes directly and answers "I have moved
your thesis block to Thursday, September 3, at 2:00 PM." Watch the Google
Calendar tab move. If the seeded plan has no block by that name, use the exact
title from the plan view.

> **reschedule what I missed today**

This one is confirm-gated. Blink proposes and asks; say **yes** on camera.

**Why both:** the plain move is a deliberate direct write (you named the exact
change), documented in `src/agent/tools.py`. The confirm gate covers
`propose_reschedule` and the Google Calendar write tools. Narrating "it asks
first" over the plain move would be a claim the repository contradicts, and they
read the repository. The two prompts together show the real rule: you name it,
it does it; Blink decides it, you approve it.

*If the calendar does not visibly move:* the account may not have the calendar
scope. Check the settings panel shows Google connected before reshooting; a
missing scope makes Blink report the plan change honestly and say the calendar
was not written, which is correct behaviour but not the beat you want.

*If "reschedule what I missed" finds nothing:* there is nothing overdue. Let a
seeded session go past its end time first, or shoot this after beat 2.

## Beat 4 · Life happens, and it replans (1:20–1:45)

> **I lost my morning, I was in hospital**

Watch it cancel, re-place the work into real free time, and say what moved.
Let the plan view finish updating before you cut.

*Fallback phrasing if it under-reacts:* `reschedule everything I missed today`.

## Beat 5 · It improves itself, with consent (1:45–2:10)

Surface the mined insight and accept it on camera. If the insight card does not
appear on its own, ask:

> **what have you noticed about how I work?**

**Never cut this beat.** It carries the whole data-lifecycle criterion:
ingests the day, keeps what is true, discards noise, changes how it plans you,
and asks first.

## Beat 2 · One tap, and the minutes are measured (0:20–0:45)

Phone. Open a session, start the timer, let it run, stop it. Show the receipt
saying what was **measured**. Do not speed this up in the edit past the point
where the numbers are readable.

## Beat 1 · The phone rings first (0:00–0:20)

Locked phone, on the desk, nothing else in frame. Wait for the notification you
scheduled in prep to arrive on its own.

The sweep runs every five minutes, so allow up to five minutes of dead
recording. Do not fake it — trim the wait in the edit.

*If it does not arrive:* check notification permission is granted on the phone,
confirm the session is actually within its nudge window, and re-shoot rather
than staging a screenshot.

## Beat 6 · Google Cloud, and close (2:10–2:30)

Cloud Run console for the service, then **scroll the live logs** so the turn you
just recorded is visible on camera. Land on the Blink face. She asked for this
proof by name.

---

## Narration pass

Record all six VO segments in one sitting, one file, with a beat of silence
between them. Roughly 300 words total, which is about two minutes at a normal
pace, leaving ~30 seconds where the screen carries itself. **Leave those
silent** — the timer starting, the calendar event moving and the log scroll all
land harder without a voice over them.

The exact lines are in `DEMO_SCRIPT_2M30.md`. Read them as engineering, not as
virtue: "measured minutes and reported minutes are never added into one number"
is a fact about the system, and it should sound like one.

---

## Edit

DaVinci Resolve is already on this machine.

1. Lay the six picture files on the timeline in beat order (1 through 6).
2. Lay the VO underneath and slide the picture to meet it.
3. Trim dead air: the notification wait, the model thinking, the page loads.
4. No music that fights the voice. If you want a bed, keep it under the
   narration and duck it to nothing during the silent moments.
5. Export 1080p, H.264.

**Hard stop at 2:30.** If it runs long, trim beat 4 to fifteen seconds, then
beat 2 to twenty. Never cut 1, 3, 5 or 6 — those carry the first-30-seconds
hook, the governed external action, the data loop, and the Cloud proof.

---

## Ship

1. Upload to YouTube, **public** (not unlisted — some judges hit sign-in walls
   on unlisted links), title `Blink — an agent that keeps working after the
   conversation ends`.
2. Paste the link into Devpost, replacing the `TBD` fields.
3. Check the Devpost page from a **logged-out browser**: video plays, live URL
   loads, repo is public.
4. Submit with time on the clock. Late is zero, regardless of the film.

---

## One honest note

Everything in the cut must be a real run against the live deployment. They dig
through the repository to check whether it does what the video says. A beat the
code cannot back is worse than a shorter film.
