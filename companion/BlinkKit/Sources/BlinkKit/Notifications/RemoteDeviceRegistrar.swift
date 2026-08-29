import Foundation

/// Hands this device's APNs token to the server (the client half of P15-10).
///
/// `POST /v1/workspaces/{id}/devices`. It carries only the opaque token and
/// which APNs host minted it; the server owns the budget, the windows and the
/// send. Registering the same token twice is an UPDATE, never a duplicate, so
/// calling this on every launch is safe and correct: an APNs token can rotate,
/// and the freshest one is the only one that still delivers.
///
/// It never throws. A failed registration must not take down a launch, and
/// there is nothing a person could do about it; the next launch tries again
/// with whatever token iOS hands over then.
public struct RemoteDeviceRegistrar: Sendable {
    private let baseURL: URL
    private let session: URLSession

    public init(baseURL: URL = BlinkAPI.baseURL(), session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    /// Which APNs host minted the token. A build carrying the *development*
    /// `aps-environment` gets a token only `api.sandbox.push.apple.com` will
    /// accept; sending it to production returns `BadDeviceToken`. A DEBUG build
    /// IS that development build, so `#if DEBUG` is the one honest signal the
    /// client has, and it must match the entitlement in `Blink.entitlements`.
    public static var currentEnvironment: String {
        #if DEBUG
        return "sandbox"
        #else
        return "production"
        #endif
    }

    /// True on a 2xx from the server. `token` is the raw `Data` iOS delivers in
    /// `didRegisterForRemoteNotificationsWithDeviceToken`; it is hex-encoded
    /// here so no call site has to know the wire shape.
    @discardableResult
    public func register(
        token: Data,
        for blink: BlinkSession,
        environment: String = RemoteDeviceRegistrar.currentEnvironment,
        appVersion: String? = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String
    ) async -> Bool {
        // A real delivery address demands a real bearer for THIS workspace. The
        // DEBUG guest session has an empty token and cannot own one, and the
        // server would refuse it anyway (`_require_owner`); refuse it here too
        // rather than send a request that can only 403.
        guard !blink.token.isEmpty, !blink.workspaceID.isEmpty else { return false }

        let hex = token.map { String(format: "%02x", $0) }.joined()
        guard hex.count >= 8 else { return false }

        let url = baseURL
            .appendingPathComponent("v1/workspaces")
            .appendingPathComponent(blink.workspaceID)
            .appendingPathComponent("devices")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("Bearer \(blink.token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 15

        var payload: [String: Any] = [
            "apns_token": hex,
            "environment": environment,
            "platform": "ios",
        ]
        if let appVersion, !appVersion.isEmpty { payload["app_version"] = appVersion }
        guard let body = try? JSONSerialization.data(withJSONObject: payload) else { return false }
        request.httpBody = body

        do {
            let (_, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else { return false }
            return (200...299).contains(http.statusCode)
        } catch {
            return false
        }
    }
}
