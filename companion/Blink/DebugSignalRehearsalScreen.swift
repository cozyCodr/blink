#if DEBUG
import SwiftUI
import BlinkKit

// DEBUG SCAFFOLDING — not product UI.
//
// S2's four kinds each fire on a real moment: ten minutes before a session,
// a morning before ten, an evening after five, and a day the server happens to
// have mined a pattern. Three of those cannot coexist in one workspace at one
// instant, so seeing all four in one sitting means asking for them.
//
// WHAT THIS CHANGES: when the notification is delivered, and which hour window
// applies. WHAT IT DOES NOT CHANGE: the words, the buttons, the `userInfo`, or
// the requirement that the payload actually supports the kind. Every button
// below goes through the SHIPPING `LocalNotificationScheduler` against the
// real API. A kind the data does not support says so, in place, rather than
// showing a specimen. Nothing here composes a string.
struct DebugSignalRehearsalScreen: View {
    @Environment(\.face) private var face

    let session: BlinkSession

    @State private var scheduler = LocalNotificationScheduler()
    @State private var lines: [SignalKind: String] = [:]
    @State private var busy: SignalKind?
    @State private var authorization: NotificationAuthorization = .notAsked
    @State private var waiting: [String] = []

    /// Long enough to put the app in the background before it lands.
    private let lead: TimeInterval = 15

    var body: some View {
        ZStack {
            face.ground.ignoresSafeArea()
            ScrollView {
                VStack(alignment: .leading, spacing: face.layout.sectionGap) {
                    Text("Signal rehearsal")
                        .font(face.displayFont)
                        .foregroundStyle(face.ink)
                    Text("Composed from the live payload, delivered in \(Int(lead)) seconds through the real scheduler.")
                        .font(face.secondaryFont)
                        .foregroundStyle(face.muted)
                    Text("permission: \(authorization.rawValue)")
                        .font(face.metaFont)
                        .foregroundStyle(face.faint)

                    pending

                    ForEach(SignalKind.allCases) { kind in
                        row(kind)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(face.layout.screenMargin)
            }
        }
        .task {
            authorization = await scheduler.requestAuthorization()
            waiting = await scheduler.pendingDescriptions()
        }
    }

    /// What the system is holding right now. This is the view that answers
    /// "did the nudge follow the session when it moved?": after a replan the
    /// list should name the same block at its NEW moment, and there should be
    /// no second entry left over at the old one.
    @ViewBuilder
    private var pending: some View {
        VStack(alignment: .leading, spacing: face.layout.rowGap) {
            Button("waiting to be delivered") {
                Task { waiting = await scheduler.pendingDescriptions() }
            }
            .font(face.bodyFont)
            .foregroundStyle(face.accent)

            if waiting.isEmpty {
                Text("nothing waiting")
                    .font(face.metaFont)
                    .foregroundStyle(face.faint)
            } else {
                ForEach(waiting, id: \.self) { line in
                    Text(line)
                        .font(face.metaFont)
                        .foregroundStyle(face.faint)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func row(_ kind: SignalKind) -> some View {
        VStack(alignment: .leading, spacing: face.layout.rowGap) {
            Button {
                Task { await rehearse(kind) }
            } label: {
                Text(kind.label)
                    .font(face.bodyFont)
                    .foregroundStyle(face.ground)
                    .frame(maxWidth: .infinity, minHeight: face.layout.minTapTarget)
                    .background(
                        RoundedRectangle(cornerRadius: face.cornerStyle.nominalRadius, style: .continuous)
                            .fill(busy == kind ? face.muted : face.accent)
                    )
            }
            .buttonStyle(.plain)
            .disabled(busy != nil)

            if let line = lines[kind] {
                Text(line)
                    .font(face.metaFont)
                    .foregroundStyle(face.faint)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(.top, face.layout.tightGap)
    }

    private func rehearse(_ kind: SignalKind) async {
        busy = kind
        defer { busy = nil }
        let result = await scheduler.rehearse(kind, for: session, after: lead)
        if let blocked = result.blocked {
            lines[kind] = "blocked: \(blocked)"
        } else if let signal = result.signal {
            lines[kind] = "\(signal.provenance.rawValue) · \(signal.body)"
        } else {
            // The honest answer, and the common one: today's data does not
            // support this kind.
            lines[kind] = "nothing to say: the payload does not support a \(kind.label) right now"
        }
    }
}
#endif
