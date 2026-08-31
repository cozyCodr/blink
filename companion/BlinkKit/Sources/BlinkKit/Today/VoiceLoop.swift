import Foundation
import Observation

// P18-04b — the hands-free evening check-in.
//
// The backend already turned the check-in into a normal grounded agent
// conversation (message replies + tools). This is the phone's HANDS-FREE shell
// around that conversation: Blink speaks its question, the mic opens on its own,
// the answer sends on its own, Blink speaks the next question, and so on, until
// the person ends it. Nobody taps a button between turns.
//
// WHAT THIS CLASS DOES AND DOES NOT DO
//
//   * It ORCHESTRATES three parts it does not own: `AgentVoice` (speak),
//     `VoiceCapture` (listen), `PlanComposer` (the /turn conversation). It holds
//     weak-in-spirit references handed in through `configure`, and drives them
//     in a strict order so no two ever run at once (never speaking over the mic,
//     never sending before the words settle).
//
//   * It carries VOICE ONLY. It never writes an outcome and never claims one —
//     the agent (server) logs through its own tools, and every reply on screen
//     is the server's sentence. This class composes exactly one user-role
//     opener to start the conversation, and otherwise only forwards what was
//     heard.
//
//   * It DEGRADES, never traps. If the mic or speech permission is denied, or
//     TTS cannot speak this device's reply, the loop falls back to the normal
//     typed/tap flow with one honest line. The structured button check-in
//     (TodayStore's `.checkIn` card) is untouched and remains the real fallback.
//
// THE ORDER, exactly:
//
//   start()  -> send the opener            (phase .opening, composer in flight)
//   a turn completes with a reply          -> speak it, forced   (phase .speaking)
//   the spoken reply finishes              -> open the mic       (phase .listening)
//   the speech settles (final / silence)   -> send it            (phase .sending)
//   a turn completes with a reply          -> speak it again … and round it goes
//
// The eyes read as listening for free: the loop opens the SAME `VoiceCapture`
// whose `isRecording` already drives the existing `wide` beat. No eye work here.
@MainActor
@Observable
public final class CheckInVoiceLoop {

    public enum Phase: Equatable {
        /// Not in the loop.
        case off
        /// The opener is in flight; nothing has been said yet.
        case opening
        /// Blink is speaking a reply.
        case speaking
        /// The mic is open, waiting for the person to answer.
        case listening
        /// The person's answer is in flight.
        case sending
    }

    public private(set) var phase: Phase = .off
    public var isActive: Bool { phase != .off }

    /// The one honest line shown when the spoken loop cannot run and it hands
    /// back to the typed/tap flow. Nil unless it just fell back. The screen
    /// shows it quietly and clears it on the next thing the person does.
    public private(set) var fellBackLine: String?

    // The parts, handed in by the screen that owns them. Not owned here.
    @ObservationIgnored private weak var voice: AgentVoice?
    @ObservationIgnored private weak var capture: VoiceCapture?
    @ObservationIgnored private weak var composer: PlanComposer?
    @ObservationIgnored private var session: BlinkSession?

    /// The one user-role line this class composes: it starts the conversation
    /// the agent runs as a check-in. Everything after is the server's words in,
    /// the person's words out. No em dashes (conversational-voice.md).
    private let opener = "Let's do today's check-in."

    public nonisolated init() {}

    /// Wire the loop to the parts and the session. Called once, where the screen
    /// configures its composer. The finish/settle callbacks are set here and
    /// guard on `isActive`, so they lie dormant for every ordinary spoken reply
    /// and only advance the loop while it is genuinely running.
    public func configure(
        voice: AgentVoice,
        capture: VoiceCapture,
        composer: PlanComposer,
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

    /// Begin the hands-free check-in. Clears any stale fallback line and sends
    /// the opener; the reply that comes back is the first thing Blink speaks.
    public func start() {
        guard !isActive, let composer, session != nil else { return }
        fellBackLine = nil
        phase = .opening
        Task { await composer.sendMessage(opener) }
    }

    /// The person's action moved on; drop a fallback line if one was showing.
    public func clearFellBack() { fellBackLine = nil }

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
        phase = .listening
        capture.beginListening()
    }

    private func speechUnavailable() {
        guard isActive else { return }
        // A hands-free flow that cannot speak is not hands-free. Say so once and
        // hand back to text; the reply itself is already on screen to read.
        fallBack("I cannot say that out loud right now, so let's keep the check-in in text.")
    }

    private func heard(_ text: String) {
        guard isActive, phase == .listening, let composer else { return }
        phase = .sending
        Task { await composer.sendMessage(text) }
    }

    private func micUnavailable() {
        guard isActive else { return }
        fallBack("I cannot listen right now, so we can carry on the check-in in text.")
    }

    // MARK: Exit

    /// End the loop cleanly, with no line: the person tapped Done, or the app
    /// went to the background. Stops the mic and any speech at once.
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
