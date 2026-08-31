import XCTest
@testable import BlinkKit

// P18-05 — the stamp that carries "start this session" across a cold launch.
//
// The three properties that matter are the ones a wrong implementation gets
// wrong: it fires exactly ONCE (a second consume must not re-open the timer on
// the next foreground), it does not fire when it is stale (nobody wants a
// timer for a session whose moment passed hours ago), and an empty store
// returns nothing rather than a phantom block.
final class SignalLaunchRequestTests: XCTestCase {

    /// Its own defaults suite, so the real app's stamp is never touched and
    /// two tests cannot see each other's writes.
    private var defaults: UserDefaults!
    private let suite = "blink.tests.signal.launch"

    override func setUp() {
        super.setUp()
        UserDefaults.standard.removePersistentDomain(forName: suite)
        defaults = UserDefaults(suiteName: suite)
    }

    override func tearDown() {
        UserDefaults.standard.removePersistentDomain(forName: suite)
        defaults = nil
        super.tearDown()
    }

    func testFreshRequestIsConsumedExactlyOnce() {
        let now = Date()
        SignalLaunchRequest.requestFocus(blockID: "b1", now: now, defaults: defaults)

        let first = SignalLaunchRequest.consumeFocus(now: now, defaults: defaults)
        XCTAssertEqual(first, SignalLaunchRequest.Focus(blockID: "b1"))

        // The second look finds nothing: one tap, one timer.
        XCTAssertNil(SignalLaunchRequest.consumeFocus(now: now, defaults: defaults))
    }

    func testStaleRequestDoesNotFireAndIsCleared() {
        let stamped = Date()
        SignalLaunchRequest.requestFocus(blockID: "b1", now: stamped, defaults: defaults)

        // Well past the window: the moment this was about has gone.
        let later = stamped.addingTimeInterval(600)
        XCTAssertNil(SignalLaunchRequest.consumeFocus(now: later, defaults: defaults))

        // And it left nothing behind that a fresh-looking clock could revive.
        XCTAssertNil(SignalLaunchRequest.consumeFocus(now: stamped, defaults: defaults))
    }

    func testConsumingWithNoRequestReturnsNothing() {
        XCTAssertNil(SignalLaunchRequest.consumeFocus(now: Date(), defaults: defaults))
    }

    /// An id-less notification (the morning brief carries no block) must not
    /// leave a stamp that a later consume reads as an intent.
    func testEmptyBlockIDIsNotAnIntent() {
        SignalLaunchRequest.requestFocus(blockID: "", defaults: defaults)
        XCTAssertNil(SignalLaunchRequest.consumeFocus(defaults: defaults))
    }

    /// The check-in's stamp and this one are separate keys, so neither can fire
    /// the other's surface. P18-04b's hands-free check-in depends on that.
    func testFocusRequestDoesNotSatisfyTheCheckInRequest() {
        SignalLaunchRequest.requestFocus(blockID: "b1", defaults: defaults)
        XCTAssertFalse(CheckInLaunchRequest.consume())
        XCTAssertEqual(SignalLaunchRequest.consumeFocus(defaults: defaults),
                       SignalLaunchRequest.Focus(blockID: "b1"))
    }
}
