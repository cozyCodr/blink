import SwiftUI
import BlinkKit

@main
struct BlinkApp: App {
    @State private var faces = FaceProvider()
    @State private var session = SessionController()
    // ONE rig for the whole app, so the eyes that ask you to sign in are
    // literally the same eyes that greet you afterwards. A rig per screen
    // would restart the blink scheduler on every transition and read as a
    // different creature arriving.
    @State private var rig = EyeRig(motion: CapsuleFace().motion)

    var body: some Scene {
        WindowGroup {
            AppRoot(session: session, rig: rig)
                .environment(faces)
                .face(faces.tokens)
        }
    }
}

/// The entry point, plus the one debug door.
///
/// Three of S7's four states only appear after a real Google round trip or a
/// real server refusal, which makes them impossible to inspect honestly. The
/// launch argument `-blinkDebugSignInStates YES` opens the state rehearsal
/// instead. DEBUG only, off unless asked for, and it drives the SAME screen
/// the app ships, so what it shows is what a person would see.
struct AppRoot: View {
    let session: SessionController
    let rig: EyeRig

    var body: some View {
        #if DEBUG
        if UserDefaults.standard.bool(forKey: "blinkDebugSignInStates") {
            DebugSignInStatesScreen()
        } else {
            RootView(session: session, rig: rig)
        }
        #else
        RootView(session: session, rig: rig)
        #endif
    }
}

/// Sign-in gates everything. There is no guest mode on the companion
/// (docs/COMPANION_ARCHITECTURE.md §4, Gap 1): signed out means S7.
struct RootView: View {
    @Environment(\.face) private var face

    let session: SessionController
    let rig: EyeRig

    /// The success beat has played and the app may take over. Kept separate
    /// from the phase so the greeting is not cut off mid-beat.
    @State private var handedOver = false

    var body: some View {
        Group {
            if session.phase.isSignedIn, handedOver {
                // Still scaffolding behind the gate: P15-04 brings the Today
                // screen. The rehearsal screen stays reachable, now wearing
                // the same rig the sign-in screen used.
                DebugEmotionRehearsalScreen(rig: rig)
            } else {
                SignInScreen(
                    phase: session.phase,
                    rig: rig,
                    onContinue: { Task { await session.signIn() } },
                    onFinished: { handedOver = true }
                )
            }
        }
        .animation(face.motion.releaseAnimation, value: handedOver)
        .task { await session.restore() }
        .onChange(of: session.phase.isSignedIn) { _, signedIn in
            if !signedIn { handedOver = false }
        }
    }
}
