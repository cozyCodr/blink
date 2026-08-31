import Foundation

// P18-05 — the bridge from "the person tapped Start timer on a nudge" (or
// tapped the nudge's body) to "open the app on that session's timer".
//
// The same discipline as `CheckInLaunchRequest`, and deliberately a SEPARATE
// stamp: the check-in intent carries nothing and starts a conversation, this
// one carries a block and opens a timer, and folding them together would make
// each able to fire the other's surface.
//
// The tap is handled in the app delegate, which may run at COLD LAUNCH before
// any screen exists, so the intent cannot be a live in-memory flag on a view.
// It is a block id plus a timestamp in UserDefaults: the delegate STAMPS it,
// and the Today screen CONSUMES it once when it next has a payload to match it
// against.
//
// WHY A FRESHNESS WINDOW. A stamp that outlived its moment would ambush someone
// who opened the app hours later for something else, and a session's moment is
// exactly the thing that passes. So a consume only counts when it is recent; a
// stale stamp is cleared and ignored.
//
// WHAT THIS IS NOT. It is not a claim that the session exists, is startable, or
// is still today's. It carries an id the notification was built with and
// nothing else. Whether that block is really there is the server's answer, read
// from the payload at the moment of the consume, and when the answer is no the
// screen does nothing at all.
public enum SignalLaunchRequest {
    /// The intent, once it has been read back out.
    public struct Focus: Equatable, Sendable {
        /// The block the notification was about. Not yet checked against
        /// anything: the caller matches it, or drops it.
        public let blockID: String

        public init(blockID: String) {
            self.blockID = blockID
        }
    }

    private static let blockKey = "blink.signal.focus.blockID"
    private static let stampKey = "blink.signal.focus.requestedAt"
    /// How long a tapped intent stays live. Long enough for a cold launch to
    /// finish and the first payload to land; short enough that a later,
    /// unrelated open never inherits it. The same window the check-in uses.
    private static let window: TimeInterval = 120

    /// The person asked to start this session from a notification. Record the
    /// intent. An empty id is not an intent and is not stored.
    public static func requestFocus(
        blockID: String,
        now: Date = Date(),
        defaults: UserDefaults = .standard
    ) {
        guard !blockID.isEmpty else { return }
        defaults.set(blockID, forKey: blockKey)
        defaults.set(now.timeIntervalSince1970, forKey: stampKey)
    }

    /// The requested block at most once per request, and only if it is still
    /// fresh. Always clears the stamp, so a request is honoured exactly once
    /// whether or not the caller can act on it.
    @discardableResult
    public static func consumeFocus(
        now: Date = Date(),
        defaults: UserDefaults = .standard
    ) -> Focus? {
        let stamp = defaults.double(forKey: stampKey)
        let blockID = defaults.string(forKey: blockKey)
        guard stamp > 0, let blockID, !blockID.isEmpty else {
            // Nothing pending, or a half-written stamp. Either way, leave
            // nothing behind for a later launch to find.
            clear(defaults)
            return nil
        }
        clear(defaults)
        guard now.timeIntervalSince1970 - stamp <= window else { return nil }
        return Focus(blockID: blockID)
    }

    private static func clear(_ defaults: UserDefaults) {
        defaults.removeObject(forKey: blockKey)
        defaults.removeObject(forKey: stampKey)
    }
}
