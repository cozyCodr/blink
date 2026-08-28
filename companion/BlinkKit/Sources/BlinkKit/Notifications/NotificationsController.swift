import Foundation
import Observation

/// What a view holds, so that no view holds a scheduler.
///
/// This is the object that makes the P15-10 swap free: `TodayScreen` reads
/// `authorization`, calls `askIfNeeded()` and calls `arrange(for:)`, and there
/// is no property, method or type on this class that names
/// `UNUserNotificationCenter`, `LocalNotificationScheduler`, APNs or a device
/// token. Constructing it with a remote scheduler instead is a one-line change
/// at the app's root.
@MainActor
@Observable
public final class NotificationsController {
    /// What the person has said. `.notAsked` until they are asked.
    public private(set) var authorization: NotificationAuthorization = .notAsked
    /// The last arrange's receipt, for the debug listing and for tests. No
    /// product screen renders it.
    public private(set) var lastReceipt: ScheduleReceipt?

    @ObservationIgnored private let scheduler: any NotificationScheduler
    @ObservationIgnored private var hasAsked = false

    /// Nonisolated for the same reason `TodayStore.init` is: a `@State`
    /// property initializer runs outside the main actor's isolation.
    public nonisolated init(scheduler: (any NotificationScheduler)? = nil) {
        self.scheduler = scheduler ?? LocalNotificationScheduler()
    }

    /// Read the current answer without asking for one.
    public func refreshAuthorization() async {
        authorization = await scheduler.authorization()
    }

    /// Ask once, at a moment where the app has something real to offer.
    ///
    /// **A no is a normal answer.** It is recorded, the app stops promising to
    /// reach them, and nothing apologises or asks again. The system would
    /// answer a second prompt from cache anyway.
    public func askIfNeeded() async {
        guard !hasAsked else { return }
        hasAsked = true
        authorization = await scheduler.requestAuthorization()
    }

    /// Sort out today's signals. Safe to call on every foreground: the
    /// scheduler is idempotent.
    public func arrange(for session: BlinkSession) async {
        guard authorization.canDeliver else { return }
        lastReceipt = await scheduler.arrangeToday(for: session)
    }

    /// Sign-out. A signed out device must not go on speaking about a plan it
    /// can no longer read.
    public func standDown() async {
        await scheduler.cancelEverything()
        lastReceipt = nil
    }

    /// The one honest line about what this device can and cannot do, or nil
    /// when there is nothing to say. Rendered by S1's footer.
    ///
    /// Note what it does NOT say: nothing here mentions a daily budget. The
    /// server owns that number and this device cannot see it, so it does not
    /// speak for it (see SignalCoalescer).
    public var limitationLine: String? {
        switch authorization {
        case .denied:
            return "Notifications are off, so I cannot reach you when a session is due. You can turn them on in Settings."
        case .quiet:
            return "I will leave anything I have in your notification list rather than interrupting you."
        case .notAsked, .allowed:
            return nil
        }
    }
}
