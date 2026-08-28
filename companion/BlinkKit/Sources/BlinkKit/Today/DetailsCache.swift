import Foundation
import OSLog

/// Diagnostics for the Today read. Same discipline as `signInLog`: it records
/// WHERE something happened, never what it carried. No title, no minute count,
/// no workspace id ever reaches this.
private let detailsLogger = Logger(subsystem: "dev.oapps.blink.companion", category: "today")

public func detailsLog(_ message: String) {
    detailsLogger.notice("\(message, privacy: .public)")
}

/// A payload and the moment THIS DEVICE received it.
///
/// `receivedAt` is the one place the device clock is authoritative, because it
/// is a statement about the device: "this is when I last heard." The stamp S1
/// shows when it is rendering cache is this value and nothing else.
public struct CachedDetails: Codable, Sendable, Equatable {
    public let details: WorkspaceDetails
    public let receivedAt: Date
}

/// Where the last payload lives between launches.
///
/// docs/COMPANION_ARCHITECTURE.md §5: "Cache the last payload in the shared
/// container so widgets and the watch render instantly and then reconcile."
/// The shared container needs an app group, which needs a provisioning
/// profile this ad-hoc-signed project does not have (see companion/README.md
/// on the same problem in the Keychain). So today it is Application Support,
/// which is private to the app; P15-09's widgets are the item that has to move
/// it, and this protocol is the seam that lets them.
public protocol DetailsCaching: Sendable {
    func load(workspaceID: String) -> CachedDetails?
    func save(_ details: WorkspaceDetails, receivedAt: Date, workspaceID: String)
    func clear(workspaceID: String)
}

public struct FileDetailsCache: DetailsCaching {
    private let directory: URL?

    public init(directory: URL? = nil) {
        if let directory {
            self.directory = directory
        } else {
            self.directory = try? FileManager.default.url(
                for: .applicationSupportDirectory,
                in: .userDomainMask,
                appropriateFor: nil,
                create: true
            ).appendingPathComponent("BlinkDetails", isDirectory: true)
        }
    }

    private func url(for workspaceID: String) -> URL? {
        // The workspace id is a path component, so it is reduced to characters
        // that cannot escape the directory. It is also never logged.
        let safe = workspaceID.replacingOccurrences(
            of: "[^A-Za-z0-9_-]", with: "_", options: .regularExpression
        )
        return directory?.appendingPathComponent("\(safe).json")
    }

    public func load(workspaceID: String) -> CachedDetails? {
        guard let url = url(for: workspaceID),
              let data = try? Data(contentsOf: url) else { return nil }
        do {
            return try JSONDecoder().decode(CachedDetails.self, from: data)
        } catch {
            // A cache this build cannot read is a cache with no timestamp we
            // can stand behind. Drop it rather than show anything from it.
            detailsLog("cache: unreadable, discarding")
            try? FileManager.default.removeItem(at: url)
            return nil
        }
    }

    public func save(_ details: WorkspaceDetails, receivedAt: Date, workspaceID: String) {
        guard let url = url(for: workspaceID), let directory else { return }
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let payload = CachedDetails(details: details, receivedAt: receivedAt)
        guard let data = try? JSONEncoder().encode(payload) else { return }
        // Complete-until-first-unlock, matching the Keychain item P15-03
        // stores, so a background refresh can still write.
        try? data.write(to: url, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
    }

    public func clear(workspaceID: String) {
        guard let url = url(for: workspaceID) else { return }
        try? FileManager.default.removeItem(at: url)
    }
}
