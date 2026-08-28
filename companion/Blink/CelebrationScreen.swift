import SwiftUI
import BlinkKit

// S5 · Celebration (docs/COMPANION_SCREENS.md).
//
// "The earned moment. Reachable only from a server response containing a
// recorded outcome. There is no local path to this screen."
//
// HOW THAT IS ENFORCED HERE: this view has exactly one initialiser and it
// requires a `RecordedOutcome`. `RecordedOutcome`'s own initialiser is
// internal to BlinkKit, so THIS FILE CANNOT BUILD ONE. The only two things
// that can are the two factories in BlinkKit/Today/RecordedOutcome.swift, and
// both take a decoded server response. Presenting this screen without the
// server having recorded something is a compiler error, not a rule somebody
// has to remember.
//
// THE BEATS, and what grounds them:
//
//   heart     — a TIMER-MEASURED outcome the server holds. capsule's
//               celebration is `.heartBurst` (FaceTokens `celebration`), and
//               this is the only place in the app that fires it.
//   satisfied — a SELF-REPORTED outcome. Its own trigger is "a focus session
//               recorded", and one deliberate slow blink is the quieter
//               register S5 asks for: "A self-reported completion still gets
//               warmth, just a quieter register and honest words."
//
// Never the reverse. A reported session does not get the heart, because the
// heart would read as "I watched you do this" and nobody watched.
struct CelebrationScreen: View {
    @Environment(\.face) private var face
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    let outcome: RecordedOutcome
    let rig: EyeRig
    var onDone: () -> Void

    @State private var burst = false

    var body: some View {
        ZStack {
            face.ground.ignoresSafeArea()

            VStack(spacing: face.layout.sectionGap) {
                Spacer(minLength: 0)
                EyesView(rig: rig, scale: 0.72)
                    .frame(height: 170)

                VStack(spacing: face.layout.tightGap) {
                    Text(DurationText.spoken(outcome.minutes))
                        .font(face.numberFont)
                        .monospacedDigit()
                        .foregroundStyle(face.ink)
                    Text(provenance)
                        .font(face.metaFont)
                        .foregroundStyle(face.faint)
                }

                Text(sentence)
                    .font(face.bodyFont)
                    .foregroundStyle(face.muted)
                    .multilineTextAlignment(.center)
                    // Without this the sentence refuses to wrap, widens the
                    // stack past the screen, and takes the padding with it.
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity)

                Spacer(minLength: 0)

                Button(action: onDone) {
                    Text("Good")
                        .font(face.bodyFont)
                        .foregroundStyle(face.ground)
                        .frame(maxWidth: .infinity, minHeight: face.layout.minTapTarget)
                        .background(
                            RoundedRectangle(cornerRadius: face.cornerStyle.nominalRadius, style: .continuous)
                                .fill(face.accent)
                        )
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Close and go back to today")
            }
            .padding(.horizontal, face.layout.screenMargin)
            .padding(.bottom, face.layout.cardPaddingBottom)
            // The face's celebration mark, behind everything. As a BACKGROUND
            // it paints without reporting a size, the same reason EyesView
            // hangs its 640pt halo there: as a ZStack sibling it would widen
            // the stack past the screen and drag the padding off with it.
            //
            // Reduced Motion gets the END STATE and no travel, which is what
            // COMPANION_SCREENS.md's accessibility section requires: "the
            // face's celebration must have a non-motion form".
            .background {
                if outcome.isMeasured {
                    starburst
                }
            }
        }
        .task { await celebrate() }
    }

    // MARK: The words

    /// The line under the number. The number's PROVENANCE, never decoration.
    private var provenance: String {
        outcome.isMeasured ? "measured, not claimed" : "on your word, and that is fine"
    }

    /// Assembled only from things the server recorded. There is no clause here
    /// that the outcome cannot back.
    ///
    /// S5's example ends "and tomorrow is already planned". That clause is
    /// deliberately absent: nothing in a check-in response says anything about
    /// tomorrow, and this screen does not read the schedule. Saying it would
    /// be exactly the fabrication the rules forbid.
    private var sentence: String {
        var parts: [String] = []
        let name = outcome.title
        switch outcome.status {
        case .done:
            parts.append(name.map { "\($0) is done." } ?? "That one is done.")
        case .partial:
            parts.append(name.map { "\($0) got a real go." } ?? "That one got a real go.")
        default:
            parts.append("Recorded.")
        }
        if outcome.streakDays > 0 {
            parts.append("Day \(outcome.streakDays) stays alive.")
        }
        return parts.joined(separator: " ")
    }

    // MARK: The beat and the haptic

    private func celebrate() async {
        if outcome.isMeasured {
            rig.emote(.heart, hold: .seconds(face.motion.heartHold))
            playHaptic()
            guard !reduceMotion else {
                burst = true
                return
            }
            withAnimation(face.motion.emotionAnimation) { burst = true }
        } else {
            // The quieter register: one deliberate slow blink, no burst, no
            // double haptic.
            rig.emote(.satisfied)
        }
    }

    /// The face's haptic, from the token. docs/COMPANION_SCREENS.md's face
    /// table: capsule warm double tap, lumen crisp single, folio thunk.
    private func playHaptic() {
        #if canImport(UIKit)
        switch face.motion.haptic {
        case .warmDouble:
            let generator = UIImpactFeedbackGenerator(style: .soft)
            generator.impactOccurred()
            DispatchQueue.main.asyncAfter(deadline: .now() + face.motion.beats.doubleBlinkGap) {
                generator.impactOccurred()
            }
        case .crispSingle:
            UIImpactFeedbackGenerator(style: .rigid).impactOccurred()
        case .thunk:
            UIImpactFeedbackGenerator(style: .heavy).impactOccurred()
        }
        #endif
    }

    // MARK: The mark

    /// capsule: "a soft sage starburst". Painted from the face's own glow
    /// token, so lumen's and folio's marks are a token change rather than a
    /// branch when P15-08 lands their identities.
    private var starburst: some View {
        Circle()
            .fill(
                RadialGradient(
                    colors: [face.glow.opacity(face.ambientOpacity * 2), face.glow.opacity(0)],
                    center: .center,
                    startRadius: 0,
                    endRadius: face.eyeGeometry.ambientSize / 2
                )
            )
            .frame(width: face.eyeGeometry.ambientSize, height: face.eyeGeometry.ambientSize)
            .scaleEffect(burst ? 1 : 0.72)
            .opacity(burst ? 1 : 0)
            .allowsHitTesting(false)
    }
}
