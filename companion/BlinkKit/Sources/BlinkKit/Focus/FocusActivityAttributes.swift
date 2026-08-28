import Foundation

// The shape the Live Activity and its Dynamic Island presentations render, and
// the ONE place the in-app timer and the lock-screen timer read their numbers
// from. The app owns the Activity and pushes every update from the DEVICE while
// it runs (docs/COMPANION_ARCHITECTURE.md §4, Gap 4: "updates from the device
// while the app runs … ActivityKit push tokens are a v2 decision, not a v1
// requirement"). Nothing here talks to APNs.
//
// This type lives in BlinkKit so the app target and the BlinkActivity widget
// extension share it verbatim, exactly as the eyes and the tokens are shared.
// It is guarded by `canImport(ActivityKit)` so BlinkKit still builds anywhere
// the framework is absent.

#if canImport(ActivityKit)
import ActivityKit

/// What the timer is doing right now. The widget renders each phase
/// differently, and only `.running` uses a self-updating clock, so a paused or
/// idle Live Activity never keeps ticking on its own.
public enum FocusLiveState: String, Codable, Hashable, Sendable {
    /// Counting up. The widget shows a live `Text(timerInterval:)`.
    case running
    /// Frozen and dimmed. "Paused. Nothing is counting."
    case paused
    /// Past the planned end with no interaction. The widget stops counting and
    /// asks nothing itself (the ask lives in-app); it reads the neutral
    /// "still open" line rather than a number climbing unattended.
    case idle
    /// The session ended and the measured minutes are the server's now.
    case ended
}

public struct FocusActivityAttributes: ActivityAttributes {
    public struct ContentState: Codable, Hashable, Sendable {
        public var phase: FocusLiveState
        /// The anchor the running clock counts from: elapsed == now - runStart.
        /// Only meaningful while `phase == .running`; the widget uses it to
        /// drive `Text(timerInterval:)` with no push needed.
        public var runStart: Date
        /// The frozen elapsed, seconds, used whenever the clock is NOT running
        /// (paused, idle, ended). The single source both the app and the widget
        /// format, so the two surfaces never disagree.
        public var frozenSeconds: Double
        /// When the planned span ends, for the ring's fill and the "past the
        /// hour you planned" line. Absolute instant so the widget can compare
        /// it to its own now.
        public var plannedEnd: Date
        public var plannedMinutes: Int
        /// The measured minutes the SERVER holds, or nil before any write has
        /// landed. This is the only number either surface may present as
        /// "saved", and it is copied from a `log-time` response, never computed.
        public var savedMinutes: Int?

        public init(
            phase: FocusLiveState,
            runStart: Date,
            frozenSeconds: Double,
            plannedEnd: Date,
            plannedMinutes: Int,
            savedMinutes: Int?
        ) {
            self.phase = phase
            self.runStart = runStart
            self.frozenSeconds = frozenSeconds
            self.plannedEnd = plannedEnd
            self.plannedMinutes = plannedMinutes
            self.savedMinutes = savedMinutes
        }

        /// The instant elapsed counting should start from for a live timer.
        /// `Text(timerInterval: elapsedAnchor...distantFuture)` reads the same
        /// elapsed the app shows, because both derive it from these fields.
        public var elapsedAnchor: Date {
            runStart
        }
    }

    /// Identifies the block this Activity is for. Constant for the Activity's
    /// life; everything that changes lives in `ContentState`.
    public let blockID: String
    public let title: String?

    public init(blockID: String, title: String?) {
        self.blockID = blockID
        self.title = title
    }
}
#endif
