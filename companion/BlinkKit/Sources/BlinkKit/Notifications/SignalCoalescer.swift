import Foundation

// The caps, and an exact statement of which of them this device enforces.
//
// S2's Rules block says two things about volume:
//
//   "Hard cap of three per day, enforced server-side by the existing budget."
//   "Never two banners within 15 minutes; the later one waits or drops."
//
// docs/COMPANION_ARCHITECTURE.md §6 puts the same split in a table: "Server-
// side budget is authoritative; the app additionally coalesces."
//
// SO, PRECISELY:
//
//  * `notification_budget = 3` lives on the workspace store
//    (src/sim/fake_store.py) with `notifications_sent` and a daily reset. It is
//    decremented where sends happen, which is server-side. There is no
//    endpoint to read it, and none to decrement it, from a phone.
//
//  * Therefore this file does NOT enforce that budget and nothing in this app
//    may say that it does. What it enforces is a CLIENT-SIDE COURTESY over the
//    signals THIS DEVICE arranges for itself: never two within fifteen
//    minutes, and never more than `SignalRules.deviceDailyCeiling` in one of
//    the user's local days. The ceiling is set to the same 3 so the device
//    cannot be the noisier of the two, not because it is reading the budget.
//
//  * The two counts are not the same count. A signal this device arranges is
//    not a send the server made, and today the server makes none to a phone at
//    all (Gap 3 is unbuilt). When P15-10 lands, the remote scheduler decrements
//    the real budget in the one place that owns it, and this coalescer stops
//    being on the path for those signals entirely.
//
// The ledger below is per-account and per-LOCAL-DAY, and the day is the
// server's answer for this user, never the device's.

/// What this device has already arranged, and when.
///
/// Persisted, because the cap is a cap on a DAY and an app that forgot on
/// relaunch would arrange the same signal three times over breakfast.
public protocol SignalLedgering: Sendable {
    /// Signals arranged for this local day: key to intended delivery instant.
    func entries(day: String, workspaceID: String) -> [String: Date]
    /// Replace this day's whole record with `entries`.
    ///
    /// A whole-day write rather than a per-signal one, because an arrange is a
    /// REBUILD: `LocalNotificationScheduler` cancels every request it owns and
    /// then re-submits from the payload it has just read. A ledger that could
    /// only be added to would remember a nudge for a session that has since
    /// moved, and refuse to arrange it at its new time. See `admit`.
    func replaceDay(_ entries: [String: Date], day: String, workspaceID: String)
    func forget(workspaceID: String)
}

/// `UserDefaults`, keyed the same way `TodayStore`'s celebration seen-set is
/// (`blink.today.celebrated`, "day|blockID"), because it is the same kind of
/// small local bookkeeping about what this device has already done.
public struct DefaultsSignalLedger: SignalLedgering {
    private let defaults: UserDefaults
    private static let storageKey = "blink.signals.arranged"

    public init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    private func storageKey(_ workspaceID: String) -> String {
        "\(Self.storageKey).\(workspaceID)"
    }

    public func entries(day: String, workspaceID: String) -> [String: Date] {
        let raw = defaults.dictionary(forKey: storageKey(workspaceID)) as? [String: Double] ?? [:]
        return raw
            .filter { $0.key.hasPrefix("\(day)|") }
            .mapValues { Date(timeIntervalSince1970: $0) }
    }

    public func replaceDay(_ entries: [String: Date], day: String, workspaceID: String) {
        // Keys carry the day, so anything from another day is dead weight.
        // Keeping only today's also means a device that sat idle for a week
        // does not start up owing itself a quota.
        let raw = entries
            .filter { $0.key.hasPrefix("\(day)|") }
            .mapValues { $0.timeIntervalSince1970 }
        defaults.set(raw, forKey: storageKey(workspaceID))
    }

    public func forget(workspaceID: String) {
        defaults.removeObject(forKey: storageKey(workspaceID))
    }
}

/// Decides which of today's composed signals this device will actually
/// arrange. Pure, apart from the ledger it reads and writes.
public struct SignalCoalescer: Sendable {
    private let ledger: any SignalLedgering

    public init(ledger: any SignalLedgering = DefaultsSignalLedger()) {
        self.ledger = ledger
    }

    /// Filter, in the order given, and leave the ledger holding exactly what
    /// this device is now standing behind for the day. Refusals carry their
    /// reason.
    ///
    /// AN ARRANGE IS A REBUILD, NOT AN APPEND, and that is the whole reason
    /// `now` is a parameter. `LocalNotificationScheduler` cancels every
    /// pending request it owns and re-submits from the payload it has just
    /// read. Every ledger entry for the day is therefore in one of two states,
    /// and the two are treated differently:
    ///
    ///   * ITS MOMENT HAS PASSED (`deliverAt <= now`). The person has already
    ///     been spoken to. It stays in the ledger, it still counts toward the
    ///     ceiling and the fifteen-minute gap, and a fresh candidate about the
    ///     same subject is refused as `.alreadyArrangedToday`. Nobody gets the
    ///     same check-in twice because they opened the app again.
    ///
    ///   * IT IS STILL IN THE FUTURE. Its request was just cancelled, so it is
    ///     not standing anywhere any more, and the fresh candidate REPLACES
    ///     it, at whatever moment the fresh payload says. This is what makes a
    ///     moved session honest: the nudge follows the block to its new time
    ///     instead of the ledger refusing it and leaving the person with
    ///     nothing, or with a banner pointing at a time the plan abandoned.
    ///     A block that has since been resolved or has left the plan composes
    ///     no candidate at all, so its entry simply does not come back.
    ///
    /// `now` is the SERVER's instant, from the payload the candidates were
    /// composed from, never the device's clock (`WorkspaceDetails.now`).
    public func admit(
        _ candidates: [NotificationSignal],
        day: String,
        workspaceID: String,
        now: Date
    ) -> (admitted: [NotificationSignal], refused: [(signal: NotificationSignal, reason: SignalRefusal)]) {
        // Only the part of the day that has already been spoken survives the
        // rebuild untouched. Everything still ahead is up for recomposition.
        var standing = ledger.entries(day: day, workspaceID: workspaceID)
            .filter { $0.value <= now }

        var admitted: [NotificationSignal] = []
        var refused: [(signal: NotificationSignal, reason: SignalRefusal)] = []

        let gap = TimeInterval(SignalRules.minimumGapMinutes * 60)

        for signal in candidates {
            let key = signal.ledgerKey(day: day)

            // Either already spoken today, or a second candidate about the
            // same subject in this very pass. One per subject per day stands.
            if standing[key] != nil {
                refused.append((signal, .alreadyArrangedToday))
                continue
            }
            if standing.count >= SignalRules.deviceDailyCeiling {
                refused.append((signal, .deviceCeilingReached(ceiling: SignalRules.deviceDailyCeiling)))
                continue
            }
            if let clash = standing.values.first(where: {
                abs($0.timeIntervalSince(signal.deliverAt)) < gap
            }) {
                let minutes = Int((abs(clash.timeIntervalSince(signal.deliverAt)) / 60).rounded())
                refused.append((signal, .tooCloseToAnother(minutes: minutes)))
                continue
            }

            admitted.append(signal)
            standing[key] = signal.deliverAt
        }

        ledger.replaceDay(standing, day: day, workspaceID: workspaceID)
        return (admitted, refused)
    }

    public func forget(workspaceID: String) {
        ledger.forget(workspaceID: workspaceID)
    }
}
