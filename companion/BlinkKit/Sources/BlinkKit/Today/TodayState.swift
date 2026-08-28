import Foundation

// S1's state machine, as pure arithmetic over one server payload.
//
// "The model judges, the code computes" (.agents/rules/agent-governance.md).
// Nothing in this file guesses, rounds in its own favour, or fills a gap.
// Every branch below is a deterministic read of blocks the server sent,
// dated by the clock the server published.

/// One planned session, ready to render.
public struct SessionCard: Equatable, Sendable, Identifiable {
    public var id: String { blockID }
    public let blockID: String
    /// The task's title. Nil-safe at the source: a block whose task is missing
    /// from the payload renders without a title rather than with an invented
    /// one, which is why this is `String?` all the way to the view.
    public let title: String?
    public let commitment: String?
    public let startsAt: Date
    public let endsAt: Date
    public let plannedMinutes: Int
    /// The measured minutes the SERVER already holds for this block, when the
    /// source is the timer. This is the reconcile floor a re-opened session
    /// starts from (S3 "Backgrounded / killed"): the device never guesses an
    /// elapsed from a run it did not witness. nil when the block has no timer
    /// minutes yet, or when its actual came from a self-report.
    public let resumedTimerMinutes: Int?
}

/// A block that has ended with no answer yet.
public struct PendingBlock: Equatable, Sendable, Identifiable {
    public let id: String
    public let title: String?
    public let endedAt: Date
    public let plannedMinutes: Int
}

/// What the card at the middle of S1 is showing. Exactly the rows of S1's
/// state table, plus one the table does not have (see `.endedAwaitingCheckIn`).
public enum TodayCard: Equatable, Sendable {
    /// New account: no tasks and no blocks anywhere.
    case emptyWorkspace
    /// There is a plan, it just does not touch today.
    case nothingPlanned
    /// The next session today, still ahead.
    case nextSession(SessionCard)
    /// A session's planned window contains the server's `now`.
    case sessionRunning(SessionCard)
    /// After the check-in hour, with ended blocks still unanswered.
    case checkIn([PendingBlock])
    /// Ended blocks are unanswered but the check-in hour has not arrived.
    ///
    /// **Not in S1's table.** The table jumps from "work was done" to
    /// "unresolved blocks after 5pm" and has no row for the hours in between,
    /// which a user in any zone can sit in for most of a day. Calling that
    /// "work done" would claim an outcome nobody recorded, so it gets its own
    /// honest line instead.
    case endedAwaitingCheckIn([PendingBlock])
    /// Today had blocks, every one of them is answered, none are left.
    case workDone
}

/// The honesty beat. Measured and self-reported minutes are separate fields
/// and there is deliberately no property that adds them together.
public struct TrackedLine: Equatable, Sendable {
    /// Minutes the timer measured (`actual_source == "timer"`).
    public let measuredMinutes: Int
    /// Minutes the user reported at check-in (`actual_source == "reported"`).
    public let reportedMinutes: Int
    /// The span today's plan asked for.
    public let plannedMinutes: Int

    public var hasAnyActual: Bool { measuredMinutes > 0 || reportedMinutes > 0 }
}

/// Everything S1 draws, derived once from one payload.
public struct TodayState: Equatable, Sendable {
    public let card: TodayCard
    public let tracked: TrackedLine
    /// Consecutive kept days, straight from the server. Zero means there is no
    /// streak to name, and the chip does not render.
    public let streakDays: Int
    public let clock: ServerClock

    /// The evening check-in hour, local to the user.
    ///
    /// From `docs/COMPANION_SCREENS.md` S1 ("Unresolved blocks from today
    /// (after 5pm)") and S2 ("Check-in: after 5pm"). The server has no
    /// constant for this: `_today_unresolved_blocks` in `src/api/server.py`
    /// filters by local DAY and leaves the hour to the caller. So the number
    /// lives here, named, and not inside a view.
    public static let checkInHour = 17

    public init(details: WorkspaceDetails) {
        let clock = ServerClock(details: details)
        self.clock = clock
        self.streakDays = details.streak

        // Cancelled blocks are invisible everywhere, exactly as
        // `compute_streak` treats them (src/core/progress.py:75). A
        // rebalance that cleared a day must not read as a missed one.
        let todays = details.blocks
            .filter { $0.status != .cancelled && clock.isToday($0.startsAt) }
            .sorted { $0.startsAt < $1.startsAt }

        self.tracked = TodayState.trackedLine(for: todays)
        self.card = TodayState.card(for: todays, details: details, clock: clock)
    }

    // MARK: The tracked line

    static func trackedLine(for todays: [BlockPayload]) -> TrackedLine {
        var measured = 0
        var reported = 0
        for block in todays {
            guard let minutes = block.actualMinutes, let source = block.actualSource else { continue }
            switch source {
            case .timer: measured += minutes
            case .reported: reported += minutes
            }
        }
        return TrackedLine(
            measuredMinutes: measured,
            reportedMinutes: reported,
            plannedMinutes: todays.reduce(0) { $0 + $1.plannedMinutes }
        )
    }

    // MARK: The card

    static func card(
        for todays: [BlockPayload],
        details: WorkspaceDetails,
        clock: ServerClock
    ) -> TodayCard {
        // A workspace with nothing to schedule. Commitments alone do not
        // count: an elicitation in progress has a commitment and no plan, and
        // "your plan lives on the web for now" is the right thing to say to
        // someone who has not made one yet.
        if details.tasks.isEmpty, details.blocks.isEmpty {
            return .emptyWorkspace
        }
        if todays.isEmpty {
            return .nothingPlanned
        }

        let now = clock.now

        if let running = todays.first(where: {
            $0.status == .planned && $0.startsAt <= now && now < $0.endsAt
        }) {
            return .sessionRunning(makeCard(running, details: details))
        }
        if let next = todays.first(where: { $0.status == .planned && $0.startsAt > now }) {
            return .nextSession(makeCard(next, details: details))
        }

        // Nothing ahead. What is behind?
        //
        // A block only becomes answerable once it has actually ENDED. The
        // server's `_today_unresolved_blocks` does not apply that filter,
        // because it is called from the evening path where it is always true;
        // here it is not, so the filter is explicit.
        let pending = todays
            .filter { $0.status == .planned && $0.endsAt <= now }
            .map {
                PendingBlock(
                    id: $0.id,
                    title: details.task(for: $0)?.title,
                    endedAt: $0.endsAt,
                    plannedMinutes: $0.plannedMinutes
                )
            }

        if pending.isEmpty {
            return .workDone
        }
        return clock.localHourNow >= checkInHour
            ? .checkIn(pending)
            : .endedAwaitingCheckIn(pending)
    }

    private static func makeCard(_ block: BlockPayload, details: WorkspaceDetails) -> SessionCard {
        SessionCard(
            blockID: block.id,
            title: details.task(for: block)?.title,
            commitment: details.commitment(for: block)?.title,
            startsAt: block.startsAt,
            endsAt: block.endsAt,
            plannedMinutes: block.plannedMinutes,
            resumedTimerMinutes: block.actualSource == .timer ? block.actualMinutes : nil
        )
    }
}

// MARK: - Durations, said out loud

public enum DurationText {
    /// "45 min", "2h", "1h 30m". Arithmetic only; nothing is rounded up to
    /// flatter a number.
    public static func spoken(_ minutes: Int) -> String {
        if minutes < 60 { return "\(minutes) min" }
        let hours = minutes / 60
        let rest = minutes % 60
        return rest == 0 ? "\(hours)h" : "\(hours)h \(rest)m"
    }
}
