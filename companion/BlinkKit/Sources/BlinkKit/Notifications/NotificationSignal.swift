import Foundation

// S2 · Notifications, as data (docs/COMPANION_SCREENS.md).
//
// "The most important surface in the app. Most days, this is the entire
// experience."
//
// Everything in this file is the CONTRACT, not an implementation. A signal
// composed on the device and a signal composed by the server in P15-10 are the
// same struct, carry the same `userInfo`, and fire the same action
// identifiers, which is the whole reason the remote scheduler can be dropped
// in behind `NotificationScheduler` without a view changing.

/// The four kinds S2 names, and there is no fifth.
public enum SignalKind: String, Codable, Sendable, CaseIterable, Identifiable {
    /// ~10 minutes before a planned session.
    case nudge
    /// First unlock before 10am, if today has sessions.
    case morningBrief = "morning_brief"
    /// After 5pm, only if today has ended unresolved blocks.
    case checkIn = "check_in"
    /// Only when the SERVER has one, at most one per day.
    case insight

    public var id: String { rawValue }

    /// The `UNNotificationCategory` identifier this kind registers under.
    /// The remote payload sets the identical string in its `aps.category`,
    /// so one set of registered actions serves both schedulers.
    public var categoryIdentifier: String { "blink.signal.\(rawValue)" }

    /// For the debug listing and the log. Never user-facing copy.
    public var label: String {
        switch self {
        case .nudge: return "nudge"
        case .morningBrief: return "morning brief"
        case .checkIn: return "check-in"
        case .insight: return "insight"
        }
    }
}

/// Where a signal's words came from.
///
/// S2 says the copy is "composed **server-side** from grounded data. The
/// device never writes notification copy." No endpoint returns finished
/// notification copy today (that is P15-10's Cloud Scheduler work), so the
/// local scheduler composes some of it from a payload it has just fetched.
/// This field records which, per signal, so the honesty claim is auditable
/// rather than assumed. Nothing renders it; the debug listing and the log
/// print it.
public enum SignalProvenance: String, Codable, Sendable {
    /// Every word came off the wire as a finished sentence
    /// (`brief.notification_body`, `insight.text`, `insight.evidence_text`).
    case serverComposed = "server"
    /// The device assembled the sentence, but every VALUE inside it came off
    /// the wire. See `SignalComposer`, which lists field by field.
    case deviceComposedFromServerData = "device_from_server_data"
}

// MARK: - Actions

/// The action identifiers, shared by both schedulers.
///
/// These strings are part of the wire contract: P15-10's APNs payload names
/// the same category, and the same buttons appear, and the same handler runs.
/// Changing one means changing the server too.
public enum SignalActionID {
    /// Nudge. Opens the app, because the timer is P15-06 and this app cannot
    /// yet start one. A background "start" that started nothing would be a
    /// claim the app cannot back.
    public static let startTimer = "blink.action.start_timer"
    /// Nudge. LOGS A REAL SKIP through `POST /checkin/resolve`. It does not
    /// snooze, and it does not leave the session standing
    /// (docs/COMPANION_SCREENS.md, S2 Rules).
    public static let notTonight = "blink.action.not_tonight"
    /// Morning brief. Opens the app. Writes nothing, claims nothing.
    public static let open = "blink.action.open"
    /// Check-in. `POST /checkin/resolve` outcome `done`, in the background.
    public static let done = "blink.action.done"
    /// Check-in. Outcome `partial`, with NO minute count: the app does not
    /// know one and the server leaves it None rather than inventing one
    /// (src/api/server.py, checkin_resolve).
    public static let partly = "blink.action.partly"
    /// Check-in. Outcome `skipped`.
    public static let skip = "blink.action.skip"
    /// Insight. The consent verdict, accepted.
    public static let adapt = "blink.action.adapt"
    /// Insight. The consent verdict, declined. Declined means never offered
    /// again (docs/COMPANION_SCREENS.md, S4).
    public static let leaveIt = "blink.action.leave_it"
}

/// One button on a notification.
public struct SignalAction: Sendable, Equatable {
    public let identifier: String
    /// The button's words. S2 gives these verbatim.
    public let title: String
    /// True when tapping it has to bring the app up. False means the system
    /// launches the app in the BACKGROUND and the handler runs with nobody
    /// looking, which is what S2 asks for: "Tapping Done should not need the
    /// app to open."
    public let opensApp: Bool

    public init(identifier: String, title: String, opensApp: Bool) {
        self.identifier = identifier
        self.title = title
        self.opensApp = opensApp
    }
}

// MARK: - Context

/// What an action needs in order to write, carried in `userInfo` and
/// reproduced byte for byte by a remote payload.
///
/// Deliberately small. A notification is not a place to cache state: it
/// carries the identifiers a write needs, and everything else is re-read from
/// the server at the moment of the write.
public struct SignalContext: Codable, Sendable, Equatable {
    /// The block an answer is about. Nil on the brief.
    public let blockID: String?
    /// The task's title, purely so a FOLLOW-UP can name what failed. Never a
    /// source of truth for anything written.
    public let taskTitle: String?
    /// The insight the consent verdict answers.
    public let insightID: String?

    public init(blockID: String? = nil, taskTitle: String? = nil, insightID: String? = nil) {
        self.blockID = blockID
        self.taskTitle = taskTitle
        self.insightID = insightID
    }

    /// The one `userInfo` key both schedulers use.
    public static let userInfoKey = "blink_signal"

    public var userInfo: [String: Any] {
        var payload: [String: Any] = [:]
        if let blockID { payload["block_id"] = blockID }
        if let taskTitle { payload["task_title"] = taskTitle }
        if let insightID { payload["insight_id"] = insightID }
        return [SignalContext.userInfoKey: payload]
    }

    /// Read a context back out of a delivered notification, local or remote.
    public static func read(from userInfo: [AnyHashable: Any]) -> SignalContext? {
        guard let payload = userInfo[userInfoKey] as? [String: Any] else { return nil }
        return SignalContext(
            blockID: payload["block_id"] as? String,
            taskTitle: payload["task_title"] as? String,
            insightID: payload["insight_id"] as? String
        )
    }
}

// MARK: - The signal

/// One notification, fully decided, waiting for a delivery mechanism.
public struct NotificationSignal: Sendable, Equatable, Identifiable {
    public let id: String
    public let kind: SignalKind
    /// Nil means no title line at all, which is what iOS wants when the whole
    /// message is one sentence.
    public let title: String?
    public let body: String
    /// The evidence line under an insight. Server words or nothing.
    public let subtitle: String?
    /// When it should arrive. Absolute, and derived from the server's clock
    /// (`ServerClock`), never from the device's idea of the day. This is what
    /// the coalescer measures gaps between, and what the log prints.
    public let deliverAt: Date
    /// The same instant expressed as a COUNTDOWN, in seconds, measured from
    /// the server's own `now` in the payload this signal was composed from.
    ///
    /// Both numbers exist because they answer different questions. `deliverAt`
    /// is a fact about the plan and belongs to the server's clock. The thing
    /// that actually fires a local notification is a device timer, and a
    /// device whose clock is a few minutes off the server would otherwise make
    /// "starts in ten minutes" false. Subtracting inside the server's own
    /// frame and handing iOS the resulting duration keeps the sentence true
    /// however wrong the phone's clock is.
    public let deliverAfter: TimeInterval
    public let actions: [SignalAction]
    public let context: SignalContext
    public let provenance: SignalProvenance

    public init(
        id: String,
        kind: SignalKind,
        title: String?,
        body: String,
        subtitle: String? = nil,
        deliverAt: Date,
        deliverAfter: TimeInterval,
        actions: [SignalAction],
        context: SignalContext,
        provenance: SignalProvenance
    ) {
        self.id = id
        self.kind = kind
        self.title = title
        self.body = body
        self.subtitle = subtitle
        self.deliverAt = deliverAt
        self.deliverAfter = deliverAfter
        self.actions = actions
        self.context = context
        self.provenance = provenance
    }

    /// The ledger key: one signal of this kind, about this thing, per local
    /// day. `day` is the SERVER's local day for the user, never the device's.
    public func ledgerKey(day: String) -> String {
        let subject = context.blockID ?? context.insightID ?? "-"
        return "\(day)|\(kind.rawValue)|\(subject)"
    }
}

// MARK: - The product rules

/// The numbers S2 states, named once so no view, scheduler or composer
/// restates them. The same reason `TodayState.checkInHour` exists.
public enum SignalRules {
    /// "~10 min before a planned session" (S2, Nudge).
    public static let nudgeLeadMinutes = 10

    /// "First unlock before 10am" (S2, Morning brief). The device cannot
    /// observe a first unlock, so this is used as a WINDOW, not a trigger:
    /// see `LocalNotificationScheduler` for what that costs and what P15-10
    /// buys back.
    public static let morningBriefBeforeHour = 10

    /// "After 5pm" (S2, Check-in). The same hour S1 flips its card on, so it
    /// is read from there rather than restated.
    public static var checkInHour: Int { TodayState.checkInHour }

    /// "Never two banners within 15 minutes; the later one waits or drops"
    /// (S2, Rules). This client drops, and says so in the receipt.
    public static let minimumGapMinutes = 15

    /// "Hard cap of three per day, enforced server-side by the existing
    /// budget" (S2, Rules) and `notification_budget = 3` in
    /// `src/sim/fake_store.py`.
    ///
    /// **This client does not enforce that budget and must never claim to.**
    /// There is no endpoint to read or decrement it from the device. This
    /// number is the ceiling on what THIS DEVICE will arrange for itself, a
    /// courtesy sized to match, and the server stays the only authority.
    /// `SignalCoalescer` says the same thing in more words.
    public static let deviceDailyCeiling = 3

    /// The shortest delay a "this is due now" signal can be given.
    /// `UNTimeIntervalNotificationTrigger` refuses zero, and a few seconds
    /// also lets someone put the phone down before it arrives.
    public static let dueNowDelaySeconds: TimeInterval = 5
}
