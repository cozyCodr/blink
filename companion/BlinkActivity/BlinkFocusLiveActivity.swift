import ActivityKit
import WidgetKit
import SwiftUI
import BlinkKit

// S3 · Focus session, on the lock screen and in the Dynamic Island.
//
// The same elapsed the in-app timer shows, from the same source of truth: the
// `FocusActivityAttributes.ContentState` the app pushes from the DEVICE. No
// push tokens (docs/COMPANION_ARCHITECTURE.md §4, Gap 4). While running, the
// clock and the ring are driven by `Text(timerInterval:)` and
// `ProgressView(timerInterval:)`, which the SYSTEM keeps current without the
// app pushing every second; paused, idle and ended states are static, so a
// backgrounded session never keeps ticking on its own.
//
// Colour and shape compose from the SAME token layer the app uses. P15-08:
// which face's tokens is decided by `context.attributes.face` — the app hands
// the chosen face over when it starts the Activity (the extension has no app
// group to read a preference from), and every view here resolves its theme
// from that. No view asks "which face": it asks the tokens.

struct BlinkFocusLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: FocusActivityAttributes.self) { context in
            let theme = Faces.tokens(for: context.attributes.face)
            LockScreenView(context: context, theme: theme)
                .activityBackgroundTint(theme.ground.opacity(0.92))
                .activitySystemActionForegroundColor(theme.accent)
        } dynamicIsland: { context in
            let state = context.state
            let theme = Faces.tokens(for: context.attributes.face)
            return DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    RingGlyph(state: state, theme: theme)
                        .frame(width: 34, height: 34)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    ElapsedText(state: state, font: .system(.title2, design: .rounded))
                        .foregroundStyle(state.phase == .running ? theme.ink : theme.muted)
                }
                DynamicIslandExpandedRegion(.center) {
                    Text(context.attributes.title ?? "Focus session")
                        .font(.caption)
                        .foregroundStyle(theme.muted)
                        .lineLimit(1)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    ExpandedBottom(context: context, theme: theme)
                }
            } compactLeading: {
                RingGlyph(state: state, theme: theme)
                    .frame(width: 20, height: 20)
            } compactTrailing: {
                ElapsedText(state: state, font: .system(.body, design: .rounded).monospacedDigit())
                    .foregroundStyle(state.phase == .running ? theme.ink : theme.muted)
                    .frame(maxWidth: 54)
            } minimal: {
                RingGlyph(state: state, theme: theme)
                    .frame(width: 20, height: 20)
            }
            .widgetURL(URL(string: "blink://focus"))
            .keylineTint(theme.accent)
        }
    }
}

// MARK: - Lock screen

private struct LockScreenView: View {
    let context: ActivityViewContext<FocusActivityAttributes>
    let theme: any FaceTokens

    var body: some View {
        let state = context.state
        HStack(spacing: 14) {
            RingGlyph(state: state, theme: theme)
                .frame(width: 48, height: 48)

            VStack(alignment: .leading, spacing: 3) {
                Text(context.attributes.title ?? "Focus session")
                    .font(.headline)
                    .foregroundStyle(theme.ink)
                    .lineLimit(1)
                StatusLine(state: state, theme: theme)
            }

            Spacer(minLength: 8)

            VStack(alignment: .trailing, spacing: 6) {
                ElapsedText(state: state, font: .system(.title, design: .rounded).monospacedDigit())
                    .foregroundStyle(state.phase == .running ? theme.ink : theme.muted)
                DoneButton(theme: theme)
            }
        }
        .padding(16)
    }
}

// MARK: - Shared pieces

/// The elapsed readout. Live while running (system-driven), static otherwise so
/// a paused or idle session does not keep counting on the lock screen.
private struct ElapsedText: View {
    let state: FocusActivityAttributes.ContentState
    let font: Font

    var body: some View {
        Group {
            if state.phase == .running {
                Text(timerInterval: state.elapsedAnchor...state.elapsedAnchor.addingTimeInterval(60 * 60 * 24),
                     countsDown: false)
                    .monospacedDigit()
            } else {
                Text(staticClock(state.frozenSeconds))
                    .monospacedDigit()
            }
        }
        .font(font)
    }
}

/// The progress ring, filling toward the planned span. Live while running
/// (system-driven), static otherwise.
private struct RingGlyph: View {
    let state: FocusActivityAttributes.ContentState
    let theme: any FaceTokens

    var body: some View {
        ZStack {
            Circle().stroke(theme.line, lineWidth: 4)
            ringFill
        }
    }

    @ViewBuilder
    private var ringFill: some View {
        if state.phase == .running {
            // System-driven fill across the planned window.
            ProgressView(timerInterval: state.elapsedAnchor...state.plannedEnd,
                         countsDown: false) { EmptyView() } currentValueLabel: { EmptyView() }
                .progressViewStyle(RingStyle(color: theme.accent))
        } else {
            Circle()
                .trim(from: 0, to: staticProgress)
                .stroke(theme.muted, style: StrokeStyle(lineWidth: 4, lineCap: .round))
                .rotationEffect(.degrees(-90))
        }
    }

    private var staticProgress: Double {
        guard state.plannedMinutes > 0 else { return state.frozenSeconds > 0 ? 1 : 0 }
        return min(1, state.frozenSeconds / (Double(state.plannedMinutes) * 60))
    }
}

/// A circular progress style so the timer-driven ProgressView reads as a ring.
private struct RingStyle: ProgressViewStyle {
    let color: Color
    func makeBody(configuration: Configuration) -> some View {
        Circle()
            .trim(from: 0, to: configuration.fractionCompleted ?? 0)
            .stroke(color, style: StrokeStyle(lineWidth: 4, lineCap: .round))
            .rotationEffect(.degrees(-90))
    }
}

private struct StatusLine: View {
    let state: FocusActivityAttributes.ContentState
    let theme: any FaceTokens
    var body: some View {
        Text(text)
            .font(.caption)
            .foregroundStyle(color)
            .lineLimit(1)
    }
    private var text: String {
        switch state.phase {
        case .running: return "\(state.plannedMinutes) min planned"
        case .paused: return "Paused. Nothing is counting."
        case .idle: return "Still open. Tap to check in."
        case .ended: return state.savedMinutes.map { "\($0) min saved" } ?? "Saved"
        }
    }
    private var color: Color {
        switch state.phase {
        case .paused, .ended: return theme.faint
        case .idle: return theme.warm
        case .running: return theme.muted
        }
    }
}

private struct ExpandedBottom: View {
    let context: ActivityViewContext<FocusActivityAttributes>
    let theme: any FaceTokens
    var body: some View {
        HStack {
            StatusLine(state: context.state, theme: theme)
            Spacer()
            DoneButton(theme: theme)
        }
    }
}

/// The Done button. Its intent runs in the APP's process (a `LiveActivityIntent`),
/// so the write still goes through the app's single `log-time` path; the widget
/// records nothing itself.
private struct DoneButton: View {
    let theme: any FaceTokens
    var body: some View {
        if #available(iOS 17.0, *) {
            Button(intent: EndFocusIntent()) {
                Text("Wrap it up")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(theme.ground)
                    .padding(.vertical, 6)
                    .padding(.horizontal, 12)
                    .background(Capsule().fill(theme.accent))
            }
            .buttonStyle(.plain)
        }
    }
}

// MARK: - Formatting

/// H:MM:SS or MM:SS from frozen seconds. A clock, never rounded.
private func staticClock(_ seconds: Double) -> String {
    let total = Int(seconds)
    let h = total / 3600, m = (total % 3600) / 60, s = total % 60
    return h > 0 ? String(format: "%d:%02d:%02d", h, m, s) : String(format: "%d:%02d", m, s)
}
