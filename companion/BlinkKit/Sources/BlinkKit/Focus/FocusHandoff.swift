import Foundation

/// The one-way channel from the Live Activity's Done button back to the running
/// `FocusController`, and the reason tapping Done on the lock screen still goes
/// through the app's single write path rather than writing from the widget.
///
/// The widget's `EndFocusIntent` is a `LiveActivityIntent`, so its `perform()`
/// runs in the APP's process, not the extension's. It calls `requestFinish()`
/// here, which does two things: posts a same-process notification the live
/// `FocusController` reacts to (finish and write immediately), and sets a
/// durable flag for the case where the app was not resident and has to
/// reconcile on its next launch. Either way the minutes are written by the same
/// `log-time` call the in-app Done uses; the widget never persists anything.
///
/// This needs no app group. `UserDefaults.standard` is the app's own, and the
/// intent runs in the app, so the flag is written and read in one process. That
/// matters because this project signs ad-hoc and cannot carry an app group
/// (see companion/README.md on the same wall the cache and Keychain hit).
public enum FocusHandoff {
    /// Set when Done was tapped and the controller was not there to hear it.
    public static let finishRequestedKey = "blink.focus.finishRequested"
    /// Posted so a resident controller finishes at once.
    public static let finishRequested = Notification.Name("blinkFocusFinishRequested")

    public static func requestFinish() {
        UserDefaults.standard.set(true, forKey: finishRequestedKey)
        NotificationCenter.default.post(name: finishRequested, object: nil)
    }

    /// Read and clear the durable flag. The controller checks this when it
    /// takes over, so a Done tapped while the app was away is not lost.
    public static func consumeFinishRequest() -> Bool {
        let requested = UserDefaults.standard.bool(forKey: finishRequestedKey)
        if requested { UserDefaults.standard.removeObject(forKey: finishRequestedKey) }
        return requested
    }
}
