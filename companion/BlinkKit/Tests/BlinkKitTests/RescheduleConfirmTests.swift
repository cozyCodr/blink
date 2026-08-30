import XCTest
@testable import BlinkKit

// P19-06 — the phone's half of the `field="reschedule"` confirm rails.
//
// The routing itself (confirmCalendar → confirmReschedule → POST /reschedule)
// runs behind a live network client and @MainActor state, so what is worth
// pinning here is the WIRE CONTRACT the routing keys on: the exact confirm the
// agent surfaces (`_confirm_question` + tools.py:764) must decode into a
// `TurnQuestion` whose field is "reschedule" and whose config carries the
// single-use `token` the YES replays — and the endpoint's reply must decode so
// the shared dispatch can render the server's own sentence verbatim. If any of
// these keys drift, the YES silently stops finding its token; these assert they
// do not.
final class RescheduleConfirmTests: XCTestCase {

    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder().decode(T.self, from: Data(json.utf8))
    }

    // MARK: The confirm the agent surfaces

    /// The reschedule confirm decodes to a `TurnQuestion` the confirm control
    /// routes: input_type "confirm", field exactly "reschedule", and the token
    /// read off config — verbatim the `_confirm_question(field="reschedule",
    /// config={action, token, summary, moves})` bag (tools.py:764).
    func testRescheduleConfirmQuestionDecodes() throws {
        let json = """
        {"type":"question","input_type":"confirm",
         "question":"Move 2 sessions to 4pm and 6pm?",
         "why":"I never move sessions on your plan without a yes first.",
         "field":"reschedule",
         "options":[{"label":"Yes","value":null,"opens_free_text":false},
                    {"label":"Not now","value":null,"opens_free_text":false}],
         "allow_free_text":false,
         "config":{"action":"reschedule","token":"rsx_abc123",
                   "summary":"Move 2 sessions to 4pm and 6pm",
                   "moves":[{"old_block_id":"b1","task_id":"t1","task":"Deep work",
                             "start":"2026-08-31T16:00:00","end":"2026-08-31T17:00:00"}]}}
        """

        let q = try decode(TurnQuestion.self, json)

        XCTAssertEqual(q.inputType, "confirm")
        XCTAssertEqual(q.field, "reschedule")
        XCTAssertEqual(q.config?.action, "reschedule")
        XCTAssertEqual(q.config?.token, "rsx_abc123",
                       "The YES replays this exact single-use token.")
    }

    /// The token survives a bare config bag too — the unrelated `moves` list and
    /// the calendar-only keys stay absent without breaking the decode.
    func testRescheduleConfigDecodesTokenAlone() throws {
        let cfg = try decode(TurnQuestionConfig.self,
                             #"{"action":"reschedule","token":"rsx_only"}"#)
        XCTAssertEqual(cfg.token, "rsx_only")
        XCTAssertNil(cfg.eventID)
        XCTAssertNil(cfg.start)
    }

    /// A config with no token (every other confirm) still decodes, leaving the
    /// reschedule token nil — so `confirmReschedule`'s empty-token guard is real.
    func testNonRescheduleConfigHasNilToken() throws {
        let cfg = try decode(TurnQuestionConfig.self,
                             #"{"action":"create","event_id":"evt_1","summary":"Deep work"}"#)
        XCTAssertNil(cfg.token)
        XCTAssertEqual(cfg.eventID, "evt_1")
    }

    // MARK: The reply the endpoint returns

    /// The phase-2 success reply (server.py:2827) decodes so the shared dispatch
    /// renders the server's grounded sentence verbatim; the phone never
    /// re-derives the count from `moved`.
    func testRescheduleReplyDecodes() throws {
        let res = try decode(TurnResponse.self,
            #"{"type":"replanned","text":"Moved 2 sessions in your plan.","moved":2,"cancelled":2}"#)
        XCTAssertEqual(res.type, "replanned")
        XCTAssertEqual(res.text, "Moved 2 sessions in your plan.")
    }

    /// A stale/used token degrades to an honest `message` (server.py:2815), and
    /// it decodes the same way — nothing changed, and the reply says so.
    func testStaleTokenReplyDecodes() throws {
        let res = try decode(TurnResponse.self,
            #"{"type":"message","text":"That reschedule expired. Ask me to reschedule again."}"#)
        XCTAssertEqual(res.type, "message")
        XCTAssertEqual(res.text, "That reschedule expired. Ask me to reschedule again.")
    }
}
