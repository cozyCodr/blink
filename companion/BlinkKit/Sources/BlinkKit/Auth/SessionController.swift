import Foundation
import Observation

/// The four states screen S7 has to draw, and nothing else
/// (docs/COMPANION_SCREENS.md, S7).
public enum SignInPhase: Equatable, Sendable {
    /// Reading the Keychain on launch. Brief, and never a sign-in screen flash.
    case checking
    case idle
    /// The browser sheet is up, or the token is round-tripping.
    case authenticating
    /// A real failure, with the copy the screen shows.
    case failed(SignInFailure)
    case signedIn(BlinkIdentity)

    public var isSignedIn: Bool {
        if case .signedIn = self { return true }
        return false
    }
}

/// Owns the session: the Keychain read on launch, the sign-in round trip, and
/// sign-out. One object, so no screen has to know where the token lives.
///
/// Nothing here decides that a beat is warranted. It publishes `phase`; the
/// screen maps a phase to an emotion, and every one of those mappings is
/// grounded in something that actually happened.
@MainActor
@Observable
public final class SessionController {
    public private(set) var phase: SignInPhase = .checking
    /// True when the account came back without the Calendar scope. Sign-in
    /// still worked; settings can offer a reconnect later.
    public private(set) var calendarGranted = true

    @ObservationIgnored private let store: any SessionTokenStore
    @ObservationIgnored private let runner: any NativeSignInRunner
    @ObservationIgnored private let identities: any IdentityReading
    @ObservationIgnored private let baseURL: URL

    /// The live session, for the API calls P15-04 will make.
    public private(set) var session: BlinkSession?

    public init(
        store: any SessionTokenStore = KeychainSessionStore(),
        // Nil rather than a default value: `WebAuthenticationSignIn` is
        // main-actor isolated and a default argument is evaluated outside the
        // initializer's isolation, so it has to be built in the body.
        runner: (any NativeSignInRunner)? = nil,
        identities: (any IdentityReading)? = nil,
        baseURL: URL = BlinkAPI.baseURL()
    ) {
        self.store = store
        self.runner = runner ?? WebAuthenticationSignIn()
        self.baseURL = baseURL
        self.identities = identities ?? BlinkIdentityClient(baseURL: baseURL)
    }

    /// Launch: is there a stored bearer, and does the server still honour it?
    ///
    /// A server that refuses the token drops it. A server we cannot reach does
    /// NOT: an unreachable network is not evidence that a session died, and
    /// throwing a good session away on a bad train ride would be fabricating a
    /// verdict. The stored identity is enough to open the app; P15-04's Today
    /// screen is the thing that has to be honest about stale data.
    public func restore() async {
        guard let stored = store.load() else {
            phase = .idle
            return
        }
        session = stored
        do {
            let identity = try await identities.identity(for: stored)
            phase = .signedIn(identity)
        } catch IdentityError.notSignedIn {
            store.clear()
            session = nil
            phase = .idle
        } catch {
            // Unconfirmed. Keep the session, open the app, say nothing untrue.
            phase = .signedIn(BlinkIdentity(workspaceID: stored.workspaceID))
        }
    }

    /// The whole round trip: Safari-class browser, bearer into the Keychain,
    /// identity from `/v1/session`.
    public func signIn() async {
        phase = .authenticating
        signInLog("stage: starting, baseURL host=\(baseURL.host ?? "nil")")
        let blink: BlinkSession
        do {
            blink = try await runner.signIn(baseURL: baseURL)
        } catch let failure as SignInFailure {
            // A cancel is not a failure to report. Straight back to the button.
            signInLog("stage: browser round trip FAILED: \(String(describing: failure))")
            phase = failure == .cancelled ? .idle : .failed(failure)
            return
        } catch {
            signInLog("stage: browser round trip threw: \(String(describing: error))")
            phase = .failed(.unconfirmed)
            return
        }
        signInLog("stage: browser round trip OK, have a session")

        do {
            try store.save(blink)
        } catch {
            // The sign-in worked but we cannot keep it, so it will not survive
            // a relaunch. Say that rather than pretend it stuck.
            signInLog("stage: KEYCHAIN SAVE FAILED: \(String(describing: error))")
            phase = .failed(.unconfirmed)
            return
        }
        signInLog("stage: keychain save OK")
        session = blink

        do {
            phase = .signedIn(try await identities.identity(for: blink))
            signInLog("stage: identity OK, signed in")
        } catch IdentityError.notSignedIn {
            signInLog("stage: server REJECTED the bearer at /v1/session")
            store.clear()
            session = nil
            phase = .failed(.refused(reason: "session_rejected"))
        } catch {
            // The token is minted and stored and the server signed it. We just
            // could not read the profile yet, so in we go with no greeting.
            signInLog("stage: identity unconfirmed (\(String(describing: error))), entering anyway")
            phase = .signedIn(BlinkIdentity(workspaceID: blink.workspaceID))
        }
    }

    /// Sign out: forget the bearer. The workspace and everything in it stay
    /// exactly where they are, ready for the next sign-in.
    public func signOut() {
        store.clear()
        session = nil
        phase = .idle
    }
}
