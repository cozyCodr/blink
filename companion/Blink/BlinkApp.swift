import SwiftUI
import BlinkKit

@main
struct BlinkApp: App {
    /// S2's actions can launch this app in the background with no scene at
    /// all, and the notification delegate has to be in place before launch
    /// finishes. That is the one thing SwiftUI's `App` cannot express, so it
    /// gets a delegate (P15-05).
    @UIApplicationDelegateAdaptor(BlinkAppDelegate.self) private var appDelegate

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
        } else if let workspace = UserDefaults.standard.string(forKey: "blinkDebugWorkspace"),
                  !workspace.hasPrefix("u_") {
            // S1 against a GUEST workspace on a local server.
            //
            // Signing in as the user is not something this project can do, and
            // several of S1's states need real data to exist first (a running
            // session, an unresolved evening, a recorded outcome). Guest
            // workspaces are ungated by design (`_gate_signed_in_workspaces`
            // only gates `u_…`), so this opens the SHIPPING screen against a
            // real API with a real payload. Nothing is stubbed: the door only
            // supplies which workspace to read.
            //
            // DEBUG only, refuses a `u_` id outright. The identity carries no
            // greeting unless `-blinkDebugGreeting "…"` supplies one, which
            // exists solely so the greeting's LIFECYCLE (it holds, then it
            // leaves) can be inspected without signing in as somebody.
            TodayScreen(
                identity: BlinkIdentity(
                    workspaceID: workspace,
                    greeting: UserDefaults.standard.string(forKey: "blinkDebugGreeting")
                ),
                session: BlinkSession(token: "", workspaceID: workspace),
                rig: rig,
                onSignedOut: {}
            )
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
            if case .signedIn(let identity) = session.phase,
               handedOver,
               let blink = session.session {
                // S1 · Today. The rehearsal screen P15-02 built is still one
                // tap away, behind TodayScreen's DEBUG-only "beats" door.
                TodayScreen(
                    identity: identity,
                    session: blink,
                    rig: rig,
                    onSignedOut: { session.signOut() }
                )
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
