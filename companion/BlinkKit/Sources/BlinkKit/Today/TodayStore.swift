import Foundation
import Observation

/// How fresh the thing on screen is.
public enum Freshness: Equatable, Sendable {
    /// The server answered during this run and this is what it said.
    case live
    /// This is the last payload we hold, and the last attempt to reconcile it
    /// did not land. The screen MUST show the stamp
    /// (docs/COMPANION_SCREENS.md S1: "Offline | Last cached payload, with
    /// 'as of 9:41' under the tracked line"; the planner calls the stamp
    /// REQUIRED, not optional).
    case cached(receivedAt: Date)
    /// Nothing to show at all, and no cache to fall back on.
    case nothing
}

/// Everything S1 needs, and the only thing that talks to the network on its
/// behalf.
///
/// The shape of the refresh is the one docs/COMPANION_ARCHITECTURE.md §5 asks
/// for: render the cache instantly, then reconcile. Failure NEVER clears what
/// is on screen and never substitutes a plausible number; it only adds the
/// stamp that says how old the truth is.
@MainActor
@Observable
public final class TodayStore {
    // MARK: What the screen reads

    public private(set) var state: TodayState?
    public private(set) var freshness: Freshness = .nothing
    /// A request is genuinely in flight. The eyes may think while this is
    /// true, and only while it is true.
    public private(set) var isLoading = false
    /// The server said no. Distinct from unreachable, because only a real
    /// refusal earns an apology (`.agents/rules/frontend-standards.md`).
    public private(set) var wasRefused = false
    /// The session is dead and the app should go back to S7.
    public private(set) var needsSignIn = false
    /// A check-in write that did not land. The copy says so rather than going
    /// quiet (COMPANION_SCREENS.md, "Action failed").
    public private(set) var lastWriteFailed = false
    /// The block whose answer is currently being written.
    public private(set) var writingBlockID: String?

    /// What the notification signals would be built from, reduced to one
    /// comparable value. Empty until the server has answered at least once.
    ///
    /// WHY THIS EXISTS. S2's signals are composed from today's blocks, and
    /// they are only worth rebuilding when today's blocks have actually
    /// changed. Rebuilding on every refresh would churn the ledger's record of
    /// what this device has already said, for nothing; rebuilding only on the
    /// first payload (which is what a `store.state != nil` key does) leaves a
    /// nudge pointing at a time the plan has since abandoned. So the screen
    /// keys its arrange on this instead.
    ///
    /// WHAT COUNTS AS "THE PLAN CHANGED", exactly: the user's local day rolled
    /// over, or a block that starts today was added, removed, moved, resized
    /// or resolved. Nothing else is in here, because nothing else changes what
    /// a signal would say or when it would arrive. It is built from the
    /// SERVER's payload only, and it is set on a live answer only: a cache
    /// read or a failed reconcile leaves it exactly where it was, so an
    /// unreachable server can never trigger a rebuild.
    public private(set) var planFingerprint = ""

    /// The earned moment, or nil. Set ONLY by `RecordedOutcome`'s factories,
    /// which only accept a decoded server response. See RecordedOutcome.swift
    /// for how that is enforced.
    public private(set) var celebration: RecordedOutcome?

    // MARK: Configuration

    @ObservationIgnored private let client: any DetailsReading
    @ObservationIgnored private let cache: any DetailsCaching
    @ObservationIgnored private let defaults: UserDefaults
    @ObservationIgnored private var details: WorkspaceDetails?
    @ObservationIgnored private var session: BlinkSession?
    /// Outcomes this device has already shown, as "day|blockID".
    @ObservationIgnored private var seen: Set<String> = []
    @ObservationIgnored private var seenLoaded = false

    private static let seenKey = "blink.today.celebrated"

    /// Nonisolated so a view can hold one in `@State`, the same reason
    /// `EyeRig.init` is (a property initializer runs outside the main actor's
    /// isolation).
    public nonisolated init(
        client: (any DetailsReading)? = nil,
        cache: any DetailsCaching = FileDetailsCache(),
        defaults: UserDefaults = .standard,
        baseURL: URL = BlinkAPI.baseURL()
    ) {
        self.client = client ?? BlinkDetailsClient(baseURL: baseURL)
        self.cache = cache
        self.defaults = defaults
    }

    // MARK: Lifecycle

    /// First paint: the cache, instantly, with its stamp. Then reconcile.
    public func load(session: BlinkSession) async {
        self.session = session
        loadSeenIfNeeded()
        if state == nil, let cached = cache.load(workspaceID: session.workspaceID) {
            details = cached.details
            state = TodayState(details: cached.details)
            freshness = .cached(receivedAt: cached.receivedAt)
            // Deliberately NOT celebrating off the cache. A cached payload is
            // not a server response arriving; it is what we already knew.
            markAllSeen(in: cached.details)
        }
        await refresh()
    }

    /// Foreground, pull-to-refresh, and the tail of a successful write.
    public func refresh() async {
        guard let session else { return }
        isLoading = true
        wasRefused = false
        defer { isLoading = false }

        do {
            let fresh = try await client.details(for: session)
            let receivedAt = Date()
            let hadSomethingBefore = details != nil
            details = fresh
            state = TodayState(details: fresh)
            freshness = .live
            planFingerprint = TodayStore.fingerprint(of: fresh)
            cache.save(fresh, receivedAt: receivedAt, workspaceID: session.workspaceID)
            considerCelebrating(fresh, isFirstEverPaint: !hadSomethingBefore)
        } catch DetailsError.notSignedIn {
            needsSignIn = true
        } catch DetailsError.cancelled {
            // We stopped asking. Nobody failed, nothing was learned, and so
            // nothing on screen changes: no stamp, no apology.
            return
        } catch DetailsError.refused(let status) {
            detailsLog("refresh refused, status \(status)")
            wasRefused = true
            degrade()
        } catch {
            degrade()
        }
    }

    /// Keep what we have and say how old it is. Never a guess, never a zero
    /// presented as a fact.
    private func degrade() {
        guard let session else { return }
        if state != nil, case .cached = freshness { return }
        if let cached = cache.load(workspaceID: session.workspaceID) {
            if state == nil {
                details = cached.details
                state = TodayState(details: cached.details)
                markAllSeen(in: cached.details)
            }
            freshness = .cached(receivedAt: cached.receivedAt)
        } else if state == nil {
            freshness = .nothing
        } else {
            // We have a payload from this run but the reconcile failed. It is
            // still the last thing the server told us, so it keeps its stamp.
            freshness = .cached(receivedAt: Date())
        }
    }

    /// The digest behind `planFingerprint`. Sorted by block id, so two
    /// payloads that describe the same day in a different order read as the
    /// same day, which they are.
    static func fingerprint(of details: WorkspaceDetails) -> String {
        let clock = ServerClock(details: details)
        let today = details.blocks
            .filter { clock.isToday($0.startsAt) }
            .map { block in
                [
                    block.id,
                    ServerClock.string(from: block.startsAt),
                    ServerClock.string(from: block.endsAt),
                    block.status.rawValue,
                ].joined(separator: "~")
            }
            .sorted()
        return ([details.today] + today).joined(separator: "|")
    }

    // MARK: The check-in write

    /// One check-in answer, through the same endpoint the web writes.
    /// docs/COMPANION_ARCHITECTURE.md §6: "All mutations go through the
    /// existing endpoints. No local mutation that the web cannot see."
    public func resolve(block blockID: String, as outcome: CheckinOutcome, title: String?) async {
        guard let session else { return }
        writingBlockID = blockID
        lastWriteFailed = false
        defer { writingBlockID = nil }

        do {
            let response = try await client.resolve(block: blockID, as: outcome, for: session)
            // The celebration comes off the SERVER'S echo of what it wrote,
            // not off the request we sent.
            let recorded = RecordedOutcome.recorded(
                from: response,
                title: title,
                streakDays: state?.streakDays ?? 0
            )
            await refresh()
            if let recorded, !hasSeen(recorded) {
                markSeen(recorded)
                celebration = recorded
            }
        } catch DetailsError.notSignedIn {
            needsSignIn = true
        } catch DetailsError.cancelled {
            // The view went away mid-write. The request may well have landed,
            // so the app says nothing either way and the next refresh settles
            // it. Claiming a failure here would be as wrong as claiming a save.
            return
        } catch {
            detailsLog("checkin write did not land")
            lastWriteFailed = true
        }
    }

    public func dismissCelebration() {
        celebration = nil
    }

    // MARK: Celebration bookkeeping

    /// Look for an outcome the server has recorded TODAY that this device has
    /// not shown yet.
    ///
    /// `isFirstEverPaint` is the guard against ambushing someone on launch
    /// with a beat for work they finished hours ago on the web. On the first
    /// payload of a run with nothing cached, today's outcomes are marked seen
    /// WITHOUT celebrating: the app has no evidence any of it is news.
    private func considerCelebrating(_ details: WorkspaceDetails, isFirstEverPaint: Bool) {
        let clock = ServerClock(details: details)
        let todaysOutcomes = details.blocks
            .filter { clock.isToday($0.startsAt) }
            .compactMap { RecordedOutcome.recorded(from: $0, in: details) }

        guard !isFirstEverPaint else {
            for outcome in todaysOutcomes { markSeen(outcome, day: details.today) }
            return
        }
        guard celebration == nil else { return }
        for outcome in todaysOutcomes where !hasSeen(outcome, day: details.today) {
            markSeen(outcome, day: details.today)
            celebration = outcome
            return
        }
    }

    private func markAllSeen(in details: WorkspaceDetails) {
        let clock = ServerClock(details: details)
        for block in details.blocks where clock.isToday(block.startsAt) {
            if let outcome = RecordedOutcome.recorded(from: block, in: details) {
                markSeen(outcome, day: details.today)
            }
        }
    }

    private func key(_ outcome: RecordedOutcome, day: String?) -> String {
        "\(day ?? details?.today ?? "")|\(outcome.blockID)"
    }

    private func hasSeen(_ outcome: RecordedOutcome, day: String? = nil) -> Bool {
        loadSeenIfNeeded()
        return seen.contains(key(outcome, day: day))
    }

    private func markSeen(_ outcome: RecordedOutcome, day: String? = nil) {
        loadSeenIfNeeded()
        seen.insert(key(outcome, day: day))
        // Keys carry the day, so yesterday's are dead weight. Keep the list
        // short rather than letting it grow for the life of the install.
        if seen.count > 200 {
            seen = Set(seen.suffix(100))
        }
        defaults.set(Array(seen), forKey: Self.seenKey)
    }

    private func loadSeenIfNeeded() {
        guard !seenLoaded else { return }
        seenLoaded = true
        seen = Set(defaults.stringArray(forKey: Self.seenKey) ?? [])
    }
}
