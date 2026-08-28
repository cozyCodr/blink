import SwiftUI
import Observation

/// Holds the chosen face and remembers it between launches.
///
/// The web keeps this in `localStorage` as `FocusSettings.face`. Moving it onto
/// the account is P15-08; until then the companion is honest about being local,
/// and `lastSyncedWithServer` stays nil so no screen can claim otherwise.
@Observable
public final class FaceProvider {
    private static let storageKey = "blink.face"

    private let defaults: UserDefaults

    public private(set) var faceID: FaceID {
        didSet { defaults.set(faceID.rawValue, forKey: Self.storageKey) }
    }

    /// When the server last confirmed this choice. Nil means local only.
    public private(set) var lastSyncedWithServer: Date?

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
