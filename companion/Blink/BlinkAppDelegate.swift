import UIKit
import UserNotifications
import BlinkKit

/// The ten lines iOS insists on, and nothing else.
///
/// A notification action can launch this app in the BACKGROUND, before any
/// scene exists and before any view has run, which is exactly what S2 asks
/// for: "Tapping Done should not need the app to open." The system requires
/// the delegate to be set before `didFinishLaunchingWithOptions` returns, so
/// it is set here rather than in a `.task`.
///
/// All the behaviour lives in `SignalActionHandler`, in BlinkKit. This file
/// forwards and does not decide, so P15-10's remote notifications land in the
/// identical handler without a line here changing.
final class BlinkAppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    /// Lazy, because `didFinishLaunchingWithOptions` has to run first: it is
    /// where the DEBUG launch arguments are captured, and a background launch
    /// carries none of them.
    private lazy var handler = SignalActionHandler(
        sessions: CompanionSignalSession(),
        baseURL: CompanionSignalSession.effectiveBaseURL()
    )

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        #if DEBUG
        CompanionSignalSession.rememberDebugLaunchArguments()
        #endif
        // If notifications are already granted, get (or refresh) the APNs token
        // now. Registering is idempotent and cheap: iOS returns a cached token
        // straight away, and a rotated one arrives through the same callback.
        Task { await Self.registerForRemoteNotificationsIfAuthorised() }
        return true
    }

    /// Coming to the foreground. A token can rotate while the app was away, and
    /// a first grant made on a previous run only becomes registrable now; both
    /// are covered by re-registering here, which the system deduplicates.
    func applicationDidBecomeActive(_ application: UIApplication) {
        Task { await Self.registerForRemoteNotificationsIfAuthorised() }
    }

    /// Only ask APNs for a token once the person has actually said yes.
    /// Registering without authorisation would fail quietly and pointlessly;
    /// provisional counts, because a provisional grant can still deliver.
    @MainActor
    private static func registerForRemoteNotificationsIfAuthorised() async {
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        switch settings.authorizationStatus {
        case .authorized, .provisional:
            UIApplication.shared.registerForRemoteNotifications()
        default:
            return
        }
    }

    /// APNs handed us a token. Forward it to the server so the push sweep has a
    /// delivery address (P15-10). A missing or guest session has no address to
    /// own, so nothing is sent — the token is not lost, the next launch re-runs
    /// this with the same token once a real session exists.
    func application(
        _ application: UIApplication,
        didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data
    ) {
        guard let blink = CompanionSignalSession().session(), !blink.token.isEmpty else { return }
        let registrar = RemoteDeviceRegistrar(baseURL: CompanionSignalSession.effectiveBaseURL())
        Task {
            let ok = await registrar.register(token: deviceToken, for: blink)
            print("[push] device registration \(ok ? "accepted" : "not accepted") " +
                  "env=\(RemoteDeviceRegistrar.currentEnvironment)")
        }
    }

    /// Registration failed (no entitlement, no network, Simulator without a
    /// paired push environment). Counts and a reason only, never the token.
    func application(
        _ application: UIApplication,
        didFailToRegisterForRemoteNotificationsWithError error: Error
    ) {
        print("[push] remote registration failed: \(error.localizedDescription)")
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse
    ) async {
        await handler.handle(
            actionIdentifier: response.actionIdentifier,
            userInfo: response.notification.request.content.userInfo
        )
    }

    /// A signal that arrives while the app is open still shows. The whole
    /// point of the surface is that it reaches someone; hiding it because they
    /// happen to be looking at Today would mean the check-in silently never
    /// appeared.
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        [.banner, .list, .sound]
    }
}

/// Where a background handler finds the bearer.
///
/// The Keychain first, because that is the real session. The DEBUG guest
/// workspace second, for the same reason `AppRoot` has that door: several of
/// these states need real server data to exist, and signing in as the user is
/// not something this project can do. A `u_` id is refused there and refused
/// here.
struct CompanionSignalSession: SignalSessionProviding {
    func session() -> BlinkSession? {
        if let real = KeychainSignalSession().session() { return real }
        #if DEBUG
        if let workspace = debugWorkspace(), !workspace.hasPrefix("u_") {
            return BlinkSession(token: "", workspaceID: workspace)
        }
        #endif
        return nil
    }

    /// The API the background handler talks to.
    ///
    /// In a shipping build this is simply `BlinkAPI.baseURL()`. In DEBUG it
    /// also honours a remembered `-blinkAPIBaseURL`, for the reason below.
    static func effectiveBaseURL() -> URL {
        #if DEBUG
        if let raw = UserDefaults.standard.string(forKey: persistedBaseURLKey),
           let url = URL(string: raw), url.scheme != nil {
            return url
        }
        #endif
        return BlinkAPI.baseURL()
    }

    #if DEBUG
    /// DEBUG SCAFFOLDING. A launch ARGUMENT lives in `NSArgumentDomain` and
    /// only for the launch that carried it. A notification action launches the
    /// app in the BACKGROUND with no arguments at all, so without this the
    /// debug workspace and the local server URL both vanish exactly when the
    /// background handler needs them. Remembering them on the foreground
    /// launch is the smallest fix, and there is no such problem in the shipping
    /// path, where the bearer is in the Keychain and the URL is production.
    private static let persistedWorkspaceKey = "blink.debug.workspace"
    private static let persistedBaseURLKey = "blink.debug.baseURL"

    static func rememberDebugLaunchArguments() {
        let defaults = UserDefaults.standard
        if let workspace = defaults.string(forKey: "blinkDebugWorkspace"), !workspace.isEmpty {
            defaults.set(workspace, forKey: persistedWorkspaceKey)
        }
        if let raw = defaults.string(forKey: "blinkAPIBaseURL"), !raw.isEmpty {
            defaults.set(raw, forKey: persistedBaseURLKey)
        }
    }

    private func debugWorkspace() -> String? {
        UserDefaults.standard.string(forKey: "blinkDebugWorkspace")
            ?? UserDefaults.standard.string(forKey: Self.persistedWorkspaceKey)
    }
    #endif
}
