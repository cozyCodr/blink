import SwiftUI
import BlinkKit

// P18-01 · The native plan (Day + Week), in-app, no web detour.
//
// The plan used to live behind a "See your week" button that opened the web.
// That was the one place the phone still sent you away to READ something it had
// already fetched. This surface answers it natively at two distances, the same
// two the web's horizon keeps close (day + week; month and quarter and year
// stay on the web for now, per the product decision).
//
// It draws only what the payload proves. The three honesty marks are the web's
// (src/web/css/horizon.css, P11-04): a placed session is an OUTLINE that is the
// plan, with what actually happened drawn INSIDE it as a fill — solid where the
// timer measured it, hatched where you told me about it, nothing at all where
// nothing is recorded, because an empty fill would claim a zero that is really
// a "not measured". Free time is open water. A now-line rides the shared axis.
//
// TOKENS ONLY. Every colour, font, corner and motion composes from
// `FaceTokens` / `FaceMotion`, so all three faces wear it without a single
// `if face ==`. Nothing here is hardcoded (README, "the rule that keeps three
// faces from becoming three apps").

enum PlanLevel: String, CaseIterable, Identifiable {
    case day, week
    var id: String { rawValue }
    var label: String { self == .day ? "Day" : "Week" }
}

struct PlanScreen: View {
    @Environment(\.face) private var face
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.dismiss) private var dismiss

    /// The plan, or nil when the app cannot reach it. Nil draws an honest
    /// no-numbers state, never a phantom grid.
    let plan: PlanModel?

    @State private var level: PlanLevel = .day
    /// The day the Day view is looking at. Defaults to today when the plan
    /// covers it, else the first day the plan does cover.
    @State private var anchorDate: String = ""

    var body: some View {
        ZStack {
            face.ground.ignoresSafeArea()
            VStack(spacing: face.layout.rowGap) {
                header
                switcher
                Group {
                    switch level {
                    case .day: dayLevel
                    case .week: weekLevel
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .animation(reduceMotion ? nil : face.motion.swapAnimation, value: level)
                legend
            }
            .padding(.horizontal, face.layout.screenMargin)
            .padding(.top, face.layout.cardPaddingTop)
            .padding(.bottom, face.layout.cardPaddingBottom)
        }
        .onAppear {
            if anchorDate.isEmpty {
                anchorDate = plan?.today?.date ?? plan?.days.first?.date ?? ""
            }
        }
    }

    // MARK: Header

    private var header: some View {
        HStack(alignment: .firstTextBaseline) {
            Text("Your plan")
                .font(face.displayFont)
                .foregroundStyle(face.ink)
            Spacer()
            if let streak = plan?.streakDays, streak > 0 {
                Text("Day \(streak)")
                    .font(face.labelFont)
                    .foregroundStyle(face.accent)
                    .padding(.vertical, face.layout.pillPaddingV)
                    .padding(.horizontal, face.layout.pillPaddingH)
                    .overlay(Capsule().stroke(face.line, lineWidth: 1))
                    .accessibilityLabel("Day \(streak) of your streak")
            }
            Button { dismiss() } label: {
                Text("Done")
                    .font(face.bodyFont)
                    .foregroundStyle(face.accent)
                    .frame(minHeight: face.layout.minTapTarget)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Close the plan")
        }
    }

    // MARK: The Day/Week switch

    private var switcher: some View {
        HStack(spacing: 0) {
            ForEach(PlanLevel.allCases) { option in
                Button {
                    withAnimation(reduceMotion ? nil : face.motion.swapAnimation) { level = option }
                } label: {
                    Text(option.label)
                        .font(face.bodyFont)
                        .foregroundStyle(level == option ? face.ground : face.muted)
                        .frame(maxWidth: .infinity, minHeight: face.layout.minTapTarget - 8)
                        .background(
                            RoundedRectangle(cornerRadius: face.cornerStyle.nominalRadius, style: .continuous)
                                .fill(level == option ? face.accent : Color.clear)
                        )
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Show the \(option.label.lowercased())")
                .accessibilityAddTraits(level == option ? .isSelected : [])
            }
        }
        .padding(4)
        .background(
            RoundedRectangle(cornerRadius: face.cornerStyle.nominalRadius, style: .continuous)
                .fill(face.control)
        )
    }

    // MARK: Day

    @ViewBuilder
    private var dayLevel: some View {
        if let plan, let day = plan.days.first(where: { $0.date == anchorDate }) ?? plan.days.first {
            VStack(spacing: face.layout.rowGap) {
                dayNav(plan: plan, day: day)
                PlanDayTimeline(day: day, clock: plan.clock)
            }
        } else {
            unreachable
        }
    }

    private func dayNav(plan: PlanModel, day: PlanDay) -> some View {
        let index = plan.days.firstIndex(where: { $0.date == day.date }) ?? 0
        return HStack(spacing: face.layout.tightGap) {
            navButton(system: "chevron.left", enabled: index > 0) {
                anchorDate = plan.days[index - 1].date
            }
            .accessibilityLabel("Previous day")
            VStack(spacing: 2) {
                Text(day.isToday ? "TODAY" : day.weekdayShort)
                    .font(face.labelFont)
                    .tracking(2)
                    .foregroundStyle(day.isToday ? face.accent : face.muted)
                Text(dayTitle(day, clock: plan.clock))
                    .font(face.cardTitleFont)
                    .foregroundStyle(face.ink)
            }
            .frame(maxWidth: .infinity)
            navButton(system: "chevron.right", enabled: index < plan.days.count - 1) {
                anchorDate = plan.days[index + 1].date
            }
            .accessibilityLabel("Next day")
        }
    }

    private func navButton(system: String, enabled: Bool, _ act: @escaping () -> Void) -> some View {
        Button(action: act) {
            Image(systemName: system)
                .font(face.bodyFont)
                .foregroundStyle(enabled ? face.muted : face.faint.opacity(0.4))
                .frame(width: face.layout.minTapTarget, height: face.layout.minTapTarget)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
    }

    // MARK: Week

    @ViewBuilder
    private var weekLevel: some View {
        if let plan, !plan.days.isEmpty {
            PlanWeekView(plan: plan)
        } else {
            unreachable
        }
    }

    // MARK: The legend, in words

    /// Only the marks the levels can actually draw, named out loud. The web's
    /// rule: the legend can never promise a mark that is not on the canvas.
    private var legend: some View {
        // One word each so the row stays even (the honest "told me about" now
        // reads "Reported": the timer MEASURED it, or you REPORTED it). Evenly
        // distributed and never wrapping, so the key sits calm at the foot of
        // the plan (user, 2026-08-30).
        HStack(spacing: 0) {
            legendMark(PlanFillSwatch(kind: .measured, face: face), "Measured")
            Spacer(minLength: face.layout.rowGap)
            legendMark(PlanFillSwatch(kind: .reported, face: face), "Reported")
            Spacer(minLength: face.layout.rowGap)
            legendMark(PlanFillSwatch(kind: .planned, face: face), "Planned")
            Spacer(minLength: face.layout.rowGap)
            legendMark(
                RoundedRectangle(cornerRadius: 3, style: .continuous)
                    .fill(face.accent.opacity(0.16))
                    .frame(width: 20, height: 12), "Open")
        }
        .font(face.metaFont)
        .foregroundStyle(face.faint)
        .frame(maxWidth: .infinity)
        .padding(.top, face.layout.rowGap)
    }

    private func legendMark<Swatch: View>(_ swatch: Swatch, _ label: String) -> some View {
        HStack(spacing: 5) {
            swatch
            Text(label)
                .lineLimit(1)
                .fixedSize(horizontal: true, vertical: false)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(label)
    }

    // MARK: The honest no-plan state

    private var unreachable: some View {
        VStack(spacing: face.layout.rowGap) {
            Spacer()
            Text("I cannot reach your plan right now.")
                .font(face.cardTitleFont)
                .foregroundStyle(face.ink)
            Text("Pull down on Today when you are back on, and it lands here.")
                .font(face.secondaryFont)
                .foregroundStyle(face.muted)
            Spacer()
        }
        .multilineTextAlignment(.center)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    // MARK: Copy

    private func dayTitle(_ day: PlanDay, clock: ServerClock) -> String {
        "\(day.weekdayShort.capitalized) \(day.dayNumber)"
    }
}

// MARK: - Day timeline

/// Today's plan as a vertical hour timeline: open water behind, placed sessions
/// as chips, the now-line across, hour labels down the gutter. Bounds come from
/// the day's own window (`PlanDay.window`), never a hardcoded range.
struct PlanDayTimeline: View {
    @Environment(\.face) private var face
    let day: PlanDay
    let clock: ServerClock

    private let hourHeight: CGFloat = 58
    private let gutter: CGFloat = 54
    private let trailingInset: CGFloat = 2

    private var totalHeight: CGFloat {
        CGFloat(day.window.span) / 60 * hourHeight
    }

    var body: some View {
        if day.blocks.isEmpty {
            emptyDay
        } else {
            ScrollViewReader { proxy in
                ScrollView {
                    timeline
                        .padding(.vertical, face.layout.rowGap)
                }
                .onAppear {
                    // Land on the current hour when it's today, else the first
                    // session, so the day opens where the eye wants to be.
                    let anchor = day.nowFraction != nil ? "now" : (day.blocks.first?.id ?? "top")
                    DispatchQueue.main.async {
                        withAnimation(.none) { proxy.scrollTo(anchor, anchor: .center) }
                    }
                }
            }
        }
    }

    private var timeline: some View {
        GeometryReader { geo in
            let width = geo.size.width
            let laneX = gutter
            let laneW = max(1, width - gutter - trailingInset)

            ZStack(alignment: .topLeading) {
                // hour gridlines + labels
                ForEach(day.window.wholeHours, id: \.self) { hour in
                    let y = yFor(hour * 60)
                    Rectangle()
                        .fill(face.line)
                        .frame(width: laneW, height: 1)
                        .position(x: laneX + laneW / 2, y: y)
                    Text(hourLabel(hour))
                        .font(face.metaFont)
                        .foregroundStyle(face.faint)
                        .frame(width: gutter - 8, alignment: .trailing)
                        .position(x: (gutter - 8) / 2, y: y)
                        .accessibilityHidden(true)
                }

                // open water: the free windows the ledger published
                ForEach(Array(day.freeBands.enumerated()), id: \.offset) { _, band in
                    let top = yFor(band.startMinute)
                    let h = max(2, yFor(band.endMinute) - top)
                    RoundedRectangle(cornerRadius: 6, style: .continuous)
                        .fill(face.accent.opacity(0.10))
                        .overlay(
                            RoundedRectangle(cornerRadius: 6, style: .continuous)
                                .stroke(face.accent.opacity(0.24), lineWidth: 1))
                        .frame(width: laneW, height: h)
                        .position(x: laneX + laneW / 2, y: top + h / 2)
                        .accessibilityHidden(true)
                }

                // placed sessions. Chips carry a 44pt legibility floor, so two
                // short back-to-back sessions inflated to that floor can cover
                // the same pixels even though their times never overlap (the
                // 07:00 pile-up, user screenshot 2026-09-01). `chipFrames`
                // resolves the collision: each chip starts at its honest time
                // position unless the previous chip's inflated bottom is lower,
                // in which case it slides just below. Times on the chip stay
                // the truth; only the pixels yield.
                ForEach(chipFrames(), id: \.block.id) { placed in
                    PlanBlockChip(block: placed.block, clock: clock)
                        .frame(width: laneW, height: placed.height)
                        .position(x: laneX + laneW / 2, y: placed.top + placed.height / 2)
                        .id(placed.block.id)
                }

                // the now-line, only when now is genuinely inside this day
                if let frac = day.nowFraction {
                    let y = frac * totalHeight
                    NowLine(face: face)
                        .frame(width: laneW + 10, height: 12)
                        .position(x: laneX + laneW / 2, y: y)
                        .id("now")
                }
            }
            .frame(width: width, height: totalHeight)
        }
        .frame(height: totalHeight)
        .id("top")
    }

    private var emptyDay: some View {
        VStack(spacing: face.layout.rowGap) {
            Spacer()
            Text(emptyHeadline)
                .font(face.cardTitleFont)
                .foregroundStyle(face.ink)
                .multilineTextAlignment(.center)
            Text(emptySub)
                .font(face.secondaryFont)
                .foregroundStyle(face.muted)
                .multilineTextAlignment(.center)
            Spacer()
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    /// The empty state never quotes a figure it does not have: the open-hours
    /// line only appears when the ledger actually published one.
    private var emptyHeadline: String {
        if let open = day.openMinutes, open > 0 {
            return "Nothing scheduled. \(DurationText.spoken(open)) open, and that's the point."
        }
        return "Nothing scheduled here yet."
    }

    private var emptySub: String {
        day.isToday
            ? "Plan something on Today and it lands right here."
            : "This day is still open. Plan into it whenever you like."
    }

    private func yFor(_ minute: Int) -> CGFloat {
        CGFloat(minute - day.window.startMinute) / CGFloat(day.window.span) * totalHeight
    }

    /// The chips' resolved frames: honest time position, 44pt legibility floor,
    /// and no two chips ever covering the same pixels. Sorted by start; a chip
    /// whose natural top sits above the previous chip's inflated bottom is
    /// nudged down below it (4pt breath), so short adjacent sessions stack
    /// instead of piling onto each other.
    private func chipFrames() -> [(block: PlanBlock, top: CGFloat, height: CGFloat)] {
        var frames: [(block: PlanBlock, top: CGFloat, height: CGFloat)] = []
        var lastBottom: CGFloat = -.greatestFiniteMagnitude
        for block in day.blocks.sorted(by: { $0.startMinute < $1.startMinute }) {
            let natural = yFor(block.startMinute)
            let height = max(44, yFor(block.endMinute) - natural)
            let top = max(natural, lastBottom + 4)
            frames.append((block, top, height))
            lastBottom = top + height
        }
        return frames
    }

    private func hourLabel(_ hour: Int) -> String {
        var comps = DateComponents()
        comps.hour = hour % 24
        let cal = Calendar(identifier: .gregorian)
        let date = cal.date(from: comps) ?? Date()
        let f = DateFormatter()
        f.locale = .autoupdatingCurrent
        f.setLocalizedDateFormatFromTemplate("j")
        return f.string(from: date)
    }
}

// MARK: - One placed session, on the day timeline

struct PlanBlockChip: View {
    @Environment(\.face) private var face
    let block: PlanBlock
    let clock: ServerClock

    var body: some View {
        let corner = face.cornerStyle.nominalRadius
        ZStack(alignment: .topLeading) {
            // the planned body: a faint wash inside the outline
            RoundedRectangle(cornerRadius: corner, style: .continuous)
                .fill(bodyWash)

            // the recorded fill, a bottom-up gauge of the recorded share. A
            // block with no actual has a zero share and draws no fill.
            if block.recordedFraction > 0 {
                GeometryReader { g in
                    fillPaint
                        .frame(height: g.size.height * block.recordedFraction)
                        .frame(maxHeight: .infinity, alignment: .bottom)
                }
            }

            // content
            VStack(alignment: .leading, spacing: 2) {
                Text(block.title ?? "Focus block")
                    .font(face.secondaryFont)
                    .foregroundStyle(face.ink)
                    .lineLimit(1)
                Text(timeRange)
                    .font(face.metaFont)
                    .foregroundStyle(face.muted)
                    .lineLimit(1)
                if let tag = statusTag {
                    Text(tag.text)
                        .font(face.metaFont)
                        .foregroundStyle(tag.color)
                }
            }
            .padding(.vertical, 8)
            .padding(.horizontal, 12)
        }
        .clipShape(RoundedRectangle(cornerRadius: corner, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: corner, style: .continuous)
                .stroke(borderColor, lineWidth: 1))
        .overlay(
            Rectangle().fill(edgeColor).frame(width: 3)
                .clipShape(RoundedRectangle(cornerRadius: corner, style: .continuous)),
            alignment: .leading)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(accessibilityText)
    }

    private var timeRange: String {
        "\(clock.clockTime(block.startsAt)) to \(clock.clockTime(block.endsAt))"
    }

    private var bodyWash: Color {
        block.status == .missed ? face.alert.opacity(0.06) : face.accent.opacity(0.12)
    }

    private var borderColor: Color {
        block.status == .missed ? face.alert.opacity(0.45) : face.accent
    }

    private var edgeColor: Color {
        block.status == .missed ? face.alert.opacity(0.5) : face.accentBright
    }

    @ViewBuilder
    private var fillPaint: some View {
        switch block.fill {
        case .measured:
            face.accent.opacity(0.46)
        case .reported:
            HatchFill(color: face.accent.opacity(0.5))
        case .none:
            Color.clear
        }
    }

    private var statusTag: (text: String, color: Color)? {
        switch block.status {
        case .missed: return ("missed", face.alert.opacity(0.85))
        case .partial: return ("partly done", face.warm)
        default: return nil
        }
    }

    private var accessibilityText: String {
        let name = block.title ?? "Focus block"
        var parts = ["\(name), \(timeRange)"]
        switch block.fill {
        case .measured(let m): parts.append("\(DurationText.spoken(m)) measured")
        case .reported(let m): parts.append("\(DurationText.spoken(m)) you told me about")
        case .none:
            if block.status == .missed { parts.append("missed") }
            else if block.status == .planned { parts.append("planned, nothing recorded yet") }
        }
        return parts.joined(separator: ", ")
    }
}

// MARK: - Week

/// Seven day cards on one shared axis, so free time reads as a gap straight
/// down the column. Today's row is the lit one; the streak sits in the header.
struct PlanWeekView: View {
    @Environment(\.face) private var face
    let plan: PlanModel

    var body: some View {
        ScrollView {
            VStack(spacing: face.layout.rowGap) {
                ForEach(plan.days) { day in
                    PlanWeekRow(day: day, window: plan.weekWindow, clock: plan.clock)
                }
            }
            .padding(.vertical, face.layout.rowGap)
        }
    }
}

struct PlanWeekRow: View {
    @Environment(\.face) private var face
    let day: PlanDay
    let window: DayWindow
    let clock: ServerClock

    private let runHeight: CGFloat = 40

    var body: some View {
        HStack(alignment: .center, spacing: face.layout.tightGap) {
            VStack(alignment: .leading, spacing: 2) {
                Text(day.isToday ? "TODAY" : day.weekdayShort)
                    .font(face.labelFont)
                    .foregroundStyle(day.isToday ? face.accent : face.muted)
                Text("\(day.dayNumber)")
                    .font(face.metaFont)
                    .foregroundStyle(face.faint)
            }
            .frame(width: 54, alignment: .leading)

            VStack(alignment: .leading, spacing: 4) {
                run
                Text(capacityLine)
                    .font(face.metaFont)
                    .foregroundStyle(face.faint)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityText)
    }

    private var run: some View {
        GeometryReader { geo in
            let width = geo.size.width
            ZStack(alignment: .topLeading) {
                RoundedRectangle(cornerRadius: 9, style: .continuous)
                    .fill(face.control)
                    .overlay(
                        RoundedRectangle(cornerRadius: 9, style: .continuous)
                            .stroke(day.isToday ? face.accent.opacity(0.55) : face.line, lineWidth: 1))

                // open water
                ForEach(Array(day.freeBands.enumerated()), id: \.offset) { _, band in
                    let x = xFor(band.startMinute, width)
                    let w = max(2, xFor(band.endMinute, width) - x)
                    Rectangle()
                        .fill(face.accent.opacity(0.14))
                        .frame(width: w, height: runHeight - 8)
                        .position(x: x + w / 2, y: runHeight / 2)
                }

                // placed sessions, thin
                ForEach(day.blocks) { block in
                    let x = xFor(block.startMinute, width)
                    let w = max(3, xFor(block.endMinute, width) - x)
                    WeekSpan(block: block)
                        .frame(width: w, height: runHeight - 10)
                        .position(x: x + w / 2, y: runHeight / 2)
                }

                // now-column, only on today and only when now is inside
                if day.isToday, window.contains(minute: clock.localMinuteOfDayNow) {
                    let x = xFor(clock.localMinuteOfDayNow, width)
                    NowColumn(face: face)
                        .frame(width: 10, height: runHeight + 4)
                        .position(x: x, y: runHeight / 2)
                }
            }
            .frame(width: width, height: runHeight)
        }
        .frame(height: runHeight)
    }

    private func xFor(_ minute: Int, _ width: CGFloat) -> CGFloat {
        CGFloat(window.fraction(ofMinute: minute)) * width
    }

    /// Planned and open, both honest: the open figure only shows when the
    /// ledger published one. A day with neither reads as open, not as empty.
    private var capacityLine: String {
        var parts: [String] = []
        if day.plannedMinutes > 0 { parts.append("\(DurationText.spoken(day.plannedMinutes)) planned") }
        if day.measuredMinutes > 0 { parts.append("\(DurationText.spoken(day.measuredMinutes)) tracked") }
        if let open = day.openMinutes, open > 0 { parts.append("\(DurationText.spoken(open)) open") }
        return parts.isEmpty ? "Open" : parts.joined(separator: " · ")
    }

    private var accessibilityText: String {
        let name = day.isToday ? "Today" : "\(day.weekdayShort.capitalized) the \(day.dayNumber)"
        let count = day.blocks.count
        let sessions = count == 0 ? "nothing scheduled"
            : count == 1 ? "one session" : "\(count) sessions"
        return "\(name), \(sessions). \(capacityLine)."
    }
}

/// One session on a week run: a slim outline with the recorded share filled
/// from the left, the same convention the day chip and the web spine keep.
struct WeekSpan: View {
    @Environment(\.face) private var face
    let block: PlanBlock

    var body: some View {
        ZStack(alignment: .leading) {
            RoundedRectangle(cornerRadius: 4, style: .continuous)
                .fill(block.status == .missed ? face.alert.opacity(0.10) : face.accent.opacity(0.16))
            if block.recordedFraction > 0 {
                GeometryReader { g in
                    fill.frame(width: g.size.width * block.recordedFraction)
                }
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: 4, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 4, style: .continuous)
                .stroke(block.status == .missed ? face.alert.opacity(0.5) : face.accent, lineWidth: 1))
    }

    @ViewBuilder
    private var fill: some View {
        switch block.fill {
        case .measured: face.accent.opacity(0.5)
        case .reported: HatchFill(color: face.accent.opacity(0.5))
        case .none: Color.clear
        }
    }
}

// MARK: - Shared marks

/// The diagonal hatch that means "you told me about it", never measured.
struct HatchFill: View {
    let color: Color
    var body: some View {
        Canvas { ctx, size in
            let step: CGFloat = 6
            var x: CGFloat = -size.height
            while x < size.width {
                var p = Path()
                p.move(to: CGPoint(x: x, y: size.height))
                p.addLine(to: CGPoint(x: x + size.height, y: 0))
                ctx.stroke(p, with: .color(color), lineWidth: 1.6)
                x += step
            }
        }
    }
}

/// The horizontal now-marker on the day timeline: a line with a leading dot.
struct NowLine: View {
    let face: any FaceTokens
    var body: some View {
        ZStack(alignment: .leading) {
            Rectangle()
                .fill(face.accent)
                .frame(height: 2)
                .shadow(color: face.glow.opacity(0.8), radius: 4)
            Circle()
                .fill(face.accent)
                .frame(width: 8, height: 8)
                .shadow(color: face.glow.opacity(0.8), radius: 4)
        }
    }
}

/// The vertical now-marker on a week run.
struct NowColumn: View {
    let face: any FaceTokens
    var body: some View {
        ZStack(alignment: .top) {
            Rectangle()
                .fill(face.accent)
                .frame(width: 2)
                .shadow(color: face.glow.opacity(0.7), radius: 3)
            Circle()
                .fill(face.accent)
                .frame(width: 7, height: 7)
                .shadow(color: face.glow.opacity(0.7), radius: 3)
        }
    }
}

/// A legend swatch built from the very paints the canvas uses, so the key and
/// the spine can never say different things (the web's rule, P11-04).
struct PlanFillSwatch: View {
    enum Kind { case measured, reported, planned }
    let kind: Kind
    let face: any FaceTokens

    var body: some View {
        RoundedRectangle(cornerRadius: 3, style: .continuous)
            .fill(background)
            .frame(width: 20, height: 12)
            .overlay(
                RoundedRectangle(cornerRadius: 3, style: .continuous)
                    .stroke(face.accent, lineWidth: 1))
            .overlay(hatch)
    }

    private var background: Color {
        switch kind {
        case .measured: return face.accent.opacity(0.46)
        case .reported: return face.accent.opacity(0.12)
        case .planned: return face.accent.opacity(0.12)
        }
    }

    @ViewBuilder
    private var hatch: some View {
        if kind == .reported {
            HatchFill(color: face.accent.opacity(0.5))
                .clipShape(RoundedRectangle(cornerRadius: 3, style: .continuous))
        }
    }
}
