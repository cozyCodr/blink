import Foundation

// P15-11 — Plan from the phone. The /turn conversation, over the SAME
// transport DetailsClient built (`BlinkDetailsClient.send`), speaking the SAME
// error vocabulary (`DetailsError`). This file is the phone's copy of the
// typed response contract `src/api/server.py` emits and `src/web/app.js`
// dispatches; the shapes below are read off those files, never invented.
//
// HONESTY: everything here is decode-only. The reply `text` is composed and
// grounded server-side and the phone renders it verbatim; no count is ever
// re-derived from the other fields.

/// One clarifying-question option (`ClarifyOption`, src/agent/conversation.py:27).
/// `value` is Optional[int] on the server and is None on every option the
/// elicitor ships today; the LABEL is what identifies the choice.
public struct TurnQuestionOption: Decodable, Sendable, Equatable, Identifiable {
    public let label: String
    public let value: Int?
    public let opensFreeText: Bool

    public var id: String { label }

    enum CodingKeys: String, CodingKey {
        case label, value
        case opensFreeText = "opens_free_text"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        label = try c.decode(String.self, forKey: .label)
        value = try c.decodeIfPresent(Int.self, forKey: .value)
        opensFreeText = try c.decodeIfPresent(Bool.self, forKey: .opensFreeText) ?? false
    }
}

/// The question's `config` bag (`ClarifyQuestion.config`,
/// src/agent/conversation.py:54). Two disjoint uses share the one struct,
/// exactly as they share `question.config` on the wire:
///   • the number input carries `{min, max, step, unit}`;
///   • a calendar confirm the AGENT surfaces carries the pending write —
///     `{action, event_id, summary, start, end}` — which the YES posts back
///     verbatim to `/calendar/events` (the web reads the same bag, app.js:6303).
/// Every field is optional and decode-only: a config with neither set decodes
/// fine, and an input reads only the keys its own type uses.
public struct TurnQuestionConfig: Decodable, Sendable, Equatable {
    // number input
    public let min: Int?
    public let max: Int?
    public let step: Int?
    public let unit: String?
    // calendar confirm (create / edit / delete)
    public let action: String?
    public let eventID: String?
    public let summary: String?
    public let start: String?
    public let end: String?

    enum CodingKeys: String, CodingKey {
        case min, max, step, unit, action, summary, start, end
        case eventID = "event_id"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        min = try c.decodeIfPresent(Int.self, forKey: .min)
        max = try c.decodeIfPresent(Int.self, forKey: .max)
        step = try c.decodeIfPresent(Int.self, forKey: .step)
        unit = try c.decodeIfPresent(String.self, forKey: .unit)
        action = try c.decodeIfPresent(String.self, forKey: .action)
        eventID = try c.decodeIfPresent(String.self, forKey: .eventID)
        summary = try c.decodeIfPresent(String.self, forKey: .summary)
        start = try c.decodeIfPresent(String.self, forKey: .start)
        end = try c.decodeIfPresent(String.self, forKey: .end)
    }
}

/// `ClarifyQuestion` (src/agent/conversation.py:33). `inputType` stays a raw
/// string on purpose: the server's Literal lists fourteen values, the elicitor
/// emits four (multi_select, single_select, number, free_text — read
/// src/agent/specialists/elicitor.py), and anything this app does not
/// recognise renders as free text rather than a dead end.
public struct TurnQuestion: Decodable, Sendable, Equatable {
    public let question: String
    public let field: String
    public let inputType: String
    public let options: [TurnQuestionOption]
    public let allowFreeText: Bool
    public let config: TurnQuestionConfig?
    public let why: String?

    enum CodingKeys: String, CodingKey {
        case question, field, options, config, why
        case inputType = "input_type"
        case allowFreeText = "allow_free_text"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        question = try c.decode(String.self, forKey: .question)
        field = try c.decode(String.self, forKey: .field)
        inputType = try c.decodeIfPresent(String.self, forKey: .inputType) ?? "free_text"
        options = try c.decodeIfPresent([TurnQuestionOption].self, forKey: .options) ?? []
        allowFreeText = try c.decodeIfPresent(Bool.self, forKey: .allowFreeText) ?? false
        config = try c.decodeIfPresent(TurnQuestionConfig.self, forKey: .config)
        why = try c.decodeIfPresent(String.self, forKey: .why)
    }
}

/// `{commitment_id, goal}`, carried verbatim between elicitation rounds —
/// the same shape the web's `session` variable holds (app.js:5395).
public struct ElicitSession: Decodable, Sendable, Equatable {
    public let commitmentID: String
    public let goal: String

    enum CodingKeys: String, CodingKey {
        case commitmentID = "commitment_id"
        case goal
    }
}

/// One typed reply off `POST /turn`, `/elicit/answer` or `/elicit/courses`.
/// Decode-only: only fields this app renders are read, and the reply text is
/// the server's sentence, verbatim.
public struct TurnResponse: Decodable, Sendable, Equatable {
    /// "message" | "planned" | "replanned" | "question" | "checkin" |
    /// "courses" | anything future. Kept raw so an unknown type degrades to
    /// its text instead of a decode failure.
    public let type: String
    public let text: String?
    /// `planned` only: how many blocks the scheduler actually placed. The
    /// grounded fact behind the heart beat (blocks_scheduled > 0), never
    /// recomputed here.
    public let blocksScheduled: Int?
    public let question: TurnQuestion?
    public let session: ElicitSession?

    enum CodingKeys: String, CodingKey {
        case type, text, question, session
        case blocksScheduled = "blocks_scheduled"
    }
}

/// The one answer value shapes the elicitor's four input types produce
/// (components.js:31-44 documents the web's): labels for selects, an
/// integer for number, a string for free text.
public enum ElicitAnswerValue: Sendable, Equatable {
    case text(String)
    case texts([String])
    case number(Int)

    var jsonObject: Any {
        switch self {
        case .text(let s): return s
        case .texts(let a): return a
        case .number(let n): return n
        }
    }

    /// What the user's answer reads as, for the echo line. Presentation only.
    public var spoken: String {
        switch self {
        case .text(let s): return s
        case .texts(let a): return a.joined(separator: ", ")
        case .number(let n): return String(n)
        }
    }
}

/// The /turn conversation, as an extension of the ONE client. Same bearer,
/// same send, same four-way error split (notSignedIn / refused / unreachable /
/// cancelled).
extension BlinkDetailsClient {
    /// `POST /v1/workspaces/{ws}/turn {"message": …, "history": …}`
    /// (TurnRequest, src/api/server.py:175). `mode` is omitted: absent means
    /// "fast", exactly what an old client sends.
    public func turn(
        message: String,
        history: [[String: String]],
        for session: BlinkSession
    ) async throws -> TurnResponse {
        var body: [String: Any] = ["message": message]
        if !history.isEmpty { body["history"] = history }
        return try await post(path: "turn", body: body, label: "turn", session: session)
    }

    /// `POST /v1/workspaces/{ws}/elicit/answer` — `{commitment_id, goal,
    /// field, value}` exactly as the web sends it (app.js:6067-6079;
    /// ElicitAnswerRequest, src/api/server.py:183).
    public func answer(
        commitmentID: String,
        goal: String,
        field: String,
        value: ElicitAnswerValue,
        for session: BlinkSession
    ) async throws -> TurnResponse {
        try await post(
            path: "elicit/answer",
            body: [
                "commitment_id": commitmentID,
                "goal": goal,
                "field": field,
                "value": value.jsonObject,
            ],
            label: "elicit/answer",
            session: session
        )
    }

    /// `POST /v1/workspaces/{ws}/elicit/courses` with NO picks — the web's
    /// Skip (app.js:6102-6110). The phone renders no course cards (out of
    /// P15-11's scope), so when the server offers them this is the honest way
    /// through to the plan.
    public func skipCourses(
        commitmentID: String,
        goal: String,
        for session: BlinkSession
    ) async throws -> TurnResponse {
        try await post(
            path: "elicit/courses",
            body: ["commitment_id": commitmentID, "goal": goal, "courses": [Any]()],
            label: "elicit/courses",
            session: session
        )
    }

    /// The YES to a calendar confirm the agent surfaced through `/turn`:
    /// `POST /v1/workspaces/{ws}/calendar/events {confirm:true, action,
    /// event_id?, summary?, start?, end?}`, the confirm-gated write the web
    /// commits to (app.js:6305, server.py:2660). The pending action rides in
    /// `question.config`; only the keys it actually carries are sent.
    ///
    /// Returns on a 200 (the server performed the write), THROWS otherwise —
    /// `DetailsError.refused` on the 502 a failed Google write becomes,
    /// `.notSignedIn` on 401/403, `.unreachable` on a dead network — so the
    /// caller only ever says "Done" after a genuine success. This is the phase-2
    /// write only; the phone never asks for the phase-1 confirm (the agent does).
    public func writeCalendarEvent(
        _ config: TurnQuestionConfig,
        for session: BlinkSession
    ) async throws {
        var body: [String: Any] = ["confirm": true]
        if let action = config.action { body["action"] = action }
        if let eventID = config.eventID { body["event_id"] = eventID }
        if let summary = config.summary { body["summary"] = summary }
        if let start = config.start { body["start"] = start }
        if let end = config.end { body["end"] = end }

        let url = baseURL
            .appendingPathComponent("v1/workspaces")
            .appendingPathComponent(session.workspaceID)
            .appendingPathComponent("calendar/events")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("Bearer \(session.token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        // A Google write round trip lives behind this one, like sync-google.
        request.timeoutInterval = 30
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)

        // Success is the 200 itself; the result body is not read. `send` maps
        // every non-200 to the shared error vocabulary the caller branches on.
        _ = try await send(request, label: "calendar/events")
    }

    private func post(
        path: String,
        body: [String: Any],
        label: String,
        session: BlinkSession
    ) async throws -> TurnResponse {
        let url = baseURL
            .appendingPathComponent("v1/workspaces")
            .appendingPathComponent(session.workspaceID)
            .appendingPathComponent(path)
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("Bearer \(session.token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        // Planning turns run real LLM steps; the details timeout is too tight.
        request.timeoutInterval = 90
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)

        let data = try await send(request, label: label)
        do {
            return try JSONDecoder().decode(TurnResponse.self, from: data)
        } catch {
            detailsLog("\(label): 200 but undecodable")
            throw DetailsError.refused(status: 200)
        }
    }
}
