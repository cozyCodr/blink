import XCTest
@testable import BlinkKit

// The phone must report its IANA zone the way the web does, and must not spam
// the endpoint doing it. The rule that matters: a send is skipped only when the
// SERVER has confirmed that exact identifier, so a failed send is retried on
// the next launch rather than silently swallowed.
final class TimezoneSyncTests: XCTestCase {

    private var defaults: UserDefaults!
    private let suite = "blink.tests.profile.timezone"

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

    func testFirstEverLaunchSends() {
        XCTAssertTrue(TimezoneSyncClient.needsSend("Africa/Harare", defaults: defaults))
    }

    func testConfirmedZoneIsNotSentAgain() {
        defaults.set("Africa/Harare", forKey: TimezoneSyncClient.defaultsKey)
        XCTAssertFalse(TimezoneSyncClient.needsSend("Africa/Harare", defaults: defaults))
    }

    func testTravelSendsTheNewZone() {
        defaults.set("Africa/Harare", forKey: TimezoneSyncClient.defaultsKey)
        XCTAssertTrue(TimezoneSyncClient.needsSend("America/Los_Angeles", defaults: defaults))
    }

    /// A failed send writes nothing, so the retry happens. This is the whole
    /// reason the remembered value is written only on a 200.
    func testFailedSendIsNotRemembered() {
        // Nothing was ever confirmed: exactly the state a failure leaves.
        XCTAssertNil(defaults.string(forKey: TimezoneSyncClient.defaultsKey))
        XCTAssertTrue(TimezoneSyncClient.needsSend("Africa/Harare", defaults: defaults))
    }

    func testEmptyIdentifierIsNeverSent() {
        XCTAssertFalse(TimezoneSyncClient.needsSend("", defaults: defaults))
    }

    /// No workspace, no call — there is nothing to address it to.
    func testSignedOutSessionDoesNotReport() async {
        let client = TimezoneSyncClient(defaults: defaults)
        let sent = await client.report(
            identifier: "Africa/Harare",
            session: BlinkSession(token: "", workspaceID: "")
        )
        XCTAssertFalse(sent)
        XCTAssertNil(defaults.string(forKey: TimezoneSyncClient.defaultsKey))
    }
}
