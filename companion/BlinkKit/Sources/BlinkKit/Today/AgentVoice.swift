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
// The toggle lives in UserDefaults (`blink.voiceEnabled`, default OFF —
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

/// Fetches and plays the reply's audio. Owned by the screen that owns the
/// conversation; one utterance at a time, and a new turn cuts the old one.
@MainActor
@Observable
public final class AgentVoice {
    /// The persisted toggle, same storage style as the face preference
    /// (FaceProvider's UserDefaults fast path). Default OFF, like the web.
    public static let storageKey = "blink.voiceEnabled"

    @ObservationIgnored private let client: BlinkDetailsClient
    @ObservationIgnored private let defaults: UserDefaults
    @ObservationIgnored private var player: AVAudioPlayer?
    @ObservationIgnored private var fetchTask: Task<Void, Never>?

    public nonisolated init(
        baseURL: URL = BlinkAPI.baseURL(),
        defaults: UserDefaults = .standard
    ) {
        self.client = BlinkDetailsClient(baseURL: baseURL)
        self.defaults = defaults
    }

    public var enabled: Bool {
        defaults.bool(forKey: Self.storageKey)   // unset reads false: default OFF
    }

    /// Speak the server's sentence, if the toggle is on. Fire-and-forget:
    /// the text is already on screen and stays there whatever happens here.
    public func speak(_ text: String, session: BlinkSession) {
        guard enabled, !text.isEmpty else { return }
        stop()
        fetchTask = Task { [weak self] in
            guard let self else { return }
            let audio: Data?
            do {
                audio = try await self.client.speech(text: text, for: session)
            } catch {
                detailsLog("tts: request failed, reply stays text-only")
                return
            }
            guard let audio else {
                detailsLog("tts: no audio from server, reply stays text-only")
                return
            }
            guard !Task.isCancelled, self.enabled else { return }
            do {
                try AVAudioSession.sharedInstance().setCategory(.playback, options: [.duckOthers])
                try AVAudioSession.sharedInstance().setActive(true)
                let player = try AVAudioPlayer(data: audio)
                self.player = player
                player.play()
            } catch {
                detailsLog("tts: audio would not play, reply stays text-only")
            }
        }
    }

    /// The interrupt: the user sent a new turn (or started talking), so the
    /// old reply stops mid-word. The web's rule, applied verbatim.
    public func stop() {
        fetchTask?.cancel()
        fetchTask = nil
        player?.stop()
        player = nil
    }
}
