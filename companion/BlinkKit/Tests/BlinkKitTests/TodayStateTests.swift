import XCTest
@testable import BlinkKit

// The card is pure arithmetic over one payload, so it is tested the same way:
// hand it blocks with known statuses and a known server clock, and assert the
// exact case it derives.
//
// The load-bearing test is `workDoneNeverFiresOverAMiss`. A block logged
// `.missed` is no longer `.planned`, so it drops out of `pending`; the old
// derivation read that empty `pending` as "work done" and told the user their
// day was done while a session had been missed. That is the false-"done" the
// governance rules forbid, and it is the screen the user actually hit.
final class TodayStateTests: XCTestCase {

    // A fixed server day. `now` is 18:00 UTC — past the 17:00 check-in hour —
    // so ended-and-unresolved blocks land on `.checkIn`, not the pre-evening
    // `.endedAwaitingCheckIn`. Timezone is nil, so the clock is UTC and the
    // ISO strings below are already local.
    private let today = "2026-08-31"

    private func at(_ iso: String) -> Date {
        try! ServerClock.date(from: iso)
    }

    /// One block for `taskID`, with a title carried on a matching task.
    private func block(
        _ id: String,
        task: String,
        start: String,
        end: String,
        status: BlockStatus
    ) -> BlockPayload {
        BlockPayload(
            id: id,
            taskID: task,
            startsAt: at(start),
            endsAt: at(end),
            status: status,
            actualMinutes: nil,
            actualSource: nil
        )
    }

    private func details(
        now: String,
        blocks: [BlockPayload],
        tasks: [TaskPayload]
    ) -> WorkspaceDetails {
        WorkspaceDetails(
            workspaceID: "ws",
            today: today,
            now: at(now),
            timezone: nil,
            streak: 0,
            onboarded: true,
            blocks: blocks,
            tasks: tasks,
            commitments: []
        )
    }

    // MARK: The bug this item exists to kill

    func testWorkDoneNeverFiresOverAMiss() {
        // Two ended blocks: one done, one missed. Nothing running, nothing
        // ahead, nothing pending. The honest card is `.missedToday`, naming
        // the miss — never `.workDone`.
        let details = details(
            now: "2026-08-31T18:00:00",
            blocks: [
                block("a", task: "t1", start: "2026-08-31T08:00:00",
                      end: "2026-08-31T09:00:00", status: .done),
                block("b", task: "t2", start: "2026-08-31T09:00:00",
                      end: "2026-08-31T10:00:00", status: .missed)
            ],
            tasks: [
                TaskPayload(id: "t1", commitmentID: "c", title: "Kept work"),
                TaskPayload(id: "t2", commitmentID: "c", title: "Missed work")
            ]
        )

        let state = TodayState(details: details)

        XCTAssertNotEqual(state.card, .workDone,
                          "A missed block must never be reported as work done.")
        guard case .missedToday(let missed) = state.card else {
            return XCTFail("Expected .missedToday, got \(state.card)")
        }
        XCTAssertEqual(missed.map(\.id), ["b"])
        XCTAssertEqual(missed.first?.title, "Missed work")
    }

    // MARK: `.workDone` is still reachable when it is genuinely true

    func testWorkDoneWhenEveryBlockIsDoneOrPartial() {
        let details = details(
            now: "2026-08-31T18:00:00",
            blocks: [
                block("a", task: "t1", start: "2026-08-31T08:00:00",
                      end: "2026-08-31T09:00:00", status: .done),
                block("b", task: "t2", start: "2026-08-31T09:00:00",
                      end: "2026-08-31T10:00:00", status: .partial)
            ],
            tasks: [
                TaskPayload(id: "t1", commitmentID: "c", title: "Morning"),
                TaskPayload(id: "t2", commitmentID: "c", title: "Afternoon")
            ]
        )

        XCTAssertEqual(TodayState(details: details).card, .workDone)
    }

    // MARK: The ordinary paths still derive correctly

    func testNextSessionWhenAPlannedBlockIsAhead() {
        // Early morning: a planned block still ahead is the next session.
        let details = details(
            now: "2026-08-31T08:00:00",
            blocks: [
                block("a", task: "t1", start: "2026-08-31T09:00:00",
                      end: "2026-08-31T10:00:00", status: .planned)
            ],
            tasks: [TaskPayload(id: "t1", commitmentID: "c", title: "Deep work")]
        )

        guard case .nextSession(let card) = TodayState(details: details).card else {
            return XCTFail("Expected .nextSession, got \(TodayState(details: details).card)")
        }
        XCTAssertEqual(card.blockID, "a")
        XCTAssertEqual(card.title, "Deep work")
    }

    func testStillPendingCheckInWinsOverAMiss() {
        // A planned block that ended unresolved is still awaiting an answer, so
        // the check-in takes precedence — even when another block was missed.
        // The missed card is only for when nothing is left to ask.
        let details = details(
            now: "2026-08-31T18:00:00",
            blocks: [
                block("a", task: "t1", start: "2026-08-31T08:00:00",
                      end: "2026-08-31T09:00:00", status: .missed),
                block("b", task: "t2", start: "2026-08-31T10:00:00",
                      end: "2026-08-31T11:00:00", status: .planned)
            ],
            tasks: [
                TaskPayload(id: "t1", commitmentID: "c", title: "Missed"),
                TaskPayload(id: "t2", commitmentID: "c", title: "Unresolved")
            ]
        )

        guard case .checkIn(let pending) = TodayState(details: details).card else {
            return XCTFail("Expected .checkIn, got \(TodayState(details: details).card)")
        }
        XCTAssertEqual(pending.map(\.id), ["b"])
    }
}
