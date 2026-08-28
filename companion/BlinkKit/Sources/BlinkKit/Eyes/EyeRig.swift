import SwiftUI
import Observation

/// The eyes' nervous system: the blink channel, the idle loops, and the
/// emotion state. The Swift counterpart of `createEyes` in `src/web/app.js`.
///
/// Everything here is driven from `FaceMotion`. There is not a single timing
/// or travel literal in this file, because a literal here is the fork that
/// `data-face` exists to prevent.
///
/// Blink is an INDEPENDENT channel. It never replaces the emotion's `scaleY`,
/// it multiplies with it, exactly as the web composes
/// `scaleY(calc(var(--emo-sy) * var(--blink-sy)))`.
@MainActor
@Observable
public final class EyeRig {
    // MARK: Observable state, read by EyesView

    /// The held emotion, or the thinking state. nil is resting.
    public private(set) var emotion: EmotionName?
    /// The blink channel's vertical scale. 1 is open.
    public private(set) var blinkScaleY: CGFloat = 1
    /// The blink channel's horizontal scale. Folio is the only face that
    /// widens as it closes; everywhere else this stays 1.
    public private(set) var blinkScaleX: CGFloat = 1
    /// The idle glance drift, points.
    public private(set) var glanceOffset: CGFloat = 0
    /// The thinking look-around drift, points.
    public private(set) var thinkOffset: CGFloat = 0
    /// The thinking shimmer, as an additive brightness on top of the pose's.
    public private(set) var shimmer: Double = 0
    /// The celebrate bounce, points.
    public private(set) var bounceOffset: CGFloat = 0

    // MARK: Configuration

    // Configuration, not state: nothing observes these, and keeping them out
    // of the observation registrar is what lets `init` be nonisolated.
    @ObservationIgnored private var motion: FaceMotion
    @ObservationIgnored private var reduceMotion: Bool

    /// A procedural beat is running, or an emotion is held. Either way the
    /// random blink scheduler waits, mirroring the `emoting` flag in app.js.
    private var isBusy: Bool { emotion != nil || isRunningProceduralBeat }
    private var isRunningProceduralBeat = false

    private var blinkSchedulerTask: Task<Void, Never>?
    private var glanceSchedulerTask: Task<Void, Never>?
    private var blinkTask: Task<Void, Never>?
    private var holdTask: Task<Void, Never>?
    private var thinkTask: Task<Void, Never>?
    private var shimmerTask: Task<Void, Never>?
    private var bounceTask: Task<Void, Never>?

    /// Nonisolated so a view or an app can hold one in `@State` without the
    /// property initializer having to run on the main actor.
    public nonisolated init(motion: FaceMotion, reduceMotion: Bool = false) {
        self.motion = motion
        self.reduceMotion = reduceMotion
    }

    // MARK: Lifecycle

    /// Start the ambient loops. Safe to call more than once.
    public func start() {
        guard blinkSchedulerTask == nil else { return }
        blinkSchedulerTask = Task { [weak self] in await self?.runBlinkScheduler() }
        glanceSchedulerTask = Task { [weak self] in await self?.runGlanceScheduler() }
    }

    /// Stop every loop and settle the channels. Called when the view leaves.
    public func stop() {
        for task in [blinkSchedulerTask, glanceSchedulerTask, blinkTask,
                     holdTask, thinkTask, shimmerTask, bounceTask] {
            task?.cancel()
        }
        blinkSchedulerTask = nil
        glanceSchedulerTask = nil
        blinkTask = nil
        holdTask = nil
        thinkTask = nil
        shimmerTask = nil
        bounceTask = nil
        blinkScaleY = 1
        blinkScaleX = 1
        glanceOffset = 0
        thinkOffset = 0
        shimmer = 0
        bounceOffset = 0
    }

    /// Re-read the environment. The face can change under the rig, and so can
    /// the Reduced Motion setting, without the view being rebuilt.
    public func update(motion: FaceMotion, reduceMotion: Bool) {
        self.motion = motion
        let motionPreferenceChanged = self.reduceMotion != reduceMotion
        self.reduceMotion = reduceMotion
        guard motionPreferenceChanged else { return }
        if reduceMotion {
            // Ambient loops stand down at once, and anything mid-travel lands
            // on its resting value rather than freezing part-way.
            thinkTask?.cancel()
            shimmerTask?.cancel()
            bounceTask?.cancel()
            glanceOffset = 0
            thinkOffset = 0
            shimmer = 0
            bounceOffset = 0
        } else if emotion == .thinking {
            startThinkingLoops()
        }
    }

    // MARK: The public API — the mirror of window.__emote

    /// Show an emotion. Newest wins: any emotion already up is dropped first,
    /// with no ease-out, so two beats can never blend into a third thing.
    ///
    /// `hold` nil holds until `clearEmotion()`, which is what `curious` wants
    /// for the whole length of a clarify question. A `hold` auto-clears.
    ///
    /// Nothing in here decides WHETHER a beat is warranted. That judgement
    /// belongs to the caller and, per
    /// `.agents/rules/frontend-standards.md`, only ever to grounded data.
    public func emote(_ name: EmotionName, hold: Duration? = nil) {
        holdTask?.cancel()
        holdTask = nil

        if name == .satisfied {
            // Not a held class at all. One deliberate slow blink over
            // whatever the eyes are already doing (app.js:296-311).
            clearEmotion(immediate: true)
            runSlowBlink()
            return
        }

        stopThinkingLoops()
        bounceTask?.cancel()
        bounceOffset = 0

        // Reduced Motion: the shape still changes, it just arrives instantly.
        withAnimation(reduceMotion ? nil : motion.emotionAnimation) {
            emotion = name
        }

        if name == .thinking {
            startThinkingLoops()
        } else if !reduceMotion, name == .celebrate, motion.beats.bounceRise != nil {
            runBounce()
        }

        if let hold {
            holdTask = Task { [weak self] in
                try? await Task.sleep(for: hold)
                guard !Task.isCancelled else { return }
                self?.clearEmotion()
            }
        }
    }

    /// Back to resting. `immediate` skips the ease-out, which is what a
    /// newest-wins replacement needs.
    public func clearEmotion(immediate: Bool = false) {
        holdTask?.cancel()
        holdTask = nil
        stopThinkingLoops()
        bounceTask?.cancel()
        bounceOffset = 0
        guard emotion != nil else { return }
        withAnimation(immediate || reduceMotion ? nil : motion.releaseAnimation) {
            emotion = nil
        }
    }

    /// One blink now. `double` is the wake beat's two-in-a-row.
    public func blink(double: Bool = false) {
        blinkTask?.cancel()
        blinkTask = Task { [weak self] in await self?.performBlink(double: double) }
    }

    // MARK: Blink

    private func performBlink(double: Bool) async {
        let animation = reduceMotion ? nil : motion.blinkAnimation(emotionHeld: emotion != nil)
        withAnimation(animation) {
            blinkScaleY = motion.blinkSquash
            blinkScaleX = motion.blinkStretch
        }
        try? await Task.sleep(for: .seconds(motion.blinkHold))
        guard !Task.isCancelled else { return }
        withAnimation(animation) {
            blinkScaleY = 1
            blinkScaleX = 1
        }
        guard double else { return }
        try? await Task.sleep(for: .seconds(motion.beats.doubleBlinkGap))
        guard !Task.isCancelled else { return }
        await performBlink(double: false)
    }

    private func runBlinkScheduler() async {
        while !Task.isCancelled {
            let wait = Double.random(in: motion.blinkInterval)
            try? await Task.sleep(for: .seconds(wait))
            guard !Task.isCancelled else { return }
            // app.js:187: no random blinks under Reduced Motion, none while an
            // emotion holds, and none while thinking (the eyes stay squinted).
            guard !reduceMotion, !isBusy else { continue }
            await performBlink(double: Double.random(in: 0..<1) < 0.25)
        }
    }

    /// `satisfied`. app.js:296-311: the transform transition is temporarily
    /// slowed, one blink runs long, then everything is restored.
    private func runSlowBlink() {
        blinkTask?.cancel()
        blinkTask = Task { [weak self] in
            guard let self else { return }
            guard !self.reduceMotion else {
                // Reduced Motion gets a plain blink (app.js:299).
                await self.performBlink(double: false)
                return
            }
            self.isRunningProceduralBeat = true
            defer { self.isRunningProceduralBeat = false }
            withAnimation(self.motion.slowBlinkAnimation) {
                self.blinkScaleY = self.motion.blinkSquash
                self.blinkScaleX = self.motion.blinkStretch
            }
            try? await Task.sleep(for: .seconds(self.motion.beats.slowBlinkHold))
            guard !Task.isCancelled else { return }
            withAnimation(self.motion.slowBlinkAnimation) {
                self.blinkScaleY = 1
                self.blinkScaleX = 1
            }
            try? await Task.sleep(for: .seconds(self.motion.beats.slowBlinkClose))
        }
    }

    // MARK: Idle glance

    private func runGlanceScheduler() async {
        while !Task.isCancelled {
            let wait = Double.random(in: motion.idle.glanceInterval)
            try? await Task.sleep(for: .seconds(wait))
            guard !Task.isCancelled else { return }
            // app.js:198-200: only when idle, never under Reduced Motion.
            guard !reduceMotion, !isBusy else { continue }
            await runGlance()
        }
    }

    /// face.css:1510-1514 — drift out at 28%, back across at 58%, settle by
    /// 100%. Held as stops rather than a keyframe so a face can drift further
    /// without a second code path.
    private func runGlance() async {
        let drift = motion.idle.glanceDrift
        guard drift.count == 2 else { return }
        let total = motion.idle.glanceDuration
        let legs: [(CGFloat, Double)] = [
            (drift[0], total * 0.28),
            (drift[1], total * 0.30),
            (0, total * 0.42)
        ]
        for (value, duration) in legs {
            withAnimation(.easeInOut(duration: duration)) { glanceOffset = value }
            try? await Task.sleep(for: .seconds(duration))
            guard !Task.isCancelled else { glanceOffset = 0; return }
        }
    }

    // MARK: Thinking

    private func startThinkingLoops() {
        guard !reduceMotion else { return }
        stopThinkingLoops()
        thinkTask = Task { [weak self] in await self?.runLookAround() }
        shimmerTask = Task { [weak self] in await self?.runShimmer() }
    }

    private func stopThinkingLoops() {
        thinkTask?.cancel()
        shimmerTask?.cancel()
        thinkTask = nil
        shimmerTask = nil
        if thinkOffset != 0 || shimmer != 0 {
            withAnimation(reduceMotion ? nil : motion.releaseAnimation) {
                thinkOffset = 0
                shimmer = 0
            }
        }
    }

    /// face.css:145-150 — stops at 25%, 55%, 80%, then home.
    private func runLookAround() async {
        let drift = motion.idle.thinkLookDrift
        guard drift.count == 3 else { return }
        let total = motion.idle.thinkLookPeriod
        let legs: [(CGFloat, Double)] = [
            (drift[0], total * 0.25),
            (drift[1], total * 0.30),
            (drift[2], total * 0.25),
            (0, total * 0.20)
        ]
        while !Task.isCancelled {
            for (value, duration) in legs {
                withAnimation(.easeInOut(duration: duration)) { thinkOffset = value }
                try? await Task.sleep(for: .seconds(duration))
                guard !Task.isCancelled else { return }
            }
        }
    }

    /// face.css:155-158 — brightness rides up and back down each cycle.
    private func runShimmer() async {
        guard let period = motion.idle.shimmerPeriod else { return }
        let peak = motion.idle.shimmerPeak - 1
        while !Task.isCancelled {
            withAnimation(.easeInOut(duration: period / 2)) { shimmer = peak }
            try? await Task.sleep(for: .seconds(period / 2))
            guard !Task.isCancelled else { return }
            withAnimation(.easeInOut(duration: period / 2)) { shimmer = 0 }
            try? await Task.sleep(for: .seconds(period / 2))
            guard !Task.isCancelled else { return }
        }
    }

    // MARK: Celebrate bounce

    /// face.css:282-286 — up at 40%, a small overshoot down at 70%, home by
    /// 100%, repeated `bounceCount` times.
    private func runBounce() {
        guard let rise = motion.beats.bounceRise, rise.count == 2 else { return }
        bounceTask = Task { [weak self] in
            guard let self else { return }
            let total = self.motion.beats.bouncePeriod
            let legs: [(CGFloat, Double)] = [
                (rise[0], total * 0.40),
                (rise[1], total * 0.30),
                (0, total * 0.30)
            ]
            for _ in 0..<self.motion.beats.bounceCount {
                for (value, duration) in legs {
                    withAnimation(self.motion.emotionCurve.animation(duration: duration)) {
                        self.bounceOffset = value
                    }
                    try? await Task.sleep(for: .seconds(duration))
                    guard !Task.isCancelled else { self.bounceOffset = 0; return }
                }
            }
        }
    }
}

// MARK: - How long a beat should stay up

public extension EmotionName {
    /// The hold the web uses for this beat, or nil where the beat is held
    /// open until something clears it (curious stays up for the whole
    /// question; thinking stays up for the whole turn).
    func defaultHold(in motion: FaceMotion) -> Duration? {
        switch self {
        case .thinking, .curious:
            return nil
        case .heart:
            return .seconds(motion.heartHold)          // app.js:5753
        case .celebrate:
            return .seconds(motion.celebrationHold)    // app.js:5939
        case .satisfied:
            return nil                                 // procedural, self-ending
        default:
            return .seconds(motion.celebrationHold)
        }
    }
}
