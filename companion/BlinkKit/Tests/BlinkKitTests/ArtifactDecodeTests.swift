import XCTest
@testable import BlinkKit

// P20-03 — the additive artifact payloads on /turn and /reschedule replies.
//
// The backend worker is growing these in parallel, so the contract worth
// pinning is DEFENSIVENESS in both directions: a reply carrying the payloads
// decodes them faithfully (times through the one naive-UTC parser), a reply
// without them decodes to empty, and a MALFORMED payload degrades to "no
// cards" without taking the reply's text down with it.
final class ArtifactDecodeTests: XCTestCase {

    private func decode(_ json: String) throws -> TurnResponse {
        try JSONDecoder().decode(TurnResponse.self, from: Data(json.utf8))
    }

    func testPlannedReplyDecodesSessionArtifacts() throws {
        let json = """
        {"type":"planned","text":"Planned 2 sessions.","blocks_scheduled":2,
         "artifacts":{"sessions":[
            {"title":"Model training","starts_at":"2026-08-31T15:00:00",
             "ends_at":"2026-08-31T16:30:00","why":"Momentum on the course",
             "calendar":true},
            {"title":"Reading","starts_at":"2026-09-01T09:00:00",
             "ends_at":"2026-09-01T09:45:00","calendar":false}]}}
        """
        let res = try decode(json)
        XCTAssertEqual(res.sessionArtifacts.count, 2)
        let first = try XCTUnwrap(res.sessionArtifacts.first)
        XCTAssertEqual(first.title, "Model training")
        XCTAssertTrue(first.calendar)
        XCTAssertEqual(first.why, "Momentum on the course")
        XCTAssertEqual(first.endsAt.timeIntervalSince(first.startsAt), 90 * 60,
                       "The meta line's 90 min comes from these two instants.")
        let second = res.sessionArtifacts[1]
        XCTAssertFalse(second.calendar)
        XCTAssertNil(second.why)
    }

    func testReplannedReplyDecodesMoves() throws {
        let json = """
        {"type":"replanned","text":"Moved 2 sessions.",
         "moves":[
            {"title":"Deep work","old_start":"2026-08-31T15:00:00",
             "new_start":"2026-08-31T18:00:00","calendar":"moved"},
            {"title":"Review","old_start":"2026-08-31T16:00:00",
             "new_start":"2026-08-31T19:00:00","calendar":"partial"}],
         "calendar_note":"One calendar event is still syncing."}
        """
        let res = try decode(json)
        XCTAssertEqual(res.moveArtifacts.count, 2)
        XCTAssertEqual(res.moveArtifacts[0].calendar, "moved")
        XCTAssertEqual(res.moveArtifacts[1].calendar, "partial")
        XCTAssertEqual(res.calendarNote, "One calendar event is still syncing.")
    }

    /// A reply that never heard of the payloads decodes exactly as before.
    func testReplyWithoutArtifactsDecodesEmpty() throws {
        let res = try decode(#"{"type":"planned","text":"Planned.","blocks_scheduled":1}"#)
        XCTAssertTrue(res.sessionArtifacts.isEmpty)
        XCTAssertTrue(res.moveArtifacts.isEmpty)
        XCTAssertNil(res.calendarNote)
    }

    /// A malformed additive payload (a session with an unparseable datetime)
    /// must not take the whole reply down: the text still renders, the cards
    /// simply do not exist.
    func testMalformedArtifactsDegradeToNoCards() throws {
        let json = """
        {"type":"planned","text":"Planned 1 session.","blocks_scheduled":1,
         "artifacts":{"sessions":[{"title":"Broken","starts_at":"not a date",
                                   "ends_at":"2026-08-31T16:00:00"}]},
         "moves":[{"title":"Broken too","old_start":"also not a date",
                   "new_start":"2026-08-31T18:00:00"}]}
        """
        let res = try decode(json)
        XCTAssertEqual(res.text, "Planned 1 session.")
        XCTAssertTrue(res.sessionArtifacts.isEmpty)
        XCTAssertTrue(res.moveArtifacts.isEmpty)
    }

    /// An absent `calendar` on a session reads as false: no chip without the
    /// grounded fact. An absent `calendar` on a move reads as "none": silence.
    func testCalendarDefaultsStayQuiet() throws {
        let json = """
        {"type":"replanned","text":"Done.",
         "artifacts":{"sessions":[{"title":"S","starts_at":"2026-08-31T15:00:00",
                                   "ends_at":"2026-08-31T16:00:00"}]},
         "moves":[{"title":"M","old_start":"2026-08-31T15:00:00",
                   "new_start":"2026-08-31T17:00:00"}]}
        """
        let res = try decode(json)
        XCTAssertEqual(res.sessionArtifacts.first?.calendar, false)
        XCTAssertEqual(res.moveArtifacts.first?.calendar, "none")
    }
}
