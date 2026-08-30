import Foundation

// P18-04b — the bridge from "the person tapped the evening check-in
// notification" to "open the app into the hands-free check-in".
//
// The tap is handled in the app delegate, which may run at COLD LAUNCH before
// any screen exists, so the intent cannot be a live in-memory flag on a view.
// It is a single timestamp in UserDefaults: the delegate STAMPS it, and the
// Today screen CONSUMES it once when it next becomes active.
//
// WHY A FRESHNESS WINDOW. A stamp that outlived its moment would ambush someone
// who opened the app hours later for something else. So a consume only counts
// when it is recent; a stale stamp is cleared and ignored. This carries no plan
// data and claims nothing — it is purely "the person just asked to check in".
public enum CheckInLaunchRequest {
    private static let key = "blink.checkin.voice.requestedAt"
    /// How long a tapped intent stays live. Long enough for a cold launch to
    /// finish and Today to appear; short enough that a later, unrelated open
    /// never inherits it.
    private static let window: TimeInterval = 120

    /// The person tapped the evening check-in notification. Record the intent.
    public static func request(now: Date = Date()) {
        UserDefaults.standard.set(now.timeIntervalSince1970, forKey: key)
    }

    /// True at most once per request, and only if it is still fresh. Always
    /// clears the stamp, so a request is honoured exactly once.
    @discardableResult
    public static func consume(now: Date = Date()) -> Bool {
        let defaults = UserDefaults.standard
        let stamp = defaults.double(forKey: key)
        guard stamp > 0 else { return false }
        defaults.removeObject(forKey: key)
        return now.timeIntervalSince1970 - stamp <= window
    }
}
