import Foundation
import OSLog

// THE SEAM. This file is the protocol; nothing here knows what delivers a
// notification.
//
// docs/COMPANION_ARCHITECTURE.md §4, Gap 3: push needs the server to decide
// WHEN, and that server work (Cloud Scheduler every five minutes, an APNs p8
// key in Secret Manager, the budget decremented in exactly one place) is
// P15-10. This item ships the local implementation behind the protocol so
// P15-10 is a swap and not a rewrite.
//
// WHAT THE SEAM HAS TO CARRY, for that swap to cost nothing at the view layer:
//
//  * ASKING. Both implementations need the same permission, in the same words,
//    with the same denied state.
//  * ARRANGING. The view says "today changed, sort out the signals" and does
//    not say what they are. The LOCAL scheduler answers by fetching, composing
//    and scheduling; the REMOTE one answers by registering this device with
//    `POST /v1/workspaces/{id}/devices` and letting the sweep decide. Same
//    call, same receipt, different truth behind it.
//  * REPORTING. What was arranged, and what was not, and why. A scheduler that
//    silently drops a signal is a scheduler nobody can audit.
//
// WHAT IS DELIBERATELY NOT ON THE SEAM: composing. `SignalComposer` is a free
// function of a payload, not a member of this protocol, precisely because
// P15-10 moves composition to the server and the protocol must not assume the
// device does it. The ACTIONS are not on it either: `SignalActionHandler` is
// shared, because S2 requires the same buttons to do the same writes whether
// the notification arrived from `UNUserNotificationCenter` or from APNs.

/// Diagnostics for the notification path. Same discipline as `detailsLog`: it
/// records WHERE something happened, never what it carried. No copy, no title,
/// no workspace id, no block id ever reaches this.
private let notificationLogger = Logger(
    subsystem: "dev.oapps.blink.companion", category: "signals"
)

public func notificationLog(_ message: String) {
    notificationLogger.notice("\(message, privacy: .public)")
}

// MARK: - Permission

/// What the person has said about notifications.
///
/// **Denied is a normal state, not an error.** Nothing in the app treats it as
/// a failure, apologises for it, or asks twice. It changes what the app can
/// promise, and the app says so plainly and moves on.
public enum NotificationAuthorization: String, Sendable, Equatable {
    /// Nobody has been asked yet.
    case notAsked
    /// Yes. Banners, sounds and the lock screen.
    case allowed
    /// Yes, but quietly: delivered to the notification list with no banner.
    /// iOS calls this provisional. It is a yes, and the app treats it as one.
    case quiet
    /// No. The only honest response is to stop promising to reach them.
    case denied

    /// Can this device deliver anything at all?
    public var canDeliver: Bool { self == .allowed || self == .quiet }
}

// MARK: - The receipt

/// Why a composed signal was not scheduled. Every one of these is a fact about
/// what this device chose to do, and every one of them is printable.
public enum SignalRefusal: Equatable, Sendable {
    /// Its subject was already covered by a signal arranged earlier today.
    case alreadyArrangedToday
    /// It would have landed within `SignalRules.minimumGapMinutes` of another.
    /// S2 says the later one "waits or drops"; this client drops.
    case tooCloseToAnother(minutes: Int)
    /// This device has already arranged its self-imposed ceiling for the day.
    /// NOT the server's budget. See `SignalCoalescer`.
    case deviceCeilingReached(ceiling: Int)
    /// Its moment is in the past. A notification cannot be delivered backwards
    /// and will not be re-dated to pretend otherwise.
    case momentPassed
    /// The system refused the request.
    case systemRefused
}

/// What an `arrange` actually did. Honest about both halves.
public struct ScheduleReceipt: Sendable, Equatable {
    public let scheduled: [NotificationSignal]
    public let refused: [(signal: NotificationSignal, reason: SignalRefusal)]
    /// Set when the arrange could not happen at all: no permission, no
    /// session, or a server nobody could reach. Nil means the arrange ran.
    public let blocked: ScheduleBlocked?

    public init(
        scheduled: [NotificationSignal] = [],
        refused: [(signal: NotificationSignal, reason: SignalRefusal)] = [],
        blocked: ScheduleBlocked? = nil
    ) {
        self.scheduled = scheduled
        self.refused = refused
        self.blocked = blocked
    }

    public static func == (lhs: ScheduleReceipt, rhs: ScheduleReceipt) -> Bool {
        lhs.blocked == rhs.blocked
            && lhs.scheduled == rhs.scheduled
            && lhs.refused.count == rhs.refused.count
            && zip(lhs.refused, rhs.refused).allSatisfy {
                $0.0.signal == $0.1.signal && $0.0.reason == $0.1.reason
            }
    }
}

/// Why nothing could be arranged. Distinguished the same way `DetailsError`
/// distinguishes: `refused` is something the server SAID, `unreachable` is
/// something nobody said, and the two never share a sentence.
public enum ScheduleBlocked: Equatable, Sendable {
    case notAuthorised(NotificationAuthorization)
    case notSignedIn
    case serverRefused
    case serverUnreachable
    /// We stopped asking. Nobody failed and nothing was learned.
    case cancelled
}

// MARK: - The protocol

/// Arranging today's signals, however they are delivered.
///
/// `LocalNotificationScheduler` (this item) and the remote scheduler (P15-10)
/// both conform. A view holds `any NotificationScheduler` and cannot tell.
public protocol NotificationScheduler: Sendable {
    /// What the person has already said. Never asks.
    func authorization() async -> NotificationAuthorization

    /// Ask, once. Returns what they said, including `.denied`, which is a
    /// normal answer.
    func requestAuthorization() async -> NotificationAuthorization

    /// Register the categories and their buttons with the system. Both
    /// implementations register the SAME identifiers, because a remote payload
    /// names a category the device must already know.
    func registerCategories() async

    /// Sort out today's signals for this account. Idempotent: calling it twice
    /// with the same server truth arranges the same thing once.
    @discardableResult
    func arrangeToday(for session: BlinkSession) async -> ScheduleReceipt

    /// Everything currently waiting to be delivered by this scheduler.
    func pendingSignals() async -> [String]

    /// Drop everything this scheduler has arranged. Used on sign-out: a signed
    /// out device must not go on speaking about someone's plan.
    func cancelEverything() async
}
