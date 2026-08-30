import Foundation
import Observation

/// P15-11 — the state behind S1's compose affordance: one /turn conversation,
/// the elicitation loop, and the grounded facts the eyes may react to.
///
/// The dispatch mirrors the web's (`dispatch(res)` in src/web/app.js:5768):
/// one response type, one branch, nothing invented. The reply text on screen
/// is ALWAYS the server's sentence verbatim; this class holds no copy of its
/// own beyond naming a failure.
///
/// WHAT THE EYES MAY READ, and what grounds each:
///   `isSending`      — a request genuinely in flight (thinking, the state)
///   `question != nil`— a question is genuinely up (curious, held)
///   `didRefuse`      — the server ANSWERED with a failure (sorry). An
///                      unreachable server never sets it, per the same line
///                      DetailsError draws.
///   `heartPending`   — the FIRST planned reply of this app session whose
///                      `blocks_scheduled` was > 0. Consumed once.
@MainActor
@Observable
public final class PlanComposer {
    // MARK: What the screen reads

    /// The text being typed. Bindable on purpose.
    public var draft = ""
    public private(set) var isSending = false
    /// The last reply text to show (message / planned / checkin / courses).
    /// Verbatim from the server.
    public private(set) var reply: String?
    /// The question currently up, or nil.
    public private(set) var question: TurnQuestion?
    /// The user's committed answer, echoed while the next round is in flight.
    public private(set) var answerEcho: String?
    /// A course offer is up (the phone renders no cards; it offers the skip).
    public private(set) var courseOfferUp = false
    /// The server answered with a failure this app could not act on.
    public private(set) var didRefuse = false
    /// The request never got an answer. Separate from `didRefuse` because
    /// nobody apologises for a dead network.
    public private(set) var wasUnreachable = false
    /// The bearer is dead; the app should fall back to sign-in.
    public private(set) var needsSignIn = false
    /// The first placed plan of this session, not yet shown. See `consumeHeart`.
    public private(set) var heartPending = false
    /// Bumps every time a plan with placed blocks lands, so Today can raise the
    /// native plan surface on it (P18-01). A counter, not a flag: two plans in
    /// a row are two openings, and a plain re-read never moves it.
    public private(set) var planLandings = 0

    // MARK: Configuration

    @ObservationIgnored private let client: BlinkDetailsClient
    @ObservationIgnored private var session: BlinkSession?
    /// Runs after a reply that CHANGED the plan (planned/replanned/checkin),
    /// so Today re-reads and its numbers stay the server's numbers.
    @ObservationIgnored private var onPlanChanged: (() async -> Void)?
    /// `{commitment_id, goal}` carried between elicitation rounds.
    @ObservationIgnored private var elicit: ElicitSession?
    /// `[{role, content}]` for message turns, the same thread the web keeps.
    @ObservationIgnored private var history: [[String: String]] = []
    @ObservationIgnored private var heartFired = false

    public nonisolated init(baseURL: URL = BlinkAPI.baseURL()) {
        self.client = BlinkDetailsClient(baseURL: baseURL)
    }

    public func configure(session: BlinkSession, onPlanChanged: @escaping () async -> Void) {
        self.session = session
        self.onPlanChanged = onPlanChanged
    }

    /// The heart beat's one exit: read true at most once per pending plan.
    public func consumeHeart() -> Bool {
        guard heartPending else { return false }
        heartPending = false
        return true
    }

    // MARK: Sending

    /// Send the draft as a /turn message. One request in flight at a time,
    /// the same guard the web holds (app.js `busy`).
    public func send() async {
        let message = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !message.isEmpty, !isSending, let session else { return }
        draft = ""
        history.append(["role": "user", "content": message])
        await run {
            try await self.client.turn(message: message, history: self.history, for: session)
        }
    }

    /// Answer the question that is up. Clears it immediately — curious ends
    /// when the answer commits — and echoes the answer while the next round
    /// is in flight.
    public func answer(_ value: ElicitAnswerValue) async {
        guard let session, let elicit, let question, !isSending else { return }
        self.question = nil
        answerEcho = value.spoken
        await run {
            try await self.client.answer(
                commitmentID: elicit.commitmentID,
                goal: elicit.goal,
                field: question.field,
                value: value,
                for: session
            )
        }
    }

    /// Decline the course offer: an empty pick, straight through to the plan.
    public func skipCourses() async {
        guard let session, let elicit, courseOfferUp, !isSending else { return }
        courseOfferUp = false
        answerEcho = "Skip those, plan without them"
        await run {
            try await self.client.skipCourses(
                commitmentID: elicit.commitmentID, goal: elicit.goal, for: session)
        }
    }

    // MARK: The one request path

    private func run(_ request: @escaping () async throws -> TurnResponse) async {
        isSending = true
        didRefuse = false
        wasUnreachable = false
        defer { isSending = false }
        do {
            let res = try await request()
            answerEcho = nil
            await dispatch(res)
        } catch DetailsError.notSignedIn {
            needsSignIn = true
        } catch DetailsError.cancelled {
            // We stopped asking. Nothing was learned; nothing changes.
            return
        } catch DetailsError.refused {
            didRefuse = true
        } catch {
            wasUnreachable = true
        }
    }

    /// One response type, one branch — the phone's copy of the web's dispatch.
    private func dispatch(_ res: TurnResponse) async {
        switch res.type {
        case "message":
            if let text = res.text {
                reply = text
                history.append(["role": "assistant", "content": text])
            }

        case "planned", "replanned":
            reply = res.text
            if let text = res.text {
                history.append(["role": "assistant", "content": text])
            }
            question = nil
            courseOfferUp = false
            elicit = nil
            // Grounded: the server's own count of placed blocks, first time
            // this session. Never recomputed here.
            if (res.blocksScheduled ?? 0) > 0 {
                if !heartFired {
                    heartFired = true
                    heartPending = true
                }
                // Every landed plan raises the plan surface, not just the first.
                planLandings += 1
            }
            await onPlanChanged?()

        case "question":
            reply = nil
            question = res.question
            if let session = res.session { elicit = session }

        case "checkin":
            // Today already owns a check-in surface; route there rather than
            // building a second one. The text is the server's line about it.
            reply = res.text
            await onPlanChanged?()

        case "courses":
            reply = res.text
            if let session = res.session { elicit = session }
            courseOfferUp = true

        default:
            // A type this app does not know. Render its text if it carried
            // one — the server's words are still true — otherwise say only
            // that, and claim nothing else.
            if let text = res.text {
                reply = text
            } else {
                didRefuse = true
            }
        }
    }
}
