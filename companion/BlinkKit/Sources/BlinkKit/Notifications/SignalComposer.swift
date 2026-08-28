import Foundation

// Where the words come from, field by field.
//
// THE RULE THIS FILE EXISTS TO KEEP
// ---------------------------------
// docs/COMPANION_SCREENS.md, S2: the copy is "composed **server-side** from
// grounded data. The device never writes notification copy."
//
// There is no endpoint that returns finished notification copy today, and
// inventing one is P15-10's work, not this item's. So two of the four kinds
// are relayed verbatim from strings the server already composes, and two are
// assembled here from values the server sent. Every assembled sentence below
// is written so that each of its claims maps to one field of one payload:
//
//   nudge     "<task title> starts in ten minutes."
//             title      <- tasks[].title, joined by blocks[].task_id
//             "ten"      <- SignalRules.nudgeLeadMinutes, and the signal is
//                           scheduled at exactly starts_at minus that lead, so
//                           the sentence is true BY CONSTRUCTION rather than
//                           by hope.
//             No title in the payload means the title-free sentence, never an
//             invented name for someone's work.
//
//   check-in  "How did <task title> go?"
//             title      <- the same join. It is a QUESTION, so it asserts
//                           nothing about the session beyond its existence.
//
//   brief     server's own sentence, verbatim, from brief.notification_body
//             (src/agent/triggers.py, execute_morning_brief), plus at most one
//             appended sentence "First at <time>." whose only variable is
//             brief.first_start rendered by ServerClock in the user's zone.
//
//   insight   server's own sentence, verbatim, from insight.text, with
//             insight.evidence_text as the subtitle. Not one word is edited.
//
// WHAT IS DELIBERATELY NOT SAID. S2's illustrative nudge reads "Rehearse the
// talk starts in ten minutes. The evening is clear for it." The second
// sentence asserts that nothing else is on the calendar this evening, and
// nothing in the payload this app reads carries that. So it is dropped. The
// same goes for the brief's "Your Work time stays clear." A sentence with no
// field behind it does not get written, however good the copy is.
//
// EVERY COMPOSER RETURNS OPTIONAL, and nil is the normal answer. Nothing to
// say is a first-class output (.agents/rules/agent-governance.md, invariant
// 5), not a gap to fill.

public struct SignalComposer: Sendable {
    public init() {}

    // MARK: Nudge

    /// ~10 minutes before a planned session (S2).
    ///
    /// Returns nil when there is no next session today, or when its lead time
    /// has already passed. A nudge delivered after the session started would
    /// be a claim about the future that is already false.
    public func nudge(from details: WorkspaceDetails) -> NotificationSignal? {
        let state = TodayState(details: details)
        guard case .nextSession(let card) = state.card else { return nil }

        let lead = TimeInterval(SignalRules.nudgeLeadMinutes * 60)
        let deliverAt = card.startsAt.addingTimeInterval(-lead)
        let after = deliverAt.timeIntervalSince(details.now)
        guard after > 0 else { return nil }

        let minutes = spelled(SignalRules.nudgeLeadMinutes)
        let body: String
        if let title = card.title {
            body = "\(title) starts in \(minutes) minutes."
        } else {
            body = "Your next session starts in \(minutes) minutes."
        }

        return NotificationSignal(
            id: "nudge.\(card.blockID)",
            kind: .nudge,
            title: nil,
            body: body,
            deliverAt: deliverAt,
            deliverAfter: after,
            actions: [
                SignalAction(
                    identifier: SignalActionID.startTimer,
                    title: "Start timer",
                    // The timer, and the write that records its minutes, are
                    // P15-06. Until then this opens the app rather than
                    // reporting in the background that it started something it
                    // did not start.
                    opensApp: true
                ),
                SignalAction(
                    identifier: SignalActionID.notTonight,
                    title: "Not tonight",
                    // A real skip, written in the background. S2: it "does not
                    // snooze silently and it does not pretend the session
                    // still stands."
                    opensApp: false
                ),
            ],
            context: SignalContext(blockID: card.blockID, taskTitle: card.title),
            provenance: .deviceComposedFromServerData
        )
    }

    // MARK: Morning brief

    /// The server's brief, relayed.
    ///
    /// Returns nil when the server composed no body, which is what it does
    /// when today holds no blocks. The device does not write a cheerful
    /// substitute for an empty day.
    public func morningBrief(
        from brief: MorningBrief,
        details: WorkspaceDetails
    ) -> NotificationSignal? {
        guard let serverBody = brief.notificationBody else { return nil }

        var body = serverBody
        // The one appended sentence, and its only variable is a field the
        // server sent. Rendered in the USER'S zone by the clock the payload
        // published, so it agrees with every other time in the app.
        if let firstStart = brief.firstStart {
            let clock = ServerClock(details: details)
            body += " First at \(clock.clockTime(firstStart))."
        }

        return NotificationSignal(
            id: "brief.\(details.today)",
            kind: .morningBrief,
            title: nil,
            body: body,
            deliverAt: details.now.addingTimeInterval(SignalRules.dueNowDelaySeconds),
            deliverAfter: SignalRules.dueNowDelaySeconds,
            // S2 gives the brief one action and a swipe: "Open · (swipe away)".
            actions: [
                SignalAction(identifier: SignalActionID.open, title: "Open", opensApp: true)
            ],
            context: SignalContext(),
            provenance: .deviceComposedFromServerData
        )
    }

    // MARK: Check-in

    /// "How did <the session> go?", for the FIRST unanswered ended block.
    ///
    /// One block at a time (docs/COMPANION_SCREENS.md, S4). Returns nil when
    /// nothing needs resolving, and S4 is explicit that this is correct
    /// behaviour: "Silence is the correct behaviour and no 'all clear'
    /// notification is sent."
    public func checkIn(from details: WorkspaceDetails) -> NotificationSignal? {
        let state = TodayState(details: details)
        let pending: [PendingBlock]
        switch state.card {
        case .checkIn(let blocks), .endedAwaitingCheckIn(let blocks):
            pending = blocks
        default:
            return nil
        }
        guard let block = pending.first else { return nil }

        let body: String
        if let title = block.title {
            body = "How did \(title) go?"
        } else {
            body = "How did that session go?"
        }

        return NotificationSignal(
            id: "checkin.\(block.id)",
            kind: .checkIn,
            title: nil,
            body: body,
            deliverAt: details.now.addingTimeInterval(SignalRules.dueNowDelaySeconds),
            deliverAfter: SignalRules.dueNowDelaySeconds,
            actions: [
                // All three write in the background. S2: "Tapping Done should
                // not need the app to open."
                SignalAction(identifier: SignalActionID.done, title: "Done", opensApp: false),
                SignalAction(identifier: SignalActionID.partly, title: "Partly", opensApp: false),
                SignalAction(identifier: SignalActionID.skip, title: "Skip", opensApp: false),
            ],
            context: SignalContext(blockID: block.id, taskTitle: block.title),
            provenance: .deviceComposedFromServerData
        )
    }

    // MARK: Insight

    /// The server's insight, relayed word for word.
    ///
    /// Returns nil when the server surfaced none, which is most days: an
    /// insight needs at least three occurrences of a pattern before it exists
    /// (`src/core/insights.py`, `MIN_OCCURRENCES`).
    public func insight(
        from brief: MorningBrief,
        details: WorkspaceDetails
    ) -> NotificationSignal? {
        guard let insight = brief.insight else { return nil }
        return NotificationSignal(
            id: "insight.\(insight.insightID)",
            kind: .insight,
            title: nil,
            body: insight.text,
            subtitle: insight.evidenceText,
            deliverAt: details.now.addingTimeInterval(SignalRules.dueNowDelaySeconds),
            deliverAfter: SignalRules.dueNowDelaySeconds,
            actions: [
                SignalAction(identifier: SignalActionID.adapt, title: "Adapt", opensApp: false),
                SignalAction(identifier: SignalActionID.leaveIt, title: "Leave it", opensApp: false),
            ],
            context: SignalContext(insightID: insight.insightID),
            provenance: .serverComposed
        )
    }

    // MARK: Composing one named kind

    /// The single entry point both the scheduler and the debug rehearsal use,
    /// so there is exactly one place a kind's words are decided.
    public func compose(
        _ kind: SignalKind,
        details: WorkspaceDetails,
        brief: MorningBrief?
    ) -> NotificationSignal? {
        switch kind {
        case .nudge:
            return nudge(from: details)
        case .morningBrief:
            return brief.flatMap { morningBrief(from: $0, details: details) }
        case .checkIn:
            return checkIn(from: details)
        case .insight:
            return brief.flatMap { insight(from: $0, details: details) }
        }
    }

    // MARK: The hour windows

    /// Which kinds S2's own trigger column allows at this moment, in the
    /// user's zone.
    ///
    /// The hour comes from `ServerClock`, so a phone in Lisbon reading a Tokyo
    /// account uses Tokyo's evening. `Date()` gets no vote here, exactly as in
    /// `TodayState`.
    public func kindsInWindow(for details: WorkspaceDetails) -> [SignalKind] {
        let hour = ServerClock(details: details).localHourNow
        var kinds: [SignalKind] = [.nudge]
        if hour < SignalRules.morningBriefBeforeHour {
            kinds.append(.morningBrief)
        }
        if hour >= SignalRules.checkInHour {
            kinds.append(.checkIn)
        }
        // S2 gives the insight no hour, only "at most one per day". The ledger
        // holds it to that; nothing else gates it.
        kinds.append(.insight)
        return kinds
    }

    // MARK: Words for small numbers

    /// "ten", not "10", because that is the register S2 writes in. Falls back
    /// to the digits rather than guessing at a word it does not have.
    private func spelled(_ value: Int) -> String {
        let words = [
            0: "zero", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
            6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
            11: "eleven", 12: "twelve", 15: "fifteen", 20: "twenty",
        ]
        return words[value] ?? String(value)
    }
}
