import SwiftUI
import BlinkKit

// S6 · Settings (docs/COMPANION_SCREENS.md).
//
// "Deliberately short. Anything that requires thinking belongs on the web."
// This is the P15-08 slice of it: the face picker (which writes the server
// field, so the web follows suit), the account row with Sign out, the escape
// hatch to the web, and the privacy link. Notifications and Calendar status
// fill in with their own items.
struct SettingsScreen: View {
    @Environment(\.face) private var face
    @Environment(FaceProvider.self) private var faces
    @Environment(\.openURL) private var openURL
    @Environment(\.dismiss) private var dismiss

    let identity: BlinkIdentity
    var onSignedOut: () -> Void

    /// P15-12: the agent's voice, default OFF like the web's `voiceEnabled`
    /// toggle, persisted locally the same way the face preference is
    /// (UserDefaults; AgentVoice re-reads it per utterance).
    @AppStorage(AgentVoice.storageKey) private var voiceEnabled = false

    var body: some View {
        ZStack {
            face.ground.ignoresSafeArea()

            ScrollView {
                VStack(alignment: .leading, spacing: face.layout.sectionGap) {
                    header
                    facePicker
                    voiceSection
                    account
                    links
                }
                .padding(face.layout.screenMargin)
            }
        }
    }

    private var header: some View {
        HStack {
            Text("Settings")
                .font(face.displayFont)
                .foregroundStyle(face.ink)
            Spacer()
            Button("Done") { dismiss() }
                .font(face.bodyFont)
                .foregroundStyle(face.accent)
        }
    }

    // MARK: Face

    private var facePicker: some View {
        VStack(alignment: .leading, spacing: face.layout.rowGap) {
            sectionLabel("FACE")
            ForEach(Faces.all, id: \.id) { candidate in
                faceRow(candidate)
            }
            // Honest about where the preference lives right now. The date
            // only moves when the server actually confirmed (FaceProvider).
            Text(faces.lastSyncedWithServer != nil
                 ? "Saved to your account. The web wears it too."
                 : "Saved on this phone. It goes on your account next time I can reach it.")
                .font(face.metaFont)
                .foregroundStyle(face.faint)
        }
    }

    private func faceRow(_ candidate: any FaceTokens) -> some View {
        let selected = faces.faceID == candidate.id
        return Button {
            faces.select(candidate.id)
        } label: {
            HStack(spacing: face.layout.tightGap) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(candidate.displayName)
                        .font(face.bodyFont)
                        .foregroundStyle(face.ink)
                    Text(candidate.tagline)
                        .font(face.metaFont)
                        .foregroundStyle(face.muted)
                }
                Spacer(minLength: face.layout.tightGap)
                if selected {
                    Image(systemName: "checkmark")
                        .font(face.bodyFont)
                        .foregroundStyle(face.accent)
                }
            }
            .padding(.vertical, face.layout.pillPaddingV)
            .padding(.horizontal, face.layout.pillPaddingH)
            .frame(maxWidth: .infinity, minHeight: face.layout.minTapTarget, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: face.cornerStyle.nominalRadius, style: .continuous)
                    .fill(selected ? face.control : Color.clear)
                    .overlay(
                        RoundedRectangle(cornerRadius: face.cornerStyle.nominalRadius, style: .continuous)
                            .stroke(selected ? face.accent : face.line, lineWidth: 1)
                    )
            )
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Wear the \(candidate.displayName) face")
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    // MARK: Voice (P15-12)

    /// The web's "Agent voice" switch, worn iOS-style. The text always
    /// renders either way; this only decides whether the reply is also
    /// spoken (Cloud TTS server-side, and silence when it cannot).
    private var voiceSection: some View {
        VStack(alignment: .leading, spacing: face.layout.rowGap) {
            sectionLabel("VOICE")
            Toggle(isOn: $voiceEnabled) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Agent voice")
                        .font(face.bodyFont)
                        .foregroundStyle(face.ink)
                    Text("Speak replies aloud, in Blink's voice.")
                        .font(face.metaFont)
                        .foregroundStyle(face.muted)
                }
            }
            .tint(face.accent)
        }
    }

    // MARK: Account

    private var account: some View {
        VStack(alignment: .leading, spacing: face.layout.rowGap) {
            sectionLabel("ACCOUNT")
            // Only what the server verified. No name means no name shown,
            // never an invented one (P15-03).
            if let name = identity.name {
                Text(name)
                    .font(face.bodyFont)
                    .foregroundStyle(face.ink)
            }
            if let email = identity.email {
                Text(email)
                    .font(face.metaFont)
                    .foregroundStyle(face.muted)
            }
            Button {
                dismiss()
                onSignedOut()
            } label: {
                Text("Sign out")
                    .font(face.bodyFont)
                    .foregroundStyle(face.alert)
                    .frame(minHeight: face.layout.minTapTarget, alignment: .leading)
            }
            .buttonStyle(.plain)
        }
    }

    // MARK: Out to the web

    private var links: some View {
        VStack(alignment: .leading, spacing: face.layout.rowGap) {
            Button {
                openURL(BlinkAPI.baseURL())
            } label: {
                Text("Open Blink on the web")
                    .font(face.bodyFont)
                    .foregroundStyle(face.accent)
                    .frame(minHeight: face.layout.minTapTarget, alignment: .leading)
            }
            .buttonStyle(.plain)
            Button {
                openURL(BlinkAPI.baseURL().appendingPathComponent("privacy"))
            } label: {
                Text("Privacy")
                    .font(face.secondaryFont)
                    .foregroundStyle(face.muted)
                    .frame(minHeight: face.layout.minTapTarget, alignment: .leading)
            }
            .buttonStyle(.plain)
        }
    }

    // MARK: Chrome

    private func sectionLabel(_ text: String) -> some View {
        Text(text)
            .font(face.labelFont)
            .tracking(2.2)   // now.css:23 letter-spacing: 0.18em at 12px
            .foregroundStyle(face.accent)
    }
}
