import Foundation

// The Live Activity's Done button, as an App Intent.
//
// A `LiveActivityIntent` runs its `perform()` in the APP's process, which is
// exactly what "the number shown is the number written" needs: the widget can
// offer a Done button, but the write still happens in the app, through the same
// `FocusController.finish()` path the in-app Done uses. The widget itself never
// touches the network and never records a minute.

#if canImport(AppIntents)
import AppIntents

@available(iOS 17.0, *)
public struct EndFocusIntent: LiveActivityIntent {
    public static var title: LocalizedStringResource = "Wrap it up"
    public static var description = IntentDescription("Finish the focus session and save the measured minutes.")

    public init() {}

    public func perform() async throws -> some IntentResult {
        FocusHandoff.requestFinish()
        return .result()
    }
}
#endif
