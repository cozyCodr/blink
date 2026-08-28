import Foundation

/// Why a read did not produce a payload.
///
/// The distinction between these two is the whole of the degrade-never-
/// fabricate rule. `refused` is something the server SAID. `unreachable` is
/// something nobody said, and the app must not apologise for it or treat it
/// as evidence about the account.
public enum DetailsError: Error, Equatable {
    /// The server answered, and the answer was no. 401 or 403.
    case notSignedIn
    /// The server answered with something this app could not read, or with an
    /// error of its own. Carries the status for the log, never for the copy.
    case refused(status: Int)
    /// The request never got an answer. Offline, a dropped connection, a
    /// server that is not there.
    case unreachable
    /// WE stopped asking: the view went away, or SwiftUI cancelled the
    /// `refreshable` task when the gesture ended. Nobody failed and nothing
    /// was learned, so this must never flip the screen to the offline stamp.
    /// Kept separate from `unreachable` for exactly the reason P15-03 kept
    /// `cancelled` separate from `refused`.
    case cancelled
}

/// The check-in answer S1 can take without leaving the screen.
/// `_CHECKIN_OUTCOME_TO_STATUS` in `src/api/server.py` accepts these three.
public enum CheckinOutcome: String, Sendable {
    case done
    case partial
    case skipped
}

/// What `POST /checkin/resolve` answers with. Decode-only: there is no
/// initialiser, which is part of why a `RecordedOutcome` cannot be forged.
public struct CheckinResolveResponse: Decodable, Sendable, Equatable {
    public let blockID: String
    public let outcome: String
    /// The minutes now ON THE BLOCK, read back after the write.
    public let actualMinutes: Int?
    /// The source now on the block. `null` when nothing was recorded.
    public let source: ActualSource?
    /// How many of today's blocks are still unanswered.
    public let remaining: Int

    enum CodingKeys: String, CodingKey {
        case blockID = "block_id"
        case outcome
        case actualMinutes = "actual_minutes"
        case source
        case remaining
    }

    /// The block status the server wrote, mapped from the outcome it echoed.
    /// `skipped` maps to `missed`, and `missed` is not an outcome anybody
    /// celebrates, which is exactly why this is nil for it.
    var recordedStatus: BlockStatus? {
        switch outcome {
        case "done": return .done
        case "partial": return .partial
        default: return nil
        }
    }
}

/// What `POST /blocks/{id}/log-time` answers with. Decode-only, for the same
/// reason `CheckinResolveResponse` is: it is the only shape a `RecordedOutcome`
/// can be read from, and a decode-only value cannot be forged from a literal.
///
/// `totalMinutes` is the ACCUMULATED measured total the server now holds on the
/// block (`accumulate_timed_minutes`, src/core/progress.py), not the delta this
/// device just sent. It is the number the focus screen shows as "saved", and it
/// is the server's number by construction: the app copies it, never computes it.
public struct LogTimeResponse: Decodable, Sendable, Equatable {
    public let blockID: String
    /// The measured minutes on the block AFTER this write. Server-authoritative.
    public let totalMinutes: Int
    /// The planned span of the block, in minutes.
    public let plannedMinutes: Int
    /// Whether this write asked the server to RESOLVE the block. A progress
    /// write (`false`) accumulates minutes and leaves the status `planned`.
    public let complete: Bool
    /// The block's status after the write. `planned` for a progress write;
    /// `done`/`partial` for a completion, resolved by arithmetic server-side.
    public let blockStatus: BlockStatus
    /// Always `.timer` on this endpoint: these are measured minutes, and a
    /// measured actual beats any later self-report.
    public let source: ActualSource

    enum CodingKeys: String, CodingKey {
        case blockID = "block_id"
        case totalMinutes = "total_minutes"
        case plannedMinutes = "planned_minutes"
        case complete
        case blockStatus = "block_status"
        case source
    }
}

public protocol DetailsReading: Sendable {
    func details(for session: BlinkSession) async throws -> WorkspaceDetails
    func resolve(
        block blockID: String,
        as outcome: CheckinOutcome,
        for session: BlinkSession
    ) async throws -> CheckinResolveResponse
    /// Write one measured stint of timer minutes against a block. `elapsedMinutes`
    /// is the DELTA since the last successful write, because the server
    /// accumulates on top of the timer total it already holds. `complete: true`
    /// asks the server to resolve the block done/partial. See FocusController for
    /// how the delta is kept in step with the server's echoed total so the two
    /// cannot drift.
    func logTime(
        block blockID: String,
        elapsedMinutes: Int,
        complete: Bool,
        for session: BlinkSession
    ) async throws -> LogTimeResponse
}

/// `GET /v1/workspaces/{id}/details` and `POST …/checkin/resolve`, both with
/// the bearer P15-03 minted.
///
/// One store, one truth (docs/COMPANION_ARCHITECTURE.md §5): the resolve here
/// is byte-for-byte the write the web makes, so a Done tapped on the phone is
/// the same record.
public struct BlinkDetailsClient: DetailsReading {
    private let baseURL: URL
    private let urlSession: URLSession

    public init(baseURL: URL = BlinkAPI.baseURL(), urlSession: URLSession = .shared) {
        self.baseURL = baseURL
        self.urlSession = urlSession
    }

    public func details(for session: BlinkSession) async throws -> WorkspaceDetails {
        let url = baseURL
            .appendingPathComponent("v1/workspaces")
            .appendingPathComponent(session.workspaceID)
            .appendingPathComponent("details")
        var request = URLRequest(url: url)
        request.setValue("Bearer \(session.token)", forHTTPHeaderField: "Authorization")
        request.timeoutInterval = 15
        // Freshness is this app's job, not the URL cache's. A stale 200 out of
        // the shared cache would be shown WITHOUT the "as of" stamp, which is
        // the one thing the offline policy forbids.
        request.cachePolicy = .reloadIgnoringLocalCacheData

        let data = try await send(request, label: "details")
        do {
            return try JSONDecoder().decode(WorkspaceDetails.self, from: data)
        } catch {
            detailsLog("details: 200 but undecodable")
            throw DetailsError.refused(status: 200)
        }
    }

    public func resolve(
        block blockID: String,
        as outcome: CheckinOutcome,
        for session: BlinkSession
    ) async throws -> CheckinResolveResponse {
        let url = baseURL
            .appendingPathComponent("v1/workspaces")
            .appendingPathComponent(session.workspaceID)
            .appendingPathComponent("checkin/resolve")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("Bearer \(session.token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 15
        // `source: "reported"` because that is what this is: the user's word.
        // No `actual_minutes`, because the app does not know one and the
        // server's own defaults are honest (done with no number = the planned
        // span; partial with no number stays None rather than a guess,
        // src/api/server.py:1656-1662).
        request.httpBody = try? JSONSerialization.data(withJSONObject: [
            "block_id": blockID,
            "outcome": outcome.rawValue,
            "source": "reported"
        ])

        let data = try await send(request, label: "checkin/resolve")
        do {
            return try JSONDecoder().decode(CheckinResolveResponse.self, from: data)
        } catch {
            detailsLog("checkin/resolve: 200 but undecodable")
            throw DetailsError.refused(status: 200)
        }
    }

    public func logTime(
        block blockID: String,
        elapsedMinutes: Int,
        complete: Bool,
        for session: BlinkSession
    ) async throws -> LogTimeResponse {
        let url = baseURL
            .appendingPathComponent("v1/workspaces")
            .appendingPathComponent(session.workspaceID)
            .appendingPathComponent("blocks")
            .appendingPathComponent(blockID)
            .appendingPathComponent("log-time")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("Bearer \(session.token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 15
        // The field is `elapsed_minutes` (LogTimeRequest, src/api/server.py:246),
        // never below zero. `complete` resolves the block when the session ends.
        request.httpBody = try? JSONSerialization.data(withJSONObject: [
            "elapsed_minutes": max(0, elapsedMinutes),
            "complete": complete
        ])

        let data = try await send(request, label: "log-time")
        do {
            return try JSONDecoder().decode(LogTimeResponse.self, from: data)
        } catch {
            detailsLog("log-time: 200 but undecodable")
            throw DetailsError.refused(status: 200)
        }
    }

    // MARK: The one transport

    private func send(_ request: URLRequest, label: String) async throws -> Data {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await urlSession.data(for: request)
        } catch let error as URLError where error.code == .cancelled {
            throw DetailsError.cancelled
        } catch {
            if Task.isCancelled { throw DetailsError.cancelled }
            detailsLog("\(label): no answer")
            throw DetailsError.unreachable
        }
        guard let http = response as? HTTPURLResponse else {
            throw DetailsError.unreachable
        }
        if http.statusCode == 401 || http.statusCode == 403 {
            detailsLog("\(label): refused, not signed in")
            throw DetailsError.notSignedIn
        }
        guard http.statusCode == 200 else {
            detailsLog("\(label): status \(http.statusCode)")
            throw DetailsError.refused(status: http.statusCode)
        }
        return data
    }
}
