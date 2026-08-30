import SwiftUI
import UIKit
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
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.scenePhase) private var scenePhase
    @Environment(FaceProvider.self) private var faces

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
    /// P15-11: the /turn conversation. The compose field, the reply, the
    /// elicitation loop. Configured alongside the store's session so a plan
    /// made here lands back in `store.refresh()`.
    @State private var composer = PlanComposer()
    /// P15-12: the agent's voice (server TTS, gated by the Settings toggle)
    /// and the hold-to-talk capture. Text never waits on either.
    @State private var voice = AgentVoice()
    @State private var voiceCapture = VoiceCapture()
    /// P18-04b: the hands-free evening check-in. Off until the person starts it
    /// (the "Talk it through" affordance on the check-in card, or a tap on the
    /// evening check-in notification). It drives voice + mic + composer in a
    /// spoken back-and-forth and falls back to the typed flow if it cannot.
    @State private var checkInLoop = CheckInVoiceLoop()
    /// P15-12: whether the software keyboard is up, so the eyes give ground
    /// instead of the compose field being pushed offscreen.
    @State private var keyboardUp = false
    /// P15-13: the greeting is a MOMENT, not a row. True for the first few
    /// seconds of an app session, then false forever (this state lives as long
    /// as the signed-in screen does, so it cannot come back on a scroll, a
    /// refresh or a returning foreground). The web speaks its greeting once on
    /// return from sign-in; this is the same event, worn visually.
    @State private var greetingShowing = true
    @State private var showingSettings = false
    /// P18-01: the native plan surface (Day + Week). Reached from the timeline
    /// door in the top bar, from "See your week", and raised automatically the
    /// moment a plan with placed blocks lands. Viewing the plan never leaves
    /// the app any more.
    @State private var showingPlan = false

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
        GeometryReader { geo in
            ZStack {
                face.ground.ignoresSafeArea()

                ScrollView {
                    VStack(spacing: face.layout.sectionGap) {
                        // P15-12: the eyes float in the upper-middle band, close
                        // to the words they converse with (the web's stage rhythm,
                        // conversation.css:51-66), and both the band and the rig
                        // shrink as the text grows or the keyboard rises. The rig's
                        // pose tables are fraction-based (P15-02), so a scaled rig
                        // stays correct. Reduced Motion snaps between sizes.
                        EyesView(rig: rig, scale: eyeScale)
                            .frame(height: ConversationScale.eyeBand(tier: textTier, keyboardUp: keyboardUp))
                            .padding(.top, geo.size.height
                                     * ConversationScale.eyesTopFraction(keyboardUp: keyboardUp))
                        greeting
                        card
                        conversation
                        footer
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.horizontal, face.layout.screenMargin)
                    .padding(.bottom, face.layout.cardPaddingBottom)
                    .animation(reduceMotion ? nil : face.motion.swapAnimation, value: eyeScale)
                }
                .refreshable {
                    retireGreeting()
                    await store.refresh()
                }
                .scrollBounceBehavior(.always)
                // P15-12: a drag lets go of the keyboard, so the compressed
                // layout always has a way back to full-size eyes.
                .scrollDismissesKeyboard(.interactively)
                // P15-14: the compose bar lives at the bottom of the screen,
                // not in the scroll. See `composeBar`.
                .safeAreaInset(edge: .bottom, spacing: 0) {
                    composeBar
                        .animation(reduceMotion ? nil : face.motion.swapAnimation,
                                   value: composer.question != nil)
                        .animation(reduceMotion ? nil : face.motion.swapAnimation,
                                   value: checkInLoop.isActive)
                }

                topBar
            }
        }
        .onReceive(NotificationCenter.default.publisher(
            for: UIResponder.keyboardWillShowNotification)) { _ in keyboardUp = true }
        .onReceive(NotificationCenter.default.publisher(
            for: UIResponder.keyboardWillHideNotification)) { _ in keyboardUp = false }
        // P15-12: the agent speaks the reply it just wrote, when the toggle
        // says to. The text is already on screen; audio failure changes
        // nothing (AgentVoice logs and stays quiet).
        //
        // P18-04b: while the hands-free check-in loop is running it owns the
        // voice (forced on, and it opens the mic when the reply finishes), so
        // the ordinary toggle-gated speak steps aside for it here.
        .onChange(of: composer.reply) { _, reply in
            if let reply, !checkInLoop.isActive { voice.speak(reply, session: session) }
        }
        .task {
            composer.configure(session: session) { await store.refresh() }
            checkInLoop.configure(
                voice: voice, capture: voiceCapture, composer: composer, session: session)
            // A tap on the evening check-in notification (even the cold launch
            // it caused) asked for the hands-free check-in. Honour it once.
            if CheckInLaunchRequest.consume() { checkInLoop.start() }
            #if DEBUG
            // P18-02 door: seed a calendar confirm exactly as the agent surfaces
            // one, so its render, YES→/calendar/events and "Not now" can be
            // exercised without a live turn. `-blinkDebugCalendarConfirm create`
            // (or edit / delete). Against a debug workspace the write itself will
            // 502, which is the honest failure path; a real signed-in session
            // commits for real.
            if let action = UserDefaults.standard.string(forKey: "blinkDebugCalendarConfirm") {
                composer.debugSeedCalendarConfirm(action: action)
            }
            #endif
            await store.load(session: session)
        }
        // P15-08 — the face preference lives on the account. Wire the
        // write-through seam first, then adopt what the server holds: server
        // wins, because it is the newest pick made on ANY device, and a pick
        // made here is pushed the moment it happens.
        .task {
            let sync = FaceSyncClient()
            let blink = session
            faces.pushToServer = { await sync.push($0, session: blink) }
            if let serverFace = identity.face {
                faces.adopt(serverFace: serverFace)
            }
        }
        // The permission ask waits for a payload on purpose. Asking on launch
        // means asking before the app has anything to offer; asking once
        // Today holds a real plan means the question has an answer behind it.
        // A no is a normal state, recorded once and never asked again.
        //
        // THE KEY IS THE PLAN, not "is there a plan yet". The signals are made
        // of today's blocks, so they are rebuilt every time today's blocks
        // change and at no other time: a replan on the web, a plan made right
        // here in the compose field, a check-in that resolved something, a day
        // that rolled over while the app was away. Anything that lands a fresh
        // payload runs through `store.refresh()`, and `planFingerprint` moves
        // only when the day or a block genuinely did, so a pull that changed
        // nothing costs nothing. Keying on `store.state != nil` instead, as
        // this once did, arranged the day's signals once and then let them go
        // stale for the rest of that day.
        .task(id: store.planFingerprint) {
            await notifications.refreshAuthorization()
            guard !store.planFingerprint.isEmpty else { return }
            await notifications.askIfNeeded()
            await notifications.arrange(for: session)
            // A first grant made just now only becomes an APNs registration if
            // something asks; the AppDelegate's foreground pass would catch it
            // on the next activation, but doing it here means the server has
            // this device's delivery address the moment permission is given.
            if notifications.authorization.canDeliver {
                UIApplication.shared.registerForRemoteNotifications()
            }
        }
        // Coming back to the app. The day may have rolled over, or the plan
        // may have been rewritten on the web while this screen sat there. Ask
        // the server; if the answer differs from what we hold, the arrange
        // above follows it. If it does not, nothing happens, which is right.
        .onChange(of: scenePhase) { _, phase in
            guard phase == .active else {
                // P18-04b: leaving the app ends the hands-free loop. A spoken
                // back-and-forth with nobody there is exactly what should stop.
                checkInLoop.stop()
                return
            }
            Task { await store.refresh() }
            // A notification tap can bring the app forward without a fresh
            // `.task`; pick up the check-in intent here too.
            if CheckInLaunchRequest.consume() { checkInLoop.start() }
        }
        // A background action wrote something. Re-read rather than assume:
        // the number on this screen is the server's number.
        .onReceive(NotificationCenter.default.publisher(for: .blinkSignalActionWrote)) { _ in
            Task { await store.refresh() }
        }
        // The person has started talking, or gone somewhere: the greeting has
        // done its job and gets out of the way.
        .onChange(of: composer.isSending) { _, sending in
            if sending {
                retireGreeting()
            } else if checkInLoop.isActive {
                // P18-04b: a /turn just completed while the loop is running.
                // Reading the composer's settled state (not a reply string that
                // could repeat) is what advances the loop exactly one turn:
                // speak the reply, or fall out on a question / failure.
                checkInLoop.turnCompleted(
                    reply: composer.reply,
                    refused: composer.didRefuse,
                    unreachable: composer.wasUnreachable,
                    hasQuestion: composer.question != nil
                )
            }
        }
        .onChange(of: keyboardUp) { _, up in
            if up { retireGreeting() }
        }
        .onChange(of: showingSettings) { _, open in
            if open { retireGreeting() }
        }
        .onChange(of: focusTarget?.blockID) { _, target in
            if target != nil { retireGreeting() }
        }
        .onChange(of: composer.needsSignIn) { _, dead in
            if dead { onSignedOut() }
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
                    session: session,
                    // The Live Activity wears the chosen face (P15-08). It
                    // rides the attributes, set once at start, because the
                    // widget extension has no app group to read from.
                    face: faces.faceID
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
        .sheet(isPresented: $showingSettings) {
            SettingsScreen(identity: identity, session: session, onSignedOut: onSignedOut)
                .environment(faces)
                .face(face)
        }
        // P18-01: the plan, native, rising over the paper the way the web's
        // horizon rises into the space the eyes vacate.
        .sheet(isPresented: $showingPlan) {
            PlanScreen(plan: store.plan)
                .environment(faces)
                .face(face)
        }
        // A plan with placed blocks just landed: show it, once per landing.
        .onChange(of: composer.planLandings) { _, _ in
            retireGreeting()
            showingPlan = true
        }
    }

    // MARK: The greeting

    @ViewBuilder
    private var greeting: some View {
        // Server-composed, from the STORED name. No name, no greeting, and
        // never one this app wrote (P15-03). The TEXT is the server's; only its
        // lifecycle is decided here.
        //
        // It says one thing, "you are back", and that stops being news within
        // seconds. So it holds for `face.motion.greetingHold` and then leaves,
        // and any interaction that means the person has moved on (a send, a
        // pull to refresh, opening Settings) retires it early. Reduced Motion
        // gets the same appearance and the same departure, without the fade.
        if let line = identity.greeting, greetingShowing {
            Text(line)
                .font(face.displayFont)
                .foregroundStyle(face.ink)
                .multilineTextAlignment(.center)
                .frame(maxWidth: .infinity)
                .transition(reduceMotion ? .identity : .opacity)
                .task {
                    try? await Task.sleep(for: .seconds(face.motion.greetingHold))
                    guard !Task.isCancelled else { return }
                    retireGreeting()
                }
        }
    }

    /// Let the greeting go, once. Safe to call from anywhere and from any
    /// number of places: it never brings the line back.
    private func retireGreeting() {
        guard greetingShowing else { return }
        withAnimation(reduceMotion ? nil : face.motion.releaseAnimation) {
            greetingShowing = false
        }
    }

    // MARK: The card

    @ViewBuilder
    private var card: some View {
        if let state = store.state {
            switch state.card {
            case .emptyWorkspace:
                // P15-11: an invitation to plan, not a dead end — written
                // straight on the paper, no card, no web detour (Settings
                // keeps the only web link). The compose field renders just
                // below (`conversation`).
                if composer.reply == nil, !composer.isSending, composer.question == nil {
                    Text("Tell me what you are working on, and I will plan it.")
                        .font(face.displayFont)
                        .foregroundStyle(face.ink)
                        .multilineTextAlignment(.center)
                        .frame(maxWidth: .infinity)
                }

            case .nothingPlanned:
                surface {
                    Text("Nothing planned for today, and that is allowed.")
                        .font(face.bodyFont)
                        .foregroundStyle(face.ink)
                    // P18-01: the week is native now. This used to open the web.
                    planButton("See your week")
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
                    // P18-04b: the hands-free way through the same check-in. The
                    // tap buttons above stay the fallback; this offers to just
                    // talk it through, Blink asking and listening on its own.
                    talkItThroughButton
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

    // MARK: The conversation (P15-11)

    /// The /turn loop under the plan card: the reply (verbatim) and the
    /// question with its typed control, written straight on the ground, no
    /// cards (the paper direction the web took). The compose field is NOT
    /// here; it is pinned to the bottom of the screen (`composeBar`), which is
    /// where a phone keeps the thing you talk into.
    ///
    /// The states CROSS-FADE on the web's swap timing
    /// (`swapMode`, app.js:457-464), soft-revealing with a small rise.
    /// Reduced Motion: the animation is nil and everything lands instantly.
    @ViewBuilder
    private var conversation: some View {
        Group {
            if let question = composer.question {
                QuestionSurface(question: question) { value in
                    voice.stop()   // answering IS a new turn: the old reply's audio yields
                    Task { await composer.answer(value) }
                } onConfirm: { yes in
                    // A calendar confirm's YES commits to /calendar/events, not
                    // /elicit/answer, and bypasses the elicit guard (P18-02).
                    voice.stop()
                    Task { await composer.confirmCalendar(yes) }
                }
                .id(question.field)   // fresh selection state per question
                .transition(swapTransition)
            } else if replyVisible {
                // The compose field itself is no longer here: it lives pinned
                // at the bottom of the screen (`composeBar`). What stays in
                // the scroll is the SAID part of the conversation, centered
                // and borderless on the paper, exactly as P15-11 left it.
                PlanReplySurface(composer: composer)
                    .transition(swapTransition)
            }
        }
        .animation(reduceMotion ? nil : face.motion.swapAnimation, value: conversationPhase)
    }

    // MARK: The compose bar, pinned to the bottom

    /// Mic, field and send, held at the bottom of the screen above the home
    /// indicator, riding the keyboard up and down.
    ///
    /// WHY IT IS AN INSET AND NOT A ROW IN THE SCROLL. On a phone the thing
    /// you talk into belongs under your thumb, and it belongs there whatever
    /// the scroll is doing. `safeAreaInset(edge: .bottom)` is the one tool
    /// that gets both halves right: it pins the bar AND lengthens the scroll
    /// view's content inset by the bar's height, so the last line of a reply
    /// can always be scrolled clear of it and is never left hiding underneath.
    ///
    /// HOW IT MEETS THE KEYBOARD. The keyboard is itself a bottom safe area
    /// inset, so the bar rises with it for free, with no keyboard-height
    /// arithmetic here at all. P15-12's `keyboardUp` keeps its existing and
    /// separate job of compressing what is ABOVE the bar (the eye band and the
    /// rig's scale, via ConversationScale), so the two work on opposite halves
    /// of the screen and never fight over the same points.
    ///
    /// A question replaces the bar rather than stacking with it, the same
    /// one-at-a-time rule the surface has always had: while Blink is asking,
    /// answering it IS the reply.
    @ViewBuilder
    private var composeBar: some View {
        if checkInLoop.isActive {
            // P18-04b: while the spoken loop runs, the field steps aside for its
            // status + Done control. There is nothing to type; the voice is the
            // interface, and Done is the way out.
            loopBar
        } else if composer.question == nil {
            VStack(spacing: 0) {
                // No divider, no chrome bar. The paper simply stops being
                // paper: content scrolling underneath dissolves into the
                // ground rather than sliding under a line (P15-11's quiet).
                LinearGradient(
                    colors: [face.ground.opacity(0), face.ground],
                    startPoint: .top,
                    endPoint: .bottom
                )
                .frame(height: face.layout.composeBarFade)
                .allowsHitTesting(false)

                VStack(spacing: face.layout.tightGap) {
                    // P18-04b: the one honest line the loop leaves when it hands
                    // back to typing (mic or speech denied, or TTS unavailable).
                    // Quiet, and it clears itself the moment the person acts.
                    if let line = checkInLoop.fellBackLine {
                        Text(line)
                            .font(face.metaFont)
                            .foregroundStyle(face.faint)
                            .multilineTextAlignment(.center)
                            .frame(maxWidth: .infinity)
                    }
                    PlanComposeField(
                        composer: composer,
                        prompt: composePrompt,
                        voice: voiceCapture,
                        onSend: {
                            voice.stop()               // sending interrupts the reply audio
                            checkInLoop.clearFellBack()  // the person moved on
                        }
                    )
                }
                .padding(.horizontal, face.layout.screenMargin)
                .padding(.bottom, face.layout.rowGap)
                .background(face.ground)
            }
            .transition(reduceMotion ? .identity : .opacity)
        }
    }

    /// One value that changes exactly when the conversation surface swaps
    /// states, so the cross-fade animates those swaps and nothing else.
    private var conversationPhase: String {
        "\(composer.question?.field ?? "")|\(replyVisible)|\(composer.isSending)|\(composer.reply ?? "")"
    }

    private var swapTransition: AnyTransition {
        .opacity.combined(with: .offset(y: face.motion.revealRise))
    }

    private var replyVisible: Bool {
        composer.answerEcho != nil || composer.isSending || composer.reply != nil
            || composer.didRefuse || composer.wasUnreachable
    }

    // MARK: The breathing layout (P15-12)

    /// How much text the conversation is carrying right now: the question up,
    /// or the reply on screen. This is what the eyes yield to.
    private var conversationCharCount: Int {
        if let q = composer.question {
            return q.question.count + (q.why?.count ?? 0)
        }
        return composer.reply?.count ?? 0
    }

    private var textTier: ConversationScale.TextTier {
        ConversationScale.TextTier(charCount: conversationCharCount)
    }

    /// Full-size on a short reply, stepping down as the words grow, one step
    /// further when the keyboard is up. The tiers live in ConversationScale
    /// with their reasoning; this screen only reads them.
    private var eyeScale: CGFloat {
        ConversationScale.eyeScale(tier: textTier, keyboardUp: keyboardUp)
    }

    private var composePrompt: String {
        if case .emptyWorkspace = store.state?.card {
            return "What are you working on?"
        }
        return "Plan something with me"
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

    // MARK: Into the plan (native, P18-01)

    /// The outline button that opens the native plan. Replaces the old
    /// "See your week" web door on the nothing-planned card.
    private func planButton(_ title: String) -> some View {
        Button {
            showingPlan = true
        } label: {
            Text(title)
                .font(face.bodyFont)
                .foregroundStyle(face.accent)
                .frame(maxWidth: .infinity, minHeight: face.layout.minTapTarget)
                .background(
                    RoundedRectangle(cornerRadius: face.cornerStyle.nominalRadius, style: .continuous)
                        .fill(Color.clear)
                        .overlay(
                            RoundedRectangle(cornerRadius: face.cornerStyle.nominalRadius, style: .continuous)
                                .stroke(face.line, lineWidth: 1))
                )
        }
        .buttonStyle(.plain)
        .accessibilityLabel("See your plan")
    }

    // MARK: The hands-free check-in (P18-04b)

    /// Starts the spoken check-in loop. An outline control, the same quiet
    /// register as "See your week": it invites, it does not shout.
    private var talkItThroughButton: some View {
        Button {
            retireGreeting()
            checkInLoop.start()
        } label: {
            Text("Talk it through")
                .font(face.bodyFont)
                .foregroundStyle(face.accent)
                .frame(maxWidth: .infinity, minHeight: face.layout.minTapTarget)
                .background(
                    RoundedRectangle(cornerRadius: face.cornerStyle.nominalRadius, style: .continuous)
                        .fill(Color.clear)
                        .overlay(
                            RoundedRectangle(cornerRadius: face.cornerStyle.nominalRadius, style: .continuous)
                                .stroke(face.line, lineWidth: 1))
                )
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Talk through the check-in with Blink")
    }

    /// The bar that replaces the compose field WHILE the loop runs: a quiet word
    /// for what it is doing right now, and a decisive way out. Tapping Done ends
    /// the loop at once (it also ends when the app goes to the background).
    private var loopBar: some View {
        VStack(spacing: 0) {
            LinearGradient(
                colors: [face.ground.opacity(0), face.ground],
                startPoint: .top,
                endPoint: .bottom
            )
            .frame(height: face.layout.composeBarFade)
            .allowsHitTesting(false)

            HStack(spacing: face.layout.tightGap) {
                Text(loopStatus)
                    .font(face.secondaryFont)
                    .foregroundStyle(face.muted)
                Spacer()
                Button {
                    checkInLoop.stop()
                } label: {
                    Text("Done")
                        .font(face.bodyFont)
                        .foregroundStyle(face.ink)
                        .frame(minHeight: face.layout.minTapTarget)
                        .padding(.horizontal, face.layout.pillPaddingH * 2)
                        .background(Capsule().fill(face.control))
                }
                .buttonStyle(.plain)
                .accessibilityLabel("End the spoken check-in")
            }
            .padding(.horizontal, face.layout.screenMargin)
            .padding(.bottom, face.layout.rowGap)
            .background(face.ground)
        }
        .transition(reduceMotion ? .identity : .opacity)
    }

    /// The honest word for the loop's current step. Not a claim of any outcome,
    /// just what the phone is doing with the voice this instant.
    private var loopStatus: String {
        switch checkInLoop.phase {
        case .listening: return "Listening"
        case .speaking: return "Speaking"
        case .opening, .sending: return "Thinking"
        case .off: return ""
        }
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

    /// The quiet chrome over the eyes: the Settings door (S6) on the left,
    /// and, in DEBUG builds only, P15-02's rehearsal doors on the right.
    private var topBar: some View {
        VStack {
            HStack {
                Button {
                    showingSettings = true
                } label: {
                    Image(systemName: "gearshape")
                        .font(face.bodyFont)
                        .foregroundStyle(face.faint)
                        .frame(width: face.layout.minTapTarget,
                               height: face.layout.minTapTarget,
                               alignment: .topLeading)
                        // Without this, only the gear's thin strokes are
                        // hittable, not the 44pt frame — the "incredibly hard
                        // to tap Settings" report (2026-08-30). Rectangle makes
                        // the whole minTapTarget square the touch area.
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Settings")
                Spacer()
                // P18-01: the always-there door into the native plan. The web's
                // peek handle, worn as a quiet glyph up here where it cannot
                // collide with the compose bar at the bottom.
                Button {
                    retireGreeting()
                    showingPlan = true
                } label: {
                    Image(systemName: "calendar.day.timeline.left")
                        .font(face.bodyFont)
                        .foregroundStyle(face.faint)
                        .frame(width: face.layout.minTapTarget,
                               height: face.layout.minTapTarget,
                               alignment: .topTrailing)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("See your plan")
            }
            Spacer()
        }
        .padding(face.layout.screenMargin)
    }

    // MARK: The beats

    /// Changes exactly when something the eyes may react to changes.
    private var beatKey: String {
        "\(store.isLoading)|\(store.state == nil)|\(store.wasRefused)"
            + "|\(composer.isSending)|\(composer.question != nil)"
            + "|\(composer.didRefuse)|\(composer.heartPending)"
            + "|\(voiceCapture.isRecording)"
    }

    private func react() {
        // P15-11's beats, each grounded:
        //   heart    — the server's planned reply said blocks_scheduled > 0,
        //              first time this session. Consumed once.
        //   sorry    — the server ANSWERED with a refusal (either surface).
        //   thinking — a request is genuinely in flight (state, not a beat).
        //   curious  — a question is genuinely up, held until it is answered.
        //   wide     — the mic is GENUINELY held and recording (P15-12): the
        //              web's listening enter, and nothing else fires it.
        if voiceCapture.isRecording {
            rig.emote(.wide)
            return
        }
        if store.wasRefused || composer.didRefuse {
            // The server answered, and the answer was no.
            rig.emote(.sorry, hold: .seconds(face.motion.celebrationHold))
            return
        }
        if composer.isSending || (store.isLoading && store.state == nil) {
            // A request is genuinely in flight. The heart, if one is
            // pending, stays pending: it fires on the pass where the
            // request ends, so thinking can never stomp it.
            rig.emote(.thinking)
            return
        }
        if composer.consumeHeart() {
            rig.emote(.heart, hold: .seconds(face.motion.heartHold))
            return
        }
        if composer.question != nil {
            rig.emote(.curious)
            return
        }
        if rig.emotion == .thinking || rig.emotion == .curious || rig.emotion == .wide {
            rig.clearEmotion()
        }
    }
}
