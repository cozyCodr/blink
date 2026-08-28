import Foundation
import Observation
import OSLog

/// Focus-session diagnostics. Same discipline as `detailsLog`: it records WHERE
/// something happened, never a title, a minute count, or a workspace id.
private let focusLogger = Logger(subsystem: "dev.oapps.blink.companion", category: "focus")

public func focusLog(_ message: String) {
    focusLogger.notice("\(message, privacy: .public)")
}

// S3 · Focus session (docs/COMPANION_SCREENS.md), the timer as a source of
// truth the in-app screen, the Live Activity and the Dynamic Island all read
// from. One controller, one elapsed, one persisted total.
//
// THE RULE THAT MATTERS, enforced structurally (COMPANION_SCREENS.md S3,
// COMPANION_ARCHITECTURE.md §6 "Numbers are the timer's numbers"):
//
//   "the number shown is the number written."
//
// How it cannot drift here:
//   • The live ELAPSED (`elapsedSeconds`) is a clock. It is the measured time
//     since this session began, and it is only ever presented AS a running
//     clock, never as a saved total.
//   • The SAVED total (`savedMinutes`) is only ever assigned from a `log-time`
//     response's `total_minutes`. There is no line in this file that sets
//     `savedMinutes` from the local clock. The compiler will show you: the only
//     writes to it are in `applyWriteResult`.
//   • When a write fails, `persist` becomes `.unsaved`, the screen says the
//     minutes are not saved yet, and it retries. It shows no total it has not
//     persisted.
//   • The celebration (S5) is reached only through
//     `RecordedOutcome.recorded(from: LogTimeResponse)`, which requires the
//     SERVER to have resolved the block. A local timer can never trigger it.
//
// RECONCILE BEFORE SHOWING A NUMBER (S3, "Backgrounded / killed"): when a
// session is re-opened, the block's server-held measured minutes are handed in
// as `resumedMinutes` and become the floor. The device never presents a
// locally-guessed elapsed from a run it did not witness; it starts a fresh
// measured segment on top of the server's truth.

@MainActor
@Observable
public final class FocusController {
    // MARK: What the screen and the activity read

    public enum Stage: Equatable, Sendable {
        case running
        case paused
        /// Past the planned end with no interaction. "Still going?"
        case idleAsk
        /// Written and resolved. The celebration, if any, is handed off.
        case finished
    }

    /// The save status, named honestly. This is what the honesty line reads.
    public enum Persist: Equatable, Sendable {
        /// Everything measured so far is on the server.
        case upToDate
        /// A write is in flight.
        case saving
        /// A write did not land. `pending` minutes are measured but NOT saved.
        /// The screen says so and a retry is scheduled.
        case unsaved(pending: Int)
    }

    public private(set) var stage: Stage = .running
    public private(set) var persist: Persist = .upToDate
    /// The measured minutes the SERVER holds. nil until a write has landed.
    /// ONLY assigned in `applyWriteResult`, from a `log-time` response.
    public private(set) var savedMinutes: Int?
    /// The earned moment, or nil. Set only from a server-resolved completion.
    public private(set) var recordedOutcome: RecordedOutcome?
    /// The bearer went dead mid-session. The screen bounces to sign-in.
    public private(set) var needsSignIn = false
    /// Whether a lock-screen Live Activity is actually up, for the screen's
    /// own honest note ("also on your lock screen").
    public private(set) var liveActivityIsUp = false

    // MARK: Immutable session facts

    public let blockID: String
    public let title: String?
    public let plannedMinutes: Int

    // MARK: The clock model
    //
    // While running, elapsed == now - startInstant. `startInstant` is the
    // effective anchor: it already folds in any earlier paused stints and any
    // reconciled server floor, so there is exactly one expression for elapsed.
    // While not running, elapsed == frozenSeconds.

    private var startInstant: Date = .distantFuture
    private var frozenSeconds: Double = 0
    /// Recomputed whenever `startInstant` moves. elapsed reaches the planned
    /// span at this instant, which is where "past the hour you planned" and the
    /// idle question begin.
    public private(set) var plannedEnd: Date = .distantFuture

    /// The measured total the server has accepted (timer minutes). The write
    /// delta is always `flooredElapsedMinutes - sentMinutes`, so repeated
    /// accumulating writes stay in step with the server and never double-count.
    private var sentMinutes = 0
    private var lastInteraction: Date = .distantPast
    private var streakDaysAtStart = 0

    // MARK: Configuration

    @ObservationIgnored private let client: any DetailsReading
    @ObservationIgnored private let session: BlinkSession
    @ObservationIgnored private let resumedMinutes: Int?
    #if canImport(ActivityKit)
    @ObservationIgnored private lazy var liveActivity = FocusLiveActivityController()
    #endif
    @ObservationIgnored private var tickTask: Task<Void, Never>?
    @ObservationIgnored private var retryTask: Task<Void, Never>?
    @ObservationIgnored private var finishObserver: NSObjectProtocol?

    /// The idle grace, seconds. The spec's ">5 min past planned end". A DEBUG
    /// launch arg shortens it so the "Still going?" path is demonstrable in a
    /// simulator without waiting out a real planned span.
    private let idleGraceSeconds: Double
    #if DEBUG
    /// DEBUG: force the next completion write to fail once, so the "not saved
    /// yet" state and the retry can be screenshotted deterministically. Real
    /// failures (a stopped server) show the identical state.
    @ObservationIgnored private var failNextWrite: Bool
    /// DEBUG: shorten the planned span to this many seconds so the overrun and
    /// idle-detection paths are demonstrable in a simulator without waiting out
    /// a real 45- or 90-minute block. Affects only the ring's timing and when
    /// "past the hour" / "Still going?" appear; the write still records the
    /// real measured minutes.
    @ObservationIgnored private let debugPlannedSeconds: Double?
    #endif

    private static let runningMarkerKey = "blink.focus.running"

    public nonisolated init(
        blockID: String,
        title: String?,
        plannedMinutes: Int,
        resumedMinutes: Int? = nil,
        session: BlinkSession,
        client: (any DetailsReading)? = nil,
        baseURL: URL = BlinkAPI.baseURL(),
        defaults: UserDefaults = .standard
    ) {
        self.blockID = blockID
        self.title = title
        self.plannedMinutes = plannedMinutes
        self.resumedMinutes = resumedMinutes
        self.session = session
        self.client = client ?? BlinkDetailsClient(baseURL: baseURL)
        // `integer(forKey:)` coerces a launch-argument string ("300") to an Int;
        // `object(_:) as? Int` would not, because the argument domain stores it
        // as a String.
        self.idleGraceSeconds = defaults.object(forKey: "blinkFocusIdleGraceSeconds") != nil
            ? Double(defaults.integer(forKey: "blinkFocusIdleGraceSeconds"))
            : 300
        #if DEBUG
        self.failNextWrite = defaults.bool(forKey: "blinkFocusFailNextWrite")
        self.debugPlannedSeconds = defaults.object(forKey: "blinkFocusDebugPlannedSeconds") != nil
            ? Double(defaults.integer(forKey: "blinkFocusDebugPlannedSeconds"))
            : nil
        #endif
    }

    // MARK: Lifecycle

    /// Start counting. `streakDays` is the streak known when the session began,
    /// used only to phrase the celebration's "Day N stays alive".
    public func begin(streakDays: Int) {
        streakDaysAtStart = streakDays
        // Reconcile: the server's measured floor, if any, before any number.
        if let resumed = resumedMinutes, resumed > 0 {
            sentMinutes = resumed
            savedMinutes = resumed
        }
        // Anchor so elapsed folds in the reconciled floor with no seam.
        startInstant = Date().addingTimeInterval(-Double(sentMinutes) * 60)
        recomputePlannedEnd()
        stage = .running
        lastInteraction = Date()
        writeRunningMarker()
        startTick()
        startActivity()
        observeFinishHandoff()
        // A Done tapped on the lock screen while the app was away.
        if FocusHandoff.consumeFinishRequest() {
            Task { await finish() }
        }
    }

    /// Leave the screen. Ambient work stops; the session's truth is on the
    /// server (or marked unsaved), so nothing is lost by tearing this down.
    public func end() {
        tickTask?.cancel()
        retryTask?.cancel()
        if let finishObserver {
            NotificationCenter.default.removeObserver(finishObserver)
        }
    }

    // MARK: Elapsed, the one clock

    public func elapsedSeconds(asOf now: Date = Date()) -> Double {
        stage == .running ? max(0, now.timeIntervalSince(startInstant)) : frozenSeconds
    }

    public func flooredElapsedMinutes(asOf now: Date = Date()) -> Int {
        Int(elapsedSeconds(asOf: now) / 60)
    }

    /// 0...1 toward the planned span. Capped at 1: past the plan the ring is
    /// full and the copy carries the overrun, not a ring past its end.
    public func progress(asOf now: Date = Date()) -> Double {
        guard plannedMinutes > 0 else { return elapsedSeconds(asOf: now) > 0 ? 1 : 0 }
        return min(1, elapsedSeconds(asOf: now) / (Double(plannedMinutes) * 60))
    }

    /// Past the planned span, still counting, still neutral (S3 "Over planned
    /// span"). Only meaningful while running.
    public func isOverrun(asOf now: Date = Date()) -> Bool {
        stage == .running && now >= plannedEnd
    }

    // MARK: User actions (each one is an interaction, for idle detection)

    public func pause() {
        guard stage == .running else { return }
        retryTask?.cancel()
        freeze()
        stage = .paused
        note()
        // A significant pause persists progress (S3 "on stop and on
        // significant pause"), so a kill while paused loses nothing.
        Task { await write(complete: false) }
        pushActivity()
    }

    public func resume() {
        guard stage == .paused else { return }
        unfreeze()
        stage = .running
        note()
        pushActivity()
    }

    /// "Still going?" → yes. The silent gap since the planned end is discarded
    /// (see `freeze` cap); counting resumes from the capped elapsed.
    public func stillGoing() {
        guard stage == .idleAsk else { return }
        unfreeze()
        stage = .running
        note()
        pushActivity()
    }

    /// "Still going?" → I stopped. Finish with the capped, measured minutes.
    public func iStopped() {
        guard stage == .idleAsk else { return }
        Task { await finish() }
    }

    /// The primary Done. Writes the measured minutes and resolves the block.
    public func done() {
        Task { await finish() }
    }

    public func retryNow() {
        retryTask?.cancel()
        Task { await finish() }
    }

    public func dismissOutcome() {
        recordedOutcome = nil
    }

    private func note() { lastInteraction = Date() }

    // MARK: Freeze / unfreeze

    private func freeze(capAtPlannedSpan: Bool = false) {
        var seconds = elapsedSeconds()
        if capAtPlannedSpan {
            // Idle: do not credit the silent overrun past the planned span.
            seconds = min(seconds, Double(plannedMinutes) * 60)
        }
        frozenSeconds = seconds
    }

    private func unfreeze() {
        startInstant = Date().addingTimeInterval(-frozenSeconds)
        recomputePlannedEnd()
    }

    private func recomputePlannedEnd() {
        var span = Double(plannedMinutes) * 60
        #if DEBUG
        if let debugPlannedSeconds { span = debugPlannedSeconds }
        #endif
        plannedEnd = startInstant.addingTimeInterval(span)
    }

    // MARK: The idle tick

    private func startTick() {
        tickTask?.cancel()
        tickTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(1))
                guard let self else { return }
                await self.checkIdle()
            }
        }
    }

    /// Past the planned end AND silent for the grace window: stop counting and
    /// ask. Never accrue minutes nobody was present for (S3 "Idle detected …
    /// Never keep counting silently").
    private func checkIdle() {
        guard stage == .running else { return }
        let now = Date()
        guard now >= plannedEnd else { return }
        guard now.timeIntervalSince(lastInteraction) >= idleGraceSeconds else { return }
        freeze(capAtPlannedSpan: true)
        stage = .idleAsk
        focusLog("idle detected, asking")
        pushActivity()
    }

    // MARK: The write, the only path to a saved number

    private func finish() async {
        // Stop the clock the moment Done is pressed: what is measured is
        // measured, and nothing accrues during the round trip.
        retryTask?.cancel()
        if stage == .running { freeze() }
        else if stage == .idleAsk { /* already frozen and capped */ }
        stage = .paused   // frozen while the write is in flight
        await write(complete: true)
    }

    /// Never cancel `retryTask` in here: the retry runs INSIDE that task, so a
    /// self-cancel would make the URLSession call throw `.cancelled` and the
    /// save would look clean when it is not. Entry points that supersede a
    /// pending retry (`finish`, `pause`, `retryNow`) cancel it before calling.
    private func write(complete: Bool) async {
        let target = flooredElapsedMinutes()
        let delta = max(0, target - sentMinutes)
        persist = .saving

        #if DEBUG
        if failNextWrite {
            failNextWrite = false
            focusLog("write: forced failure (debug)")
            handleWriteFailure(target: target, complete: complete)
            return
        }
        #endif

        do {
            let response = try await client.logTime(
                block: blockID, elapsedMinutes: delta, complete: complete, for: session
            )
            applyWriteResult(response, complete: complete)
        } catch DetailsError.notSignedIn {
            needsSignIn = true
        } catch DetailsError.cancelled {
            // Nothing learned. Leave the save status as it was; the next action
            // settles it. Claiming a failure here would be as wrong as a save.
            persist = savedMinutes.map { _ in .upToDate } ?? .upToDate
        } catch {
            focusLog("write: did not land")
            handleWriteFailure(target: target, complete: complete)
        }
    }

    /// The ONLY writer of `savedMinutes` and `sentMinutes`. Both come straight
    /// off the server's echo. This is the whole of "the number shown is the
    /// number written".
    private func applyWriteResult(_ response: LogTimeResponse, complete: Bool) {
        sentMinutes = response.totalMinutes
        savedMinutes = response.totalMinutes
        persist = .upToDate

        if complete {
            stage = .finished
            clearRunningMarker()
            // Celebration is server-only: this factory returns nil unless the
            // server actually resolved the block.
            recordedOutcome = RecordedOutcome.recorded(
                from: response, title: title, streakDays: streakDaysAtStart
            )
            endActivity(final: response)
        } else {
            pushActivity()
        }
    }

    private func handleWriteFailure(target: Int, complete: Bool) {
        // Show nothing as saved that is not saved. The pending minutes are
        // named as unsaved, and a retry is scheduled.
        persist = .unsaved(pending: target)
        if complete { stage = .paused }   // stay open; a total was NOT recorded
        retryTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(4))
            guard let self, !Task.isCancelled else { return }
            await self.write(complete: complete)
        }
    }

    // MARK: The Live Activity

    private func startActivity() {
        #if canImport(ActivityKit)
        liveActivity.start(blockID: blockID, title: title, state: contentState())
        liveActivityIsUp = liveActivity.isRunning
        #endif
    }

    private func pushActivity() {
        #if canImport(ActivityKit)
        liveActivity.update(contentState())
        #endif
    }

    private func endActivity(final response: LogTimeResponse? = nil) {
        #if canImport(ActivityKit)
        var state = contentState()
        state.phase = .ended
        if let response { state.savedMinutes = response.totalMinutes }
        liveActivity.end(state)
        liveActivityIsUp = false
        #endif
    }

    #if canImport(ActivityKit)
    private func contentState() -> FocusActivityAttributes.ContentState {
        let running = stage == .running
        let liveState: FocusLiveState
        switch stage {
        case .running: liveState = .running
        case .paused: liveState = .paused
        case .idleAsk: liveState = .idle
        case .finished: liveState = .ended
        }
        return FocusActivityAttributes.ContentState(
            phase: liveState,
            runStart: startInstant,
            frozenSeconds: running ? 0 : frozenSeconds,
            plannedEnd: plannedEnd,
            plannedMinutes: plannedMinutes,
            savedMinutes: savedMinutes
        )
    }
    #endif

    // MARK: The finish handoff from the lock screen

    private func observeFinishHandoff() {
        finishObserver = NotificationCenter.default.addObserver(
            forName: FocusHandoff.finishRequested, object: nil, queue: .main
        ) { [weak self] _ in
            _ = FocusHandoff.consumeFinishRequest()
            Task { @MainActor in await self?.finish() }
        }
    }

    // MARK: The running marker (reconcile after a kill)

    private func writeRunningMarker() {
        UserDefaults.standard.set([
            "block_id": blockID,
            "workspace_id": session.workspaceID
        ], forKey: Self.runningMarkerKey)
    }

    private func clearRunningMarker() {
        UserDefaults.standard.removeObject(forKey: Self.runningMarkerKey)
    }

    /// The block a killed session was running against, if any, so the app can
    /// re-open it and reconcile against the server rather than guess.
    public static func interruptedBlockID(workspaceID: String) -> String? {
        guard let marker = UserDefaults.standard.dictionary(forKey: runningMarkerKey),
              marker["workspace_id"] as? String == workspaceID else { return nil }
        return marker["block_id"] as? String
    }
}
