import SwiftUI
import BlinkKit

// S3 · Focus session, in-app (docs/COMPANION_SCREENS.md).
//
// "the task title, a large elapsed readout, a ring filling toward the planned
// span, and Pause / Done. The eyes hold the focused ambient state, exactly like
// the web."
//
// THE EYES: the web's "focused ambient" is not an emotion beat. It is the
// `now-focused` class (src/web/css/face.css:344), which only dims the glow and
// slows the breath while the timer runs. There is no `focused` pose in the
// twelve-emotion vocabulary and this screen invents none: it clears any held
// beat so the eyes simply rest and breathe, which is that ambient.
//
// THE NUMBER: the large readout is the live measured CLOCK, and it is only ever
// a clock. The only "saved" number on screen comes from `savedMinutes`, which
// the controller copies from a `log-time` response and never computes. When a
// write fails the screen says the minutes are not saved yet; it shows no total
// it has not persisted. See FocusController for the structural guarantee.
struct FocusScreen: View {
    @Environment(\.face) private var face
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    let rig: EyeRig
    let streakDays: Int
    var onClose: () -> Void
    var onSignedOut: () -> Void

    @State private var controller: FocusController

    init(
        controller: FocusController,
        rig: EyeRig,
        streakDays: Int,
        onClose: @escaping () -> Void,
        onSignedOut: @escaping () -> Void
    ) {
        _controller = State(wrappedValue: controller)
        self.rig = rig
        self.streakDays = streakDays
        self.onClose = onClose
        self.onSignedOut = onSignedOut
    }

    var body: some View {
        ZStack {
            face.ground.ignoresSafeArea()

            VStack(spacing: face.layout.sectionGap) {
                Spacer(minLength: 0)
                header
                dial
                statusLine
                Spacer(minLength: 0)
                controls
            }
            .padding(.horizontal, face.layout.screenMargin)
            .padding(.bottom, face.layout.cardPaddingBottom)
        }
        .onAppear {
            // The focused ambient: rest the eyes, no beat.
            rig.clearEmotion()
            controller.begin(streakDays: streakDays)
        }
        .onDisappear { controller.end() }
        .onChange(of: controller.needsSignIn) { _, dead in
            if dead { onSignedOut() }
        }
        .fullScreenCover(item: Binding(
            get: { controller.recordedOutcome },
            set: { if $0 == nil { controller.dismissOutcome() } }
        )) { outcome in
            CelebrationScreen(outcome: outcome, rig: rig) {
                controller.dismissOutcome()
                onClose()
            }
            .face(face)
        }
    }

    // MARK: Header

    @ViewBuilder
    private var header: some View {
        VStack(spacing: face.layout.tightGap) {
            Text(controller.title ?? "Focus session")
                .font(face.cardTitleFont)
                .foregroundStyle(face.ink)
                .multilineTextAlignment(.center)
            Text("\(DurationText.spoken(controller.plannedMinutes)) planned")
                .font(face.metaFont)
                .foregroundStyle(face.faint)
        }
        .frame(maxWidth: .infinity)
    }

    // MARK: The dial (ring + elapsed), redrawn each second

    private var dial: some View {
        TimelineView(.periodic(from: .now, by: 1)) { context in
            let now = context.date
            let running = controller.stage == .running
            let progress = controller.progress(asOf: now)
            ZStack {
                Circle()
                    .stroke(face.line, lineWidth: ringWidth)
                Circle()
                    .trim(from: 0, to: progress)
                    .stroke(
                        (running ? face.accent : face.muted),
                        style: StrokeStyle(lineWidth: ringWidth, lineCap: .round)
                    )
                    .rotationEffect(.degrees(-90))
                    .animation(reduceMotion ? nil : .linear(duration: 1), value: progress)

                VStack(spacing: face.layout.tightGap) {
                    Text(clock(controller.elapsedSeconds(asOf: now)))
                        .font(face.numberFont)
                        .monospacedDigit()
                        .foregroundStyle(running ? face.ink : face.muted)
                        .opacity(running ? 1 : 0.55)
                    ringCaption(now: now, progress: progress)
                }
            }
            .frame(width: dialSize, height: dialSize)
            .accessibilityElement(children: .combine)
            .accessibilityLabel(accessibilityReadout(now: now))
        }
        .frame(maxWidth: .infinity)
    }

    @ViewBuilder
    private func ringCaption(now: Date, progress: Double) -> some View {
        switch controller.stage {
        case .running where controller.isOverrun(asOf: now):
            Text(overrunLine(now: now))
                .font(face.metaFont)
                .foregroundStyle(face.muted)
        case .running where progress >= 0.9:
            // S3: "At 90% of planned, the ring completes and reads 'enough to
            // count'." Honest: the block resolves done at 90% server-side too
            // (timed_block_status, src/core/progress.py:117).
            Text("enough to count")
                .font(face.metaFont)
                .foregroundStyle(face.accent)
        case .paused:
            Text("Paused. Nothing is counting.")
                .font(face.metaFont)
                .foregroundStyle(face.faint)
        case .idleAsk:
            Text("Waiting on you")
                .font(face.metaFont)
                .foregroundStyle(face.warm)
        default:
            Text("measuring")
                .font(face.metaFont)
                .foregroundStyle(face.faint)
        }
    }

    // MARK: The honesty / save line

    @ViewBuilder
    private var statusLine: some View {
        VStack(spacing: face.layout.rowGap) {
            // The save status, said plainly. Never a total that is not saved.
            switch controller.persist {
            case .unsaved(let pending):
                Text(pending > 0
                     ? "Those \(DurationText.spoken(pending)) are measured but not saved yet. I will try again."
                     : "Your minutes are not saved yet. I will try again.")
                    .font(face.secondaryFont)
                    .foregroundStyle(face.warm)
                    .multilineTextAlignment(.center)
                Button("Try now") { controller.retryNow() }
                    .font(face.bodyFont)
                    .foregroundStyle(face.accent)
            case .saving:
                Text("Saving your minutes.")
                    .font(face.metaFont)
                    .foregroundStyle(face.faint)
            case .upToDate:
                if let saved = controller.savedMinutes {
                    // A server-held number, shown only from `savedMinutes`.
                    Text("\(DurationText.spoken(saved)) saved so far")
                        .font(face.metaFont)
                        .foregroundStyle(face.muted)
                } else if controller.liveActivityIsUp {
                    Text("Also on your lock screen.")
                        .font(face.metaFont)
                        .foregroundStyle(face.faint)
                }
            }
        }
        .frame(maxWidth: .infinity)
    }

    // MARK: Controls

    @ViewBuilder
    private var controls: some View {
        switch controller.stage {
        case .idleAsk:
            VStack(spacing: face.layout.rowGap) {
                Text("Still going?")
                    .font(face.cardTitleFont)
                    .foregroundStyle(face.ink)
                HStack(spacing: face.layout.tightGap) {
                    actionButton("Yes, still on it", prominent: true) { controller.stillGoing() }
                        .accessibilityLabel("Keep the session running")
                    actionButton("I stopped", prominent: false) { controller.iStopped() }
                        .accessibilityLabel("Stop and save what was measured")
                }
            }
        case .finished:
            EmptyView()
        default:
            HStack(spacing: face.layout.tightGap) {
                if controller.stage == .paused {
                    actionButton("Resume", prominent: false) { controller.resume() }
                        .accessibilityLabel("Resume the session")
                } else {
                    actionButton("Pause", prominent: false) { controller.pause() }
                        .accessibilityLabel("Pause the session")
                }
                actionButton("Done", prominent: true) { controller.done() }
                    .accessibilityLabel("Finish and save the measured minutes")
            }
        }
    }

    private func actionButton(_ title: String, prominent: Bool, _ act: @escaping () -> Void) -> some View {
        Button(action: act) {
            Text(title)
                .font(face.bodyFont)
                .foregroundStyle(prominent ? face.ground : face.ink)
                .frame(maxWidth: .infinity, minHeight: face.layout.minTapTarget)
                .background(
                    RoundedRectangle(cornerRadius: face.cornerStyle.nominalRadius, style: .continuous)
                        .fill(prominent ? face.accent : face.control)
                )
        }
        .buttonStyle(.plain)
    }

    // MARK: Formatting

    /// H:MM:SS or MM:SS. A clock, tabular, never rounded.
    private func clock(_ seconds: Double) -> String {
        let total = Int(seconds)
        let h = total / 3600
        let m = (total % 3600) / 60
        let s = total % 60
        return h > 0
            ? String(format: "%d:%02d:%02d", h, m, s)
            : String(format: "%d:%02d", m, s)
    }

    private func overrunLine(now: Date) -> String {
        let past = max(0, controller.flooredElapsedMinutes(asOf: now) - controller.plannedMinutes)
        return "\(DurationText.spoken(past)) past the hour you planned."
    }

    private func accessibilityReadout(now: Date) -> String {
        let mins = controller.flooredElapsedMinutes(asOf: now)
        switch controller.stage {
        case .running: return "\(DurationText.spoken(mins)) measured, counting."
        case .paused: return "\(DurationText.spoken(mins)) measured, paused."
        case .idleAsk: return "\(DurationText.spoken(mins)) measured, waiting to hear if you are still going."
        case .finished: return "\(DurationText.spoken(mins)) measured, done."
        }
    }

    // MARK: Sizing

    private var dialSize: CGFloat { 260 }
    private var ringWidth: CGFloat { 12 }
}
