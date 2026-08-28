import AuthenticationServices
import Foundation
import OSLog
import UIKit

/// Diagnostics for the sign-in round trip. Deliberately narrow: it records
/// WHERE the flow stopped, never WHAT was carried. A token, an auth code and a
/// workspace id must never reach a log, so nothing here prints a query value.
private let signInLogger = Logger(subsystem: "dev.oapps.blink.companion", category: "signin")

/// `.notice`, not `.debug`: debug-level entries are not persisted to the log
/// store, so they cannot be read back after the fact with `log show`. This has
/// to survive the round trip to be worth anything.
public func signInLog(_ message: String) {
    signInLogger.notice("\(message, privacy: .public)")
}

/// Why a sign-in did not finish. The distinction is not pedantry: it decides
/// whether the face is allowed to apologise.
///
/// `.agents/rules/frontend-standards.md` (the truthfulness rule): `sorry` means
/// the request actually failed. So only `.refused` and `.unavailable`, which
/// are the server saying no in as many words, may fire it. `.cancelled` is the
/// user changing their mind, and `.unconfirmed` is a round trip that never came
/// back, where nobody knows what happened and the app must not pretend it does.
public enum SignInFailure: Error, Equatable, Sendable {
    /// The person closed the browser sheet. Not an error, and not an apology.
    case cancelled
    /// The server refused, and said so. Carries its short machine token.
    case refused(reason: String)
    /// Sign-in is not configured on this server (no session secret).
    case unavailable
    /// The round trip never completed, or came back unreadable. We do not know
    /// whether it worked, so the app claims nothing either way.
    case unconfirmed

    /// Whether the face may apologise for this.
    public var isConfirmedRejection: Bool {
        switch self {
        case .refused, .unavailable: return true
        case .cancelled, .unconfirmed: return false
        }
    }
}

/// Opens the sign-in flow in a Safari-class browser and returns the session.
public protocol NativeSignInRunner: Sendable {
    @MainActor
    func signIn(baseURL: URL) async throws -> BlinkSession
}

/// The real one: `ASWebAuthenticationSession`.
///
/// Safari-class, which is what satisfies Google's rule against OAuth in
/// embedded webviews. A hand-rolled `WKWebView` gets rejected, and would also
/// mean the app could read the user's Google password, which is exactly what
/// this design exists to avoid.
///
/// `prefersEphemeralWebBrowserSession` stays false so someone already signed
/// into Google on this phone gets a one-tap consent rather than a password box.
@MainActor
public final class WebAuthenticationSignIn: NSObject, NativeSignInRunner {
    private var session: ASWebAuthenticationSession?

    public override init() { super.init() }

    public func signIn(baseURL: URL) async throws -> BlinkSession {
        let state = BlinkAuth.makeState()
        let url = BlinkAuth.connectURL(baseURL: baseURL, state: state)

        let callback: URL = try await withCheckedThrowingContinuation { continuation in
            // The completion is shared by both initialisers below.
            let handler: (URL?, Error?) -> Void = { callbackURL, error in
                if let callbackURL {
                    signInLog("callback received: scheme=\(callbackURL.scheme ?? "nil") host=\(callbackURL.host ?? "nil")")
                    continuation.resume(returning: callbackURL)
                } else if let error = error as? ASWebAuthenticationSessionError,
                          error.code == .canceledLogin {
                    signInLog("session cancelled by the person")
                    continuation.resume(throwing: SignInFailure.cancelled)
                } else {
                    // Something went wrong in the browser and we never saw a
                    // reply. That is unconfirmed, not a rejection.
                    signInLog("session ended with no callback, error=\(String(describing: error))")
                    continuation.resume(throwing: SignInFailure.unconfirmed)
                }
            }
            // `init(url:callbackURLScheme:)` was deprecated in iOS 17.4 in favour
            // of the typed `callback:` form, and on iOS 18+ the old one does not
            // reliably match a custom scheme any more: the browser dismisses on
            // the redirect but the completion never receives the URL, which
            // presents as a sign-in that silently "did not complete". Use the
            // modern initialiser wherever it exists.
            let session: ASWebAuthenticationSession
            if #available(iOS 17.4, *) {
                session = ASWebAuthenticationSession(
                    url: url,
                    callback: .customScheme(BlinkAuth.callbackScheme),
                    completionHandler: handler
                )
            } else {
                session = ASWebAuthenticationSession(
                    url: url,
                    callbackURLScheme: BlinkAuth.callbackScheme,
                    completionHandler: handler
                )
            }
            session.presentationContextProvider = self
            session.prefersEphemeralWebBrowserSession = false
            self.session = session
            guard session.start() else {
                continuation.resume(throwing: SignInFailure.unconfirmed)
                return
            }
        }
        session = nil

        let reply: NativeCallback
        do {
            reply = try BlinkAuth.readCallback(callback)
        } catch {
            signInLog("callback unreadable: \(String(describing: error))")
            throw SignInFailure.unconfirmed
        }
        switch reply {
        case .failure(let reason, let echoed):
            guard echoed == state else {
                signInLog("correlator mismatch on a failure reply")
                throw SignInFailure.unconfirmed
            }
            signInLog("server refused: \(reason)")
            if reason == "unavailable" { throw SignInFailure.unavailable }
            throw SignInFailure.refused(reason: reason)
        case .success(let session, _, let echoed):
            // The correlator has to match the request this app made. A reply
            // for someone else's request is not an outcome we can act on.
            guard echoed == state else {
                signInLog("correlator mismatch: reply is not for this request")
                throw SignInFailure.unconfirmed
            }
            signInLog("sign-in succeeded")
            return session
        }
    }
}

extension WebAuthenticationSignIn: ASWebAuthenticationPresentationContextProviding {
    public func presentationAnchor(
        for session: ASWebAuthenticationSession
    ) -> ASPresentationAnchor {
        UIApplication.shared.connectedScenes
            .compactMap { $0 as? UIWindowScene }
            .flatMap(\.windows)
            .first { $0.isKeyWindow }
            ?? ASPresentationAnchor()
    }
}
