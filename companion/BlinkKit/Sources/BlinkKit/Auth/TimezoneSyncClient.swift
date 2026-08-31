import Foundation

/// Tells the server which day the user is living in (P15-00, phone side).
///
/// The server keeps the IANA zone on the profile, and EVERY time it computes
/// or prints — the day boundary behind "today", the check-in's window, the
/// clock times on the Today card — is resolved in that zone. Left unset it
/// falls back to UTC, so a workspace used only from the phone showed 08:10 for
/// a 10:10 session on GMT+2: honest about what the server holds, and wrong
/// about the world. The web has always posted this on load
/// (`reportTimezone`, src/web/app.js); the phone did not, which is the bug.
///
/// `POST /v1/workspaces/{ws}/profile/timezone` with body `{"timezone": "<IANA>"}`,
/// matching `TimezoneRequest` in src/api/server.py. The server rejects a zone it
/// cannot load with a 422 and keeps the one it had, and only writes when the
/// value actually changed.
///
/// Fire-and-forget by design, exactly like `FaceSyncClient`: this is a silent
/// background sync that must never block the screen, never surface an error and
/// never claim anything. A failure leaves the server holding what it held —
/// the current, wrong-but-honest behaviour — and the next launch tries again.
public struct TimezoneSyncClient: Sendable {
    /// The last identifier the SERVER confirmed with a 200. Nothing else is
    /// ever written here, so a failed send cannot be mistaken for a delivered
    /// one and the retry happens on the next launch.
    static let defaultsKey = "blink.profile.reportedTimezone"

    private let baseURL: URL
    private let urlSession: URLSession
    private let defaults: UserDefaults

    public init(
        baseURL: URL = BlinkAPI.baseURL(),
        urlSession: URLSession = .shared,
        defaults: UserDefaults = .standard
    ) {
        self.baseURL = baseURL
        self.urlSession = urlSession
        self.defaults = defaults
    }

    /// Whether this identifier still needs sending. A zone changes
    /// approximately never, and this fires on every launch and every
    /// foreground, so the common case must cost nothing at all.
    static func needsSend(_ identifier: String, defaults: UserDefaults) -> Bool {
        guard !identifier.isEmpty else { return false }
        return defaults.string(forKey: defaultsKey) != identifier
    }

    /// Report the device's zone if it differs from the last one the server
    /// confirmed. Returns true only when a 200 came back, so the caller (and
    /// the tests) can tell a delivered sync from a skipped or failed one.
    /// Never throws: there is no caller who could act on a failure.
    @discardableResult
    public func report(
        identifier: String = TimeZone.current.identifier,
        session: BlinkSession
    ) async -> Bool {
        guard !session.workspaceID.isEmpty else { return false }
        guard Self.needsSend(identifier, defaults: defaults) else { return false }

        var request = URLRequest(
            url: baseURL.appendingPathComponent(
                "v1/workspaces/\(session.workspaceID)/profile/timezone"
            )
        )
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if !session.token.isEmpty {
            request.setValue("Bearer \(session.token)", forHTTPHeaderField: "Authorization")
        }
        request.timeoutInterval = 15
        request.httpBody = try? JSONEncoder().encode(["timezone": identifier])

        guard let (_, response) = try? await urlSession.data(for: request),
              let http = response as? HTTPURLResponse else {
            detailsLog("profile/timezone: no answer, keeping \(identifier) unsent")
            return false
        }
        guard http.statusCode == 200 else {
            detailsLog("profile/timezone: status \(http.statusCode)")
            return false
        }
        // Remembered ONLY on a confirmed 200.
        defaults.set(identifier, forKey: Self.defaultsKey)
        detailsLog("profile/timezone: reported \(identifier)")
        return true
    }
}
