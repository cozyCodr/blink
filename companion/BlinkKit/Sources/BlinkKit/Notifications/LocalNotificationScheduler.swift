import Foundation
import UserNotifications

// The local implementation, and an honest account of what it cannot do.
//
// WHAT THIS GUARANTEES
//
//  1. Every signal it schedules was composed from a payload the SERVER
//     answered with, seconds earlier. Nothing is scheduled from cache, from a
//     guess, or from a timer with no data behind it.
//  2. It arranges only what the moment supports. A nudge whose lead has passed
//     is dropped rather than re-dated. A brief with no server body is not sent.
//     A check-in with nothing unresolved is not sent.
//  3. It is idempotent. Every arrange cancels this scheduler's own pending
//     requests and rebuilds them from the payload it just read, so a session
//     that was cancelled on the web stops being nudged the next time the app
//     runs.
//  4. It coalesces, as a courtesy, and says so in the receipt.
//
// WHAT IT CANNOT DO, AND WHY P15-10 EXISTS
//
//  * It cannot observe a first unlock, so S2's "first unlock before 10am"
//    becomes a WINDOW: the brief is arranged only if the app is arranging
//    while the user's local hour is still before ten. Close the app at 7am and
//    the brief does not arrive at 8.
//  * It cannot wake up. Every signal it holds was decided the last time the
//    app ran. If a block is cancelled, moved or completed on the web after
//    that, the nudge already scheduled is stale until the app next runs. The
//    stale copy is still made of true statements from the moment it was
//    composed; it is simply older than the plan.
//  * It cannot see the server's notification budget, and never claims to. See
//    SignalCoalescer.
//
// P15-10 replaces every one of those with a Cloud Scheduler sweep that
// composes at send time and decrements the real budget. It conforms to this
// same protocol, registers these same categories with these same action
// identifiers, and puts the same `SignalContext` in `userInfo`, so the views
// and `SignalActionHandler` do not change.

public final class LocalNotificationScheduler: NotificationScheduler, @unchecked Sendable {
    private let center: UNUserNotificationCenter
    private let details: any DetailsReading
    private let source: any SignalSourceReading
    private let composer: SignalComposer
    private let coalescer: SignalCoalescer

    public init(
        center: UNUserNotificationCenter = .current(),
        details: (any DetailsReading)? = nil,
        source: (any SignalSourceReading)? = nil,
        composer: SignalComposer = SignalComposer(),
        coalescer: SignalCoalescer = SignalCoalescer(),
        baseURL: URL = BlinkAPI.baseURL()
    ) {
        self.center = center
        self.details = details ?? BlinkDetailsClient(baseURL: baseURL)
        self.source = source ?? BlinkSignalClient(baseURL: baseURL)
        self.composer = composer
        self.coalescer = coalescer
    }

    // MARK: Permission

    public func authorization() async -> NotificationAuthorization {
        let settings = await center.notificationSettings()
        switch settings.authorizationStatus {
        case .notDetermined: return .notAsked
        case .denied: return .denied
        case .provisional: return .quiet
        case .authorized, .ephemeral: return .allowed
        @unknown default:
            // An answer this build does not recognise is not a yes. The app
            // does not promise to reach someone on a status it cannot read.
            return .denied
        }
    }

    public func requestAuthorization() async -> NotificationAuthorization {
        let current = await authorization()
        // Asking twice is not asking, it is nagging, and iOS answers the
        // second one from cache anyway.
        guard current == .notAsked else { return current }
        do {
            _ = try await center.requestAuthorization(options: [.alert, .sound, .badge])
        } catch {
            notificationLog("authorization: request failed")
        }
        return await authorization()
    }

    public func registerCategories() async {
        var categories: Set<UNNotificationCategory> = []
        for kind in SignalKind.allCases {
            categories.insert(
                UNNotificationCategory(
                    identifier: kind.categoryIdentifier,
                    actions: Self.actions(for: kind).map(Self.systemAction),
                    intentIdentifiers: [],
                    options: []
                )
            )
        }
        center.setNotificationCategories(categories)
    }

    /// The buttons for a kind, decided in ONE place so the category registered
    /// at launch and the signal composed later cannot disagree. A remote
    /// payload names only the category, so this table is what a P15-10
    /// notification's buttons will be too.
    public static func actions(for kind: SignalKind) -> [SignalAction] {
        switch kind {
        case .nudge:
            return [
                SignalAction(identifier: SignalActionID.startTimer, title: "Start timer", opensApp: true),
                SignalAction(identifier: SignalActionID.notTonight, title: "Not tonight", opensApp: false),
            ]
        case .morningBrief:
            return [SignalAction(identifier: SignalActionID.open, title: "Open", opensApp: true)]
        case .checkIn:
            return [
                SignalAction(identifier: SignalActionID.done, title: "Done", opensApp: false),
                SignalAction(identifier: SignalActionID.partly, title: "Partly", opensApp: false),
                SignalAction(identifier: SignalActionID.skip, title: "Skip", opensApp: false),
            ]
        case .insight:
            return [
                SignalAction(identifier: SignalActionID.adapt, title: "Adapt", opensApp: false),
                SignalAction(identifier: SignalActionID.leaveIt, title: "Leave it", opensApp: false),
            ]
        }
    }

    private static func systemAction(_ action: SignalAction) -> UNNotificationAction {
        UNNotificationAction(
            identifier: action.identifier,
            title: action.title,
            // No `.foreground` means iOS launches the app in the BACKGROUND
            // and calls the delegate with nobody looking, which is exactly
            // what S2 asks for. `.destructive` is never used: a skip is not a
            // deletion and red is not this product's vocabulary
            // (.agents/rules/agent-governance.md, "Misses get truth, not
            // shame").
            options: action.opensApp ? [.foreground] : []
        )
    }

    // MARK: Arranging

    @discardableResult
    public func arrangeToday(for session: BlinkSession) async -> ScheduleReceipt {
        let permission = await authorization()
        guard permission.canDeliver else {
            // Denied is a normal state. Nothing is scheduled, nothing is
            // apologised for, and the receipt says which it was.
            return ScheduleReceipt(blocked: .notAuthorised(permission))
        }

        await registerCategories()

        let payload: WorkspaceDetails
        do {
            payload = try await details.details(for: session)
        } catch DetailsError.notSignedIn {
            return ScheduleReceipt(blocked: .notSignedIn)
        } catch DetailsError.cancelled {
            return ScheduleReceipt(blocked: .cancelled)
        } catch DetailsError.refused {
            return ScheduleReceipt(blocked: .serverRefused)
        } catch {
            return ScheduleReceipt(blocked: .serverUnreachable)
        }

        let window = composer.kindsInWindow(for: payload)

        // The brief and the insight are the server's own sentences, so they
        // are only asked for when the window actually wants one. A `/trigger`
        // that cannot be reached costs those two kinds and nothing else: the
        // nudge and the check-in still go out.
        var brief: MorningBrief?
        if window.contains(.morningBrief) || window.contains(.insight) {
            brief = try? await source.morningBrief(for: session)
        }

        var candidates: [NotificationSignal] = []
        for kind in window {
            if let signal = composer.compose(kind, details: payload, brief: brief) {
                candidates.append(signal)
            }
        }
        // Earliest first, so the fifteen-minute rule drops the LATER one, as
        // S2 words it.
        candidates.sort { $0.deliverAt < $1.deliverAt }

        await cancelOwnRequests()

        let (admitted, refused) = coalescer.admit(
            candidates, day: payload.today, workspaceID: session.workspaceID
        )

        var scheduled: [NotificationSignal] = []
        var failures = refused
        for signal in admitted {
            if await submit(signal) {
                scheduled.append(signal)
            } else {
                failures.append((signal, .systemRefused))
            }
        }

        notificationLog("arranged \(scheduled.count), refused \(failures.count)")
        return ScheduleReceipt(scheduled: scheduled, refused: failures)
    }

    /// Hand one signal to the system. Returns false if the system refused it.
    private func submit(_ signal: NotificationSignal) async -> Bool {
        let content = UNMutableNotificationContent()
        if let title = signal.title { content.title = title }
        if let subtitle = signal.subtitle { content.subtitle = subtitle }
        content.body = signal.body
        content.sound = .default
        content.categoryIdentifier = signal.kind.categoryIdentifier
        content.userInfo = signal.context.userInfo
        // The thread groups a day's signals together in the notification list
        // rather than stacking four separate conversations.
        content.threadIdentifier = "blink.signals"

        // The countdown, not the wall-clock instant: see
        // `NotificationSignal.deliverAfter` for why the subtraction happens in
        // the server's frame.
        let after = max(signal.deliverAfter, SignalRules.dueNowDelaySeconds)
        let request = UNNotificationRequest(
            identifier: signal.id,
            content: content,
            trigger: UNTimeIntervalNotificationTrigger(timeInterval: after, repeats: false)
        )
        do {
            try await center.add(request)
            return true
        } catch {
            notificationLog("submit refused by the system: \(signal.kind.rawValue)")
            return false
        }
    }

    // MARK: Housekeeping

    public func pendingSignals() async -> [String] {
        await center.pendingNotificationRequests().map(\.identifier)
    }

    public func cancelEverything() async {
        await cancelOwnRequests()
        center.removeAllDeliveredNotifications()
    }

    /// Only this scheduler's own requests. It does not clear the whole centre,
    /// because a future item may put something else in it.
    private func cancelOwnRequests() async {
        let mine = await center.pendingNotificationRequests()
            .filter { request in
                SignalKind.allCases.contains {
                    request.content.categoryIdentifier == $0.categoryIdentifier
                }
            }
            .map(\.identifier)
        guard !mine.isEmpty else { return }
        center.removePendingNotificationRequests(withIdentifiers: mine)
    }

    #if DEBUG
    /// DEBUG SCAFFOLDING. Compose ONE named kind from the real payload and
    /// deliver it shortly, so each of S2's four can be seen and screenshotted.
    ///
    /// What this changes: the delivery time, and the hour window. What it does
    /// NOT change: the words, the buttons, the `userInfo`, or the requirement
    /// that the data actually supports the signal. A rehearsed check-in still
    /// needs a real unresolved ended block; a rehearsed insight still needs an
    /// insight the server surfaced. If the payload does not support the kind,
    /// this returns nil and the caller says so rather than showing a specimen.
    public func rehearse(
        _ kind: SignalKind,
        for session: BlinkSession,
        after seconds: TimeInterval
    ) async -> (signal: NotificationSignal?, blocked: ScheduleBlocked?) {
        let permission = await authorization()
        guard permission.canDeliver else { return (nil, .notAuthorised(permission)) }
        await registerCategories()

        let payload: WorkspaceDetails
        do {
            payload = try await details.details(for: session)
        } catch DetailsError.notSignedIn {
            return (nil, .notSignedIn)
        } catch DetailsError.refused {
            return (nil, .serverRefused)
        } catch {
            return (nil, .serverUnreachable)
        }

        var brief: MorningBrief?
        if kind == .morningBrief || kind == .insight {
            brief = try? await source.morningBrief(for: session)
        }
        guard let composed = composer.compose(kind, details: payload, brief: brief) else {
            return (nil, nil)
        }
        let shifted = NotificationSignal(
            id: composed.id,
            kind: composed.kind,
            title: composed.title,
            body: composed.body,
            subtitle: composed.subtitle,
            deliverAt: payload.now.addingTimeInterval(seconds),
            deliverAfter: seconds,
            actions: composed.actions,
            context: composed.context,
            provenance: composed.provenance
        )
        return (await submit(shifted) ? shifted : nil, nil)
    }
    #endif
}
