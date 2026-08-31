import XCTest
@testable import BlinkKit

// P18-06 — the spoken loop's state machine, tested without a microphone.
//
// The loop drives three parts through protocols (`VoiceLoopEar`,
// `VoiceLoopMouth`, `VoiceLoopSink`), which is exactly what lets these tests
// stand in for the mic, the speaker and the server and assert the ORDER: a tap
// opens the mic, silence sends nothing, a finished reply reopens the mic, and
// every exit is clean.
//
// The load-bearing test is `nothingHeardIsEverPutOnScreen`: what the recognizer
// heard goes to the server and to nowhere else. The user's whole complaint was
// seeing their own words with the recognizer's mistakes in them, so the loop
// must have no path that hands a transcript to a view.

@MainActor
private final class FakeEar: VoiceLoopEar {
    var limitationLine: String?
    var onAutoSettle: ((String) -> Void)?
    var onAutoUnavailable: (() -> Void)?
    private(set) var listenCount = 0
    private(set) var cancelCount = 0

    func beginListening() { listenCount += 1 }
    func cancelListening() { cancelCount += 1 }

    /// The mic settled on words (or on nothing, which it must ignore).
    func settle(_ text: String) { onAutoSettle?(text) }
    func fail() { onAutoUnavailable?() }
}

@MainActor
private final class FakeMouth: VoiceLoopMouth {
    var onFinished: (() -> Void)?
    var onUnavailable: (() -> Void)?
    private(set) var spoken: [String] = []
    private(set) var stopCount = 0

    func speak(_ text: String, session: BlinkSession, force: Bool) { spoken.append(text) }
    func stop() { stopCount += 1 }

    /// Playback reached its end.
    func finish() { onFinished?() }
    func unavailable() { onUnavailable?() }
}

@MainActor
private final class FakeSink: VoiceLoopSink {
    private(set) var sent: [String] = []
    func sendMessage(_ text: String) async { sent.append(text) }
}

@MainActor
final class VoiceLoopTests: XCTestCase {

    private var ear = FakeEar()
    private var mouth = FakeMouth()
    private var sink = FakeSink()
    private var loop = VoiceLoop()
    private let session = BlinkSession(token: "t", workspaceID: "u_test")

    override func setUp() async throws {
        ear = FakeEar()
        mouth = FakeMouth()
        sink = FakeSink()
        loop = VoiceLoop()
        loop.configure(voice: mouth, capture: ear, composer: sink, session: session)
    }

    /// The loop's sends go out on a Task; let it land before asserting.
    private func settleTasks() async {
        for _ in 0..<4 { await Task.yield() }
    }

    // MARK: The mic starts a conversation (P18-06)

    func testTappingTheMicListensImmediatelyWithNoOpener() async {
        loop.startConversation()
        await settleTasks()

        XCTAssertEqual(loop.phase, .listening)
        XCTAssertEqual(loop.mode, .conversation)
        XCTAssertEqual(ear.listenCount, 1, "the mic opens on the tap, not after a question")
        XCTAssertTrue(sink.sent.isEmpty, "the person opened, so nothing is composed for them")
        XCTAssertTrue(mouth.spoken.isEmpty)
    }

    func testEmptySpeechNeverSends() async {
        loop.startConversation()
        ear.settle("")
        ear.settle("   ")
        await settleTasks()

        XCTAssertTrue(sink.sent.isEmpty, "silence must never become a turn")
        XCTAssertEqual(loop.phase, .listening, "and the loop keeps waiting on real words")
    }

    func testNothingHeardIsEverPutOnScreen() async {
        loop.startConversation()
        ear.settle("move the writing block to four")
        await settleTasks()

        // The ONLY destination for heard speech is the server.
        XCTAssertEqual(sink.sent, ["move the writing block to four"])
        XCTAssertEqual(loop.phase, .sending)
        // Nothing the loop exposes carries the words: its whole public surface
        // is a phase, a mode, and (only on a failure) one honest line.
        XCTAssertNil(loop.fellBackLine)
    }

    func testAFinishedReplyReopensTheMic() async {
        loop.startConversation()
        ear.settle("what is left today")
        await settleTasks()

        loop.turnCompleted(reply: "Two sessions.", refused: false,
                           unreachable: false, hasQuestion: false)
        XCTAssertEqual(loop.phase, .speaking)
        XCTAssertEqual(mouth.spoken, ["Two sessions."])

        mouth.finish()
        XCTAssertEqual(loop.phase, .listening)
        XCTAssertEqual(ear.listenCount, 2, "the loop goes round without a tap")
    }

    func testStopExitsCleanly() async {
        loop.startConversation()
        loop.stop()

        XCTAssertEqual(loop.phase, .off)
        XCTAssertFalse(loop.isActive)
        XCTAssertEqual(ear.cancelCount, 1)
        // Two: the tap that opened the loop cut any reply still playing, and
        // the exit cuts again. Both are the same "an interrupt is what you do
        // in order to speak" rule.
        XCTAssertEqual(mouth.stopCount, 2)
        XCTAssertNil(loop.fellBackLine, "a deliberate Done explains nothing")

        // A settle that lands after the exit is ignored, not sent.
        ear.settle("too late")
        await settleTasks()
        XCTAssertTrue(sink.sent.isEmpty)
    }

    // MARK: Interruption

    func testTappingTheMicWhileBlinkSpeaksCutsTheReplyAndListens() async {
        loop.startConversation()
        ear.settle("hello")
        await settleTasks()
        loop.turnCompleted(reply: "Here is a long reply.", refused: false,
                           unreachable: false, hasQuestion: false)
        XCTAssertEqual(loop.phase, .speaking)
        let stopsBefore = mouth.stopCount

        loop.micTapped()

        XCTAssertEqual(loop.phase, .listening, "the interrupt hands the turn straight back")
        XCTAssertEqual(mouth.stopCount, stopsBefore + 1, "the audio is cut mid-word")
        XCTAssertEqual(ear.listenCount, 2)
        XCTAssertTrue(loop.isActive, "interrupting is not leaving")
    }

    func testTappingTheMicWhileListeningEndsTheLoop() async {
        loop.startConversation()
        loop.micTapped()

        XCTAssertEqual(loop.phase, .off)
        XCTAssertNil(loop.fellBackLine)
    }

    // MARK: Degrading, never trapping

    func testADeniedMicFallsBackInsteadOfPretendingToListen() async {
        ear.limitationLine = "no microphone access"
        loop.startConversation()

        XCTAssertEqual(loop.phase, .off)
        XCTAssertEqual(ear.listenCount, 0, "it never claims to be listening")
        XCTAssertEqual(loop.fellBackLine,
                       "I cannot listen right now, so we can keep going in text.")
    }

    func testAMicThatDiesMidLoopHandsBackToTyping() async {
        loop.startConversation()
        ear.fail()

        XCTAssertEqual(loop.phase, .off)
        XCTAssertNotNil(loop.fellBackLine)
    }

    func testSpeechThatCannotPlayHandsBackToTyping() async {
        loop.startConversation()
        ear.settle("hello")
        await settleTasks()
        loop.turnCompleted(reply: "Hi.", refused: false,
                           unreachable: false, hasQuestion: false)
        mouth.unavailable()

        XCTAssertEqual(loop.phase, .off)
        XCTAssertEqual(loop.fellBackLine,
                       "I cannot say that out loud right now, so let's keep talking in text.")
    }

    func testAFailedTurnLeavesTheLoopWithoutASecondExcuse() async {
        loop.startConversation()
        ear.settle("hello")
        await settleTasks()
        loop.turnCompleted(reply: nil, refused: true, unreachable: false, hasQuestion: false)

        XCTAssertEqual(loop.phase, .off)
        XCTAssertNil(loop.fellBackLine, "the reply surface already names the failure")
    }

    func testAnEmptyReplyEndsTheLoopRatherThanSpeakingNothing() async {
        loop.startConversation()
        ear.settle("hello")
        await settleTasks()
        loop.turnCompleted(reply: "   ", refused: false, unreachable: false, hasQuestion: false)

        XCTAssertEqual(loop.phase, .off)
        XCTAssertTrue(mouth.spoken.isEmpty)
    }

    // MARK: The check-in, unchanged (P18-04b)

    func testCheckInStillOpensWithItsOpener() async {
        loop.start()
        await settleTasks()

        XCTAssertEqual(loop.phase, .opening)
        XCTAssertEqual(loop.mode, .checkIn)
        XCTAssertEqual(sink.sent, ["Let's do today's check-in."])
        XCTAssertEqual(ear.listenCount, 0, "Blink opens the check-in, so it speaks first")
    }

    func testCheckInSpeaksThenListensThenSends() async {
        loop.start()
        await settleTasks()
        loop.turnCompleted(reply: "How did the writing go?", refused: false,
                           unreachable: false, hasQuestion: false)
        XCTAssertEqual(mouth.spoken, ["How did the writing go?"])

        mouth.finish()
        XCTAssertEqual(loop.phase, .listening)

        ear.settle("I finished it")
        await settleTasks()
        XCTAssertEqual(sink.sent, ["Let's do today's check-in.", "I finished it"])
        XCTAssertEqual(loop.phase, .sending)
    }

    func testCheckInStillHandsAStructuredQuestionBackToTapping() async {
        loop.start()
        await settleTasks()
        loop.turnCompleted(reply: "Which one?", refused: false,
                           unreachable: false, hasQuestion: true)

        XCTAssertEqual(loop.phase, .off)
        XCTAssertTrue(mouth.spoken.isEmpty, "a tap question is not a spoken one")
    }

    func testCheckInKeepsItsOwnFallbackWording() async {
        loop.start()
        await settleTasks()
        loop.turnCompleted(reply: "How did it go?", refused: false,
                           unreachable: false, hasQuestion: false)
        mouth.unavailable()

        XCTAssertEqual(loop.fellBackLine,
                       "I cannot say that out loud right now, so let's keep the check-in in text.")
    }

    func testASecondStartWhileRunningChangesNothing() async {
        loop.startConversation()
        loop.start()
        await settleTasks()

        XCTAssertEqual(loop.mode, .conversation)
        XCTAssertTrue(sink.sent.isEmpty, "the opener cannot barge into a live conversation")
    }
}
