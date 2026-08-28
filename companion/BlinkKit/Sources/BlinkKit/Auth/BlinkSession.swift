import Foundation

// The identity the companion holds, and nothing more.
//
// The app never touches Google. It opens Blink's own `/oauth/connect` in a
// Safari-class browser, Google returns to Blink's already-registered https
// callback, and the server hands back a Blink bearer over the custom scheme.
// So a compromised device leaks a revocable Blink session, never the user's
// Google account (docs/COMPANION_ARCHITECTURE.md §4, Gap 1).

/// What the app stores after a successful sign-in.
public struct BlinkSession: Sendable, Equatable, Codable {
    /// The bearer, sent as `Authorization: Bearer <token>`. Signed by the
    /// server with the same HMAC and the same secret as the web's cookie.
    public let token: String
    /// The `u_` workspace every API call addresses. The server derives it from
    /// the Google subject; the app is only told the result.
    public let workspaceID: String

    public init(token: String, workspaceID: String) {
        self.token = token
        self.workspaceID = workspaceID
    }
}

/// What the profile will carry once P15-08 puts the face on the account.
///
/// The field exists here so the Today screen and the face picker have a shape
/// to read, and `faceIsSynced` is false everywhere until that item ships. The
/// app must not claim a preference is synced when it is not
/// (.agents/rules/agent-governance.md, degrade never fabricate).
public struct BlinkIdentity: Sendable, Equatable {
    public let workspaceID: String
    /// The verified name, when Google supplied one and the server stored it.
    /// Nil means no name, which means no greeting: never an invented one.
    public let name: String?
    public let email: String?
    /// One warm line composed SERVER-side from the stored name. Nil when there
    /// is no stored name.
    public let greeting: String?
    /// The account's face, once P15-08 syncs it. Nil today, and
    /// `faceIsSynced` says so plainly.
    public let face: FaceID?

    public var faceIsSynced: Bool { face != nil }

    public init(
        workspaceID: String,
        name: String? = nil,
        email: String? = nil,
        greeting: String? = nil,
        face: FaceID? = nil
    ) {
        self.workspaceID = workspaceID
        self.name = name
        self.email = email
        self.greeting = greeting
        self.face = face
    }
}

/// What `/oauth/connect` can hand back over `blink://auth`.
public enum NativeCallback: Sendable, Equatable {
    case success(BlinkSession, calendarGranted: Bool, state: String?)
    /// The server said, in as many words, that it could not sign this person
    /// in. `reason` is its short machine token, never shown to the user.
    case failure(reason: String, state: String?)
}

public enum NativeCallbackError: Error, Equatable {
    /// Not the scheme and host we asked to come back on.
    case wrongDestination
    /// A reply carrying neither a token nor an error is not a reply we can
    /// act on, and guessing which it meant would be fabricating an outcome.
    case unreadable
}

public enum BlinkAuth {
    /// The one custom-scheme URL the server will redirect a session to. It is
    /// allow-listed server-side too (`blink_auth.NATIVE_REDIRECTS`); this
    /// constant only has to agree with it.
    public static let nativeRedirect = "blink://auth"
    public static let callbackScheme = "blink"
    public static let callbackHost = "auth"

    /// The URL `ASWebAuthenticationSession` opens. Note what is NOT here: no
    /// client id, no client secret, no scopes, no PKCE. The app asks Blink to
    /// sign it in and Blink owns the whole conversation with Google.
    public static func connectURL(baseURL: URL, state: String) -> URL {
        var components = URLComponents(
            url: baseURL.appendingPathComponent("oauth/connect"),
            resolvingAgainstBaseURL: false
        )!
        components.queryItems = [
            URLQueryItem(name: "native", value: nativeRedirect),
            URLQueryItem(name: "state", value: state),
        ]
        return components.url!
    }

    /// A correlator the app can check on the way back. The server echoes it
    /// untouched, so a reply that does not match the request is discarded.
    /// url-safe characters only, which is what the server accepts.
    public static func makeState() -> String {
        UUID().uuidString.replacingOccurrences(of: "-", with: "")
    }

    /// Read the reply. Pure, so the whole contract is testable without a
    /// browser, a server, or a simulator.
    public static func readCallback(_ url: URL) throws -> NativeCallback {
        guard url.scheme?.lowercased() == callbackScheme,
              url.host?.lowercased() == callbackHost else {
            throw NativeCallbackError.wrongDestination
        }
        let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?
            .queryItems ?? []
        func value(_ name: String) -> String? {
            items.first { $0.name == name }?.value.flatMap { $0.isEmpty ? nil : $0 }
        }
        let state = value("state")
        if let reason = value("error") {
            return .failure(reason: reason, state: state)
        }
        guard let token = value("token"), let workspace = value("ws") else {
            throw NativeCallbackError.unreadable
        }
        return .success(
            BlinkSession(token: token, workspaceID: workspace),
            // The server flags a missing Calendar box explicitly. Absent flag
            // means granted; the app never assumes the happier reading.
            calendarGranted: value("calendar") != "missing_scope",
            state: state
        )
    }
}
