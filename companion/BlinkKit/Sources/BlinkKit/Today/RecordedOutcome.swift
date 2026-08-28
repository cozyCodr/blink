import Foundation

// S5's key, and the reason S5 cannot be reached any other way.
//
// docs/COMPANION_SCREENS.md, S5: "Reachable only from a server response
// containing a recorded outcome. There is no local path to this screen."
// docs/COMPANION_ARCHITECTURE.md §6: "there is no 'celebrate' code path that
// fires on a timer or a local guess."
//
// HOW THAT IS ENFORCED, mechanically rather than by discipline:
//
//  1. `CelebrationScreen` takes a `RecordedOutcome` and has no other
//     initialiser, so it cannot be presented without one.
//  2. `RecordedOutcome`'s memberwise initialiser is `internal`, and BlinkKit
//     is a separate module from the app target. Code in `companion/Blink/`
//     therefore CANNOT construct one. This is a compiler error, not a rule.
//  3. Inside BlinkKit the initialiser has exactly three call sites, all in
//     this file, and each takes a value that came off the wire: a decoded
//     `BlockPayload` from `GET /details`, a decoded `CheckinResolveResponse`
//     from `POST /checkin/resolve`, or a decoded `LogTimeResponse` from
//     `POST /blocks/{id}/log-time`. None can be built from a literal because
//     all three are decode-only.
//  4. Both factories return nil unless the server actually recorded an
//     outcome: a resolved status AND a minute count AND a source. A block the
//     server left `planned` produces nothing, and so does a `partial` with no
//     number (the API deliberately leaves that None rather than inventing how
//     far someone got, `src/api/server.py:1657`).
//
// `grep -rn "RecordedOutcome(" companion/` is the check, and it is expected to
// print only the three lines in this file.

/// Something the server has on record. Not a hope, not a local timer's guess,
/// not a plan that was merely made.
public struct RecordedOutcome: Equatable, Sendable, Identifiable {
    public var id: String { blockID }

    public let blockID: String
    /// The task's title, when the payload carried it.
    public let title: String?
    /// The number the server holds. This is the number S5 shows, and there is
    /// no other.
    public let minutes: Int
    /// Measured or self-reported. S5 reads completely differently for each and
    /// never dresses one as the other.
    public let source: ActualSource
    public let status: BlockStatus
    /// Consecutive kept days at the moment the server answered.
    public let streakDays: Int

    /// INTERNAL ON PURPOSE. See the note at the top of this file: this is the
    /// wall that stops the app target from reaching S5 on its own.
    init(
        blockID: String,
        title: String?,
        minutes: Int,
        source: ActualSource,
        status: BlockStatus,
        streakDays: Int
    ) {
        self.blockID = blockID
        self.title = title
        self.minutes = minutes
        self.source = source
        self.status = status
        self.streakDays = streakDays
    }

    /// True only for a timer-measured outcome. The full celebration is for
    /// this and nothing else.
    public var isMeasured: Bool { source == .timer }
}

// MARK: - The only two ways to get one

extension RecordedOutcome {
    /// Read an outcome off a block the server sent in `GET /details`.
    ///
    /// Returns nil for anything the server has not actually recorded.
    static func recorded(
        from block: BlockPayload,
        in details: WorkspaceDetails
    ) -> RecordedOutcome? {
        guard block.status == .done || block.status == .partial,
              let minutes = block.actualMinutes,
              let source = block.actualSource,
              minutes > 0
        else { return nil }
        return RecordedOutcome(
            blockID: block.id,
            title: details.task(for: block)?.title,
            minutes: minutes,
            source: source,
            status: block.status,
            streakDays: details.streak
        )
    }

    /// Read an outcome off the answer to `POST /checkin/resolve`. The server
    /// echoes what it WROTE (`actual_minutes` and `source` are read back off
    /// the block, `src/api/server.py:1670-1673`), so this is the record, not
    /// the request.
    static func recorded(
        from response: CheckinResolveResponse,
        title: String?,
        streakDays: Int
    ) -> RecordedOutcome? {
        guard let minutes = response.actualMinutes,
              let source = response.source,
              minutes > 0,
              let status = response.recordedStatus
        else { return nil }
        return RecordedOutcome(
            blockID: response.blockID,
            title: title,
            minutes: minutes,
            source: source,
            status: status,
            streakDays: streakDays
        )
    }

    /// Read an outcome off the answer to `POST /blocks/{id}/log-time` with
    /// `complete: true`. The server resolved the block by pure arithmetic
    /// against the planned span (`timed_block_status`, src/core/progress.py),
    /// wrote the outcome timer-sourced, and echoed the status it landed on
    /// (`block_status`, read back off the mutated block, src/api/server.py:1712).
    /// So this is the record the server holds, not the minutes the device
    /// hoped to write.
    ///
    /// Returns nil for anything short of a recorded completion: a
    /// `complete: false` progress write leaves `block_status == planned` and
    /// produces nothing, and a zero total produces nothing. The source is
    /// always `.timer` here, so this is the measured, heart-earning path.
    static func recorded(
        from response: LogTimeResponse,
        title: String?,
        streakDays: Int
    ) -> RecordedOutcome? {
        guard response.complete,
              response.blockStatus == .done || response.blockStatus == .partial,
              response.totalMinutes > 0
        else { return nil }
        return RecordedOutcome(
            blockID: response.blockID,
            title: title,
            minutes: response.totalMinutes,
            source: response.source,
            status: response.blockStatus,
            streakDays: streakDays
        )
    }
}
