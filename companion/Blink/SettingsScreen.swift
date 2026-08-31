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
    /// The bearer P15-03 minted. Settings reads and writes the calendar with
    /// the same credential every other screen uses.
    let session: BlinkSession
    var onSignedOut: () -> Void

    /// Calendar state, as the SERVER states it. Nothing is shown until it
    /// answers, and nothing here claims a sync that did not happen.
    @State private var calendar = CalendarController()

    /// P15-12: the agent's voice, persisted locally the same way the face
    /// preference is (UserDefaults; AgentVoice re-reads it per utterance).
    /// Seeded from `AgentVoice.defaultEnabled` (ON) rather than a literal, so
    /// this switch and the voice itself can never disagree about what an
    /// untouched toggle means.
    @AppStorage(AgentVoice.storageKey) private var voiceEnabled = AgentVoice.defaultEnabled

    // DEBUG-only rehearsal doors, moved here off the main Today screen (user,
    // 2026-08-30): developer tools, not something a person using Blink should
    // see. Absent entirely from a Release build.
    #if DEBUG
    @State private var showingBeats = false
    @State private var showingSignals = false
    #endif

    var body: some View {
        ZStack {
            face.ground.ignoresSafeArea()

            ScrollView {
                VStack(alignment: .leading, spacing: face.layout.sectionGap) {
                    header
                    facePicker
                    calendarSection
                    voiceSection
                    account
                    links
                    #if DEBUG
                    developerSection
                    #endif
                }
                .padding(face.layout.screenMargin)
            }
        }
        .task { await calendar.load(session: session) }
        .onChange(of: calendar.needsSignIn) { _, dead in
            if dead {
                dismiss()
                onSignedOut()
            }
        }
        #if DEBUG
        .sheet(isPresented: $showingBeats) {
            DebugEmotionRehearsalScreen()
                .environment(faces)
                .face(face)
        }
        .sheet(isPresented: $showingSignals) {
            DebugSignalRehearsalScreen(session: session)
                .face(face)
        }
        #endif
    }

    #if DEBUG
    /// The emotion-beat and signal rehearsal screens, reachable only from here
    /// in a DEBUG build. They used to sit on the Today screen's top bar; a
    /// person testing Blink should never see them, so they moved behind
    /// Settings and vanish from Release entirely.
    private var developerSection: some View {
        VStack(alignment: .leading, spacing: face.layout.rowGap) {
            sectionLabel("DEVELOPER")
            Button("Emotion beats") { showingBeats = true }
                .font(face.bodyFont)
                .foregroundStyle(face.accent)
                .frame(minHeight: face.layout.minTapTarget, alignment: .leading)
            Button("Signal rehearsal") { showingSignals = true }
                .font(face.bodyFont)
                .foregroundStyle(face.accent)
                .frame(minHeight: face.layout.minTapTarget, alignment: .leading)
        }
    }
    #endif

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

    // MARK: Calendar

    /// Three honest states and one action.
    ///
    /// Connected and granted: Blink is reading the calendar on its own, and
    /// "Sync now" is there for the impatient. Connected without Calendar
    /// permission: say exactly that, and point at the web, because the consent
    /// screen lives there and this app will not grow a second OAuth flow. Not
    /// connected: the same pointer, no pretending.
    @ViewBuilder
    private var calendarSection: some View {
        VStack(alignment: .leading, spacing: face.layout.rowGap) {
            sectionLabel("CALENDAR")
            if let status = calendar.status {
                switch status.standing {
                case .connected:
                    Text(status.email ?? "Google Calendar is connected.")
                        .font(face.bodyFont)
                        .foregroundStyle(face.ink)
                    Text("I read your calendar on my own, so plans go around what is already there.")
                        .font(face.metaFont)
                        .foregroundStyle(face.muted)
                    syncRow

                case .connectedWithoutCalendarPermission:
                    Text("Signed in, no Calendar permission yet.")
                        .font(face.bodyFont)
                        .foregroundStyle(face.ink)
                    Text("Until you grant it, I am planning without knowing what is already booked. Connect it on the web and keep the Calendar box checked.")
                        .font(face.metaFont)
                        .foregroundStyle(face.warm)
                    webLink("Fix this on the web")

                case .notConnected:
                    Text("Not connected.")
                        .font(face.bodyFont)
                        .foregroundStyle(face.ink)
                    Text("Connect Google Calendar on the web and I will keep it in step from then on.")
                        .font(face.metaFont)
                        .foregroundStyle(face.muted)
                    webLink("Connect it on the web")
                }
            } else if calendar.isLoading {
                Text("Checking.")
                    .font(face.metaFont)
                    .foregroundStyle(face.muted)
            } else {
                // The read did not answer. Silence about the calendar beats a
                // guess about it.
                Text("I could not reach the server to check this.")
                    .font(face.metaFont)
                    .foregroundStyle(face.muted)
            }
        }
    }

    private var syncRow: some View {
        VStack(alignment: .leading, spacing: face.layout.rowGap) {
            Button {
                Task { await calendar.sync(session: session) }
            } label: {
                Text(calendar.isSyncing ? "Syncing" : "Sync now")
                    .font(face.bodyFont)
                    .foregroundStyle(face.accent)
                    .frame(minHeight: face.layout.minTapTarget, alignment: .leading)
            }
            .buttonStyle(.plain)
            .disabled(calendar.isSyncing)

            // Counts, from the server's own answer. No titles, because the
            // phone is never sent one.
            if calendar.lastSyncFailed {
                Text("That pull did not go through. Your calendar is as I last had it.")
                    .font(face.metaFont)
                    .foregroundStyle(face.warm)
            } else if let result = calendar.lastResult {
                Text("^[\(result.eventsCount) event](inflect: true) read, ^[\(result.constraintsCreated) busy block](inflect: true) in your week.")
                    .font(face.metaFont)
                    .foregroundStyle(face.faint)
            }
        }
    }

    private func webLink(_ title: String) -> some View {
        Button {
            openURL(BlinkAPI.baseURL())
        } label: {
            Text(title)
                .font(face.bodyFont)
                .foregroundStyle(face.accent)
                .frame(minHeight: face.layout.minTapTarget, alignment: .leading)
        }
        .buttonStyle(.plain)
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
