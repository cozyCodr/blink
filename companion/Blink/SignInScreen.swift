import SwiftUI
import BlinkKit

// S7 · Sign-in (docs/COMPANION_SCREENS.md).
//
// "Single screen: the eyes, one sentence, one button." All four states the
// spec names live here: idle, authenticating, error, success.
//
// The screen is driven by a `SignInPhase` and does not own the flow, which is
// what lets the debug harness rehearse every state without a Google round trip
// and lets the real controller drive it with nothing stubbed.
//
// Every beat the eyes take here is TRUTHFUL (.agents/rules/frontend-standards.md):
// `thinking` while the token is genuinely in flight, `sorry` only when the
// server actually refused, `happy` only once a bearer is minted and stored.
// A cancelled sheet and a round trip that never came back both go quiet,
// because neither is a rejection anybody can confirm.
struct SignInScreen: View {
    @Environment(\.face) private var face

    let phase: SignInPhase
    let rig: EyeRig
    /// The person asked to sign in.
    var onContinue: () -> Void
    /// The success beat has been shown; the app can take over.
    var onFinished: () -> Void

    var body: some View {
        ZStack {
            face.ground.ignoresSafeArea()

            VStack(spacing: 0) {
                Spacer(minLength: 0)
                EyesView(rig: rig)
                    .frame(maxHeight: 220)
                Spacer(minLength: 0)
                copy
                action
                    .padding(.top, 28)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 32)
            .padding(.bottom, 24)
        }
        .task(id: phaseKey) { await react() }
    }

    // MARK: The sentence

    @ViewBuilder
    private var copy: some View {
        VStack(spacing: 12) {
            Text(headline)
                .font(face.displayFont)
                .foregroundStyle(face.ink)
                .multilineTextAlignment(.center)
            if let subline {
                Text(subline)
                    .font(face.bodyFont)
                    .foregroundStyle(isError ? face.warm : face.muted)
                    .multilineTextAlignment(.center)
            }
        }
        .animation(face.motion.releaseAnimation, value: phaseKey)
    }

    private var headline: String {
        switch phase {
        case .signedIn(let identity):
            // The greeting is composed server-side from the STORED name. No
            // name, no greeting, and never one this app made up.
            return identity.greeting ?? "You are in."
        case .authenticating:
            return "Just a moment."
        default:
            return "Blink keeps your plans, your calendar and your name on your account."
        }
    }

    private var subline: String? {
        switch phase {
        case .checking:
            return nil
        case .idle:
            return "One sign-in covers all three."
        case .authenticating:
            return "Finishing with Google."
        case .failed(let failure):
            switch failure {
            case .unavailable:
                return "Sign-in is not set up on this server yet."
            case .refused, .unconfirmed, .cancelled:
                return "That did not complete. Want to try again?"
            }
        case .signedIn:
            return nil
        }
    }

    private var isError: Bool {
        if case .failed = phase { return true }
        return false
    }

    // MARK: The button

    @ViewBuilder
    private var action: some View {
        switch phase {
        case .idle, .failed:
            Button(action: onContinue) {
                Text(isError ? "Try again" : "Continue with Google")
                    .font(face.bodyFont)
                    .foregroundStyle(face.ground)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 16)
                    .background(
                        RoundedRectangle(
                            cornerRadius: face.cornerStyle.nominalRadius,
                            style: .continuous
                        )
                        .fill(face.accent)
                    )
            }
            .buttonStyle(.plain)
        case .checking, .authenticating, .signedIn:
            // No button while something is genuinely in flight, and none once
            // it landed. There is nothing here to press twice.
            Color.clear.frame(height: 1)
        }
    }

    // MARK: The beats

    /// A value that changes exactly when the phase does, so `.task(id:)`
    /// re-runs once per transition rather than once per redraw.
    private var phaseKey: String {
        switch phase {
        case .checking: return "checking"
        case .idle: return "idle"
        case .authenticating: return "authenticating"
        case .failed(let f): return "failed:\(f)"
        case .signedIn: return "signedIn"
        }
    }

    private func react() async {
        let hold = Duration.seconds(face.motion.celebrationHold)
        switch phase {
        case .checking:
            break
        case .idle:
            // "wide on focus": the eyes open as the screen takes attention.
            rig.emote(.wide, hold: hold)
        case .authenticating:
            // A STATE, held for exactly as long as the round trip runs.
            rig.emote(.thinking)
        case .failed(let failure):
            if failure.isConfirmedRejection {
                rig.emote(.sorry, hold: hold)
            } else {
                // Cancelled, or never came back. Nobody was refused, so
                // nobody apologises.
                rig.clearEmotion()
            }
        case .signedIn:
            rig.emote(.happy, hold: hold)
            try? await Task.sleep(for: hold)
            guard !Task.isCancelled else { return }
            onFinished()
        }
    }
}
