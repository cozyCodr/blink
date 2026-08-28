import SwiftUI
import Observation

/// Holds the chosen face and remembers it between launches.
///
/// The web keeps this in `localStorage` as `FocusSettings.face` and, since
/// P15-08, on the ACCOUNT (`UserProfile.face`), so every surface wears the
/// same skin. This class is the companion's read-through/write-through point
/// for that field:
///
/// - `UserDefaults` stays the FAST PATH: the app paints the remembered face
///   before any request lands, exactly as the web paints localStorage before
///   first paint.
/// - On load, a face the SERVER holds wins over the local one (`adopt`),
///   because it is the newest pick made on ANY device — the same conflict
///   rule the web applies.
/// - A pick made here goes to the server through `pushToServer`,
///   fire-and-forget. Offline, the pick stays local and honest:
///   `lastSyncedWithServer` only ever moves when the server actually
///   confirmed (degrade never fabricate, .agents/rules/agent-governance.md).
@Observable
public final class FaceProvider {
    private static let storageKey = "blink.face"

    private let defaults: UserDefaults

    public private(set) var faceID: FaceID {
        didSet { defaults.set(faceID.rawValue, forKey: Self.storageKey) }
    }

    /// When the server last confirmed this choice. Nil means local only.
    public private(set) var lastSyncedWithServer: Date?

    /// The write-through seam. The app wires this once it holds a session
    /// (`FaceSyncClient.push`); nil means signed out, and a pick simply stays
    /// local. Returns whether the server confirmed the write.
    @ObservationIgnored public var pushToServer: (@Sendable (FaceID) async -> Bool)?

    @ObservationIgnored private var pushTask: Task<Void, Never>?

    public init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        let stored = defaults.string(forKey: Self.storageKey)
        // capsule is the default face (AGENT.md, planner P10-00).
        self.faceID = stored.flatMap(FaceID.init(rawValue:)) ?? .capsule
    }

    public var tokens: any FaceTokens { Faces.tokens(for: faceID) }

    public func select(_ id: FaceID) {
        guard id != faceID else { return }
        faceID = id
        push(id)
    }

    /// The server told us the account's face. Server wins: it is the newest
    /// pick made on any device, and a pick made HERE is pushed the moment it
    /// happens, so this can only ever replay the user's own latest choice.
    public func adopt(serverFace id: FaceID) {
        lastSyncedWithServer = Date()
        guard id != faceID else { return }
        faceID = id
    }

    private func push(_ id: FaceID) {
        guard let pushToServer else { return }
        pushTask?.cancel()
        pushTask = Task { [weak self] in
            let confirmed = await pushToServer(id)
            guard confirmed, !Task.isCancelled else { return }
            self?.lastSyncedWithServer = Date()
        }
    }
}

// MARK: - Environment

private struct FaceTokensKey: EnvironmentKey {
    static let defaultValue: any FaceTokens = CapsuleFace()
}

public extension EnvironmentValues {
    /// Any view reads the current face with `@Environment(\.face) var face`.
    var face: any FaceTokens {
        get { self[FaceTokensKey.self] }
        set { self[FaceTokensKey.self] = newValue }
    }
}

public extension View {
    /// Puts a face into the environment for this subtree.
    func face(_ tokens: any FaceTokens) -> some View {
        environment(\.face, tokens)
    }
}
