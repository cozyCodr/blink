import Foundation

// The subset of `GET /v1/workspaces/{id}/details` that screen S1 reads.
//
// Deliberately a SUBSET. The real payload has twenty-one top-level keys
// (blocks, commitments, constraints, conversation, disruptions, findings,
// key_points, ledger_days, memory, milestones, now, onboarded, profile,
// questions, schedule_report, streak, tasks, timezone, today, workspace_id,
// zones) and S1 answers one question: "what is next, and am I on track?".
// Decoding the rest would invite a view to reach for it.
//
// Every date on the wire is a NAIVE UTC ISO string (`src/api/server.py:339`,
// the "P11-03 one clock" comment). Nothing here parses with the device's
// calendar or timezone; see `ServerClock`, which is the only thing in the app
// allowed to answer "which day is this?".

/// `Block.status` in `src/types/entities.py:77`.
public enum BlockStatus: String, Codable, Sendable, Equatable {
    case planned, done, partial, missed, cancelled

    /// The check-in has an answer for this block.
    public var isResolved: Bool {
        switch self {
        case .done, .partial, .missed: return true
        case .planned, .cancelled: return false
        }
    }
}

/// Where a block's `actual_minutes` came from.
/// `src/types/entities.py:82`: "timer" is MEASURED, "reported" is self-report.
/// The two are never added together anywhere in this app.
public enum ActualSource: String, Codable, Sendable, Equatable {
    /// The timer ran. This is the only number the app may call "tracked".
    case timer
    /// The user said so at check-in. Always named as their word, never as a
    /// measurement.
    case reported
}

public struct BlockPayload: Codable, Sendable, Equatable, Identifiable {
    public let id: String
    public let taskID: String
    /// Naive UTC.
    public let startsAt: Date
    /// Naive UTC.
    public let endsAt: Date
    public let status: BlockStatus
    public let actualMinutes: Int?
    public let actualSource: ActualSource?

    enum CodingKeys: String, CodingKey {
        case id
        case taskID = "task_id"
        case startsAt = "starts_at"
        case endsAt = "ends_at"
        case status
        case actualMinutes = "actual_minutes"
        case actualSource = "actual_source"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        taskID = try c.decode(String.self, forKey: .taskID)
        startsAt = try ServerClock.date(from: c.decode(String.self, forKey: .startsAt))
        endsAt = try ServerClock.date(from: c.decode(String.self, forKey: .endsAt))
        // An unknown status is not a reason to drop the whole payload, and it
        // is not a reason to guess either. It parks as `planned`, which is the
        // one value that claims nothing about an outcome.
        status = (try? c.decode(BlockStatus.self, forKey: .status)) ?? .planned
        actualMinutes = try c.decodeIfPresent(Int.self, forKey: .actualMinutes)
        actualSource = try? c.decodeIfPresent(ActualSource.self, forKey: .actualSource)
    }

    /// The span the plan asked for, in minutes. Arithmetic, not judgement.
    public var plannedMinutes: Int {
        max(0, Int(endsAt.timeIntervalSince(startsAt) / 60))
    }
}

public struct TaskPayload: Codable, Sendable, Equatable, Identifiable {
    public let id: String
    public let commitmentID: String
    public let title: String

    enum CodingKeys: String, CodingKey {
        case id
        case commitmentID = "commitment_id"
        case title
    }

    /// Memberwise, for the cache round trip and for tests.
    public init(id: String, commitmentID: String, title: String) {
        self.id = id
        self.commitmentID = commitmentID
        self.title = title
    }
}

public struct CommitmentPayload: Codable, Sendable, Equatable, Identifiable {
    public let id: String
    public let title: String

    /// Memberwise, for the cache round trip and for tests.
    public init(id: String, title: String) {
        self.id = id
        self.title = title
    }
}

/// One free stretch inside a ledger day, `src/api/server.py:437-439`.
/// Both bounds are naive UTC, read through `ServerClock` like every other date.
public struct FreeWindowPayload: Codable, Sendable, Equatable {
    /// Naive UTC.
    public let start: Date
    /// Naive UTC.
    public let end: Date

    public init(start: Date, end: Date) {
        self.start = start
        self.end = end
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        start = try ServerClock.date(from: c.decode(String.self, forKey: .start))
        end = try ServerClock.date(from: c.decode(String.self, forKey: .end))
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(ServerClock.string(from: start), forKey: .start)
        try c.encode(ServerClock.string(from: end), forKey: .end)
    }

    enum CodingKeys: String, CodingKey { case start, end }

    /// The span in minutes. Arithmetic, not judgement.
    public var minutes: Int { max(0, Int(end.timeIntervalSince(start) / 60)) }
}

/// One day of the capacity ledger, `src/api/server.py:429-443`. The plan view
/// reads two fields off it: the `available` free minutes and the `free_windows`
/// it published, which draw the "open water" the day still has. The other four
/// (`gross`, `constrained`, `calendar`, `reserve`) are deliberately left on the
/// wire — the plan does not draw them, so it does not decode them.
public struct LedgerDayPayload: Codable, Sendable, Equatable, Identifiable {
    public var id: String { date }
    /// The user's local calendar day, `YYYY-MM-DD`.
    public let date: String
    /// Free minutes the ledger computed for this day, BEFORE placement. The
    /// plan never quotes this beside a full run — see `PlanDay.openMinutes`.
    public let available: Int?
    public let freeWindows: [FreeWindowPayload]

    enum CodingKeys: String, CodingKey {
        case date, available
        case freeWindows = "free_windows"
    }

    public init(date: String, available: Int?, freeWindows: [FreeWindowPayload]) {
        self.date = date
        self.available = available
        self.freeWindows = freeWindows
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        date = try c.decode(String.self, forKey: .date)
        available = try c.decodeIfPresent(Int.self, forKey: .available)
        freeWindows = try c.decodeIfPresent([FreeWindowPayload].self, forKey: .freeWindows) ?? []
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(date, forKey: .date)
        try c.encodeIfPresent(available, forKey: .available)
        try c.encode(freeWindows, forKey: .freeWindows)
    }
}

/// The details bundle, as far as S1 is concerned.
public struct WorkspaceDetails: Codable, Sendable, Equatable {
    public let workspaceID: String
    /// The USER'S local calendar day, localised server-side
    /// (`src/api/server.py`, the P15-00 note). Authoritative. The device
    /// clock never gets a vote.
    public let today: String
    /// The server's instant, naive UTC.
    public let now: Date
    /// The IANA zone the server used to work out `today`. Null until the user
    /// has been told to the server, in which case the server used UTC and so
    /// does this app.
    public let timezone: String?
    /// Derived server-side at read time from block history
    /// (`src/core/progress.py:39`), never a stored counter.
    public let streak: Int
    public let onboarded: Bool
    public let blocks: [BlockPayload]
    public let tasks: [TaskPayload]
    public let commitments: [CommitmentPayload]
    /// The capacity ledger, one entry per day the server planned over. The plan
    /// view reads its free windows as "open water"; every other screen ignores
    /// it. Empty until the server has answered with a real plan.
    public let ledgerDays: [LedgerDayPayload]

    enum CodingKeys: String, CodingKey {
        case workspaceID = "workspace_id"
        case today, now, timezone, streak, onboarded, blocks, tasks, commitments
        case ledgerDays = "ledger_days"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        workspaceID = try c.decode(String.self, forKey: .workspaceID)
        today = try c.decode(String.self, forKey: .today)
        now = try ServerClock.date(from: c.decode(String.self, forKey: .now))
        timezone = try c.decodeIfPresent(String.self, forKey: .timezone)
        streak = try c.decodeIfPresent(Int.self, forKey: .streak) ?? 0
        onboarded = try c.decodeIfPresent(Bool.self, forKey: .onboarded) ?? false
        blocks = try c.decodeIfPresent([BlockPayload].self, forKey: .blocks) ?? []
        tasks = try c.decodeIfPresent([TaskPayload].self, forKey: .tasks) ?? []
        commitments = try c.decodeIfPresent([CommitmentPayload].self, forKey: .commitments) ?? []
        ledgerDays = try c.decodeIfPresent([LedgerDayPayload].self, forKey: .ledgerDays) ?? []
    }

    /// Memberwise, for the cache's own round trip and for tests. Encoding is
    /// symmetric with decoding so a cached payload reads back identically.
    public init(
        workspaceID: String,
        today: String,
        now: Date,
        timezone: String?,
        streak: Int,
        onboarded: Bool,
        blocks: [BlockPayload],
        tasks: [TaskPayload],
        commitments: [CommitmentPayload],
        ledgerDays: [LedgerDayPayload] = []
    ) {
        self.workspaceID = workspaceID
        self.today = today
        self.now = now
        self.timezone = timezone
        self.streak = streak
        self.onboarded = onboarded
        self.blocks = blocks
        self.tasks = tasks
        self.commitments = commitments
        self.ledgerDays = ledgerDays
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(workspaceID, forKey: .workspaceID)
        try c.encode(today, forKey: .today)
        try c.encode(ServerClock.string(from: now), forKey: .now)
        try c.encodeIfPresent(timezone, forKey: .timezone)
        try c.encode(streak, forKey: .streak)
        try c.encode(onboarded, forKey: .onboarded)
        try c.encode(blocks, forKey: .blocks)
        try c.encode(tasks, forKey: .tasks)
        try c.encode(commitments, forKey: .commitments)
        try c.encode(ledgerDays, forKey: .ledgerDays)
    }

    // MARK: Joins

    /// The task a block is for, or nil when the payload does not carry it.
    /// Nil means the app says less, never that it invents a title.
    public func task(for block: BlockPayload) -> TaskPayload? {
        tasks.first { $0.id == block.taskID }
    }

    /// The commitment a block ultimately serves.
    public func commitment(for block: BlockPayload) -> CommitmentPayload? {
        guard let task = task(for: block) else { return nil }
        return commitments.first { $0.id == task.commitmentID }
    }
}

extension BlockPayload {
    /// Memberwise, for the cache round trip and for tests.
    public init(
        id: String,
        taskID: String,
        startsAt: Date,
        endsAt: Date,
        status: BlockStatus,
        actualMinutes: Int?,
        actualSource: ActualSource?
    ) {
        self.id = id
        self.taskID = taskID
        self.startsAt = startsAt
        self.endsAt = endsAt
        self.status = status
        self.actualMinutes = actualMinutes
        self.actualSource = actualSource
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(id, forKey: .id)
        try c.encode(taskID, forKey: .taskID)
        try c.encode(ServerClock.string(from: startsAt), forKey: .startsAt)
        try c.encode(ServerClock.string(from: endsAt), forKey: .endsAt)
        try c.encode(status, forKey: .status)
        try c.encodeIfPresent(actualMinutes, forKey: .actualMinutes)
        try c.encodeIfPresent(actualSource, forKey: .actualSource)
    }
}
