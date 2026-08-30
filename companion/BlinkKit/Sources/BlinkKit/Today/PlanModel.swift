import Foundation

// P18-01 — the native plan, as pure arithmetic over one server payload.
//
// The same rule TodayState lives by: "the model judges, the code computes"
// (.agents/rules/agent-governance.md). Nothing here guesses a time, invents a
// title, or fills a gap. Every position is a straight fraction of a window the
// data defined, dated by the one clock the server published (ServerClock).
//
// This is the phone's answer to the web's horizon (src/web/app.js buildRun /
// spineGeom / openMinutes). The web draws a day as a horizontal RUN; the phone
// draws it as a vertical timeline, which is the shape a thumb reads. The three
// honesty marks are the same at both:
//   open water   free time the ledger published        (accent wash)
//   a placed run OUTLINE = planned, FILL = recorded     (solid timer, hatch reported)
//   the now-line the marker riding the shared axis
// A block with no actual has NO fill: an empty fill would read "zero measured"
// when the truth is "not measured", and that is the claim this layer avoids.

/// A slice of the day, in minutes since local midnight, that a timeline maps
/// positions onto. Derived from the data, never hardcoded past the waking-hours
/// fallback a plan-less day still deserves.
public struct DayWindow: Equatable, Sendable {
    /// Minute-of-day of the top edge (a whole hour).
    public let startMinute: Int
    /// Minute-of-day of the bottom edge (a whole hour).
    public let endMinute: Int

    public init(startMinute: Int, endMinute: Int) {
        self.startMinute = startMinute
        self.endMinute = endMinute
    }

    public var span: Int { max(1, endMinute - startMinute) }

    /// Hours the window covers, for the vertical timeline's height and its
    /// hour gridlines. First and last are whole hours by construction.
    public var wholeHours: [Int] {
        stride(from: startMinute, through: endMinute, by: 60).map { $0 / 60 }
    }

    /// Where a minute-of-day sits in the window, 0 at the top edge to 1 at the
    /// bottom. Clamped, so a stray value never draws outside the track.
    public func fraction(ofMinute minute: Int) -> Double {
        min(1, max(0, Double(minute - startMinute) / Double(span)))
    }

    /// Does the window actually contain this minute (not merely clamp to an
    /// edge)? The now-line only draws when now is genuinely inside.
    public func contains(minute: Int) -> Bool {
        minute >= startMinute && minute <= endMinute
    }

    /// The waking-hours fallback a day with no data still gets, mirroring the
    /// web's `spineGeom` (`if (lo == null) { start = 7*60; end = 22*60 }`).
    public static let waking = DayWindow(startMinute: 7 * 60, endMinute: 22 * 60)

    /// Floor the low bound to its hour, ceil the high bound to its hour, and
    /// never let the window squeeze below six hours (web `spineGeom`).
    static func around(minutes intervals: [(Int, Int)]) -> DayWindow {
        let lows = intervals.map(\.0)
        let highs = intervals.map(\.1)
        guard let lo = lows.min(), let hi = highs.max(), hi > lo else { return .waking }
        var start = max(0, (lo / 60) * 60)
        var end = min(24 * 60, Int((Double(hi) / 60).rounded(.up)) * 60)
        if end - start < 360 {                       // never below six hours
            end = min(24 * 60, start + 360)
            start = max(0, end - 360)
        }
        return DayWindow(startMinute: start, endMinute: end)
    }
}

/// How a placed block's recorded minutes were come by. The plan draws the two
/// differently and never sums them, exactly as TrackedLine refuses to.
public enum PlanFill: Equatable, Sendable {
    /// Nothing recorded against the plan yet. Draws as outline only.
    case none
    /// The timer measured it. Solid fill.
    case measured(minutes: Int)
    /// The user reported it at check-in. Hatched fill.
    case reported(minutes: Int)

    /// The recorded minutes, whatever the source. Zero when nothing is recorded.
    public var minutes: Int {
        switch self {
        case .none: return 0
        case .measured(let m), .reported(let m): return m
        }
    }
}

/// One placed session, ready for the timeline. A view reads this and never
/// reaches back into the payload.
public struct PlanBlock: Equatable, Sendable, Identifiable {
    public let id: String
    /// The task's title, or nil when the payload did not carry it. Nil means
    /// the view says less, never that it invents a name.
    public let title: String?
    public let commitment: String?
    public let startsAt: Date
    public let endsAt: Date
    /// Minute-of-day of the block's edges, in the user's zone.
    public let startMinute: Int
    public let endMinute: Int
    public let status: BlockStatus
    public let plannedMinutes: Int
    public let fill: PlanFill
    /// The block's planned window contains the server's `now`.
    public let isLive: Bool

    /// The recorded share of the planned span, 0 to 1, for the fill's length.
    /// A block with no actual has a zero share and so draws no fill at all.
    public var recordedFraction: Double {
        guard plannedMinutes > 0 else { return 0 }
        return min(1, Double(fill.minutes) / Double(plannedMinutes))
    }
}

/// One day of the plan: its own timeline window, its placed blocks, the open
/// water it still holds, and what was planned and recorded on it.
public struct PlanDay: Equatable, Sendable, Identifiable {
    public var id: String { date }
    /// The user's local calendar day, `YYYY-MM-DD`.
    public let date: String
    public let isToday: Bool
    /// Short weekday, e.g. "MON". Derived in the user's zone.
    public let weekdayShort: String
    /// Day of the month, e.g. 3.
    public let dayNumber: Int
    /// The day's own timeline bounds, for the vertical Day view.
    public let window: DayWindow
    public let blocks: [PlanBlock]
    /// Free windows mapped to minute-of-day, for the "open water" bands.
    public let freeBands: [(startMinute: Int, endMinute: Int)]
    /// Genuinely open minutes: the free windows minus the work already sitting
    /// in them, or the ledger's own figure when it published no windows. Nil
    /// when the ledger said nothing at all, so a view never quotes a made-up
    /// number (`PlanDay.openMinutes` mirrors the web's `openMinutes`).
    public let openMinutes: Int?
    public let plannedMinutes: Int
    public let measuredMinutes: Int
    public let reportedMinutes: Int
    /// The now-line's position in this day's window, 0 to 1, or nil when now is
    /// not this day or falls outside the window.
    public let nowFraction: Double?

    public var hasBlocks: Bool { !blocks.isEmpty }

    public static func == (lhs: PlanDay, rhs: PlanDay) -> Bool {
        lhs.date == rhs.date && lhs.isToday == rhs.isToday
            && lhs.window == rhs.window && lhs.blocks == rhs.blocks
            && lhs.openMinutes == rhs.openMinutes
            && lhs.plannedMinutes == rhs.plannedMinutes
            && lhs.measuredMinutes == rhs.measuredMinutes
            && lhs.reportedMinutes == rhs.reportedMinutes
            && lhs.nowFraction == rhs.nowFraction
            && lhs.freeBands.map(\.startMinute) == rhs.freeBands.map(\.startMinute)
            && lhs.freeBands.map(\.endMinute) == rhs.freeBands.map(\.endMinute)
    }
}

/// Everything the plan surface draws, derived once from one payload.
public struct PlanModel: Equatable, Sendable {
    /// The days the server planned over, in order. Today first when present is
    /// NOT assumed; the order is the server's date order.
    public let days: [PlanDay]
    /// The single window every Week run shares, so seven days line up to the
    /// minute (the web computes this once per render and hands it to each run).
    public let weekWindow: DayWindow
    /// Consecutive kept days, straight from the server. Zero renders no streak.
    public let streakDays: Int
    public let clock: ServerClock

    /// Today's day, when the plan actually covers it. Nil is honest: the plan
    /// may not touch today, and the Day view says so rather than drawing a
    /// phantom.
    public var today: PlanDay? { days.first(where: { $0.isToday }) }

    public init(details: WorkspaceDetails) {
        let clock = ServerClock(details: details)
        self.clock = clock
        self.streakDays = details.streak

        // Bucket every non-cancelled block by the local day its start falls on.
        // Cancelled blocks are invisible everywhere, exactly as TodayState and
        // the server's streak both treat them.
        var blocksByDate: [String: [BlockPayload]] = [:]
        for block in details.blocks where block.status != .cancelled {
            let day = clock.localDay(of: block.startsAt)
            blocksByDate[day, default: []].append(block)
        }

        let ledgerByDate = Dictionary(
            details.ledgerDays.map { ($0.date, $0) }, uniquingKeysWith: { a, _ in a })

        // The set of days to draw: the ledger's, plus any day that carries a
        // block but no ledger entry. Sorted, so the week reads left to right.
        let dates = Set(details.ledgerDays.map(\.date))
            .union(blocksByDate.keys)
            .sorted()

        // One shared window across every day's blocks and free windows, for the
        // week's alignment.
        var allIntervals: [(Int, Int)] = []
        for date in dates {
            for block in blocksByDate[date] ?? [] {
                allIntervals.append(PlanModel.minuteSpan(of: block, on: date, clock: clock))
            }
            for window in ledgerByDate[date]?.freeWindows ?? [] {
                allIntervals.append((clock.localMinuteOfDay(of: window.start),
                                     PlanModel.boundedEnd(of: window.end, on: date, clock: clock)))
            }
        }
        self.weekWindow = DayWindow.around(minutes: allIntervals)

        self.days = dates.map { date in
            PlanModel.day(
                date: date,
                blocks: (blocksByDate[date] ?? []).sorted { $0.startsAt < $1.startsAt },
                ledger: ledgerByDate[date],
                details: details,
                clock: clock
            )
        }
    }

    // MARK: One day

    static func day(
        date: String,
        blocks rawBlocks: [BlockPayload],
        ledger: LedgerDayPayload?,
        details: WorkspaceDetails,
        clock: ServerClock
    ) -> PlanDay {
        let isToday = date == clock.today

        let freeBands: [(startMinute: Int, endMinute: Int)] = (ledger?.freeWindows ?? [])
            .map { (clock.localMinuteOfDay(of: $0.start), boundedEnd(of: $0.end, on: date, clock: clock)) }
            .filter { $0.1 > $0.0 }
            .map { (startMinute: $0.0, endMinute: $0.1) }

        let planBlocks: [PlanBlock] = rawBlocks.map { block in
            let (startMin, endMin) = minuteSpan(of: block, on: date, clock: clock)
            let live = block.status == .planned
                && block.startsAt <= clock.now && clock.now < block.endsAt
            return PlanBlock(
                id: block.id,
                title: details.task(for: block)?.title,
                commitment: details.commitment(for: block)?.title,
                startsAt: block.startsAt,
                endsAt: block.endsAt,
                startMinute: startMin,
                endMinute: endMin,
                status: block.status,
                plannedMinutes: block.plannedMinutes,
                fill: fill(of: block),
                isLive: live
            )
        }

        // The day's own window: floored/ceiled around its blocks and free time,
        // waking-hours fallback when it has neither.
        var intervals: [(Int, Int)] = planBlocks.map { ($0.startMinute, $0.endMinute) }
        intervals += freeBands.map { ($0.startMinute, $0.endMinute) }
        let window = DayWindow.around(minutes: intervals)

        var measured = 0, reported = 0, planned = 0
        for block in planBlocks {
            planned += block.plannedMinutes
            switch block.fill {
            case .measured(let m): measured += m
            case .reported(let m): reported += m
            case .none: break
            }
        }

        let nowMinute = clock.localMinuteOfDayNow
        let nowFraction: Double? = (isToday && window.contains(minute: nowMinute))
            ? window.fraction(ofMinute: nowMinute)
            : nil

        let parsed = clock.calendarDay(from: date)

        return PlanDay(
            date: date,
            isToday: isToday,
            weekdayShort: parsed.weekdayShort,
            dayNumber: parsed.dayNumber,
            window: window,
            blocks: planBlocks,
            freeBands: freeBands,
            openMinutes: openMinutes(ledger: ledger, blocks: planBlocks, on: date, clock: clock),
            plannedMinutes: planned,
            measuredMinutes: measured,
            reportedMinutes: reported,
            nowFraction: nowFraction
        )
    }

    // MARK: Arithmetic

    /// A block's minute-of-day span on the day it starts. A block whose planned
    /// window rolls past local midnight is clamped to the day's end, so it
    /// never draws with a negative height.
    static func minuteSpan(of block: BlockPayload, on date: String, clock: ServerClock) -> (Int, Int) {
        let start = clock.localMinuteOfDay(of: block.startsAt)
        let end = boundedEnd(of: block.endsAt, on: date, clock: clock, floor: start)
        return (start, end)
    }

    private static func fill(of block: BlockPayload) -> PlanFill {
        guard let minutes = block.actualMinutes, minutes > 0, let source = block.actualSource else {
            return .none
        }
        switch source {
        case .timer: return .measured(minutes: minutes)
        case .reported: return .reported(minutes: minutes)
        }
    }

    /// Genuinely open minutes, the number the run actually draws (web
    /// `openMinutes`): the published free windows minus the work already in
    /// them. With no windows, the ledger's own `available`. With neither, nil —
    /// no figure is invented.
    static func openMinutes(
        ledger: LedgerDayPayload?, blocks: [PlanBlock], on date: String, clock: ServerClock
    ) -> Int? {
        let windows = (ledger?.freeWindows ?? [])
            .map { (clock.localMinuteOfDay(of: $0.start), boundedEnd(of: $0.end, on: date, clock: clock)) }
            .filter { $0.1 > $0.0 }
        guard !windows.isEmpty else { return ledger?.available }

        let spans = blocks.map { ($0.startMinute, $0.endMinute) }.filter { $0.1 > $0.0 }
        var total = 0
        for w in windows {
            // Subtract the placed spans that overlap this window, merged.
            let cuts = spans
                .map { (max(w.0, $0.0), min(w.1, $0.1)) }
                .filter { $0.1 > $0.0 }
                .sorted { $0.0 < $1.0 }
            var taken = 0, edge = w.0
            for cut in cuts {
                if cut.1 <= edge { continue }
                taken += cut.1 - max(edge, cut.0)
                edge = cut.1
            }
            total += (w.1 - w.0) - taken
        }
        return max(0, total)
    }

    /// A datetime's minute-of-day, clamped to the end of the day it belongs to
    /// when it has rolled past local midnight (or, with `floor`, to sit after a
    /// span's own start). 1440 is midnight-at-the-bottom of the window.
    private static func boundedEnd(
        of instant: Date, on date: String, clock: ServerClock, floor: Int? = nil
    ) -> Int {
        var minute = clock.localMinuteOfDay(of: instant)
        if clock.localDay(of: instant) != date { minute = 24 * 60 }
        if let floor, minute <= floor { minute = 24 * 60 }
        return minute
    }
}
