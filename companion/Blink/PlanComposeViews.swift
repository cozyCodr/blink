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

struct PlanComposeField: View {
    @Environment(\.face) private var face
    @Bindable var composer: PlanComposer
    var prompt: String
    /// P15-12: hold-to-talk. Owned by the screen so the eyes can react to it.
    var voice: VoiceCapture?
    /// Fired the moment a send actually leaves, so the screen can cut any
    /// reply audio (an interrupt is something you do to send — the web's rule).
    var onSend: () -> Void = {}

    /// While the hold is live the transcript streams straight into the draft,
    /// which IS the review surface: release leaves it there, editable, never
    /// auto-sent (createVoiceInput's release-to-edit flow, app.js:1141-1148).
    private var isListening: Bool { voice?.isRecording ?? false }

    private func send() {
        onSend()
        Task { await composer.send() }
    }

    var body: some View {
        VStack(spacing: face.layout.tightGap) {
            HStack(spacing: face.layout.tightGap) {
                if let voice {
                    micButton(voice)
                }
                field
                sendButton
            }
            // The one-time explanation when the mic cannot listen. A denied
            // permission is a normal state: it is named once, and the field
            // above keeps working (the web's unsupported fallback, app.js:1156).
            if let voice, voice.explained, let line = voice.limitationLine {
                Text(line)
                    .font(face.metaFont)
                    .foregroundStyle(face.faint)
                    .multilineTextAlignment(.center)
            }
        }
        // Live transcription: while the hold is on, the words land in the
        // draft as they are heard, so release-to-review is seamless (the
        // web's interim results streaming onto the surface, app.js:1170-1178).
        .onChange(of: voice?.transcript ?? "") { _, live in
            guard let voice, voice.isRecording else { return }
            composer.draft = live
        }
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

    /// HOLD to record (a zero-distance drag is the hold, exactly what the
    /// web's pointerdown/pointerup pair is); release settles the transcript
    /// into the field for review. A hold while denied explains once.
    private func micButton(_ voice: VoiceCapture) -> some View {
        Image(systemName: isListening ? "waveform" : "mic")
            .font(face.bodyFont.weight(.semibold))
            .foregroundStyle(isListening ? face.ground : face.accent)
            .frame(width: face.layout.minTapTarget, height: face.layout.minTapTarget)
            .background(
                Circle()
                    .fill(isListening ? face.accent : face.control)
                    .overlay(Circle().stroke(face.line, lineWidth: 1))
            )
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { _ in
                        guard !isListening, !composer.isSending else { return }
                        if voice.limitationLine != nil {
                            // Cannot listen. Say so once, then stay quiet.
                            if !voice.explained { voice.markExplained() }
                            return
                        }
                        voice.beginHold()
                    }
                    .onEnded { _ in
                        let text = voice.endHold()
                        if !text.isEmpty { composer.draft = text }
                        if voice.limitationLine != nil, !voice.explained {
                            voice.markExplained()
                        }
                    }
            )
            .accessibilityLabel(isListening ? "Listening. Release to review." : "Hold to talk")
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
        case "multi_select": multiSelect
        case "single_select": singleSelect
        case "number": numberInput
        default: freeTextInput
        }
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
