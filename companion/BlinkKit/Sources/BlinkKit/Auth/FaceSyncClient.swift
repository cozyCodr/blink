import Foundation

/// Writes the chosen face onto the account, so the web follows suit (P15-08).
///
/// `PATCH /v1/workspaces/{ws}/profile/face` with the same bearer every other
/// call carries. Fire-and-forget by design: a failed write leaves the pick
/// local, `FaceProvider.lastSyncedWithServer` stays where it was, and the next
/// signed-in load reconciles. Nothing retries in a loop and nothing nags.
public struct FaceSyncClient: Sendable {
    private let baseURL: URL
    private let urlSession: URLSession

    public init(baseURL: URL = BlinkAPI.baseURL(), urlSession: URLSession = .shared) {
        self.baseURL = baseURL
        self.urlSession = urlSession
    }

    /// True only when the server answered 200. Anything else — unreachable,
    /// refused, unreadable — is "not confirmed", never "probably fine".
    public func push(_ face: FaceID, session: BlinkSession) async -> Bool {
        var request = URLRequest(
            url: baseURL.appendingPathComponent(
                "v1/workspaces/\(session.workspaceID)/profile/face"
            )
        )
        request.httpMethod = "PATCH"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if !session.token.isEmpty {
            request.setValue("Bearer \(session.token)", forHTTPHeaderField: "Authorization")
        }
        request.timeoutInterval = 15
        request.httpBody = try? JSONEncoder().encode(["face": face.rawValue])

        guard let (_, response) = try? await urlSession.data(for: request),
              let http = response as? HTTPURLResponse else {
            return false
        }
        return http.statusCode == 200
    }
}
