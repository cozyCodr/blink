#if DEBUG
import SwiftUI
import BlinkKit

// DEBUG SCAFFOLDING — not product UI.
//
// The native plan (P18-01) needs real block history, a ledger and a streak to
// show its Day and Week at their best, and signing in as a user is not
// something this project can do. The `-blinkDebugWorkspace` door reaches a live
// guest workspace (empty, so it only ever shows the empty state), which is not
// enough to inspect the populated timeline.
//
// So this door hands PlanScreen a HAND-BUILT `WorkspaceDetails` and renders the
// SHIPPING surface against it. Nothing about PlanScreen is stubbed: it derives
// its own PlanModel exactly as it does in the app, so what shows here is what a
// person with this plan would see. The sample is transparently fake — a week of
// a machine-learning course and marathon training — and exists only to exercise
// the layout, the honesty marks, and the empty state.
//
// `-blinkDebugPlan populated` (default) or `-blinkDebugPlan empty`.
struct DebugPlanRehearsalScreen: View {
    @Environment(\.face) private var face
    let variant: String

    var body: some View {
        let details = variant == "empty" ? PlanSample.empty() : PlanSample.populated()
        return PlanScreen(plan: PlanModel(details: details))
    }
}

/// The fabricated payloads behind the rehearsal door. All times are naive UTC
/// and the sample carries no timezone, so `ServerClock` reads them as UTC and
/// the timeline lands each block on the hour it names.
enum PlanSample {
    private static let today = "2026-08-30"

    private static func utc(_ y: Int, _ mo: Int, _ d: Int, _ h: Int, _ mi: Int) -> Date {
        var c = DateComponents()
        c.year = y; c.month = mo; c.day = d; c.hour = h; c.minute = mi
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = .gmt
        return cal.date(from: c) ?? Date()
    }

    // (date, sh, sm, eh, em, status, source, actualMin, title, commitmentIndex)
    private struct B {
        let date: String, sh: Int, sm: Int, eh: Int, em: Int
        let status: BlockStatus, source: ActualSource?, actual: Int?
        let title: String, commitment: Int
    }
    // (date, [(sh,sm,eh,em)], available)
    private struct L { let date: String, windows: [(Int, Int, Int, Int)], available: Int }

    private static let commitments = [
        CommitmentPayload(id: "c_ml", title: "Machine learning course"),
        CommitmentPayload(id: "c_run", title: "Marathon training"),
    ]

    static func populated() -> WorkspaceDetails {
        let rows: [B] = [
            // last few days
            B(date: "2026-08-25", sh: 9, sm: 0, eh: 10, em: 30, status: .done, source: .timer, actual: 88, title: "Course intro", commitment: 0),
            B(date: "2026-08-26", sh: 15, sm: 0, eh: 16, em: 0, status: .done, source: .timer, actual: 52, title: "Easy 5k run", commitment: 1),
            B(date: "2026-08-27", sh: 10, sm: 0, eh: 11, em: 30, status: .done, source: .timer, actual: 84, title: "Notebook setup", commitment: 0),
            B(date: "2026-08-27", sh: 18, sm: 0, eh: 18, em: 45, status: .partial, source: .reported, actual: 20, title: "Recovery jog", commitment: 1),
            // yesterday: a missed one and a reported one, told truthfully
            B(date: "2026-08-29", sh: 10, sm: 0, eh: 11, em: 0, status: .done, source: .timer, actual: 55, title: "Intervals workout", commitment: 1),
            B(date: "2026-08-29", sh: 13, sm: 0, eh: 14, em: 0, status: .partial, source: .reported, actual: 25, title: "Read chapter 4", commitment: 0),
            B(date: "2026-08-29", sh: 16, sm: 0, eh: 17, em: 0, status: .missed, source: nil, actual: nil, title: "Tempo run", commitment: 1),
            // today
            B(date: today, sh: 9, sm: 0, eh: 9, em: 45, status: .done, source: .timer, actual: 45, title: "Linear algebra review", commitment: 0),
            B(date: today, sh: 11, sm: 0, eh: 12, em: 0, status: .done, source: .timer, actual: 38, title: "Gradient descent notebook", commitment: 0),
            B(date: today, sh: 14, sm: 0, eh: 15, em: 30, status: .planned, source: nil, actual: nil, title: "Backprop from scratch", commitment: 0),
            B(date: today, sh: 16, sm: 0, eh: 16, em: 30, status: .planned, source: nil, actual: nil, title: "Easy 5k run", commitment: 1),
            // ahead
            B(date: "2026-08-31", sh: 9, sm: 0, eh: 10, em: 0, status: .planned, source: nil, actual: nil, title: "Week review", commitment: 0),
        ]
        let ledger: [L] = [
            L(date: "2026-08-25", windows: [(8, 0, 9, 0), (10, 30, 12, 0)], available: 150),
            L(date: "2026-08-26", windows: [(9, 0, 12, 0)], available: 180),
            L(date: "2026-08-27", windows: [(12, 0, 15, 0)], available: 180),
            L(date: "2026-08-28", windows: [(9, 0, 17, 0)], available: 480),
            L(date: "2026-08-29", windows: [(8, 0, 10, 0), (11, 0, 13, 0), (14, 0, 16, 0)], available: 360),
            L(date: today, windows: [(8, 0, 9, 0), (9, 45, 11, 0), (12, 0, 14, 0), (15, 30, 16, 0), (16, 30, 18, 30)], available: 405),
            L(date: "2026-08-31", windows: [(10, 0, 12, 0)], available: 120),
        ]
        return details(rows: rows, ledger: ledger, streak: 6)
    }

    /// A day that exists with open capacity but no blocks, so the Day view
    /// draws its designed empty state (with a real open figure) rather than the
    /// no-plan state.
    static func empty() -> WorkspaceDetails {
        let ledger: [L] = [
            L(date: today, windows: [(9, 0, 18, 0)], available: 540),
        ]
        return details(rows: [], ledger: ledger, streak: 0)
    }

    private static func details(rows: [B], ledger: [L], streak: Int) -> WorkspaceDetails {
        var tasks: [TaskPayload] = []
        var blocks: [BlockPayload] = []
        for (i, r) in rows.enumerated() {
            let taskID = "t_\(i)"
            let parts = r.date.split(separator: "-").compactMap { Int($0) }
            let y = parts[0], mo = parts[1], d = parts[2]
            tasks.append(TaskPayload(id: taskID, commitmentID: commitments[r.commitment].id, title: r.title))
            blocks.append(BlockPayload(
                id: "b_\(i)",
                taskID: taskID,
                startsAt: utc(y, mo, d, r.sh, r.sm),
                endsAt: utc(y, mo, d, r.eh, r.em),
                status: r.status,
                actualMinutes: r.actual,
                actualSource: r.source))
        }
        let ledgerDays: [LedgerDayPayload] = ledger.map { l in
            let parts = l.date.split(separator: "-").compactMap { Int($0) }
            let y = parts[0], mo = parts[1], d = parts[2]
            return LedgerDayPayload(
                date: l.date,
                available: l.available,
                freeWindows: l.windows.map {
                    FreeWindowPayload(start: utc(y, mo, d, $0.0, $0.1), end: utc(y, mo, d, $0.2, $0.3))
                })
        }
        return WorkspaceDetails(
            workspaceID: "ws_plan_rehearsal",
            today: today,
            now: utc(2026, 8, 30, 14, 30),
            timezone: nil,
            streak: streak,
            onboarded: true,
            blocks: blocks,
            tasks: tasks,
            commitments: commitments,
            ledgerDays: ledgerDays)
    }
}
#endif
