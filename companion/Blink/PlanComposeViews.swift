import SwiftUI
import BlinkKit

// P15-11 — the compose affordance and the typed-response views.
//
// This file is the seed of the per-platform response component kit: the web
// renders the /turn contract in `dispatch(res)` + `renderClarifyQuestion`
// (src/web/app.js, components.js); these views are the same contract's
// SwiftUI rendering, one view per response type:
//
//   "message"/"planned"/"checkin"/"courses" text  -> PlanReplySurface (verbatim)
//   "question" + multi_select                     -> QuestionSurface (toggle chips + Send)
//   "question" + single_select                    -> QuestionSurface (tap-sends chips)
//   "question" + number                           -> QuestionSurface (numeric field + Send)
//   "question" + anything else                    -> QuestionSurface (free text + Send)
//   the compose field itself                      -> PlanComposeField
//
// WRITING ON PAPER, not cards: the reply and the question render with no
// container chrome at all — centered text directly on `face.ground`, the same
// direction the web took (`p.said`, conversation.css:152-156: centered, no
// box, never labelled). Chips float beneath the question and deal in with the
// web's stagger (clarify.css:229-238), on the shared motion tokens.
//
// HONESTY: no string in a reply surface originates on the phone. The one
// exception is naming a failure, and it says only what was observed.

// MARK: The compose field

/// THE DOCK, the phone's reading of the web's `#dock` (src/web/index.html:244).
///
/// The web does NOT keep a text field open. It offers a centered row of equal
/// circles — a keyboard, the mic, an attach "+" — with one teaching line under
/// them, and the mic "reads as the primary one through colour and weight, not
/// through size". That is a presence you speak to. A permanently-open field is
/// a chat app, and it competes with the eyes for the whole screen's attention.
///
/// So this slot holds three states, never two things at once:
///
///   resting   — keyboard + mic circles, and the hint line
///   typing    — the field row (mic, field, send), keyboard raised
///   listening — the mic in its active look, the hint reading "Listening"
///
/// NO ATTACH BUTTON. The web has one because multimodal ingest exists there
/// (`#attach-file`); this app has no photo ingest at all, and a "+" that did
/// nothing would claim a capability Blink does not have on the phone
/// (agent-governance.md: never offer an action you cannot take). Two circles.
///
/// THE HINT NAMES THE REAL GESTURE. The web says "Hold the mic"; the phone's
/// mic is a TAP toggle (see `micButton`), so the phone's line says tap. Copy
/// that describes a gesture the app does not have is a lie the dock tells.
struct PlanComposeField: View {
    @Environment(\.face) private var face
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Bindable var composer: PlanComposer
    var prompt: String
    /// P15-12: hold-to-talk. Owned by the screen so the eyes can react to it.
    var voice: VoiceCapture?
    /// Fired the moment a send actually leaves, so the screen can cut any
    /// reply audio (an interrupt is something you do to send — the web's rule).
    var onSend: () -> Void = {}

    /// The person asked for the field by tapping the keyboard. Cleared when
    /// they leave it with nothing typed, and by a send.
    @State private var typing = false
    @FocusState private var fieldFocused: Bool

    /// While the hold is live the transcript streams straight into the draft,
    /// which IS the review surface: release leaves it there, editable, never
    /// auto-sent (createVoiceInput's release-to-edit flow, app.js:1141-1148).
    private var isListening: Bool { voice?.isRecording ?? false }

    private var draftText: String {
        composer.draft.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// NEVER STRAND WHAT WAS TYPED OR SAID. The field is up whenever it was
    /// asked for, and it stays up for as long as there are words in the draft
    /// whatever the keyboard is doing. This is also what keeps the release-to-
    /// review contract: a transcript settles in `composer.draft`, so the moment
    /// listening stops with text the field is on screen holding it, readable
    /// and editable, and still nothing was sent on the person's behalf.
    private var fieldUp: Bool { typing || !draftText.isEmpty }

    private func send() {
        onSend()
        typing = false
        fieldFocused = false
        Task { await composer.send() }
    }

    var body: some View {
        VStack(spacing: face.layout.tightGap) {
            if fieldUp {
                HStack(spacing: face.layout.tightGap) {
                    if let voice {
                        micButton(voice)
                    }
                    field
                    sendButton
                }
            } else {
                HStack(spacing: face.layout.rowGap) {
                    keyboardButton
                    if let voice {
                        micButton(voice)
                    }
                }
            }
            hintLine
        }
        .animation(reduceMotion ? nil : face.motion.swapAnimation, value: fieldUp)
        .animation(reduceMotion ? nil : face.motion.swapAnimation, value: isListening)
        // Live transcription: while the hold is on, the words land in the
        // draft as they are heard, so release-to-review is seamless (the
        // web's interim results streaming onto the surface, app.js:1170-1178).
        .onChange(of: voice?.transcript ?? "") { _, live in
            guard let voice, voice.isRecording else { return }
            composer.draft = live
        }
        // Leaving the field with nothing in it puts the dock back. Leaving it
        // with something in it does NOT: see `fieldUp`.
        .onChange(of: fieldFocused) { _, focused in
            if !focused, draftText.isEmpty { typing = false }
        }
    }

    /// The hint, or the one honest line about a mic that cannot listen.
    ///
    /// A denied permission is a normal state: it is named once and it takes
    /// this slot when it does, because the teaching line would be telling the
    /// person to tap a mic that will not work (the web's unsupported fallback,
    /// app.js:1156). VoiceOver hears the gesture from the buttons' own labels,
    /// so the teaching line is not read a second time; "Listening" is a state
    /// nothing else announces, so that one is.
    @ViewBuilder
    private var hintLine: some View {
        if let voice, voice.explained, let line = voice.limitationLine {
            hintText(line, spoken: true)
        } else if isListening {
            hintText("Listening", spoken: true)
        } else if !fieldUp {
            hintText("Tap the mic to talk, or the keyboard to type.", spoken: false)
        }
    }

    private func hintText(_ line: String, spoken: Bool) -> some View {
        Text(line)
            .font(face.metaFont)
            .foregroundStyle(face.faint)
            .multilineTextAlignment(.center)
            .accessibilityHidden(!spoken)
    }

    /// The quiet circle that opens the field. Same size as the mic, a whole
    /// register below it in colour: the web's `.dock-btn` next to `.mic`.
    private var keyboardButton: some View {
        Button {
            typing = true
            // The field has to EXIST before focus can land on it, so the ask
            // waits out the swap that puts it there (the same cross-fade the
            // state change above rides). The keyboard then rises on its own.
            Task { @MainActor in
                try? await Task.sleep(for: .seconds(face.motion.swapFade))
                fieldFocused = true
            }
        } label: {
            Image(systemName: "keyboard")
                .font(face.bodyFont.weight(.semibold))
                .foregroundStyle(face.muted)
                .frame(width: face.layout.minTapTarget, height: face.layout.minTapTarget)
                .background(
                    Circle()
                        .fill(face.control)
                        .overlay(Circle().stroke(face.line, lineWidth: 1))
                )
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .disabled(composer.isSending || isListening)
        .accessibilityLabel("Type your message")
    }

    private var field: some View {
        TextField(isListening ? "Listening" : prompt, text: $composer.draft, axis: .vertical)
                .font(face.bodyFont)
                .foregroundStyle(face.ink)
                .multilineTextAlignment(.center)
                .lineLimit(1...4)
                .padding(.vertical, face.layout.pillPaddingV)
                .padding(.horizontal, face.layout.pillPaddingH)
                // The web's `.field` is a quiet filled well, no border; the
                // iOS reading of that is a soft capsule of `control`.
                .background(
                    RoundedRectangle(cornerRadius: face.cornerStyle.nominalRadius, style: .continuous)
                        .fill(face.control)
                )
                .focused($fieldFocused)
                .onSubmit { send() }
                .disabled(composer.isSending || isListening)
    }

    private var sendButton: some View {
        Button {
            send()
        } label: {
            Image(systemName: "arrow.up")
                .font(face.bodyFont.weight(.semibold))
                .foregroundStyle(face.ground)
                .frame(width: face.layout.minTapTarget, height: face.layout.minTapTarget)
                .background(Circle().fill(face.accent))
        }
        .buttonStyle(.plain)
        .disabled(composer.isSending || isListening
                  || composer.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
        .accessibilityLabel("Send to Blink")
    }

    /// TAP to start listening, tap again to stop. Hands-free, so you are not
    /// pinning a small button down the whole time you talk — the hold gesture
    /// that preceded this was awkward on a phone (user, 2026-08-30). The
    /// transcript streams into the draft live and settles there for review on
    /// stop. A tap while denied explains once, then stays quiet. A light haptic
    /// marks each start and stop so the toggle feels definite without looking.
    ///
    /// THE HERO, BY WEIGHT AND NOT BY SIZE (the web's dock comment). On the
    /// resting dock the mic wears the accent fill against the keyboard's quiet
    /// control, at exactly the same `minTapTarget` circle. Inside the field row
    /// the send arrow is the decisive control, so the mic steps back to the
    /// quiet register it has always had there. Listening keeps the accent
    /// either way, because that one is a state, not a rank.
    private func micButton(_ voice: VoiceCapture) -> some View {
        let filled = isListening || !fieldUp
        return micBody(voice, filled: filled)
    }

    private func micBody(_ voice: VoiceCapture, filled: Bool) -> some View {
        Button {
            if isListening {
                let text = voice.endHold()
                if !text.isEmpty { composer.draft = text }
                return
            }
            guard !composer.isSending else { return }
            if voice.limitationLine != nil {
                // Cannot listen. Say so once, then stay quiet.
                if !voice.explained { voice.markExplained() }
                return
            }
            // Talking replaces typing: let the keyboard go so the words being
            // heard are not landing behind it.
            fieldFocused = false
            voice.beginHold()
        } label: {
            Image(systemName: isListening ? "waveform" : "mic")
                .font(face.bodyFont.weight(.semibold))
                .foregroundStyle(filled ? face.ground : face.accent)
                .frame(width: face.layout.minTapTarget, height: face.layout.minTapTarget)
                .background(
                    Circle()
                        .fill(filled ? face.accent : face.control)
                        .overlay(Circle().stroke(face.line, lineWidth: 1))
                )
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .sensoryFeedback(.impact(weight: .light), trigger: isListening)
        .accessibilityLabel(isListening ? "Listening. Tap to stop." : "Tap to talk")
    }
}

// MARK: The reply, written on the paper

/// The server's sentence, verbatim, centered on the ground with no box. Also
/// carries the failure line and the in-flight echo, because they live where
/// the reply will land.
struct PlanReplySurface: View {
    @Environment(\.face) private var face
    let composer: PlanComposer

    var body: some View {
        VStack(spacing: face.layout.rowGap) {
            if let echo = composer.answerEcho {
                Text(echo)
                    .font(face.secondaryFont)
                    .foregroundStyle(face.muted)
            }
            if composer.isSending {
                Text("Thinking it through.")
                    .font(face.secondaryFont)
                    .foregroundStyle(face.faint)
            } else if let reply = composer.reply {
                Text(reply)   // verbatim; grounded server-side
                    .font(tieredFont(for: reply))
                    .minimumScaleFactor(ConversationScale.textMinimumScale)
                    .foregroundStyle(face.ink)
            }
            if composer.didRefuse {
                Text("That did not go through. Say it again and I will retry.")
                    .font(face.secondaryFont)
                    .foregroundStyle(face.warm)
            } else if composer.wasUnreachable {
                Text("I could not reach the planner just now. Try again when you are back on.")
                    .font(face.secondaryFont)
                    .foregroundStyle(face.faint)
            }
            if composer.courseOfferUp, !composer.isSending {
                Button {
                    Task { await composer.skipCourses() }
                } label: {
                    Text("Plan without them")
                        .font(face.bodyFont)
                        .foregroundStyle(face.accent)
                        .frame(minHeight: face.layout.minTapTarget)
                        .padding(.horizontal, face.layout.pillPaddingH)
                        .background(Capsule().fill(face.control))
                }
                .buttonStyle(.plain)
            }
        }
        .multilineTextAlignment(.center)
        .frame(maxWidth: .infinity)
    }

    /// P15-12: the reply's base type steps down as it grows, so a long
    /// answer trades size for room instead of pushing everything offscreen.
    /// The tiers pick between EXISTING face token fonts (display -> card
    /// title -> body), so a face's identity — and the user's Dynamic Type
    /// size, which all three ride — is never fought.
    private func tieredFont(for text: String) -> Font {
        switch ConversationScale.TextTier(charCount: text.count) {
        case .short: return face.displayFont
        case .medium: return face.cardTitleFont
        case .long: return face.bodyFont
        }
    }
}

// MARK: The question, on the same paper

/// One ClarifyQuestion, rendered by its `input_type`: the question centered
/// on the ground, the chips floating beneath it, dealt in with the web's
/// stagger. `.id(question.field)` on the call site resets state per question.
struct QuestionSurface: View {
    @Environment(\.face) private var face
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let question: TurnQuestion
    var onSubmit: (ElicitAnswerValue) -> Void
    /// A `confirm` question routes here instead of `onSubmit`: its yes/no is a
    /// boolean, not an `ElicitAnswerValue`, and a calendar confirm commits to a
    /// different endpoint than `/elicit/answer` (P18-02). The default is a
    /// no-op so the non-confirm call sites need not pass it.
    var onConfirm: (Bool) -> Void = { _ in }

    @State private var chosen: Set<String> = []      // multi_select, by label
    @State private var freeText = ""                 // "Other…" / free text
    @State private var otherOpen = false
    @State private var numberText = ""
    /// Flips on appearance; each chip's entrance rides its own delayed
    /// animation off this one change (the deal, clarify.css:229-238).
    @State private var dealt = false

    var body: some View {
        VStack(spacing: face.layout.rowGap) {
            Text(question.question)
                .font(questionFont)
                .minimumScaleFactor(ConversationScale.textMinimumScale)
                .foregroundStyle(face.ink)
            if let why = question.why, !why.isEmpty {
                Text(why)
                    .font(face.metaFont)
                    .foregroundStyle(face.faint)
            }
            control
        }
        .multilineTextAlignment(.center)
        .frame(maxWidth: .infinity)
        .onAppear { dealt = true }   // Reduced Motion: the animation is nil, so this lands instantly
    }

    /// Same tiering as the reply (P15-12): a long question steps down through
    /// the face's own token fonts rather than crowding its chips off screen.
    private var questionFont: Font {
        let count = question.question.count + (question.why?.count ?? 0)
        switch ConversationScale.TextTier(charCount: count) {
        case .short: return face.displayFont
        case .medium: return face.cardTitleFont
        case .long: return face.bodyFont
        }
    }

    @ViewBuilder
    private var control: some View {
        switch question.inputType {
        case "confirm": confirmControl
        case "multi_select": multiSelect
        case "single_select": singleSelect
        case "number": numberInput
        default: freeTextInput
        }
    }

    // confirm -> two decisive buttons, the same yes/not-now the web renders
    // (components.js:556-570). Tapping either IS the commit, so there is no
    // separate Send; the labels come from the server's options when it sends
    // them and fall back to Yes / Not now, as the web's do. The yes / no goes
    // to `onConfirm`, not `onSubmit`: a calendar confirm's YES commits to
    // `/calendar/events`, never `/elicit/answer` (P18-02).
    private var confirmControl: some View {
        let yesLabel = question.options.first?.label ?? "Yes"
        let noLabel = question.options.dropFirst().first?.label ?? "Not now"
        return HStack(spacing: face.layout.tightGap) {
            confirmButton(yesLabel, decisive: true) { onConfirm(true) }
            confirmButton(noLabel, decisive: false) { onConfirm(false) }
        }
        .frame(maxWidth: .infinity)
    }

    /// One confirm button, dealt in with the same rise as the chips. The YES is
    /// the decisive fill (accent on ground, the web's `.btn.go`); the "Not now"
    /// is the quiet control (the web's `.btn.ghost`).
    private func confirmButton(_ label: String, decisive: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(label)
                .font(face.bodyFont)
                .foregroundStyle(decisive ? face.ground : face.ink)
                .lineLimit(1)
                .frame(maxWidth: .infinity, minHeight: face.layout.minTapTarget)
                .padding(.horizontal, face.layout.pillPaddingH)
                .background(Capsule().fill(decisive ? face.accent : face.control))
        }
        .buttonStyle(.plain)
        .opacity(dealt ? 1 : 0)
        .offset(y: dealt ? 0 : face.motion.revealRise)
        .animation(reduceMotion ? nil : face.motion.dealAnimation(index: decisive ? 0 : 1), value: dealt)
        .accessibilityLabel(label)
    }

    // multi_select -> toggle chips + Send. The value is the chosen LABELS
    // (plus the free text when "Other…" is on): the server's options carry no
    // scalar values and the profile stores strings
    // (UserProfile.platforms, src/types/entities.py:156).
    private var multiSelect: some View {
        VStack(spacing: face.layout.rowGap) {
            chipGrid { index, option in
                chip(option.label, on: isOn(option), index: index) { toggle(option) }
            }
            if otherOpen {
                otherField("Something else")
            }
            sendButton(enabled: !multiValue.isEmpty) {
                onSubmit(.texts(multiValue))
            }
        }
    }

    private var multiValue: [String] {
        var vals = question.options
            .filter { !$0.opensFreeText && chosen.contains($0.label) }
            .map(\.label)
        let extra = freeText.trimmingCharacters(in: .whitespacesAndNewlines)
        if otherOpen, !extra.isEmpty { vals.append(extra) }
        return vals
    }

    // single_select -> tapping a chip IS the commit, exactly as the web's
    // decisive control works. "Other…" opens the text field instead.
    private var singleSelect: some View {
        VStack(spacing: face.layout.rowGap) {
            chipGrid { index, option in
                chip(option.label, on: isOn(option), index: index) {
                    if option.opensFreeText {
                        otherOpen.toggle()
                    } else {
                        onSubmit(.text(option.label))
                    }
                }
            }
            if otherOpen {
                otherField("Tell me more")
                sendButton(enabled: !freeText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty) {
                    onSubmit(.text(freeText.trimmingCharacters(in: .whitespacesAndNewlines)))
                }
            }
        }
    }

    // number -> a numeric field, clamped to the server's config.
    private var numberInput: some View {
        VStack(spacing: face.layout.rowGap) {
            HStack(spacing: face.layout.tightGap) {
                TextField("0", text: $numberText)
                    .keyboardType(.numberPad)
                    .font(face.bodyFont)
                    .foregroundStyle(face.ink)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 96)
                    .padding(.vertical, face.layout.pillPaddingV)
                    .padding(.horizontal, face.layout.pillPaddingH)
                    .background(fieldShape)
                if let unit = question.config?.unit {
                    Text(unit)
                        .font(face.secondaryFont)
                        .foregroundStyle(face.muted)
                }
            }
            .frame(maxWidth: .infinity)
            sendButton(enabled: numberValue != nil) {
                if let n = numberValue { onSubmit(.number(n)) }
            }
        }
    }

    private var numberValue: Int? {
        guard var n = Int(numberText.trimmingCharacters(in: .whitespaces)) else { return nil }
        if let min = question.config?.min { n = max(n, min) }
        if let max = question.config?.max { n = Swift.min(n, max) }
        return n
    }

    // Anything unrecognised -> free text, so no question is ever a dead end.
    private var freeTextInput: some View {
        VStack(spacing: face.layout.rowGap) {
            otherField("Type your answer")
            sendButton(enabled: !freeText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty) {
                onSubmit(.text(freeText.trimmingCharacters(in: .whitespacesAndNewlines)))
            }
        }
    }

    // MARK: Pieces

    private func isOn(_ option: TurnQuestionOption) -> Bool {
        option.opensFreeText ? otherOpen : chosen.contains(option.label)
    }

    private func toggle(_ option: TurnQuestionOption) {
        if option.opensFreeText {
            otherOpen.toggle()
        } else if chosen.contains(option.label) {
            chosen.remove(option.label)
        } else {
            chosen.insert(option.label)
        }
    }

    private func chipGrid<Chip: View>(
        @ViewBuilder _ make: @escaping (Int, TurnQuestionOption) -> Chip
    ) -> some View {
        LazyVGrid(
            columns: [GridItem(.adaptive(minimum: 108), spacing: face.layout.tightGap)],
            alignment: .center,
            spacing: face.layout.tightGap
        ) {
            ForEach(Array(question.options.enumerated()), id: \.element.id) { index, option in
                make(index, option)
            }
        }
    }

    /// One floating chip, dealt in with the web's stagger. Reduced Motion
    /// arrives instantly (`dealt` still flips; the animation is nil).
    private func chip(_ label: String, on: Bool, index: Int, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(label)
                .font(face.secondaryFont)
                .foregroundStyle(on ? face.ground : face.ink)
                .lineLimit(1)
                .frame(maxWidth: .infinity, minHeight: face.layout.minTapTarget)
                .background(Capsule().fill(on ? face.accent : face.control))
        }
        .buttonStyle(.plain)
        .opacity(dealt ? 1 : 0)
        .offset(y: dealt ? 0 : face.motion.revealRise)
        .animation(reduceMotion ? nil : face.motion.dealAnimation(index: index), value: dealt)
        .accessibilityLabel(label)
        .accessibilityAddTraits(on ? .isSelected : [])
    }

    private func otherField(_ prompt: String) -> some View {
        TextField(prompt, text: $freeText, axis: .vertical)
            .font(face.bodyFont)
            .foregroundStyle(face.ink)
            .multilineTextAlignment(.center)
            .lineLimit(1...3)
            .padding(.vertical, face.layout.pillPaddingV)
            .padding(.horizontal, face.layout.pillPaddingH)
            .background(fieldShape)
    }

    private var fieldShape: some View {
        RoundedRectangle(cornerRadius: face.cornerStyle.nominalRadius, style: .continuous)
            .fill(face.control)
    }

    private func sendButton(enabled: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text("Send")
                .font(face.bodyFont)
                .foregroundStyle(enabled ? face.ground : face.muted)
                .frame(minHeight: face.layout.minTapTarget)
                .padding(.horizontal, face.layout.pillPaddingH * 3)
                .background(Capsule().fill(enabled ? face.accent : face.control))
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
    }
}
