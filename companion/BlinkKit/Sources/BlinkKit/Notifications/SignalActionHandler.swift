import Foundation
import UserNotifications

// What happens when someone taps a button on a notification.
//
// DELIBERATELY NOT PART OF `NotificationScheduler`. S2 requires the same
// buttons to do the same writes whether the notification came from
// `UNUserNotificationCenter` today or from APNs after P15-10, so the handler
// reads only the action identifier and the `SignalContext` in `userInfo`, both
// of which the two paths share byte for byte. Swapping the scheduler does not
// swap this.
//
// THE TWO RULES THIS FILE KEEPS
//
//  * **In the background where possible.** Every writing action is registered
//    WITHOUT `.foreground`, so iOS launches the app in the background and
//    calls this with nobody looking. S2: "Tapping Done should not need the app
//    to open."
//
//  * **A failed write says so.** S2: "if the write fails, the notification's
//    follow-up says so rather than going quiet." And it says which failure it
//    was. A server that ANSWERED no is a different sentence from a server
//    nobody could reach, because after an unreachable server this app does not
//    know whether the write landed, and saying "nothing was recorded" would be
//    as much of a fabrication as saying "saved".

/// Where the bearer comes from when the app was launched into the background
/// by a notification and no screen has run.
///
/// The Keychain item is stored `.afterFirstUnlock` for exactly this
/// (companion/README.md, P15-03).
public protocol SignalSessionProviding: Sendable {
    func session() -> BlinkSession?
}

public struct KeychainSignalSession: SignalSessionProviding {
    private let store: any SessionTokenStore

    public init(store: any SessionTokenStore = KeychainSessionStore()) {
        self.store = store
    }

    public func session() -> BlinkSession? { store.load() }
}

public extension Notification.Name {
    /// Posted after an action wrote successfully, so a Today screen that
    /// happens to be on-screen reconciles with the server instead of holding a
    /// number the server has just changed. It carries no data: the screen
    /// re-reads `/details` rather than trusting a payload passed in memory.
    static let blinkSignalActionWrote = Notification.Name("blink.signal.action.wrote")
}

public struct SignalActionHandler: @unchecked Sendable {
    private let writer: any SignalWriting
    private let sessions: any SignalSessionProviding
    private let center: UNUserNotificationCenter

    public init(
        writer: (any SignalWriting)? = nil,
        sessions: any SignalSessionProviding = KeychainSignalSession(),
        center: UNUserNotificationCenter = .current(),
        baseURL: URL = BlinkAPI.baseURL()
    ) {
        self.writer = writer ?? BlinkSignalClient(baseURL: baseURL)
        self.sessions = sessions
        self.center = center
    }

    /// The single entry point the app delegate forwards to.
    public func handle(actionIdentifier: String, userInfo: [AnyHashable: Any]) async {
        // Tapping the notification body, or dismissing it, writes nothing. The
        // app opening is not an answer to "how did it go?".
        guard actionIdentifier != UNNotificationDefaultActionIdentifier,
              actionIdentifier != UNNotificationDismissActionIdentifier else { return }

        let context = SignalContext.read(from: userInfo) ?? SignalContext()

        switch actionIdentifier {
        case SignalActionID.startTimer, SignalActionID.open:
            // Foreground actions. They write nothing here and claim nothing.
            //
            // WHERE "Start timer" ACTUALLY GOES (P18-05): the app delegate
            // stamps `SignalLaunchRequest` with the block from this same
            // `SignalContext` before forwarding here, and the Today screen
            // opens the focus session for it once it holds a payload that
            // still contains that block. It is not started in the background,
            // because a timer nobody is looking at measures nothing.
            return

        case SignalActionID.done:
            await resolve(.done, context: context)
        case SignalActionID.partly:
            await resolve(.partial, context: context)
        case SignalActionID.skip:
            await resolve(.skipped, context: context)
        case SignalActionID.notTonight:
            // "Not tonight" LOGS A REAL SKIP. Same endpoint, same record, same
            // outcome as tapping Skip on the check-in. It is not a snooze.
            await resolve(.skipped, context: context)

        case SignalActionID.adapt:
            await respond(accept: true, context: context)
        case SignalActionID.leaveIt:
            await respond(accept: false, context: context)

        default:
            notificationLog("action: unrecognised identifier")
        }
    }

    // MARK: The check-in write

    private func resolve(_ outcome: CheckinOutcome, context: SignalContext) async {
        guard let blockID = context.blockID else {
            notificationLog("action: no block to resolve")
            return
        }
        guard let session = sessions.session() else {
            await followUp(signedOut(context))
            return
        }
        do {
            let response = try await writer.resolve(block: blockID, as: outcome, for: session)
            notificationLog("action: resolve landed")
            await followUp(confirmation(for: response, context: context))
            NotificationCenter.default.post(name: .blinkSignalActionWrote, object: nil)
        } catch DetailsError.notSignedIn {
            await followUp(signedOut(context))
        } catch DetailsError.refused {
            await followUp(serverSaidNo(context))
        } catch {
            await followUp(nobodyAnswered(context))
        }
    }

    // MARK: The consent write

    private func respond(accept: Bool, context: SignalContext) async {
        guard let insightID = context.insightID else {
            notificationLog("action: no insight to answer")
            return
        }
        guard let session = sessions.session() else {
            await followUp(signedOut(context))
            return
        }
        do {
            let text = try await writer.respondToInsight(insightID, accept: accept, for: session)
            notificationLog("action: insight answer landed")
            // The server's own sentence about what it changed, or nothing.
            // The device does not narrate a write it did not perform.
            if let text {
                await followUp(text, quiet: true)
            }
            NotificationCenter.default.post(name: .blinkSignalActionWrote, object: nil)
        } catch DetailsError.notSignedIn {
            await followUp(signedOut(context))
        } catch DetailsError.refused {
            await followUp(serverSaidNo(context))
        } catch {
            await followUp(nobodyAnswered(context))
        }
    }

    // MARK: What the follow-up says

    /// The name of the thing, or a phrase that names nothing.
    private func subject(_ context: SignalContext) -> String {
        context.taskTitle ?? "that session"
    }

    /// The quiet confirmation, built from the server's ECHO of what it wrote,
    /// never from the request that was sent (`CheckinResolveResponse` reads
    /// `actual_minutes` and `source` back off the block after the write,
    /// src/api/server.py).
    private func confirmation(
        for response: CheckinResolveResponse,
        context: SignalContext
    ) -> String {
        let name = subject(context)
        switch response.outcome {
        case "done":
            // Self-reported, because that is what a tap is. S5's quieter
            // register, in its own words.
            return "\(name) is logged as done. That one is on your word, and that is fine."
        case "partial":
            // No number, because the server recorded none and this app will
            // not invent how far someone got.
            return "\(name) is logged as partly done."
        default:
            return "\(name) is logged as skipped. No drama, it is just what happened."
        }
    }

    /// The server answered, and the answer was no.
    private func serverSaidNo(_ context: SignalContext) -> String {
        "That did not save. Nothing was recorded for \(subject(context)), so open Blink and I will take it again."
    }

    /// Nobody answered. The write may well have landed, so this says what it
    /// knows and no more.
    private func nobodyAnswered(_ context: SignalContext) -> String {
        "I could not reach your plan, so I do not know whether that saved. Open Blink and I will check on \(subject(context))."
    }

    /// docs/COMPANION_SCREENS.md, "Empty and error states, as a policy".
    private func signedOut(_ context: SignalContext) -> String {
        "You are signed out. Sign in and your answer for \(subject(context)) is still here."
    }

    /// Put the follow-up where the person will see it.
    ///
    /// A confirmation is `passive`: it lands in the list without a banner or a
    /// sound, which is what "a quiet confirmation" means. A failure is
    /// `active`, because a person who thinks they logged something and did not
    /// needs to find that out.
    private func followUp(_ body: String, quiet: Bool = false) async {
        let content = UNMutableNotificationContent()
        content.body = body
        content.threadIdentifier = "blink.signals"
        content.interruptionLevel = quiet ? .passive : .active
        content.sound = quiet ? nil : .default
        let request = UNNotificationRequest(
            identifier: "blink.followup.\(UUID().uuidString)",
            content: content,
            // Nil trigger means now. There is nothing to wait for: the write
            // has already succeeded or already failed.
            trigger: nil
        )
        try? await center.add(request)
    }
}
