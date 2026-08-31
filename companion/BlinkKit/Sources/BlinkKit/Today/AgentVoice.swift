import Foundation
import AVFoundation
import Observation

// P15-12 — the agent speaks, when asked to.
//
// The server already owns the voice: `POST /v1/workspaces/{id}/tts` returns
// `{audio_base64, mime}` (Chirp3-HD Charon, src/api/server.py:899-913,
// src/agent/tts.py), or a null audio_base64 when Cloud TTS is unavailable.
// This class is the phone's copy of the web's whole-file path
// (`prepareWhole`, app.js:4946-4976), with the same honesty rules:
//
//   - The TEXT renders regardless. Nothing here is on the reply's path.
//   - Audio failure is SILENT degradation: a null payload, a dead network, a
//     decode error all end in "no audio", logged once, never surfaced as an
//     error and never faked as success.
//   - A new turn interrupts: `stop()` is called the moment the user sends,
//     because an interrupt is something you do in order to speak (the web's
//     rule, P7-01).
//
// The toggle lives in UserDefaults (`blink.voiceEnabled`, default ON —
// matching the web's `voiceEnabled` default) and is re-read per utterance, so
// switching it off mid-request drops the audio exactly as app.js:4961 does.

/// `{audio_base64, mime}` off /tts. Decode-only.
struct TtsResponse: Decodable, Sendable {
    let audioBase64: String?
    let mime: String?

    enum CodingKeys: String, CodingKey {
        case audioBase64 = "audio_base64"
        case mime
    }
}

extension BlinkDetailsClient {
    /// `POST /v1/workspaces/{ws}/tts {"text": …}`. Returns the decoded audio
    /// bytes, or nil when the server said it has no voice right now (null
    /// audio_base64 — still HTTP 200, by design).
    public func speech(text: String, for session: BlinkSession) async throws -> Data? {
        let url = baseURL
            .appendingPathComponent("v1/workspaces")
            .appendingPathComponent(session.workspaceID)
            .appendingPathComponent("tts")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("Bearer \(session.token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 30
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["text": text])

        let data = try await send(request, label: "tts")
        guard let res = try? JSONDecoder().decode(TtsResponse.self, from: data),
              let b64 = res.audioBase64,
              let audio = Data(base64Encoded: b64) else {
            return nil
        }
        return audio
    }
}

/// Forwards `AVAudioPlayer`'s finish callback to a closure. AVAudioPlayer
/// holds its delegate weakly and needs an NSObject, so this tiny shim keeps
/// AgentVoice free of an NSObject base while still hearing "the reply ended".
private final class PlaybackWatcher: NSObject, AVAudioPlayerDelegate {
    private let onFinish: () -> Void
    init(onFinish: @escaping () -> Void) { self.onFinish = onFinish }
    func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        onFinish()
    }
}

/// Fetches and plays the reply's audio. Owned by the screen that owns the
/// conversation; one utterance at a time, and a new turn cuts the old one.
@MainActor
@Observable
public final class AgentVoice {
    /// The persisted toggle, same storage style as the face preference
    /// (FaceProvider's UserDefaults fast path). Unset means ON, like the web:
    /// see `defaultEnabled`.
    public static let storageKey = "blink.voiceEnabled"

    @ObservationIgnored private let client: BlinkDetailsClient
    @ObservationIgnored private let defaults: UserDefaults
    @ObservationIgnored private var player: AVAudioPlayer?
    @ObservationIgnored private var watcher: PlaybackWatcher?
    @ObservationIgnored private var fetchTask: Task<Void, Never>?
    @ObservationIgnored private var watchdog: Task<Void, Never>?

    /// P18-04b — the hands-free check-in loop needs to know when a spoken reply
    /// ENDS, so it can open the mic on its own. Fired once per utterance, on the
    /// main actor, only for playback that reached its end (or the watchdog that
    /// guards a missed delegate). A deliberate `stop()` never fires it, because
    /// an interrupt is not a finish.
    @ObservationIgnored public var onFinished: (() -> Void)?
    /// P18-04b — fired when an utterance could NOT be spoken (no audio from the
    /// server, a dead network, a player that refused). The loop reads this as
    /// "this device cannot carry a spoken flow right now" and degrades to the
    /// typed flow rather than waiting on audio that will never come.
    @ObservationIgnored public var onUnavailable: (() -> Void)?

    public nonisolated init(
        baseURL: URL = BlinkAPI.baseURL(),
        defaults: UserDefaults = .standard
    ) {
        self.client = BlinkDetailsClient(baseURL: baseURL)
        self.defaults = defaults
    }

    /// What the toggle reads before anyone has touched it. ON (user, 2026-09-01:
    /// "voice agent on by default on mobile", the same call they made for the
    /// web). Blink is a thing you talk with, so it should speak the first time
    /// without a settings trip. SettingsScreen seeds its `@AppStorage` from this
    /// same constant, so the switch and the voice can never disagree.
    public static let defaultEnabled = true

    public var enabled: Bool {
        // `bool(forKey:)` cannot tell "off" from "never set", and those mean
        // different things here: an explicit off must be honoured, an untouched
        // toggle takes the default above.
        guard defaults.object(forKey: Self.storageKey) != nil else {
            return Self.defaultEnabled
        }
        return defaults.bool(forKey: Self.storageKey)
    }

    /// Speak the server's sentence. Fire-and-forget: the text is already on
    /// screen and stays there whatever happens here.
    ///
    /// `force` speaks even when the toggle is off. The hands-free check-in loop
    /// (P18-04b) is a spoken flow by definition, so it forces voice for its own
    /// duration WITHOUT touching the persisted toggle — switching it off there
    /// would be a preference change the user did not make. Everywhere else the
    /// toggle still gates, exactly as before.
    public func speak(_ text: String, session: BlinkSession, force: Bool = false) {
        guard force || enabled, !text.isEmpty else { return }
        stop()
        fetchTask = Task { [weak self] in
            guard let self else { return }
            let audio: Data?
            do {
                audio = try await self.client.speech(text: text, for: session)
            } catch {
                detailsLog("tts: request failed, reply stays text-only")
                self.finishUnavailable()
                return
            }
            guard let audio else {
                detailsLog("tts: no audio from server, reply stays text-only")
                self.finishUnavailable()
                return
            }
            guard !Task.isCancelled, (force || self.enabled) else { return }
            do {
                let av = AVAudioSession.sharedInstance()
                // .playback plays THROUGH the ring/silent switch (that is the
                // whole point of the category). Set + activate every utterance
                // so a prior .record session (the mic) can never leave us muted.
                // .duckOthers lowers the user's music for the reply rather than
                // stopping it, which is right for a brief spoken line.
                //
                // mode: .default is LOAD-BEARING (2026-08-30, "the replies after
                // the first go quiet"). The hands-free check-in speaks, opens the
                // mic, then speaks again. The mic runs in `.measurement` mode
                // (VoiceCapture.startRecognition), which disables the system's
                // output signal processing and drops the level — and the
                // TWO-argument setCategory(_:options:) does NOT reset the mode, it
                // carries the previous one over. So the first reply spoke in
                // .default (full level), but every reply after a listen inherited
                // .measurement and came out quiet. Naming .default here resets it
                // on every utterance, so the second reply and on are as loud as
                // the first.
                try av.setCategory(.playback, mode: .default, options: [.duckOthers])
                try av.setActive(true)
                let player = try AVAudioPlayer(data: audio)
                player.volume = 1.0
                let watcher = PlaybackWatcher { [weak self] in
                    Task { @MainActor in self?.finishPlaying() }
                }
                player.delegate = watcher
                self.player = player
                self.watcher = watcher
                let prepared = player.prepareToPlay()
                let started = player.play()
                // The device-vs-simulator diagnostic (2026-08-30): outputVolume
                // is the system MEDIA volume for the current route. If it reads
                // 0 the phone's media volume is down and nothing plays, however
                // correct the code is; started=false means the player refused.
                detailsLog("tts: play prepared=\(prepared) started=\(started) "
                    + "bytes=\(audio.count) cat=\(av.category.rawValue) "
                    + "mode=\(av.mode.rawValue) "
                    + "outVol=\(String(format: "%.2f", av.outputVolume))")
                guard started else {
                    // The player refused. Nobody will get a finish callback, so
                    // say "unavailable" now rather than let the loop wait.
                    self.finishUnavailable()
                    return
                }
                // A belt-and-braces guard: if the finish delegate were ever
                // missed, this reopens the flow when the clip's own length has
                // elapsed, so the loop can never hang on a spoken reply.
                self.armWatchdog(seconds: player.duration + 0.5)
            } catch {
                detailsLog("tts: audio would not play — \(error.localizedDescription)")
                self.finishUnavailable()
            }
        }
    }

    /// The interrupt: the user sent a new turn (or started talking), so the
    /// old reply stops mid-word. The web's rule, applied verbatim. A deliberate
    /// interrupt fires NEITHER callback — it is not a finish and not a failure.
    public func stop() {
        fetchTask?.cancel()
        fetchTask = nil
        watchdog?.cancel()
        watchdog = nil
        player?.stop()
        player = nil
        watcher = nil
    }

    /// Playback reached its end (or the watchdog stood in for a missed
    /// delegate). Tear down, then let the loop open the mic.
    private func finishPlaying() {
        watchdog?.cancel()
        watchdog = nil
        player = nil
        watcher = nil
        onFinished?()
    }

    /// The utterance could not be spoken. Tear down, then let the loop degrade.
    private func finishUnavailable() {
        watchdog?.cancel()
        watchdog = nil
        player = nil
        watcher = nil
        onUnavailable?()
    }

    private func armWatchdog(seconds: Double) {
        watchdog?.cancel()
        watchdog = Task { [weak self] in
            try? await Task.sleep(for: .seconds(max(seconds, 0.5)))
            guard let self, !Task.isCancelled else { return }
            self.finishPlaying()
        }
    }
}
