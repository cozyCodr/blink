import SwiftUI
import BlinkKit

// DEBUG SCAFFOLDING — not product UI.
//
// The direct equivalent of `window.__emote(name, holdMs)` on the web: every
// beat in the vocabulary, one tappable row apiece, so a beat can be rehearsed
// without waiting for the event that earns it.
//
// Nothing here is a trigger. Tapping a row is a person asking to SEE a beat,
// which is the only reason a beat may fire without grounded data behind it
// (.agents/rules/frontend-standards.md, the truthfulness rule). Wiring the
// real triggers is P15-04 and P15-05.
struct DebugEmotionRehearsalScreen: View {
    @Environment(FaceProvider.self) private var faces
    @Environment(\.face) private var face
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    @State private var rig: EyeRig
    @State private var showingTokens = false
    @State private var holdsOpen = false

    /// P15-03 hoists one rig to `BlinkApp` so the eyes carry across the
    /// sign-in transition. Left defaulted so this screen still stands alone.
    init(rig: EyeRig = EyeRig(motion: CapsuleFace().motion)) {
        _rig = State(wrappedValue: rig)
    }

    var body: some View {
        ZStack {
            face.ground.ignoresSafeArea()

            VStack(spacing: 0) {
                stage
                Divider().overlay(face.line)
                rows
            }
        }
        .sheet(isPresented: $showingTokens) {
            DebugFaceSwatchScreen()
                .environment(faces)
                .face(face)
        }
    }

    // MARK: The eyes

    private var stage: some View {
        ZStack {
            EyesView(rig: rig)
            VStack {
                HStack {
                    Text(nowShowing)
                        .font(face.monoFont)
                        .foregroundStyle(face.faint)
                    Spacer()
                    Button("Tokens") { showingTokens = true }
                        .font(face.monoFont)
                        .foregroundStyle(face.accent)
                }
                Spacer()
                VStack(spacing: 6) {
                    if !isCapsuleEyeShape {
                        // Degrade, never fabricate: the pose tables for lumen
                        // and folio are transcribed, but their eye shapes are
                        // not drawn yet. Say so rather than letting capsule
                        // bodies pass as another face.
                        Text("These are capsule bodies wearing \(face.displayName)'s ink. \(face.displayName)'s own eye shape lands in P15-08.")
                            .font(face.monoFont)
                            .foregroundStyle(face.warm)
                            .multilineTextAlignment(.center)
                    }
                    if reduceMotion {
                        Text("Reduced Motion is on. Shapes still change, they just arrive without travel.")
                            .font(face.monoFont)
                            .foregroundStyle(face.warm)
                            .multilineTextAlignment(.center)
                    }
                }
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 12)
        }
        .frame(height: 300)
        .frame(maxWidth: .infinity)
        .clipped()
    }

    private var isCapsuleEyeShape: Bool {
        if case .capsule = face.eyeShape { return true }
        return false
    }

    private var nowShowing: String {
        guard let emotion = rig.emotion else { return "resting" }
        return emotion.rawValue
    }

    // MARK: The rows

    private var rows: some View {
        ScrollView {
            LazyVStack(spacing: 0) {
                Button {
                    holdsOpen.toggle()
                } label: {
                    HStack(alignment: .firstTextBaseline, spacing: 12) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Hold each beat open")
                                .font(face.bodyFont)
                                .foregroundStyle(face.ink)
                            Text(holdsOpen
                                 ? "On. A beat stays up until you pick another."
                                 : "Off. A beat lasts as long as the web gives it.")
                                .font(face.monoFont)
                                .foregroundStyle(face.faint)
                        }
                        Spacer(minLength: 8)
                        Text(holdsOpen ? "on" : "off")
                            .font(face.monoFont)
                            .foregroundStyle(holdsOpen ? face.accent : face.faint)
                    }
                    .padding(.horizontal, 20)
                    .padding(.vertical, 14)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)

                ForEach(EmotionName.allCases) { name in
                    row(name)
                    Divider().overlay(face.line).padding(.leading, 20)
                }

                HStack(spacing: 12) {
                    utility("Blink") { rig.blink() }
                    utility("Double blink") { rig.blink(double: true) }
                    utility("Back to resting") { rig.clearEmotion() }
                }
                .padding(20)
            }
        }
    }

    private func row(_ name: EmotionName) -> some View {
        Button {
            rig.emote(name, hold: holdsOpen ? nil : name.defaultHold(in: face.motion))
        } label: {
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 8) {
                        Text(name.rawValue)
                            .font(face.bodyFont)
                            .foregroundStyle(face.ink)
                        Text(kindLabel(name))
                            .font(face.monoFont)
                            .foregroundStyle(face.faint)
                    }
                    Text(name.trigger)
                        .font(face.monoFont)
                        .foregroundStyle(face.muted)
                        .multilineTextAlignment(.leading)
                }
                Spacer(minLength: 8)
                if rig.emotion == name {
                    Circle().fill(face.accent).frame(width: 8, height: 8)
                }
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 13)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Rehearse \(name.rawValue)")
        .accessibilityHint(name.trigger)
    }

    private func kindLabel(_ name: EmotionName) -> String {
        switch name.kind {
        case .held: return "held"
        case .state: return "state"
        case .procedural: return "procedural"
        }
    }

    private func utility(_ title: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title)
                .font(face.monoFont)
                .foregroundStyle(face.ink)
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .background(
                    RoundedRectangle(cornerRadius: face.cornerStyle.nominalRadius)
                        .fill(face.control)
                )
        }
        .buttonStyle(.plain)
    }
}
