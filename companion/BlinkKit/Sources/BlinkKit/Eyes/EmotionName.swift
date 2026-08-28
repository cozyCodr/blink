import Foundation

/// The face's expression vocabulary, mirroring `createEyes` in
/// `src/web/app.js` and the `.emote-*` blocks in `src/web/css/face.css`.
///
/// The vocabulary is thirteen beats, not twelve, and they are not all the same
/// kind of thing. `.agents/rules/frontend-standards.md` says it plainly:
/// "TWELVE expressions plus the `think` state". Its own table then lists
/// twelve expression rows (happy, wide, sorry, curious, satisfied, heart,
/// surprised, sleepy, proud, worried, sheepish, celebrate) plus the `think`
/// row. The code agrees: `app.js:265-266` holds ELEVEN classes, because
/// `satisfied` is procedural and `thinking` is a state.
///
/// So the three kinds below are real, and the view has to treat them
/// differently. See `EmotionKind`.
public enum EmotionName: String, CaseIterable, Sendable, Identifiable, Codable {
    /// State, not a class: the squint plus the look-around, held for as long
    /// as the agent is actually thinking. face.css:141-143.
    case thinking
    case happy
    case wide
    case sorry
    case curious
    case heart
    case surprised
    case sleepy
    case proud
    case sheepish
    case worried
    case celebrate
    /// Procedural: one deliberate slow blink, no held class at all.
    /// app.js:296-311.
    case satisfied

    public var id: String { rawValue }

    /// The eleven held classes, in the order `app.js:265-266` lists them.
    /// Kept as its own list so a drift between the two files is visible.
    public static let heldClasses: [EmotionName] = [
        .happy, .wide, .sorry, .curious, .heart,
        .surprised, .sleepy, .proud, .sheepish, .worried, .celebrate
    ]

    public var kind: EmotionKind {
        switch self {
        case .thinking: return .state
        case .satisfied: return .procedural
        default: return .held
        }
    }

    /// What the beat honestly means. Straight from the trigger table in
    /// `.agents/rules/frontend-standards.md`. Nothing here fires on its own:
    /// wiring these to real events is P15-04 and P15-05, and a beat only ever
    /// fires when the grounded data backs it.
    public var trigger: String {
        switch self {
        case .thinking: return "The agent is working on it"
        case .happy: return "A general positive beat"
        case .wide: return "Entering listening"
        case .sorry: return "An error, or a turn that failed"
        case .curious: return "Held while a clarify question is up"
        case .heart: return "The first plan of the session that placed blocks"
        case .surprised: return "Waking from sleep"
        case .sleepy: return "The drowsy beat just before sleep lands"
        case .proud: return "Every successful plan after the first"
        case .sheepish: return "An honest miss, like nothing concrete to place"
        case .worried: return "A plan landed but nothing, or not all of it, could be placed"
        case .celebrate: return "Calendar sync came back clean"
        case .satisfied: return "A schedule committed, or a focus session recorded"
        }
    }
}

public enum EmotionKind: Sendable, Equatable {
    /// A class that stays on until it is cleared or its hold expires.
    case held
    /// An ambient state driven by what the agent is doing, not a beat.
    case state
    /// A one-shot animation with no resting form of its own.
    case procedural
}
