import Foundation

// The calendar surface, over the SAME transport `BlinkDetailsClient` built and
// speaking the SAME error vocabulary (`DetailsError`). Two reads, one write,
// no OAuth: the consent flow lives on the web and this app never opens a
// second one.
//
// HONESTY: everything here is decode-only and count-only. The phone learns
// whether a calendar is connected, whether Calendar permission was granted,
// and how many events and busy intervals the last pull produced. It never
// receives an event title, a guest or a location, and it never says the
// calendar is current unless the server said a pull succeeded.

/// `GET /v1/workspaces/{id}/calendar/status`.
public struct CalendarStatus: Decodable, Sendable, Equatable {
    /// A Google account is linked to this workspace.
    public let connected: Bool
    /// The linked account, when the server knows one.
    public let email: String?
    /// The Calendar scope was actually granted. Connected WITHOUT this is the
    /// state where the person unchecked the Calendar box on Google's screen.
    public let calendarGranted: Bool
    /// When a pull last SUCCEEDED, or nil. Nil means nobody may claim the
    /// calendar is up to date.
    public let lastSyncedAt: String?

    enum CodingKeys: String, CodingKey {
        case connected, email
        case calendarGranted = "calendar_granted"
        case lastSyncedAt = "last_synced_at"
    }

    public init(connected: Bool, email: String? = nil,
                calendarGranted: Bool = false, lastSyncedAt: String? = nil) {
        self.connected = connected
        self.email = email
        self.calendarGranted = calendarGranted
        self.lastSyncedAt = lastSyncedAt
    }

    /// The three states worth different words in the UI.
    public enum Standing: Sendable, Equatable {
        case notConnected
        case connectedWithoutCalendarPermission
        case connected
    }

    public var standing: Standing {
        if !connected { return .notConnected }
        return calendarGranted ? .connected : .connectedWithoutCalendarPermission
    }
}

/// What `POST /v1/workspaces/{id}/calendar/sync-google` answers with. Counts
/// only, which is all the server sends and all this app wants.
public struct CalendarSyncResult: Decodable, Sendable, Equatable {
    public let eventsCount: Int
    public let constraintsCreated: Int

    enum CodingKeys: String, CodingKey {
        case eventsCount = "events_count"
        case constraintsCreated = "constraints_created"
    }
}

public protocol CalendarReading: Sendable {
    func calendarStatus(for session: BlinkSession) async throws -> CalendarStatus
    func syncCalendar(for session: BlinkSession) async throws -> CalendarSyncResult
}

extension BlinkDetailsClient: CalendarReading {
    public func calendarStatus(for session: BlinkSession) async throws -> CalendarStatus {
        var request = URLRequest(url: calendarURL("status", session: session))
        request.setValue("Bearer \(session.token)", forHTTPHeaderField: "Authorization")
        request.timeoutInterval = 15
        request.cachePolicy = .reloadIgnoringLocalCacheData

        let data = try await send(request, label: "calendar/status")
        do {
            return try JSONDecoder().decode(CalendarStatus.self, from: data)
        } catch {
            detailsLog("calendar/status: 200 but undecodable")
            throw DetailsError.refused(status: 200)
        }
    }

    public func syncCalendar(for session: BlinkSession) async throws -> CalendarSyncResult {
        var request = URLRequest(url: calendarURL("sync-google", session: session))
        request.httpMethod = "POST"
        request.setValue("Bearer \(session.token)", forHTTPHeaderField: "Authorization")
        request.timeoutInterval = 30   // a Google round trip lives behind this one
        let data = try await send(request, label: "calendar/sync-google")
        do {
            return try JSONDecoder().decode(CalendarSyncResult.self, from: data)
        } catch {
            detailsLog("calendar/sync-google: 200 but undecodable")
            throw DetailsError.refused(status: 200)
        }
    }

    private func calendarURL(_ leaf: String, session: BlinkSession) -> URL {
        baseURL
            .appendingPathComponent("v1/workspaces")
            .appendingPathComponent(session.workspaceID)
            .appendingPathComponent("calendar")
            .appendingPathComponent(leaf)
    }
}

/// What Settings shows about the calendar, and the one action it offers.
///
/// Every line it renders is a fact the server stated. Unknown stays unknown
/// (nothing is shown until a status read answers), a failed sync says so and
/// leaves the previous counts alone, and "not connected" points at the web
/// rather than pretending the phone can ask Google.
@MainActor
@Observable
public final class CalendarController {
    public private(set) var status: CalendarStatus?
    public private(set) var isLoading = false
    public private(set) var isSyncing = false
    /// The counts from the most recent SUCCESSFUL sync started here, or nil.
    public private(set) var lastResult: CalendarSyncResult?
    /// True when the most recent sync attempt did not succeed. Cleared by the
    /// next attempt, never shown alongside a success.
    public private(set) var lastSyncFailed = false
    /// The server answered 401/403: the caller signs out rather than guessing.
    public private(set) var needsSignIn = false

    private let client: any CalendarReading

    public init(client: any CalendarReading = BlinkDetailsClient()) {
        self.client = client
    }

    public func load(session: BlinkSession) async {
        isLoading = true
        defer { isLoading = false }
        do {
            status = try await client.calendarStatus(for: session)
        } catch DetailsError.notSignedIn {
            needsSignIn = true
        } catch {
            // Unreachable or refused: say nothing about the calendar rather
            // than inventing a state for it.
        }
    }

    public func sync(session: BlinkSession) async {
        guard !isSyncing else { return }
        isSyncing = true
        lastSyncFailed = false
        defer { isSyncing = false }
        do {
            lastResult = try await client.syncCalendar(for: session)
            status = try? await client.calendarStatus(for: session)
        } catch DetailsError.notSignedIn {
            needsSignIn = true
        } catch DetailsError.cancelled {
            // Nobody failed, nothing was learned.
        } catch {
            lastResult = nil
            lastSyncFailed = true
        }
    }
}
