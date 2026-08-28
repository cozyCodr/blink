import Foundation

// The grounded material a signal is allowed to be made of, and the two writes
// its buttons perform.
//
// Nothing new is built on the server for this item. `POST /trigger` with
// `morning_brief` and `POST /onboarding/answer` with step `insight_response`
// both already run in production; this file only reads them from Swift.

/// What `POST /v1/workspaces/{id}/trigger {"trigger":"morning_brief"}` answers
/// with, under `brief` (`src/api/server.py`, `trigger_routine`).
///
/// The important field is `notificationBody`: a finished sentence the SERVER
/// composed from its own counts (`src/agent/triggers.py`,
/// `execute_morning_brief`). The device relays it and does not rewrite it.
/// It is `nil` when today holds no blocks, and a nil body means no brief is
/// sent at all: silence is a first-class output
/// (.agents/rules/agent-governance.md, invariant 5).
public struct MorningBrief: Sendable, Equatable {
    /// The server's own count of today's planned blocks.
    public let blocksToday: Int
    /// The first block's start, naive UTC, or nil when there is none.
    public let firstStart: Date?
    public let totalMinutes: Int
    /// Server-composed. The whole sentence, or nothing.
    public let notificationBody: String?
    /// At most one, and absent entirely when the history holds no pattern.
    public let insight: ServerInsight?
}

/// A P9-09 insight exactly as the server surfaces it. Every string here is the
/// server's; the device never edits one.
public struct ServerInsight: Sendable, Equatable {
    /// Deterministic, so a dismissal permanently silences the same pattern.
    public let insightID: String
    public let kind: String
    /// The sentence to show. Composed from a deterministic template and
    /// naturalised by the model with the evidence numbers required verbatim.
    public let text: String
    /// The numbers underneath it. Server words.
    public let evidenceText: String?
}

/// Reading the material a signal is composed from.
public protocol SignalSourceReading: Sendable {
    /// Today's brief, and the insight riding it when there is one.
    func morningBrief(for session: BlinkSession) async throws -> MorningBrief
}

/// The writes a notification's buttons perform. Both go to endpoints that
/// already exist, and both write the record the web would have written
/// (docs/COMPANION_ARCHITECTURE.md §5, "One store, one truth").
public protocol SignalWriting: Sendable {
    /// Done / Partly / Skip, and "Not tonight", which is a real skip.
    func resolve(
        block blockID: String,
        as outcome: CheckinOutcome,
        for session: BlinkSession
    ) async throws -> CheckinResolveResponse

    /// Adapt / Leave it. The consent verdict on a surfaced insight
    /// (docs/COMPANION_SCREENS.md S4: "the answer posts to the existing
    /// consent endpoint").
    ///
    /// Returns the SERVER'S own sentence about what it changed, which is what
    /// the confirmation shows. The server re-mines the pattern from its own
    /// history and "cites only what actually changed"
    /// (`_handle_insight_response`), so relaying it is the one way the device
    /// can confirm this without describing a write it did not perform. Nil
    /// means the server said nothing, and then the device says nothing either.
    func respondToInsight(
        _ insightID: String,
        accept: Bool,
        for session: BlinkSession
    ) async throws -> String?
}

/// The real client. Composes `BlinkDetailsClient` for the two things it
/// already does rather than restating them.
public struct BlinkSignalClient: SignalSourceReading, SignalWriting {
    private let baseURL: URL
    private let urlSession: URLSession
    private let details: any DetailsReading

    public init(
        baseURL: URL = BlinkAPI.baseURL(),
        urlSession: URLSession = .shared,
        details: (any DetailsReading)? = nil
    ) {
        self.baseURL = baseURL
        self.urlSession = urlSession
        self.details = details ?? BlinkDetailsClient(baseURL: baseURL, urlSession: urlSession)
    }

    // MARK: Reading

    public func morningBrief(for session: BlinkSession) async throws -> MorningBrief {
        let body = try await post(
            path: "trigger",
            json: ["trigger": "morning_brief"],
            session: session,
            label: "trigger/morning_brief"
        )
        guard let brief = body["brief"] as? [String: Any] else {
            // The endpoint answered, but not with a brief. Nothing to relay,
            // and nothing to invent.
            return MorningBrief(
                blocksToday: 0, firstStart: nil, totalMinutes: 0,
                notificationBody: nil, insight: nil
            )
        }
        var insight: ServerInsight?
        if let raw = brief["insight"] as? [String: Any],
           let id = raw["insight_id"] as? String,
           let text = raw["text"] as? String, !text.isEmpty {
            insight = ServerInsight(
                insightID: id,
                kind: raw["kind"] as? String ?? "",
                text: text,
                evidenceText: (raw["evidence_text"] as? String).flatMap { $0.isEmpty ? nil : $0 }
            )
        }
        return MorningBrief(
            blocksToday: brief["blocks_today"] as? Int ?? 0,
            firstStart: (brief["first_start"] as? String).flatMap { try? ServerClock.date(from: $0) },
            totalMinutes: brief["total_minutes"] as? Int ?? 0,
            notificationBody: (brief["notification_body"] as? String).flatMap { $0.isEmpty ? nil : $0 },
            insight: insight
        )
    }

    // MARK: Writing

    public func resolve(
        block blockID: String,
        as outcome: CheckinOutcome,
        for session: BlinkSession
    ) async throws -> CheckinResolveResponse {
        try await details.resolve(block: blockID, as: outcome, for: session)
    }

    public func respondToInsight(
        _ insightID: String,
        accept: Bool,
        for session: BlinkSession
    ) async throws -> String? {
        let body = try await post(
            path: "onboarding/answer",
            json: [
                "step": "insight_response",
                "value": ["insight_id": insightID, "accept": accept],
            ],
            session: session,
            label: "onboarding/answer"
        )
        return (body["text"] as? String).flatMap { $0.isEmpty ? nil : $0 }
    }

    // MARK: The one transport

    /// The same shape, and the same error vocabulary, as `BlinkDetailsClient`.
    /// `refused` is something the server SAID; `unreachable` is something
    /// nobody said. Every caller in this folder depends on that distinction to
    /// keep its follow-up honest.
    private func post(
        path: String,
        json: [String: Any],
        session: BlinkSession,
        label: String
    ) async throws -> [String: Any] {
        let url = baseURL
            .appendingPathComponent("v1/workspaces")
            .appendingPathComponent(session.workspaceID)
            .appendingPathComponent(path)
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("Bearer \(session.token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 15
        request.httpBody = try? JSONSerialization.data(withJSONObject: json)

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await urlSession.data(for: request)
        } catch let error as URLError where error.code == .cancelled {
            throw DetailsError.cancelled
        } catch {
            if Task.isCancelled { throw DetailsError.cancelled }
            notificationLog("\(label): no answer")
            throw DetailsError.unreachable
        }
        guard let http = response as? HTTPURLResponse else { throw DetailsError.unreachable }
        if http.statusCode == 401 || http.statusCode == 403 {
            notificationLog("\(label): refused, not signed in")
            throw DetailsError.notSignedIn
        }
        guard http.statusCode == 200 || http.statusCode == 202 else {
            notificationLog("\(label): status \(http.statusCode)")
            throw DetailsError.refused(status: http.statusCode)
        }
        guard let body = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            notificationLog("\(label): 200 but unreadable")
            throw DetailsError.refused(status: 200)
        }
        return body
    }
}
