import SwiftUI
import BlinkKit

// P20-03 — the session and move cards under a planned reply.
//
// The design contract is the approved "Blink Screen System" page: a horizontal
// card, date tile on the left (accent strip on top with the weekday or TODAY,
// the big day number, the small clock time under it), the body on the right.
// A session's body carries the title, the "3:00 to 4:30 · 90 min" meta line,
// the why in italic when the server sent one, and the ON YOUR CALENDAR chip
// only when the server said `calendar: true`. A move's body shows the old
// time struck through and faint, an arrow, and the new time in a bright
// accent chip; CALENDAR MOVED only on "moved"; a warm retrying note on
// "partial"/"failed"; nothing at all on "none".
//
// HONESTY: every string on these cards is the server's own payload or a
// formatting of its own datetimes through the ONE clock (`ServerClock`, the
// user's stored zone, never the device's). No card exists without its payload.
// Composed only from FaceTokens/FaceMotion; no literal colour, font, corner
// or duration anywhere below.

// MARK: The session card

struct SessionCardView: View {
    @Environment(\.face) private var face
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    let session: TurnSessionArtifact
    let clock: ServerClock
    /// Place in the dealt stack, for the web's stagger (clarify.css:229-238).
    let index: Int

    @State private var dealt = false

    private var minutes: Int {
        max(0, Int(session.endsAt.timeIntervalSince(session.startsAt) / 60))
    }

    private var metaLine: String {
        "\(clock.clockTime(session.startsAt)) to \(clock.clockTime(session.endsAt)) · \(minutes) min"
    }

    var body: some View {
        HStack(alignment: .top, spacing: face.layout.rowGap) {
            DateTile(instant: session.startsAt, clock: clock)
            VStack(alignment: .leading, spacing: face.layout.tightGap) {
                Text(session.title)
                    .font(face.cardTitleFont)
                    .foregroundStyle(face.ink)
                Text(metaLine)
                    .font(face.metaFont)
                    .foregroundStyle(face.muted)
                if let why = session.why, !why.isEmpty {
                    // The server's reason, verbatim, spoken quietly in the
                    // face's own display voice.
                    Text(why)
                        .font(face.metaFont.italic())
                        .foregroundStyle(face.faint)
                }
                if session.calendar {
                    ArtifactChip(label: "ON YOUR CALENDAR")
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .modifier(ArtifactCardChrome(index: index, dealt: $dealt))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(accessibilityText)
    }

    /// The same truth the eye reads, in one spoken line.
    private var accessibilityText: String {
        var parts = [
            session.title,
            "\(clock.clockTime(session.startsAt)) to \(clock.clockTime(session.endsAt))",
        ]
        if let why = session.why, !why.isEmpty { parts.append(why) }
        if session.calendar { parts.append("on your calendar") }
        return parts.joined(separator: ", ")
    }
}

// MARK: The move card

struct MoveCardView: View {
    @Environment(\.face) private var face
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    let move: TurnMoveArtifact
    let clock: ServerClock
    let index: Int

    @State private var dealt = false

    var body: some View {
        HStack(alignment: .top, spacing: face.layout.rowGap) {
            DateTile(instant: move.newStart, clock: clock)
            VStack(alignment: .leading, spacing: face.layout.tightGap) {
                Text(move.title)
                    .font(face.cardTitleFont)
                    .foregroundStyle(face.ink)
                HStack(spacing: face.layout.tightGap) {
                    Text(clock.clockTime(move.oldStart))
                        .font(face.metaFont)
                        .strikethrough(true, color: face.alert)
                        .foregroundStyle(face.faint)
                    Image(systemName: "arrow.right")
                        .font(face.metaFont)
                        .foregroundStyle(face.muted)
                    Text(clock.clockTime(move.newStart))
                        .font(face.metaFont)
                        .foregroundStyle(face.ground)
                        .padding(.vertical, face.layout.pillPaddingV / 2)
                        .padding(.horizontal, face.layout.pillPaddingH)
                        .background(Capsule().fill(face.accentBright))
                }
                switch move.calendar {
                case "moved":
                    ArtifactChip(label: "CALENDAR MOVED")
                case "partial", "failed":
                    // Warm, not alarmed: the plan moved, the calendar copy is
                    // still catching up. The server keeps retrying.
                    Text("Calendar still catching up. I will keep retrying.")
                        .font(face.metaFont)
                        .foregroundStyle(face.warm)
                default:
                    // "none" (or anything future): the calendar was never in
                    // play, so the card says nothing about it.
                    EmptyView()
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .modifier(ArtifactCardChrome(index: index, dealt: $dealt))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(accessibilityText)
    }

    private var accessibilityText: String {
        var parts = [
            move.title,
            "moved from \(clock.clockTime(move.oldStart)) to \(clock.clockTime(move.newStart))",
        ]
        switch move.calendar {
        case "moved": parts.append("calendar moved")
        case "partial", "failed": parts.append("calendar still catching up")
        default: break
        }
        return parts.joined(separator: ", ")
    }
}

// MARK: Shared pieces

/// The date tile on a card's left edge: the accent strip naming the day
/// (TODAY when it is, in the USER'S zone, never the device's), the big day
/// number, the small clock time under it.
private struct DateTile: View {
    @Environment(\.face) private var face

    let instant: Date
    let clock: ServerClock

    private var dayLabel: (strip: String, number: Int) {
        let day = clock.localDay(of: instant)
        let parts = clock.calendarDay(from: day)
        return (clock.isToday(instant) ? "TODAY" : parts.weekdayShort, parts.dayNumber)
    }

    var body: some View {
        let label = dayLabel
        VStack(spacing: 0) {
            Text(label.strip)
                .font(face.labelFont)
                .foregroundStyle(face.ground)
                .lineLimit(1)
                .frame(maxWidth: .infinity)
                .padding(.vertical, face.layout.pillPaddingV / 2)
                .background(face.accent)
            VStack(spacing: 0) {
                Text("\(label.number)")
                    .font(face.numberFont)
                    .minimumScaleFactor(0.5)
                    .foregroundStyle(face.ink)
                Text(clock.clockTime(instant))
                    .font(face.metaFont)
                    .foregroundStyle(face.muted)
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)
            }
            .padding(.vertical, face.layout.tightGap)
            .padding(.horizontal, face.layout.tightGap / 2)
        }
        .frame(width: face.layout.minTapTarget * 1.6)
        .background(face.control)
        .clipShape(RoundedRectangle(
            cornerRadius: face.cornerStyle.nominalRadius, style: .continuous))
    }
}

/// The small all-caps capsule that states a calendar fact the server grounded.
private struct ArtifactChip: View {
    @Environment(\.face) private var face

    let label: String

    var body: some View {
        Text(label)
            .font(face.labelFont)
            .foregroundStyle(face.muted)
            .padding(.vertical, face.layout.pillPaddingV / 2)
            .padding(.horizontal, face.layout.pillPaddingH)
            .background(Capsule().fill(face.control))
            .overlay(Capsule().stroke(face.line, lineWidth: 1))
    }
}

/// The card's surface, padding, corner and deal-in, shared by both card kinds
/// so they are the same shape by construction. The deal is the web's stagger
/// (`face.motion.dealAnimation(index:)`); Reduced Motion arrives instantly
/// (`dealt` still flips, the animation is nil — the same pattern
/// QuestionSurface uses).
private struct ArtifactCardChrome: ViewModifier {
    @Environment(\.face) private var face
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    let index: Int
    @Binding var dealt: Bool

    func body(content: Content) -> some View {
        content
            .padding(.top, face.layout.cardPaddingTop)
            .padding(.horizontal, face.layout.cardPaddingSide)
            .padding(.bottom, face.layout.cardPaddingBottom)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: face.cornerStyle.nominalRadius, style: .continuous)
                    .fill(face.surface)
            )
            .opacity(dealt ? 1 : 0)
            .offset(y: dealt ? 0 : face.motion.revealRise)
            .animation(reduceMotion ? nil : face.motion.dealAnimation(index: index), value: dealt)
            .onAppear { dealt = true }
    }
}
