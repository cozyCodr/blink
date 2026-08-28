import Foundation
import CoreGraphics

// P15-12 — the conversation surface's breathing room: how low the eyes sit,
// how far they shrink as the words grow, and how the whole column compresses
// when the keyboard rises.
//
// WHERE THE NUMBERS COME FROM. The web's stage rhythm is the source for the
// vertical proportion: conversation.css:66 reserves the bottom of the stage
// (`padding-bottom: min(42vh, 384px)`) and centers the eyes in what remains,
// which puts the pair's center at roughly 29% of the viewport — an
// upper-middle band, with the surface in the lower band beneath it. The
// TIERS, though, are iOS-only, no web equivalent: the web never scales its
// eyes or steps its reply size by length (the `.said` block is a fixed 20px
// serif inside a scrolling band, conversation.css:154). A phone has no
// scrolling band to hide behind, so the rig and the type give ground
// smoothly instead. Every threshold below is therefore a NAMED, documented
// choice rather than a transcription.
public enum ConversationScale {

    /// How much text the conversation is currently carrying: the reply or
    /// question on screen plus everything visibly stacked with it. Character
    /// count, because what matters is occupied lines, not vocabulary.
    /// Tiers (iOS-only, no web equivalent):
    ///   short  — a sentence; fits in ~2 lines of the display serif.
    ///   medium — a couple of sentences; the display serif would eat the band.
    ///   long   — a full question with a why, or a multi-sentence reply.
    public enum TextTier {
        case short
        case medium
        case long

        public init(charCount: Int) {
            switch charCount {
            case ..<90: self = .short       // ~2 lines at the 28pt display serif
            case ..<220: self = .medium     // ~3 lines at the 22pt card title
            default: self = .long
            }
        }
    }

    /// The eye rig's scale per tier, applied to the whole rig container
    /// (P15-02's pose tables are fraction-based, so a scaled rig stays
    /// correct). 0.62 is Today's existing resting scale (TodayScreen, P15-04);
    /// the lower stops are iOS-only choices that keep the pair legible while
    /// yielding lines to the words.
    public static func eyeScale(tier: TextTier, keyboardUp: Bool) -> CGFloat {
        let resting: CGFloat
        switch tier {
        case .short: resting = 0.62
        case .medium: resting = 0.50
        case .long: resting = 0.40
        }
        // The keyboard takes roughly half the screen; the eyes give one more
        // step rather than being pushed offscreen, floored where the pair
        // still reads as a face (iOS-only).
        return keyboardUp ? max(resting - 0.12, 0.34) : resting
    }

    /// The height the rig's frame reserves, shrinking with the rig so the
    /// freed lines actually go to the text (150pt is Today's existing frame).
    public static func eyeBand(tier: TextTier, keyboardUp: Bool) -> CGFloat {
        150 * (eyeScale(tier: tier, keyboardUp: keyboardUp) / 0.62)
    }

    /// The top inset that floats the eyes in the upper-middle band, as a
    /// fraction of the screen height. 0.16 puts the pair's center near the
    /// web's ~29% stage line (conversation.css:66's rhythm, read as a
    /// proportion); with the keyboard up the band compresses to nearly
    /// nothing so the compose field stays visible.
    public static func eyesTopFraction(keyboardUp: Bool) -> CGFloat {
        keyboardUp ? 0.02 : 0.16
    }

    /// The floor the tiered reply type may shrink to, via
    /// `minimumScaleFactor`. Shrinks only our own base tier — the fonts are
    /// Dynamic Type relative, so the user's accessibility size is never
    /// fought (iOS-only, no web equivalent: the web scrolls instead).
    public static let textMinimumScale: CGFloat = 0.85
}
