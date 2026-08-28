import Foundation

/// Where the API lives.
///
/// One place, so no screen ever builds a URL out of a string literal. The
/// default is production; a launch argument points a simulator run at a local
/// server (`-blinkAPIBaseURL http://localhost:8078`) without a code change.
public enum BlinkAPI {
    public static let productionBaseURL = URL(string: "https://blink.oapps.dev")!

    public static func baseURL(defaults: UserDefaults = .standard) -> URL {
        guard let raw = defaults.string(forKey: "blinkAPIBaseURL"),
              let url = URL(string: raw), url.scheme != nil else {
            return productionBaseURL
        }
        return url
    }
}

/// Reads identity from the API on behalf of a bearer.
public protocol IdentityReading: Sendable {
    func identity(for session: BlinkSession) async throws -> BlinkIdentity
}

public enum IdentityError: Error, Equatable {
    /// The server does not recognise this bearer. It is dead: sign in again.
    case notSignedIn
    /// We could not reach the server or could not read what it said. The
    /// session might be perfectly good, so nothing is discarded on this.
    case unconfirmed
}

/// `GET /v1/session` with `Authorization: Bearer …`, which is the same answer
/// the web reads from its cookie and is composed server-side.
public struct BlinkIdentityClient: IdentityReading {
    private let baseURL: URL
    private let session: URLSession

    public init(baseURL: URL = BlinkAPI.baseURL(), session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    public func identity(for blink: BlinkSession) async throws -> BlinkIdentity {
        var request = URLRequest(url: baseURL.appendingPathComponent("v1/session"))
        request.setValue("Bearer \(blink.token)", forHTTPHeaderField: "Authorization")
        request.timeoutInterval = 15

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw IdentityError.unconfirmed
        }
        guard let http = response as? HTTPURLResponse else {
            throw IdentityError.unconfirmed
        }
        if http.statusCode == 401 || http.statusCode == 403 {
            throw IdentityError.notSignedIn
        }
        guard http.statusCode == 200,
              let body = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else {
            throw IdentityError.unconfirmed
        }
        guard body["signed_in"] as? Bool == true else {
            throw IdentityError.notSignedIn
        }
        return BlinkIdentity(
            workspaceID: body["workspace_id"] as? String ?? blink.workspaceID,
            name: body["name"] as? String,
            email: body["email"] as? String,
            // The greeting is composed server-side from the STORED name. The
            // app shows it or shows nothing; it never writes one itself.
            greeting: body["greeting"] as? String,
            // P15-08 will fill this in. Absent means the face preference is
            // still local only, and no screen may claim otherwise.
            face: (body["face"] as? String).flatMap(FaceID.init(rawValue:))
        )
    }
}
