#if DEBUG
import SwiftUI
import BlinkKit

// DEBUG SCAFFOLDING — not product UI, and not reachable from the app.
//
// The same idea as `DebugEmotionRehearsalScreen`: S7 has four states
// (docs/COMPANION_SCREENS.md), and three of them need a real Google round trip
// or a real server refusal to appear. This drives the SCREEN through each one
// so the states can be seen, screenshotted and diffed against the spec without
// anyone faking a rejection to a real user.
//
// Nothing here is a trigger. Choosing a state is a person asking to SEE it,
// which is the only reason a beat may fire without grounded data behind it.
struct DebugSignInStatesScreen: View {
    @Environment(\.face) private var face

    @State private var rig = EyeRig(motion: CapsuleFace().motion)
    @State private var phase: SignInPhase = .idle

    private static let states: [(String, SignInPhase)] = [
        ("idle", .idle),
        ("authenticating", .authenticating),
        ("error", .failed(.refused(reason: "verification_failed"))),
        ("success", .signedIn(BlinkIdentity(
            workspaceID: "u_rehearsal",
            name: "Bright Dev",
            greeting: "Good to see you, Bright."
        ))),
    ]

    var body: some View {
        ZStack(alignment: .bottom) {
            SignInScreen(
                phase: phase,
                rig: rig,
                onContinue: { phase = .authenticating },
                onFinished: {}
            )
            picker
        }
    }

    private var picker: some View {
        HStack(spacing: 8) {
            ForEach(Self.states, id: \.0) { name, state in
                Button { phase = state } label: {
                    Text(name)
                        .font(face.monoFont)
                        .foregroundStyle(isShowing(state) ? face.accent : face.faint)
                        .padding(.vertical, 14)
                        .padding(.horizontal, 4)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
        .padding(.bottom, 24)
    }

    private func isShowing(_ state: SignInPhase) -> Bool { state == phase }
}
#endif
