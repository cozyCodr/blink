import SwiftUI
import BlinkKit

// S1 · Today (docs/COMPANION_SCREENS.md).
//
// "The only screen most people see. One glance answers 'what is next, and am
// I on track?'" Layout, top to bottom, exactly as the spec orders it: the eyes
// -> greeting -> next-session card -> primary action -> tracked line -> streak
// chip.
//
// THE BEATS THIS SCREEN FIRES, and what grounds each one
// (.agents/rules/frontend-standards.md, the truthfulness rule):
//
//   thinking — held while a request is GENUINELY in flight and there is
//              nothing on screen yet. It is a state, not a beat, and it ends
//              when the request does.
//   sorry    — only when the server ANSWERED with a refusal. An unreachable
//              server is not a refusal and nobody apologises for it, the same
//              line P15-03 drew in `SignInFailure.isConfirmedRejection`.
//
// That is the whole list. Nothing fires for "nothing planned", for "work
// done", or on appearance: none of those are things that happened, and a beat
// with no event behind it is a lie the face tells.
//
// The celebration is NOT fired from here. See CelebrationScreen.swift and
// BlinkKit/Today/RecordedOutcome.swift.
struct TodayScreen: View {
    @Environment(\.face) private var face
    @Environment(\.openURL) private var openURL
    #if DEBUG
    @Environment(FaceProvider.self) private var faces
    #endif

    let identity: BlinkIdentity
    let session: BlinkSession
    let rig: EyeRig
    var onSignedOut: () -> Void

    @State private var store: TodayStore
    /// S2's surface, held as `NotificationsController` and nothing more
    /// specific. This screen cannot name a scheduler, a notification centre or
    /// APNs, which is what makes P15-10's remote implementation a swap at the
    /// root rather than an edit here.
    @State private var notifications = NotificationsController()
    /// The session S1 is about to run, or nil. Set by the Start button and
    /// cleared when S3 closes; presenting it is what opens the focus timer.
    @State private var focusTarget: SessionCard?
    @State private var showingRehearsal = false
    #if DEBUG
    @State private var showingSignals = false
    #endif

    init(
        identity: BlinkIdentity,
        session: BlinkSession,
        rig: EyeRig,
        store: TodayStore = TodayStore(),
        onSignedOut: @escaping () -> Void
    ) {
        self.identity = identity
        self.session = session
        self.rig = rig
        self.onSignedOut = onSignedOut
        _store = State(wrappedValue: store)
    }

    var body: some View {
        ZStack {
            face.ground.ignoresSafeArea()

            ScrollView {
                VStack(spacing: face.layout.sectionGap) {
                    EyesView(rig: rig, scale: 0.62)
                        .frame(height: 150)
                    greeting
                    card
                    footer
                }
                .frame(maxWidth: .infinity)
                .padding(.horizontal, face.layout.screenMargin)
                .padding(.bottom, face.layout.cardPaddingBottom)
            }
            .refreshable { await store.refresh() }
            .scrollBounceBehavior(.always)

            #if DEBUG
            debugDoor
            #endif
        }
        .task { await store.load(session: session) }
        // The permission ask waits for a payload on purpose. Asking on launch
        // means asking before the app has anything to offer; asking once
        // Today holds a real plan means the question has an answer behind it.
        // A no is a normal state, recorded once and never asked again.
        .task(id: store.state != nil) {
            await notifications.refreshAuthorization()
            guard store.state != nil else { return }
            await notifications.askIfNeeded()
            await notifications.arrange(for: session)
        }
        // A background action wrote something. Re-read rather than assume:
        // the number on this screen is the server's number.
        .onReceive(NotificationCenter.default.publisher(for: .blinkSignalActionWrote)) { _ in
            Task { await store.refresh() }
        }
        .onChange(of: store.needsSignIn) { _, dead in
            if dead {
                Task { await notifications.standDown() }
                onSignedOut()
            }
        }
        .task(id: beatKey) { react() }
        .fullScreenCover(item: Binding(
            get: { store.celebration },
            set: { if $0 == nil { store.dismissCelebration() } }
        )) { outcome in
            CelebrationScreen(outcome: outcome, rig: rig) { store.dismissCelebration() }
                .face(face)
        }
        // S3 · Focus session, over Today. Its own controller owns the timer and
        // the write; Today re-reads on dismissal so its numbers stay the
        // server's numbers.
        .fullScreenCover(item: $focusTarget) { target in
            FocusScreen(
                controller: FocusController(
                    blockID: target.blockID,
                    title: target.title,
                    plannedMinutes: target.plannedMinutes,
                    resumedMinutes: target.resumedTimerMinutes,
                    session: session
                ),
                rig: rig,
                streakDays: store.state?.streakDays ?? 0,
                onClose: {
                    focusTarget = nil
                    Task { await store.refresh() }
                },
                onSignedOut: {
                    focusTarget = nil
                    onSignedOut()
                }
            )
            .face(face)
        }
        #if DEBUG
        .sheet(isPresented: $showingRehearsal) {
            DebugEmotionRehearsalScreen()
                .environment(faces)
                .face(face)
        }
        .sheet(isPresented: $showingSignals) {
            DebugSignalRehearsalScreen(session: session)
                .face(face)
        }
        #endif
    }

    // MARK: The greeting

    @ViewBuilder
    private var greeting: some View {
        // Server-composed, from the STORED name. No name, no greeting, and
        // never one this app wrote (P15-03).
        if let line = identity.greeting {
            Text(line)
                .font(face.displayFont)
                .foregroundStyle(face.ink)
                .multilineTextAlignment(.center)
                .frame(maxWidth: .infinity)
        }
    }

    // MARK: The card

    @ViewBuilder
    private var card: some View {
        if let state = store.state {
            switch state.card {
            case .emptyWorkspace:
                surface {
                    Text("Your plan lives on the web for now. Make one, and I will keep you to it.")
                        .font(face.bodyFont)
                        .foregroundStyle(face.ink)
                        .multilineTextAlignment(.leading)
                    webButton("Open Blink on the web", prominent: true)
                }

            case .nothingPlanned:
                surface {
                    Text("Nothing planned for today, and that is allowed.")
                        .font(face.bodyFont)
                        .foregroundStyle(face.ink)
                    webButton("See your week", prominent: false)
                }

            case .nextSession(let session):
                surface {
                    label("NEXT")
                    sessionHeader(session, clock: state.clock)
                    Text("\(state.clock.clockTime(session.startsAt)) · \(DurationText.spoken(session.plannedMinutes))")
                        .font(face.metaFont)
                        .foregroundStyle(face.muted)
                    startButton(session, title: "Start focus session")
                }

            case .sessionRunning(let session):
                surface {
                    label("NOW")
                    sessionHeader(session, clock: state.clock)
                    Text("Started \(state.clock.clockTime(session.startsAt)). \(DurationText.spoken(session.plannedMinutes)) planned.")
                        .font(face.metaFont)
                        .foregroundStyle(face.muted)
                    // "Open session" reconciles against the server's measured
                    // floor rather than resuming a locally-guessed elapsed.
                    startButton(session, title: session.resumedTimerMinutes != nil ? "Open session" : "Start focus session")
                }

            case .checkIn(let pending):
                surface {
                    label("HOW DID IT GO?")
                    ForEach(pending) { block in
                        checkInRow(block)
                    }
                    if store.lastWriteFailed {
                        Text("That did not save. I will try again in a moment.")
                            .font(face.secondaryFont)
                            .foregroundStyle(face.warm)
                    }
                }

            case .endedAwaitingCheckIn(let pending):
                surface {
                    Text(pending.count == 1
                         ? "Today's session has wrapped. I will ask how it went this evening."
                         : "Today's sessions have wrapped. I will ask how they went this evening.")
                        .font(face.bodyFont)
                        .foregroundStyle(face.ink)
                    ForEach(pending) { block in
                        Text(block.title ?? "One session")
                            .font(face.secondaryFont)
                            .foregroundStyle(face.muted)
                    }
                }

            case .workDone:
                surface {
                    Text("That's today's work done.")
                        .font(face.cardTitleFont)
                        .foregroundStyle(face.ink)
                }
            }
        } else {
            unreachableCard
        }
    }

    /// No cache, no payload. Say what happened and what to do, and show no
    /// numbers at all. A zero here would be a fabricated fact.
    private var unreachableCard: some View {
        surface {
            if store.isLoading {
                Text("Reading your plan.")
                    .font(face.bodyFont)
                    .foregroundStyle(face.muted)
            } else if store.wasRefused {
                Text("That did not go through. Pull down and I will try again.")
                    .font(face.bodyFont)
                    .foregroundStyle(face.ink)
            } else {
                Text("I cannot reach your plan right now, and I have nothing saved yet. Pull down when you are back on.")
                    .font(face.bodyFont)
                    .foregroundStyle(face.ink)
            }
        }
    }

    @ViewBuilder
    private func sessionHeader(_ session: SessionCard, clock: ServerClock) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: face.layout.tightGap) {
            Circle()
                .fill(face.accent)
                .frame(width: face.layout.dotSize, height: face.layout.dotSize)
                .alignmentGuide(.firstTextBaseline) { $0[.bottom] }
            // No title means the payload did not carry one. Say less rather
            // than invent a name for someone's work.
            Text(session.title ?? "Your next session")
                .font(face.cardTitleFont)
                .foregroundStyle(face.ink)
        }
        if let commitment = session.commitment {
            Text(commitment)
                .font(face.secondaryFont)
                .foregroundStyle(face.faint)
        }
    }

    /// S1's primary action: start (or re-open) the focus session. The timer,
    /// the Live Activity and the write that records the measured minutes are
    /// S3, presented over Today. On dismissal Today re-reads, so the number it
    /// shows is the server's number.
    private func startButton(_ session: SessionCard, title: String) -> some View {
        Button {
            focusTarget = session
        } label: {
            Text(title)
                .font(face.bodyFont)
                .foregroundStyle(face.ground)
                .frame(maxWidth: .infinity, minHeight: face.layout.minTapTarget)
                .background(
                    RoundedRectangle(cornerRadius: face.cornerStyle.nominalRadius, style: .continuous)
                        .fill(face.accent)
                )
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Start a focus session for \(session.title ?? "your next session")")
    }

    @ViewBuilder
    private func checkInRow(_ block: PendingBlock) -> some View {
        VStack(alignment: .leading, spacing: face.layout.rowGap) {
            Text(block.title ?? "That session")
                .font(face.cardTitleFont)
                .foregroundStyle(face.ink)
            HStack(spacing: face.layout.tightGap) {
                answerButton("Done", .done, block)
                answerButton("Partly", .partial, block)
                answerButton("Skip", .skipped, block)
            }
        }
    }

    private func answerButton(_ title: String, _ outcome: CheckinOutcome, _ block: PendingBlock) -> some View {
        Button {
            Task { await store.resolve(block: block.id, as: outcome, title: block.title) }
        } label: {
            Text(title)
                .font(face.bodyFont)
                .foregroundStyle(outcome == .done ? face.ground : face.ink)
                .frame(maxWidth: .infinity, minHeight: face.layout.minTapTarget)
                .background(
                    RoundedRectangle(cornerRadius: face.cornerStyle.nominalRadius, style: .continuous)
                        .fill(outcome == .done ? face.accent : face.control)
                )
        }
        .buttonStyle(.plain)
        .disabled(store.writingBlockID != nil)
        // VoiceOver names the OUTCOME, not the widget
        // (COMPANION_SCREENS.md, Accessibility).
        .accessibilityLabel(voiceOverLabel(for: outcome, block: block))
    }

    private func voiceOverLabel(for outcome: CheckinOutcome, block: PendingBlock) -> String {
        let name = block.title ?? "this session"
        switch outcome {
        case .done: return "Log \(name) as done"
        case .partial: return "Log \(name) as partly done"
        case .skipped: return "Log \(name) as skipped"
        }
    }

    // MARK: The tracked line, the stamp, the streak

    @ViewBuilder
    private var footer: some View {
        VStack(spacing: face.layout.rowGap) {
            if let state = store.state {
                trackedLine(state.tracked)
                stamp
                if state.streakDays > 0 {
                    Text("Day \(state.streakDays)")
                        .font(face.labelFont)
                        .foregroundStyle(face.accent)
                        .padding(.vertical, face.layout.pillPaddingV)
                        .padding(.horizontal, face.layout.pillPaddingH)
                        .overlay(
                            Capsule().stroke(face.line, lineWidth: 1)
                        )
                        .accessibilityLabel("Day \(state.streakDays) of your streak")
                }
            } else {
                stamp
            }
            notificationLimitation
        }
        .frame(maxWidth: .infinity)
    }

    /// What this device can and cannot do about reaching you.
    ///
    /// Only renders when there is something true to say. A granted permission
    /// says nothing, because a working thing does not need announcing, and
    /// nothing here mentions a daily cap: the budget lives on the server, this
    /// device cannot read it, and it does not speak for it.
    @ViewBuilder
    private var notificationLimitation: some View {
        if let line = notifications.limitationLine {
            Text(line)
                .font(face.metaFont)
                .foregroundStyle(face.faint)
                .multilineTextAlignment(.center)
        }
    }

    /// THE honesty beat. Measured minutes and self-reported minutes are named
    /// separately and are never added into one number. There is deliberately
    /// no code path below that sums them.
    @ViewBuilder
    private func trackedLine(_ tracked: TrackedLine) -> some View {
        let planned = DurationText.spoken(tracked.plannedMinutes)
        if tracked.measuredMinutes > 0, tracked.reportedMinutes > 0 {
            twoTone(
                strong: "\(DurationText.spoken(tracked.measuredMinutes)) tracked",
                rest: " of \(planned) planned, plus \(DurationText.spoken(tracked.reportedMinutes)) you told me about"
            )
        } else if tracked.measuredMinutes > 0 {
            twoTone(
                strong: "\(DurationText.spoken(tracked.measuredMinutes)) tracked",
                rest: " of \(planned) planned today"
            )
        } else if tracked.reportedMinutes > 0 {
            // Nothing was measured, so nothing is called tracked.
            twoTone(
                strong: "\(DurationText.spoken(tracked.reportedMinutes)) you told me about",
                rest: ", of \(planned) planned today"
            )
        } else if tracked.plannedMinutes > 0 {
            Text("\(planned) planned today")
                .font(face.secondaryFont)
                .foregroundStyle(face.muted)
        }
    }

    private func twoTone(strong: String, rest: String) -> some View {
        (Text(strong).foregroundColor(face.ink) + Text(rest).foregroundColor(face.muted))
            .font(face.secondaryFont)
            .multilineTextAlignment(.center)
    }

    /// REQUIRED whenever the screen is rendering cache, not optional.
    @ViewBuilder
    private var stamp: some View {
        if case .cached(let receivedAt) = store.freshness {
            Text("I cannot reach your plan right now. This is what I last knew, as of \(stampTime(receivedAt)).")
                .font(face.metaFont)
                .foregroundStyle(face.faint)
                .multilineTextAlignment(.center)
        }
    }

    /// The stamp is the one place the DEVICE clock is the right clock: it
    /// records when this device last heard from the server, which is a fact
    /// about this device. It is shown in the user's own zone when the server
    /// published one, so it agrees with every other time on the screen.
    private func stampTime(_ instant: Date) -> String {
        let formatter = DateFormatter()
        formatter.timeZone = store.state?.clock.timeZone ?? .autoupdatingCurrent
        formatter.locale = .autoupdatingCurrent
        formatter.setLocalizedDateFormatFromTemplate("jmm")
        return formatter.string(from: instant)
    }

    // MARK: Out to the web

    private func webButton(_ title: String, prominent: Bool) -> some View {
        Button {
            openURL(BlinkAPI.baseURL())
        } label: {
            Text(title)
                .font(face.bodyFont)
                .foregroundStyle(prominent ? face.ground : face.accent)
                .frame(maxWidth: .infinity, minHeight: face.layout.minTapTarget)
                .background(
                    RoundedRectangle(cornerRadius: face.cornerStyle.nominalRadius, style: .continuous)
                        .fill(prominent ? face.accent : Color.clear)
                        .overlay(
                            RoundedRectangle(cornerRadius: face.cornerStyle.nominalRadius, style: .continuous)
                                .stroke(prominent ? Color.clear : face.line, lineWidth: 1)
                        )
                )
        }
        .buttonStyle(.plain)
    }

    // MARK: Chrome

    @ViewBuilder
    private func label(_ text: String) -> some View {
        Text(text)
            .font(face.labelFont)
            .tracking(2.2)   // now.css:23 letter-spacing: 0.18em at 12px
            .foregroundStyle(face.accent)
    }

    private func surface<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: face.layout.rowGap, content: content)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.top, face.layout.cardPaddingTop)
            .padding(.horizontal, face.layout.cardPaddingSide)
            .padding(.bottom, face.layout.cardPaddingBottom)
            .background(
                RoundedRectangle(cornerRadius: face.cornerStyle.nominalRadius, style: .continuous)
                    .fill(face.surface)
                    .overlay(
                        RoundedRectangle(cornerRadius: face.cornerStyle.nominalRadius, style: .continuous)
                            .stroke(face.line, lineWidth: 1)
                    )
            )
    }

    #if DEBUG
    /// P15-02's rehearsal screen stays one tap away, as it was before this
    /// screen took its place in RootView. DEBUG only; not product UI.
    private var debugDoor: some View {
        VStack {
            HStack {
                Spacer()
                Button("beats") { showingRehearsal = true }
                    .font(face.metaFont)
                    .foregroundStyle(face.faint)
                Button("signals") { showingSignals = true }
                    .font(face.metaFont)
                    .foregroundStyle(face.faint)
                    .padding(.leading, face.layout.tightGap)
            }
            Spacer()
        }
        .padding(face.layout.screenMargin)
    }
    #endif

    // MARK: The beats

    /// Changes exactly when something the eyes may react to changes.
    private var beatKey: String {
        "\(store.isLoading)|\(store.state == nil)|\(store.wasRefused)"
    }

    private func react() {
        if store.wasRefused {
            // The server answered, and the answer was no.
            rig.emote(.sorry, hold: .seconds(face.motion.celebrationHold))
            return
        }
        if store.isLoading, store.state == nil {
            // A request is genuinely in flight and there is nothing to show.
            rig.emote(.thinking)
            return
        }
        if rig.emotion == .thinking {
            rig.clearEmotion()
        }
    }
}
