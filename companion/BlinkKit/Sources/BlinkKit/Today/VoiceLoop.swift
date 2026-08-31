import Foundation
import Observation

// P18-04b / P18-06 — the hands-free spoken loop.
//
// The backend already runs every conversation as a normal grounded agent turn
// (message replies + tools). This is the phone's HANDS-FREE shell around that
// conversation: Blink speaks, the mic opens on its own, the answer sends on its
// own, Blink speaks again, and so on, until the person ends it. Nobody taps a
// button between turns.
//
// TWO MODES, ONE MACHINE (P18-06). The loop was built for the evening check-in
// and now also carries the general "I tapped the mic" conversation. The only
// difference between them is HOW THE LOOP OPENS:
//
//   .checkIn      — Blink starts. One composed opener goes out, and the reply
//                   that comes back is the first thing spoken. (`start()`)
//   .conversation — the PERSON starts. They tapped the mic, so they are already
//                   talking: the mic opens immediately and nothing is composed
//                   on their behalf. (`startConversation()`)
//
// After the opening move the two are the same loop, line for line.
//
// WHAT THIS CLASS DOES AND DOES NOT DO
//
//   * It ORCHESTRATES three parts it does not own: a mouth (`AgentVoice`), an
//     ear (`VoiceCapture`) and the conversation (`PlanComposer`). It holds them
//     weakly through `configure`, and drives them in a strict order so no two
//     ever run at once (never speaking over the mic, never sending before the
//     words settle).
//
//   * It NEVER SHOWS A TRANSCRIPT (P18-06, user: seeing the recognizer's
//     mistakes on screen is worse than not seeing the words at all). What was
//     heard goes straight to the server and never touches the compose draft.
//     The only surface this loop asks for is a status word and a way out.
//
//   * It carries VOICE ONLY. It never writes an outcome and never claims one —
//     the agent (server) logs through its own tools, and every reply on screen
//     is the server's sentence. In `.checkIn` it composes exactly one user-role
//     opener; in `.conversation` it composes nothing at all.
//
//   * It DEGRADES, never traps. If the mic or speech permission is denied, or
//     TTS cannot speak this device's reply, the loop falls back to the normal
//     typed/tap flow with one honest line. The structured button check-in
//     (TodayStore's `.checkIn` card) is untouched and remains the real fallback.
//
// THE ORDER, exactly:
//
//   start()             -> send the opener        (phase .opening, in flight)
//   startConversation() -> open the mic           (phase .listening)
//   a turn completes with a reply                 -> speak it   (phase .speaking)
//   the spoken reply finishes                     -> open the mic (phase .listening)
//   the speech settles (final / silence)          -> send it    (phase .sending)
//   a turn completes with a reply                 -> speak it again … and round it goes
//
// INTERRUPTION. While Blink is speaking, `micTapped()` cuts the audio mid-word
// and opens the mic at once, so nobody has to wait out a reply they have already
// heard enough of. TRUE VOICE-ACTIVATED barge-in (mic live during playback) is
// NOT implemented: it needs `.playAndRecord`/`.voiceChat` echo cancellation, and
// without that the mic hears Blink's own voice and answers itself. A loop that
// talks to itself is worse than one you tap to interrupt.
//
// The eyes read as listening for free: the loop opens the SAME `VoiceCapture`
// whose `isRecording` already drives the existing `wide` beat. No eye work here.

/// The ear the loop opens. `VoiceCapture` is the only real one; the protocol
/// exists so the state machine can be tested without a microphone.
@MainActor
public protocol VoiceLoopEar: AnyObject {
    /// Non-nil when this device cannot listen right now (denied, unavailable).
    var limitationLine: String? { get }
    /// Fired with a settled, non-empty utterance. Never with silence.
    var onAutoSettle: ((String) -> Void)? { get set }
    /// Fired when the mic could not listen at all.
    var onAutoUnavailable: (() -> Void)? { get set }
    func beginListening()
    func cancelListening()
}

/// The mouth the loop speaks through. `AgentVoice` is the only real one.
@MainActor
public protocol VoiceLoopMouth: AnyObject {
    /// Fired once when an utterance reached its end. A deliberate `stop()`
    /// never fires it, because an interrupt is not a finish.
    var onFinished: (() -> Void)? { get set }
    /// Fired when an utterance could not be spoken at all.
    var onUnavailable: (() -> Void)? { get set }
    func speak(_ text: String, session: BlinkSession, force: Bool)
    func stop()
}

/// The conversation the loop forwards into. `PlanComposer` is the only real one.
@MainActor
public protocol VoiceLoopSink: AnyObject {
    func sendMessage(_ text: String) async
}

// The real parts satisfy these already; the conformances live here so neither
// AgentVoice nor VoiceCapture nor PlanComposer has to know a loop exists.
extension VoiceCapture: VoiceLoopEar {}
extension AgentVoice: VoiceLoopMouth {}
extension PlanComposer: VoiceLoopSink {}

@MainActor
@Observable
public final class VoiceLoop {

    /// How the loop OPENED. Nothing after the opening move reads this except
    /// the wording of the one honest fallback line.
    public enum Mode: Equatable {
        /// Blink starts, with the check-in opener.
        case checkIn
        /// The person started, by tapping the mic. No opener.
        case conversation
    }

    public enum Phase: Equatable {
        /// Not in the loop.
        case off
        /// The opener is in flight; nothing has been said yet. (`.checkIn` only.)
        case opening
        /// Blink is speaking a reply.
        case speaking
        /// The mic is open, waiting for the person to talk.
        case listening
        /// What the person said is in flight.
        case sending
    }

    public private(set) var phase: Phase = .off
    public private(set) var mode: Mode = .checkIn
    public var isActive: Bool { phase != .off }

    /// The one honest line shown when the spoken loop cannot run and it hands
    /// back to the typed/tap flow. Nil unless it just fell back. The screen
    /// shows it quietly and clears it on the next thing the person does.
    public private(set) var fellBackLine: String?

    // The parts, handed in by the screen that owns them. Not owned here.
    @ObservationIgnored private weak var voice: (any VoiceLoopMouth)?
    @ObservationIgnored private weak var capture: (any VoiceLoopEar)?
    @ObservationIgnored private weak var composer: (any VoiceLoopSink)?
    @ObservationIgnored private var session: BlinkSession?

    /// The one user-role line this class composes, and only in `.checkIn`: it
    /// starts the conversation the agent runs as a check-in. Everything after is
    /// the server's words in, the person's words out. No em dashes
    /// (conversational-voice.md).
    private let opener = "Let's do today's check-in."

    public nonisolated init() {}

    /// Wire the loop to the parts and the session. Called once, where the screen
    /// configures its composer. The finish/settle callbacks are set here and
    /// guard on `isActive`, so they lie dormant for every ordinary spoken reply
    /// and only advance the loop while it is genuinely running.
    public func configure(
        voice: any VoiceLoopMouth,
        capture: any VoiceLoopEar,
        composer: any VoiceLoopSink,
        session: BlinkSession
    ) {
        self.voice = voice
        self.capture = capture
        self.composer = composer
        self.session = session

        voice.onFinished = { [weak self] in self?.spokenReplyFinished() }
        voice.onUnavailable = { [weak self] in self?.speechUnavailable() }
        capture.onAutoSettle = { [weak self] text in self?.heard(text) }
        capture.onAutoUnavailable = { [weak self] in self?.micUnavailable() }
    }

    // MARK: Entry

    /// Begin the hands-free check-in: BLINK opens. Clears any stale fallback
    /// line and sends the opener; the reply that comes back is the first thing
    /// Blink speaks.
    public func start() {
        guard !isActive, let composer, session != nil else { return }
        fellBackLine = nil
        mode = .checkIn
        phase = .opening
        Task { await composer.sendMessage(opener) }
    }

    /// Begin a spoken conversation: the PERSON opens, because they just tapped
    /// the mic. No opener is composed and nothing is said first; the mic goes
    /// straight on. Any reply audio still playing is cut, since the tap was the
    /// person deciding to talk.
    public func startConversation() {
        guard !isActive, composer != nil, session != nil, let capture else { return }
        fellBackLine = nil
        // A mic that cannot listen must say so rather than leave the loop
        // sitting in a "Listening" that never hears anything.
        guard capture.limitationLine == nil else {
            mode = .conversation
            fallBack(micLine(for: .conversation))
            return
        }
        voice?.stop()
        mode = .conversation
        phase = .listening
        capture.beginListening()
    }

    /// The person's action moved on; drop a fallback line if one was showing.
    public func clearFellBack() { fellBackLine = nil }

    // MARK: Interruption

    /// A tap on the mic WHILE the loop runs.
    ///
    /// Speaking  -> the interrupt: cut the audio mid-word and listen at once.
    ///              "I can still interrupt when my voice comes out" (user).
    /// Otherwise -> the mic is already the person's turn, or a turn is in
    ///              flight, so tapping it again ends the loop.
    public func micTapped() {
        guard isActive else { return }
        guard phase == .speaking else { stop(); return }
        interrupt()
    }

    /// Cut the reply and open the mic. Safe to call only while speaking; any
    /// other phase is left exactly as it was.
    public func interrupt() {
        guard isActive, phase == .speaking, let capture else { return }
        voice?.stop()          // an interrupt fires neither finish nor failure
        listen(capture)
    }

    // MARK: The turn boundary (driven by the screen)

    /// The screen calls this when the composer finishes a /turn (its `isSending`
    /// fell to false) while the loop is running. Reading the composer's settled
    /// state here — rather than off a reply string that could repeat — is what
    /// makes each turn advance exactly once.
    public func turnCompleted(
        reply: String?,
        refused: Bool,
        unreachable: Bool,
        hasQuestion: Bool
    ) {
        guard isActive, phase == .opening || phase == .sending else { return }

        // A structured question (an elicit surface) is a tap interaction, not a
        // spoken one. Hand it back to the normal flow to answer, quietly.
        if hasQuestion { stop(); return }

        // The turn failed. The reply surface already carries the honest failure
        // line ("that did not go through" / "could not reach the planner"), so
        // leave the loop without inventing a second one; the typed flow returns.
        if refused || unreachable { stop(); return }

        guard let reply, !reply.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              let voice, let session else { stop(); return }

        phase = .speaking
        voice.speak(reply, session: session, force: true)
    }

    // MARK: The spoken-reply -> mic -> settle -> send chain

    private func spokenReplyFinished() {
        guard isActive, phase == .speaking, let capture else { return }
        listen(capture)
    }

    /// Open the mic for the person's turn, or fall back if this device cannot.
    /// Every path into `.listening` goes through here, so the loop can never
    /// sit in a "Listening" that no microphone is behind.
    private func listen(_ capture: any VoiceLoopEar) {
        guard capture.limitationLine == nil else {
            fallBack(micLine(for: mode))
            return
        }
        phase = .listening
        capture.beginListening()
    }

    private func speechUnavailable() {
        guard isActive else { return }
        // A hands-free flow that cannot speak is not hands-free. Say so once and
        // hand back to text; the reply itself is already on screen to read.
        switch mode {
        case .checkIn:
            fallBack("I cannot say that out loud right now, so let's keep the check-in in text.")
        case .conversation:
            fallBack("I cannot say that out loud right now, so let's keep talking in text.")
        }
    }

    /// A settled utterance. It goes STRAIGHT to the server: it is never written
    /// into the compose draft, so the recognizer's guesses are never on screen.
    private func heard(_ text: String) {
        guard isActive, phase == .listening, let composer else { return }
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        phase = .sending
        Task { await composer.sendMessage(text) }
    }

    private func micUnavailable() {
        guard isActive else { return }
        fallBack(micLine(for: mode))
    }

    private func micLine(for mode: Mode) -> String {
        switch mode {
        case .checkIn:
            return "I cannot listen right now, so we can carry on the check-in in text."
        case .conversation:
            return "I cannot listen right now, so we can keep going in text."
        }
    }

    // MARK: Exit

    /// End the loop cleanly, with no line: the person tapped Done or the mic
    /// again, or the app went to the background. Stops the mic and any speech
    /// at once.
    public func stop() {
        guard isActive else { return }
        phase = .off
        capture?.cancelListening()
        voice?.stop()
    }

    /// End the loop AND leave one honest sentence about why the spoken flow
    /// stopped, so it is never a silent dead end.
    private func fallBack(_ line: String) {
        phase = .off
        capture?.cancelListening()
        voice?.stop()
        fellBackLine = line
    }
}
