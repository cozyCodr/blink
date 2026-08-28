import Foundation
import Security

/// Where the bearer lives between launches.
///
/// A protocol because the watch app, the notification service extension and
/// the previews all need one, and only the first two want the real Keychain.
public protocol SessionTokenStore: Sendable {
    func load() -> BlinkSession?
    func save(_ session: BlinkSession) throws
    func clear()
}

public enum KeychainError: Error, Equatable {
    /// The OSStatus the Keychain returned. Never carries the item itself.
    case status(OSStatus)
}

/// The real store: one generic-password item holding the token and the
/// workspace id it binds to.
///
/// **Accessibility** is `.afterFirstUnlock`, not `.whenUnlocked`, because the
/// bearer has to be readable while the phone is locked: P15-07's watch app and
/// P15-05's background notification handlers both read it without anyone
/// looking at the screen. It is deliberately NOT `...ThisDeviceOnly`-relaxed
/// in the other direction either: the item does not sync to iCloud, so a
/// session cannot follow a restore onto another device.
///
/// **The access group** is what lets the watch extension read the same item.
/// It is configuration rather than a constant because this project signs
/// ad-hoc (`CODE_SIGN_IDENTITY = "-"`, no development team), and a keychain
/// sharing entitlement without a matching provisioning profile makes every
/// `SecItemAdd` fail with `errSecMissingEntitlement`. So the group is read
/// from the `BlinkKeychainAccessGroup` Info.plist key, is absent today, and
/// P15-07 sets it alongside the entitlement it needs. Absent means the item
/// simply lives in the app's own group, which is exactly what a phone-only
/// build wants.
public struct KeychainSessionStore: SessionTokenStore {
    private let service: String
    private let account: String
    private let accessGroup: String?

    public init(
        service: String = "dev.oapps.blink.companion.session",
        account: String = "blink.session",
        accessGroup: String? = Bundle.main
            .object(forInfoDictionaryKey: "BlinkKeychainAccessGroup") as? String
    ) {
        self.service = service
        self.account = account
        // An empty string in a plist is not an access group, it is an unset
        // build setting that expanded to nothing.
        self.accessGroup = accessGroup.flatMap { $0.isEmpty ? nil : $0 }
    }

    /// True when the item will be shared with the watch and the extensions.
    /// The debug screen reads this so it can say which it is rather than
    /// implying the sharing already works.
    public var sharesWithExtensions: Bool { accessGroup != nil }

    private var baseQuery: [String: Any] {
        var query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        if let accessGroup {
            query[kSecAttrAccessGroup as String] = accessGroup
        }
        return query
    }

    public func load() -> BlinkSession? {
        var query = baseQuery
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data else { return nil }
        return try? JSONDecoder().decode(BlinkSession.self, from: data)
    }

    public func save(_ session: BlinkSession) throws {
        let data = try JSONEncoder().encode(session)
        // Replace rather than update-or-insert: one item, one identity, and no
        // chance of a stale attribute surviving a re-sign-in.
        SecItemDelete(baseQuery as CFDictionary)
        var attributes = baseQuery
        attributes[kSecValueData as String] = data
        attributes[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        let status = SecItemAdd(attributes as CFDictionary, nil)
        guard status == errSecSuccess else { throw KeychainError.status(status) }
    }

    public func clear() {
        SecItemDelete(baseQuery as CFDictionary)
    }
}

/// Previews and rehearsal only. Never reached by a signed build's sign-in path.
public final class InMemorySessionStore: SessionTokenStore, @unchecked Sendable {
    private let lock = NSLock()
    private var session: BlinkSession?

    public init(session: BlinkSession? = nil) { self.session = session }

    public func load() -> BlinkSession? {
        lock.lock(); defer { lock.unlock() }
        return session
    }

    public func save(_ session: BlinkSession) {
        lock.lock(); defer { lock.unlock() }
        self.session = session
    }

    public func clear() {
        lock.lock(); defer { lock.unlock() }
        session = nil
    }
}
