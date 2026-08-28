import Foundation

// The app-side owner of the focus Live Activity: start it, push every state
// change to it from the DEVICE, and end it. No push tokens, no server
// involvement (docs/COMPANION_ARCHITECTURE.md §4, Gap 4).
//
// Guarded by `canImport(ActivityKit)` and by the runtime availability check, so
// a build or a device without ActivityKit simply runs the in-app timer with no
// lock-screen surface, rather than failing.

#if canImport(ActivityKit)
import ActivityKit

@MainActor
public final class FocusLiveActivityController {
    private var activity: Activity<FocusActivityAttributes>?

    public init() {}

    /// Whether the system will let us show a Live Activity at all. False in the
    /// simulator on some OS versions, and whenever the user has turned Live
    /// Activities off for the app. The caller degrades quietly when this is
    /// false: the in-app timer is the source of truth regardless.
    public var isAvailable: Bool {
        ActivityAuthorizationInfo().areActivitiesEnabled
    }

    /// True once a Live Activity is live for this session.
    public var isRunning: Bool { activity != nil }

    public func start(
        blockID: String,
        title: String?,
        face: FaceID = .capsule,
        state: FocusActivityAttributes.ContentState
    ) {
        guard isAvailable, activity == nil else { return }
        let attributes = FocusActivityAttributes(blockID: blockID, title: title, face: face)
        do {
            activity = try Activity.request(
                attributes: attributes,
                content: ActivityContent(state: state, staleDate: nil),
                pushType: nil   // device-driven only; explicitly no push token
            )
            focusLog("live activity started")
        } catch {
            // A refusal here is not a session failure. The timer runs on.
            focusLog("live activity refused to start")
            activity = nil
        }
    }

    public func update(_ state: FocusActivityAttributes.ContentState) {
        guard let activity else { return }
        Task {
            await activity.update(ActivityContent(state: state, staleDate: nil))
        }
    }

    /// End the Activity, showing the final state briefly. `.immediate` when the
    /// user is done and looking at the app; the system clears it shortly after.
    public func end(_ finalState: FocusActivityAttributes.ContentState) {
        guard let activity else { return }
        self.activity = nil
        Task {
            await activity.end(
                ActivityContent(state: finalState, staleDate: nil),
                dismissalPolicy: .default
            )
        }
    }
}
#endif
