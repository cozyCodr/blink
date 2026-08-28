import Foundation
import AVFoundation
import Speech
import Observation

// P15-12 — hold to talk.
//
// The interaction grammar is the web's `createVoiceInput` (src/web/app.js:
// 1101-1240): HOLD the mic to record, live transcription streams while held,
// and RELEASE settles the transcript into the editable compose field —
// reviewed, then sent. Never auto-sent (the web's auto-send is a separate
// opt-in this item does not carry).
//
// Denied permission is a NORMAL state, not an error: the mic explains itself
// once and yields to typing (the web's unsupported-browser fallback does the
// same — it opens the field to type, app.js:1156-1161). Nothing here nags,
// retries, or apologises twice.
@MainActor
@Observable
public final class VoiceCapture {

    public enum Phase: Equatable {
        /// Ready. Nothing held, nothing owed.
        case idle
        /// The permission sheets are up (first hold only).
        case requesting
        /// Held and listening; `transcript` is streaming.
        case recording
        /// The user said no to the mic or to speech recognition. Normal.
        case denied
        /// This device's recognizer is not available right now (no speech
        /// support, or recognition offline). Typing works; say so once.
        case unavailable
    }

    public private(set) var phase: Phase = .idle
    /// Final + interim results while recording, live.
    public private(set) var transcript = ""
    /// Whether the denied/unavailable line has been shown this session, so
    /// the explanation happens once and then the mic just stays quiet.
    public private(set) var explained = false

    @ObservationIgnored private var engine: AVAudioEngine?
    @ObservationIgnored private var recognizer: SFSpeechRecognizer?
    @ObservationIgnored private var request: SFSpeechAudioBufferRecognitionRequest?
    @ObservationIgnored private var task: SFSpeechRecognitionTask?
    /// True from beginHold to endHold, so a release that lands while the
    /// permission sheets are still up does not start a ghost recording.
    @ObservationIgnored private var held = false

    public nonisolated init() {}

    public var isRecording: Bool { phase == .recording }

    /// The one line the mic says when it cannot listen, shown once.
    public var limitationLine: String? {
        switch phase {
        case .denied:
            return "I do not have microphone access, so typing is the way. You can change that in Settings if you want me to listen."
        case .unavailable:
            return "Speech recognition is not available on this device right now, so typing is the way."
        default:
            return nil
        }
    }

    public func markExplained() { explained = true }

    /// The hold begins. Asks for both permissions on the first ever hold;
    /// starts streaming recognition when they are granted.
    public func beginHold() {
        guard phase == .idle else { return }
        held = true
        transcript = ""
        phase = .requesting
        Task { [weak self] in
            guard let self else { return }
            let micOK = await Self.requestMicrophone()
            let speechOK = micOK ? await Self.requestSpeech() : false
            guard micOK, speechOK else {
                self.phase = .denied
                return
            }
            // Released while the sheets were up: granted, but this hold is
            // over. Next hold records.
            guard self.held else {
                self.phase = .idle
                return
            }
            self.startRecognition()
        }
    }

    /// The hold ends. Returns the transcript accrued so far — the caller
    /// settles it into the compose field for review, never sends it.
    @discardableResult
    public func endHold() -> String {
        held = false
        let text = transcript.trimmingCharacters(in: .whitespacesAndNewlines)
        guard phase == .recording || phase == .requesting else { return text }
        stopRecognition()
        if phase == .recording { phase = .idle }
        return text
    }

    // MARK: The engine

    private func startRecognition() {
        let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
        guard let recognizer, recognizer.isAvailable else {
            phase = .unavailable
            return
        }
        let engine = AVAudioEngine()
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true   // live transcription, like the web's interim results
        do {
            let audioSession = AVAudioSession.sharedInstance()
            try audioSession.setCategory(.record, mode: .measurement, options: .duckOthers)
            try audioSession.setActive(true, options: .notifyOthersOnDeactivation)
            let input = engine.inputNode
            let format = input.outputFormat(forBus: 0)
            // No input channels means no capturable microphone (a simulator
            // without host audio, or exotic routes). Say unavailable rather
            // than let the audio engine fail deeper down.
            guard format.channelCount > 0 else {
                detailsLog("voice: no input channels, speech capture unavailable")
                phase = .unavailable
                return
            }
            input.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in
                request.append(buffer)
            }
            engine.prepare()
            try engine.start()
        } catch {
            detailsLog("voice: audio engine would not start")
            phase = .unavailable
            return
        }
        self.engine = engine
        self.request = request
        self.recognizer = recognizer
        phase = .recording
        task = recognizer.recognitionTask(with: request) { [weak self] result, error in
            Task { @MainActor [weak self] in
                guard let self else { return }
                if let result {
                    // bestTranscription already folds final + interim.
                    self.transcript = result.bestTranscription.formattedString
                }
                if error != nil, self.phase == .recording {
                    // Ended on its own (timeout, network). Keep what was
                    // heard; the release path settles it. Same recovery as
                    // the web's rec.onend (app.js:1191-1194).
                    self.stopRecognition()
                    self.phase = .idle
                }
            }
        }
    }

    private func stopRecognition() {
        engine?.stop()
        engine?.inputNode.removeTap(onBus: 0)
        request?.endAudio()
        task?.cancel()
        engine = nil
        request = nil
        task = nil
        recognizer = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    // MARK: Permissions

    private static func requestMicrophone() async -> Bool {
        await withCheckedContinuation { cont in
            AVAudioApplication.requestRecordPermission { cont.resume(returning: $0) }
        }
    }

    private static func requestSpeech() async -> Bool {
        await withCheckedContinuation { cont in
            SFSpeechRecognizer.requestAuthorization { status in
                cont.resume(returning: status == .authorized)
            }
        }
    }
}
