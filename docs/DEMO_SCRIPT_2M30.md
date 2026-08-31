# Blink — 2:30 Demo Script & Shot List

Target **≤ 2:30**. The judges watch four minutes and stop, so 2:30 is deliberate:
every second is a scored second, and a tight film reads as confidence.

Recorded against the live deployment at https://blink.oapps.dev and the iOS
companion on a real iPhone. **Narration is human** (the judge said plainly she
prefers it: "Don't use AI voices. It feels less genuine to me"). Blink's own
Charon voice is the product and is heard in-app, which is a different thing.

## What earns points, and where it lives in this cut

Every judged criterion the session named has exactly one beat that carries it.
Nothing here is decoration.

| What the judges said | Beat |
|---|---|
| "Wowing them in the first like maybe 30 seconds" | 1 |
| Agentic, not "a chatbot that remembers" | 3, 4 |
| Governed external action = "extra bonus mark" | 3 |
| Data lifecycle, filtering, self-improvement | 5 |
| Show it running on Google Cloud, "scroll through the logs" | 6 |
| Honesty as architectural discipline | 2, 5 |

## Runtime budget

150 seconds. Voice-over is **~300 words** (≈2:00 at 150 wpm), leaving ~30
seconds of screen-led silence where the product speaks for itself. Do not fill
those. The timer starting, the calendar event moving and the log scroll all
land harder without narration over them.

---

## 1 · 0:00–0:20 · The phone rings first

**Screen:** A real iPhone, locked. A Blink notification arrives on its own and
says WHY, in Blink's voice. No app open, no setup, no talking head.

**VO (~30 words):** "Nobody opened an app. Blink decided this mattered now, and
said why. It runs while you are not looking, which is the whole point."

**Why first:** it is the only 20 seconds that prove autonomy before any claim is
made. A demo that opens on a dashboard has already lost the 30 seconds she named.

---

## 2 · 0:20–0:45 · One tap, and the minutes are measured

**Screen:** Tap the notification, the focus timer opens on that block, run it,
stop it. The receipt says **measured, not claimed**.

**VO (~45 words):** "One tap opens the session. When it ends, Blink records what
the timer measured. If you tell it something instead, it records that separately
and says so. Measured minutes and reported minutes are never added into one
number, anywhere in the system."

**Why:** the honesty contract, shown rather than asserted. She files this under
architectural discipline, so state it as engineering, not as a virtue.

---

## 3 · 0:45–1:20 · It changes your real calendar, and asks first

**Screen:** Web. Say (or type) "move my thesis block to Thursday at 2." Blink
proposes, **asks for confirmation**, you accept, and a **real Google Calendar
event moves on camera** in a second tab.

**VO (~55 words):** "Ask it to move something and it does not just talk. It
proposes, waits for a yes, then writes to the real Google Calendar. The write
tools are structurally unreachable until you confirm: the agent cannot call them
inside a turn. And the reply reports the plan and the calendar as two separate
truths, because one can fail while the other succeeds."

**Why:** this is the "extra bonus mark" beat. Governed external action, with the
governance visible. Have Calendar open in a second tab BEFORE recording.

---

## 4 · 1:20–1:45 · Life happens, and it replans itself

**Screen:** "I lost my morning, I was in hospital." Blink cancels, re-places the
work into real free time, and the plan view updates.

**VO (~40 words):** "Tell it the day fell apart and it does not ask you to
re-enter anything. It re-places the work into time you actually have, around the
events already on your calendar, and tells you exactly what moved."

**Why:** this is the difference between an agent and a chatbot with memory. It
acts on state without being walked through it.

---

## 5 · 1:45–2:10 · It improves itself, with consent

**Screen:** The mined insight surfacing ("your Monday evenings never survive"),
accepted on camera, and the plan changing shape because of it.

**VO (~50 words):** "Blink watches which sessions survive and which never do. It
filters that history into one suggestion, asks permission, and only then changes
how it plans you. That is the loop: it ingests your day, keeps what is true,
throws away what is noise, and gets better at you."

**Why:** the category-deciding beat. Her bar was not memory, it was using memory
to improve engagement, filtering the data, and shaping how the agent responds.
Say those words. **Never cut this beat.**

---

## 6 · 2:10–2:30 · Running on Google Cloud, and close

**Screen:** Cloud Run console, then **scroll the live logs** so the turn you just
ran is visible. Land on the Blink face.

**VO (~35 words):** "All of it runs on Cloud Run, on Gemini, with the plan in
Firestore. These are the logs of what you just watched. Blink keeps working after
the conversation ends."

**Why:** she asked for exactly this proof, by name. Real logs, on camera.

---

## Prep, 30 minutes before recording

1. **Seed real state.** A commitment, four or five tasks, sessions today and
   later in the week, one already mirrored to Google Calendar. An empty account
   demos nothing.
2. **Two browser tabs:** Blink, and Google Calendar showing the week. Beat 3 is
   worthless if the calendar is not visible when the event moves.
3. **Fire the notification for real.** Do not fake beat 1. Schedule a session a
   few minutes out and let the real signal arrive, or capture it earlier and cut
   it in. It must be a genuine notification.
4. **Phone capture:** connect the iPhone by cable, QuickTime → File → New Movie
   Recording → select the iPhone as the camera source. Clean, full-resolution,
   no bezel crop needed.
5. **Screen capture:** QuickTime screen recording, or `Cmd+Shift+5`. Record at
   the display's native size and do not resize the window mid-take.
6. **Voice:** record narration as a separate audio pass and lay it under the
   picture. Reading live while operating makes both worse. One take per beat is
   fine; you are cutting anyway.
7. **Edit:** DaVinci Resolve is already on this machine. Cut to the beat
   boundaries above, do not add music that fights the voice, and leave the
   screen-led moments silent.

## Cut ladder, if it runs long

Trim beat 4 to fifteen seconds, then beat 2 to twenty. **Never** cut beats 1, 3,
5 or 6: they carry the first-30-seconds hook, the governed external action, the
data loop, and the Google Cloud proof. Those are the four things the judges said
out loud that they score.

## One honest note

Everything in this cut must be a real run against the live deployment. She said
they dig through the code to check "if it's actually saying what you're saying".
A demo beat that the repository cannot back is worse than a shorter film.
