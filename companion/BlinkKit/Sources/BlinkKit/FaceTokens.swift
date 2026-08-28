import SwiftUI

// FaceTokens — the Swift mirror of the web's `data-face` scope.
//
// The web app scopes every face-identity rule under
// `:where(html[data-face="…"])` (see src/web/css/face.css:1-12). This protocol
// is the same idea in Swift: one conformance per face, and NO view anywhere
// hardcodes a colour, a font, a corner radius or a duration. Adding a fourth
// face means adding one conformance, exactly like adding a `data-face` block.
//
// Source of truth for every value below:
//   src/web/css/tokens.css   (the Nocturne :root, which IS the capsule face)
//   src/web/css/face.css     (the three `data-face` blocks)
//   src/web/app.js           (blink + emotion scheduling)
//   docs/COMPANION_SCREENS.md ("Face variation summary" table)
// Each conformance cites the exact line it transcribed, so the two
// implementations can be diffed later.

// MARK: - Identity

public enum FaceID: String, CaseIterable, Sendable, Codable, Identifiable {
    case capsule
    case lumen
    case folio

    public var id: String { rawValue }
}

// MARK: - Shape vocabulary

/// How a face draws a container corner.
/// Values from docs/COMPANION_SCREENS.md, "Face variation summary" → Corners.
public enum CornerStyle: Sendable, Equatable {
    /// Soft, generous. capsule.
    case rounded(CGFloat)
    /// Tight and mechanical. lumen.
    case squared(CGFloat)
    /// Uneven, drawn by hand. folio. The four corners differ by `jitter` points.
    case handDrawn(base: CGFloat, jitter: CGFloat)

    /// The single radius to use where a shape cannot vary its corners.
    public var nominalRadius: CGFloat {
        switch self {
        case .rounded(let r): return r
        case .squared(let r): return r
        case .handDrawn(let base, _): return base
        }
    }
}

/// The form the eyes take. P15-02 draws these; P15-01 only names them.
public enum EyeShape: Sendable, Equatable {
    /// Glowing vertical capsules. capsule.
    case capsule(cornerRadius: CGFloat)
    /// Dark round dots joined by a hairline. lumen.
    case dot(joinedByHairline: Bool)
    /// Hand-inked marks with a line boil. folio.
    case inkBlot
}

/// The earned beat. Never fires on a timer or a local guess
/// (.agents/rules/agent-governance.md, "Celebration is earned").
public enum Celebration: Sendable, Equatable {
    case heartBurst
    case confetti
    case stampedStar
}

/// docs/COMPANION_SCREENS.md, "Face variation summary" → Haptic.
public enum FaceHaptic: Sendable, Equatable {
    case warmDouble
    case crispSingle
    case thunk
}

// MARK: - Geometry

/// The eye rig's measurements. Transcribed from the `--eye-*` custom
/// properties in each face's block; the web uses px, we read them as points.
public struct EyeGeometry: Sendable, Equatable {
    public var eyeWidth: CGFloat
    public var eyeHeight: CGFloat
    /// Gap between the two eyes at rest.
    public var eyesGap: CGFloat
    /// Gap between the eye row and whatever sits under it.
    public var faceGap: CGFloat
    /// Gap when the pair spreads (`.eyes.spread`).
    public var spreadGap: CGFloat
    /// Scale when the rig parks at the top of the screen.
    public var parkScale: CGFloat
    /// Diameter of the ambient halo behind the pair (`--ambient-size`).
    /// Only drawn where `castsGlow` is true.
    public var ambientSize: CGFloat
    /// The specular highlight inside one eye. Width and height are equal and
    /// the mark is a circle. Zero width means this face has no glint at all
    /// (lumen face.css:487 and folio face.css:855 both `display: none` it).
    public var glintSize: CGFloat
    /// The glint's inset from the eye's top-left corner, in points.
    public var glintInset: CGSize
    /// The connecting hairline between the two eyes, where the face has one
    /// (lumen). Zero thickness means no hairline at all.
    /// face.css:454 `height: 3px`.
    public var hairlineThickness: CGFloat
    /// How far each end of that hairline runs UNDER its eye, so no state can
    /// ever open a gap between line and eye. face.css:453
    /// `left: calc(var(--eye-w) - 12px)` — the last 12px sit beneath the dot.
    public var hairlineUnderlap: CGFloat

    public init(
        eyeWidth: CGFloat,
        eyeHeight: CGFloat,
        eyesGap: CGFloat,
        faceGap: CGFloat,
        spreadGap: CGFloat,
        parkScale: CGFloat,
        ambientSize: CGFloat,
        glintSize: CGFloat,
        glintInset: CGSize,
        hairlineThickness: CGFloat = 0,
        hairlineUnderlap: CGFloat = 0
    ) {
        self.eyeWidth = eyeWidth
        self.eyeHeight = eyeHeight
        self.eyesGap = eyesGap
        self.faceGap = faceGap
        self.spreadGap = spreadGap
        self.parkScale = parkScale
        self.ambientSize = ambientSize
        self.glintSize = glintSize
        self.glintInset = glintInset
        self.hairlineThickness = hairlineThickness
        self.hairlineUnderlap = hairlineUnderlap
    }
}

// MARK: - Layout

/// The spacing a face lays its surfaces out on.
///
/// P15-04 added this because S1 is the first screen with real chrome, and a
/// padding written inline in a view is exactly the fork `data-face` exists to
/// prevent. Every value is transcribed from the web's shared chrome, with its
/// stylesheet line beside it.
///
/// **The web does not vary these per face today.** `chrome.css` and `now.css`
/// are not inside a `data-face` scope, so all three conformances carry the
/// same numbers, and that sameness is a fact about the stylesheet rather than
/// a shortcut. The property exists on the protocol so a face that wants to
/// breathe differently has somewhere to say so without a view learning its
/// name.
public struct FaceLayout: Sendable, Equatable {
    /// A card's inner padding. chrome.css:36 `padding: 22px 24px 24px`.
    public var cardPaddingTop: CGFloat
    public var cardPaddingSide: CGFloat
    public var cardPaddingBottom: CGFloat
    /// Between two stacked sections. chrome.css:53 `gap: 18px`.
    public var sectionGap: CGFloat
    /// Between rows inside one section. chrome.css:56 `gap: 12px`.
    public var rowGap: CGFloat
    /// Between an icon and its label. chrome.css:10 `gap: 8px`.
    public var tightGap: CGFloat
    /// A pill control's padding. chrome.css:9 `padding: 9px 14px`.
    public var pillPaddingV: CGFloat
    public var pillPaddingH: CGFloat
    /// The commitment dot beside a session title. now.css:35 `9px`.
    public var dotSize: CGFloat
    /// The screen's own horizontal margin. chrome.css:25 `padding: 24px`.
    public var screenMargin: CGFloat
    /// Minimum tap target. docs/COMPANION_SCREENS.md, Accessibility:
    /// "Minimum tap target 44pt on iOS".
    public var minTapTarget: CGFloat

    public init(
        cardPaddingTop: CGFloat,
        cardPaddingSide: CGFloat,
        cardPaddingBottom: CGFloat,
        sectionGap: CGFloat,
        rowGap: CGFloat,
        tightGap: CGFloat,
        pillPaddingV: CGFloat,
        pillPaddingH: CGFloat,
        dotSize: CGFloat,
        screenMargin: CGFloat,
        minTapTarget: CGFloat
    ) {
        self.cardPaddingTop = cardPaddingTop
        self.cardPaddingSide = cardPaddingSide
        self.cardPaddingBottom = cardPaddingBottom
        self.sectionGap = sectionGap
        self.rowGap = rowGap
        self.tightGap = tightGap
        self.pillPaddingV = pillPaddingV
        self.pillPaddingH = pillPaddingH
        self.dotSize = dotSize
        self.screenMargin = screenMargin
        self.minTapTarget = minTapTarget
    }

    /// The web's shared chrome, which is what all three faces sit on today.
    public static let webChrome = FaceLayout(
        cardPaddingTop: 22,      // chrome.css:36
        cardPaddingSide: 24,     // chrome.css:36
        cardPaddingBottom: 24,   // chrome.css:36
        sectionGap: 18,          // chrome.css:53
        rowGap: 12,              // chrome.css:56
        tightGap: 8,             // chrome.css:10
        pillPaddingV: 9,         // chrome.css:9
        pillPaddingH: 14,        // chrome.css:9
        dotSize: 9,              // now.css:35
        screenMargin: 24,        // chrome.css:25
        minTapTarget: 44         // COMPANION_SCREENS.md, Accessibility
    )
}

// MARK: - Motion

/// A CSS `cubic-bezier(x1, y1, x2, y2)`, carried across verbatim so a curve
/// can be diffed against the stylesheet it came from.
public struct TimingCurve: Sendable, Equatable {
    public var x1: Double
    public var y1: Double
    public var x2: Double
    public var y2: Double

    public init(_ x1: Double, _ y1: Double, _ x2: Double, _ y2: Double) {
        self.x1 = x1
        self.y1 = y1
        self.x2 = x2
        self.y2 = y2
    }

    public func animation(duration: Double) -> Animation {
        .timingCurve(x1, y1, x2, y2, duration: duration)
    }
}

/// The ambient loops that run when nothing is happening: the halo's breath,
/// the idle glance, and the thinking state's look-around and shimmer.
///
/// These are loops, so every one of them stands down under Reduced Motion
/// (face.css:1517-1522). They are tokens rather than literals for the same
/// reason the rest of `FaceMotion` is: lumen drifts further than capsule and
/// folio further still, and none of that belongs inside a view.
public struct IdleMotion: Sendable {
    /// Seconds between idle glances. app.js:213 uses the coarse-pointer range
    /// on touch devices, which the companion always is.
    public var glanceInterval: ClosedRange<Double>
    /// One glance, seconds. face.css:1509 `animation: glance 2.4s`.
    public var glanceDuration: Double
    /// The two drift stops of the glance keyframe, points, held at 28% and 58%
    /// of `glanceDuration` before settling back to zero. face.css:1510-1514.
    public var glanceDrift: [CGFloat]
    /// One look-around while thinking, seconds.
    public var thinkLookPeriod: Double
    /// The three drift stops of the look-around keyframe, points, held at 25%,
    /// 55% and 80% of `thinkLookPeriod`.
    public var thinkLookDrift: [CGFloat]
    /// One shimmer cycle while thinking, seconds. nil where the face does not
    /// shimmer.
    public var shimmerPeriod: Double?
    /// Peak brightness of that shimmer. face.css:157 `brightness(1.22)`.
    public var shimmerPeak: Double
    /// One halo breath, seconds. face.css:27 `animation: haloBreath 7s`.
    public var haloPeriod: Double
    /// Halo scale at the top of that breath. face.css:31.
    public var haloScale: CGFloat

    public init(
        glanceInterval: ClosedRange<Double>,
        glanceDuration: Double,
        glanceDrift: [CGFloat],
        thinkLookPeriod: Double,
        thinkLookDrift: [CGFloat],
        shimmerPeriod: Double?,
        shimmerPeak: Double,
        haloPeriod: Double,
        haloScale: CGFloat
    ) {
        self.glanceInterval = glanceInterval
        self.glanceDuration = glanceDuration
        self.glanceDrift = glanceDrift
        self.thinkLookPeriod = thinkLookPeriod
        self.thinkLookDrift = thinkLookDrift
        self.shimmerPeriod = shimmerPeriod
        self.shimmerPeak = shimmerPeak
        self.haloPeriod = haloPeriod
        self.haloScale = haloScale
    }
}

/// One held pose of a line boil: the stop-motion wobble that keeps a drawn
/// outline from ever sitting perfectly still (folio). Custom properties
/// animate DISCRETELY on the web, so each keyframe is a held frame; here each
/// held frame is one of these.
public struct BoilPose: Sendable, Equatable {
    /// The corner set of this held frame, or nil where the boil only carries
    /// rotation (an emotion that overrides the corners outright keeps its own,
    /// exactly as a CSS class overriding `border-radius` beats the base calc
    /// while `--boil-rot` still rides the composed transform, face.css:842).
    public var corners: CornerRadii?
    /// `--boil-rot` for this frame.
    public var rotation: Angle

    public init(corners: CornerRadii?, rotation: Angle) {
        self.corners = corners
        self.rotation = rotation
    }
}

/// A whole boil: the held frames and how long one full cycle takes.
/// `period / poses.count` is one held frame.
public struct BoilLoop: Sendable, Equatable {
    public var period: Double
    public var poses: [BoilPose]

    public init(period: Double, poses: [BoilPose]) {
        self.period = period
        self.poses = poses
    }

    /// Seconds one frame is held. face.css:851 `steps(1, end)` over the cycle.
    public var frameDuration: Double {
        period / Double(max(poses.count, 1))
    }
}

/// folio's celebrate: a five-pointed star THUNKS down above the pair like a
/// rubber stamp, in hard held poses rather than a tween (face.css:1114-1126).
public struct StampBeat: Sendable, Equatable {
    /// The stamp's box, points. face.css:1116 `width/height: 58px`.
    public var size: CGFloat
    /// How far above the pair's top edge it lands. face.css:1115 `top: -84px`.
    public var rise: CGFloat
    /// Where it starts: oversized and tilted. face.css:1124 `scale(2.1) rotate(-14deg)`.
    public var startScale: CGFloat
    public var startRotation: Angle
    /// Where it lands. face.css:1125 `scale(1) rotate(-4deg)`.
    public var landedRotation: Angle
    /// The whole thunk, seconds. face.css:1121 `folio-stamp 0.32s`.
    public var period: Double
    /// Hard poses on the way down. face.css:1121 `steps(2, end)`.
    public var steps: Int

    public init(
        size: CGFloat,
        rise: CGFloat,
        startScale: CGFloat,
        startRotation: Angle,
        landedRotation: Angle,
        period: Double,
        steps: Int
    ) {
        self.size = size
        self.rise = rise
        self.startScale = startScale
        self.startRotation = startRotation
        self.landedRotation = landedRotation
        self.period = period
        self.steps = steps
    }
}

/// The one-shot beats: the celebrate bounce, the deliberate slow blink that
/// IS `satisfied`, and the gap inside a double blink.
public struct BeatMotion: Sendable {
    /// One bounce, seconds. face.css:274 `eyesBounce 0.45s … 2`.
    public var bouncePeriod: Double
    /// How many bounces. face.css:274, the `2` at the end of the shorthand.
    public var bounceCount: Int
    /// The two travel stops of the bounce keyframe, points, held at 40% and
    /// 70% of `bouncePeriod`. nil where the face's celebrate does not bounce
    /// the pair at all (folio stamps a star instead, face.css:1114).
    public var bounceRise: [CGFloat]?
    /// `satisfied`: the slowed close, seconds. app.js:305.
    public var slowBlinkClose: Double
    /// `satisfied`: how long the lids stay shut, seconds. app.js:309.
    public var slowBlinkHold: Double
    /// The pause between the two blinks of a double blink, seconds. app.js:181.
    public var doubleBlinkGap: Double
    /// The stamped star, where the face's celebrate stamps one (folio,
    /// face.css:1114-1126). nil where celebrate does something else.
    public var stamp: StampBeat?

    public init(
        bouncePeriod: Double,
        bounceCount: Int,
        bounceRise: [CGFloat]?,
        slowBlinkClose: Double,
        slowBlinkHold: Double,
        doubleBlinkGap: Double,
        stamp: StampBeat? = nil
    ) {
        self.bouncePeriod = bouncePeriod
        self.bounceCount = bounceCount
        self.bounceRise = bounceRise
        self.slowBlinkClose = slowBlinkClose
        self.slowBlinkHold = slowBlinkHold
        self.doubleBlinkGap = doubleBlinkGap
        self.stamp = stamp
    }
}

/// Motion is a token, not a detail (docs/COMPANION_ARCHITECTURE.md §3).
/// If motion lived in the views, every animation would grow an
/// `if face == .folio`, which is the exact fork `data-face` exists to avoid.
///
/// Every number here is transcribed from the CSS keyframes and the blink
/// scheduler in app.js. Where the architecture doc's illustrative value
/// disagrees with the CSS, the CSS wins and the difference is noted inline.
public struct FaceMotion: Sendable {
    /// One full breath, seconds. CSS `animation: <name> Ns`.
    public var breathePeriod: Double
    /// Vertical travel at the top of the breath, points.
    ///
    /// The architecture doc calls this `breatheAmplitude` and describes it as
    /// a scale delta. The CSS does not scale: every face's breathe keyframe is
    /// a `translateY`. Renamed to say what it actually is.
    public var breatheRise: CGFloat

    /// The close (and the open), seconds. This is the eye-shape transform
    /// transition, not the hold. face.css `transition: transform 0.12s …`.
    public var blinkDuration: Double
    /// How long the lids stay shut between close and open, seconds.
    /// app.js: `setTimeout(…, 110)`.
    public var blinkHold: Double
    /// Seconds between random blinks. app.js: `2600 + Math.random() * 3600`.
    public var blinkInterval: ClosedRange<Double>
    /// Vertical scale at full close. Multiplies with the emotion's own scaleY,
    /// exactly as `scaleY(calc(var(--emo-sy) * var(--blink-sy)))` does.
    public var blinkSquash: CGFloat
    /// Horizontal scale at full close. Folio's cartoon squash-and-stretch
    /// widen; 1.0 (no widen) elsewhere.
    public var blinkStretch: CGFloat

    /// The transition INTO an emotion, seconds.
    public var emotionDuration: Double
    /// The cubic-bezier the emotion transform rides, as CSS writes it:
    /// (x1, y1, x2, y2).
    public var emotionCurve: TimingCurve
    /// The settle back to neutral, seconds (the `.emote-ease` window).
    public var releaseDuration: Double

    /// folio only: seconds per full boil cycle. nil where there is no boil.
    public var boilPeriod: Double?
    /// folio only: held poses per cycle. `boilPeriod / boilSteps` is one frame.
    public var boilSteps: Int
    /// The held frames themselves, in keyframe order. Empty where there is no
    /// boil. Count matches `boilSteps` where both are set (face.css:858-863).
    public var boilPoses: [BoilPose]

    /// The boil as one value, for the renderer. nil where the face does not boil.
    public var boilLoop: BoilLoop? {
        guard let boilPeriod, !boilPoses.isEmpty else { return nil }
        return BoilLoop(period: boilPeriod, poses: boilPoses)
    }

    /// How long the earned beat holds, seconds.
    public var celebrationHold: Double
    /// How long the heart holds, seconds.
    public var heartHold: Double
    /// How long a returning-greeting holds before it leaves, seconds. The
    /// greeting is a MOMENT on return, the way the web speaks it once, not a
    /// row that lives on the screen (P15-13).
    public var greetingHold: Double

    // P15-11: the conversation surface's motion, mirroring the web.
    /// The mode cross-fade: how long one conversation state eases out before
    /// the next eases in (`swapMode`, app.js:457-464: 140ms).
    public var swapFade: Double
    /// One chip's deal-in (clarify.css:229: `clarifyDeal 0.26s`).
    public var dealDuration: Double
    /// The stagger between dealt chips (clarify.css:232-238: 40ms apart).
    public var dealStagger: Double
    /// How far dealt/revealed content rises from, points
    /// (clarify.css:240: `translateY(6px)`).
    public var revealRise: CGFloat

    public var haptic: FaceHaptic

    /// The ambient loops.
    public var idle: IdleMotion
    /// The one-shot beats.
    public var beats: BeatMotion

    public init(
        breathePeriod: Double,
        breatheRise: CGFloat,
        blinkDuration: Double,
        blinkHold: Double,
        blinkInterval: ClosedRange<Double>,
        blinkSquash: CGFloat,
        blinkStretch: CGFloat,
        emotionDuration: Double,
        emotionCurve: TimingCurve,
        releaseDuration: Double,
        boilPeriod: Double?,
        boilSteps: Int,
        boilPoses: [BoilPose] = [],
        celebrationHold: Double,
        heartHold: Double,
        greetingHold: Double = 4.0,
        swapFade: Double,
        dealDuration: Double,
        dealStagger: Double,
        revealRise: CGFloat,
        haptic: FaceHaptic,
        idle: IdleMotion,
        beats: BeatMotion
    ) {
        self.idle = idle
        self.beats = beats
        self.breathePeriod = breathePeriod
        self.breatheRise = breatheRise
        self.blinkDuration = blinkDuration
        self.blinkHold = blinkHold
        self.blinkInterval = blinkInterval
        self.blinkSquash = blinkSquash
        self.blinkStretch = blinkStretch
        self.emotionDuration = emotionDuration
        self.emotionCurve = emotionCurve
        self.releaseDuration = releaseDuration
        self.boilPeriod = boilPeriod
        self.boilSteps = boilSteps
        self.boilPoses = boilPoses
        self.celebrationHold = celebrationHold
        self.heartHold = heartHold
        self.greetingHold = greetingHold
        self.swapFade = swapFade
        self.dealDuration = dealDuration
        self.dealStagger = dealStagger
        self.revealRise = revealRise
        self.haptic = haptic
    }

    /// The conversation-state cross-fade (app.js:457-464).
    public var swapAnimation: Animation {
        emotionCurve.animation(duration: swapFade)
    }

    /// One dealt chip's entrance, delayed by its place in the reading order
    /// (clarify.css:229-238).
    public func dealAnimation(index: Int) -> Animation {
        emotionCurve.animation(duration: dealDuration)
            .delay(Double(index) * dealStagger)
    }

    /// The animation that carries an emotion in.
    public var emotionAnimation: Animation {
        emotionCurve.animation(duration: emotionDuration)
    }

    /// The animation that carries an emotion back out.
    public var releaseAnimation: Animation {
        emotionCurve.animation(duration: releaseDuration)
    }

    /// One breath, looping.
    public var breatheAnimation: Animation {
        .easeInOut(duration: breathePeriod / 2).repeatForever(autoreverses: true)
    }

    /// The blink's own animation. face.css:107 gives the shape a 0.12s
    /// `cubic-bezier(0.4, 0, 0.2, 1)`, but while an emotion holds, face.css:184
    /// replaces that transition wholesale, so a blink taken mid-emotion rides
    /// the slower emotion curve. Same rule here.
    public func blinkAnimation(emotionHeld: Bool) -> Animation {
        emotionHeld
            ? emotionAnimation
            : TimingCurve(0.4, 0, 0.2, 1).animation(duration: blinkDuration)
    }

    /// `satisfied`: app.js:305 swaps the transform transition for a 0.45s
    /// ease-in-out for the duration of the one deliberate blink.
    public var slowBlinkAnimation: Animation {
        .easeInOut(duration: beats.slowBlinkClose)
    }
}

// MARK: - The protocol

public protocol FaceTokens: Sendable {
    var id: FaceID { get }
    /// Shown in the face picker. User-facing copy follows
    /// .agents/rules/conversational-voice.md.
    var displayName: String { get }
    var tagline: String { get }

    // Grounds
    var ground: Color { get }
    var surface: Color { get }
    /// The ground an input control sits on. Every face declares its own,
    /// because on a face whose ground IS white a control cannot also be white
    /// (tokens.css:25-31).
    var control: Color { get }

    // Ink
    var ink: Color { get }
    var muted: Color { get }
    var faint: Color { get }
    var line: Color { get }

    // Signals
    var accent: Color { get }
    var accentBright: Color { get }
    /// "watch this" amber.
    var warm: Color { get }
    /// "this is slipping" clay. Never used as a shame colour
    /// (.agents/rules/agent-governance.md, "Misses get truth, not shame").
    var alert: Color { get }

    // The eyes. Capsule paints a vertical gradient through the three stops;
    // the flat-ink faces set all three to the same value.
    var eyeInkTop: Color { get }
    var eyeInkMid: Color { get }
    var eyeInkBottom: Color { get }
    /// Dim beats (sorry, sleepy) grey the ink and keep it OPAQUE.
    var eyeInkDim: Color { get }
    /// The heart lobes.
    var heart: Color { get }
    /// The colour the glow / ring / shadow reads as. `--glow-rgb`.
    var glow: Color { get }
    /// Whether this face glows at all. Capsule does; lumen and folio hide the
    /// halo entirely (face.css:420, face.css:794).
    var castsGlow: Bool { get }
    /// The `--glow` multiplier the face sits at when nothing is happening.
    /// face.css:120 `[data-state="idle"] { --glow: 0.5 }`. Emotions that dim
    /// (sorry, sleepy) override it; everything else inherits it.
    var restingGlow: CGFloat { get }
    /// Peak alpha of the ambient halo at `--glow: 1`. face.css:19.
    var ambientOpacity: Double { get }

    // Type
    var displayFont: Font { get }
    var bodyFont: Font { get }
    var monoFont: Font { get }
    /// A card's own headline, one step under the screen's. The face's serif
    /// (or its equivalent) at now.css:30's `clamp(18px, 2.6vw, 24px)`, taken
    /// at the middle of that clamp because a phone is not a viewport slider.
    var cardTitleFont: Font { get }
    /// The all-caps section label above a card. now.css:22 `12px/600`.
    var labelFont: Font { get }
    /// A measured number, large and mono with tabular figures. now.css:44
    /// `clamp(44px, 8vw, 64px)`, and now.css:89's phone pass `clamp(38px,
    /// 12vw, 52px)`; 48 sits inside both.
    var numberFont: Font { get }
    /// The quiet mono line under a number: the "as of" stamp, a duration,
    /// the source of an actual. now.css:60 `12px` mono.
    var metaFont: Font { get }
    /// A secondary sentence, smaller than body. now.css:73 `14px`.
    var secondaryFont: Font { get }
    /// What actually resolved on this device, so the debug screen (and the
    /// README) can be honest about a fallback instead of quietly showing
    /// San Francisco.
    var fontResolutions: [FontResolution] { get }

    // Shape
    var cornerStyle: CornerStyle { get }
    /// The spacing this face lays surfaces out on.
    var layout: FaceLayout { get }
    var eyeShape: EyeShape { get }
    var eyeGeometry: EyeGeometry { get }
    var celebration: Celebration { get }

    // Motion
    var motion: FaceMotion { get }
    /// Where every emotion lives: one table of channel targets per face,
    /// transcribed from that face's `.emote-*` block. This is the hook that
    /// keeps three faces from becoming three eye views — `EyesView` reads the
    /// table and never asks which face it is wearing.
    var emotionPoses: EmotionPoseTable { get }
}

/// The registry. Order is the order the picker offers them in.
public enum Faces {
    public static let all: [any FaceTokens] = [CapsuleFace(), LumenFace(), FolioFace()]

    public static func tokens(for id: FaceID) -> any FaceTokens {
        switch id {
        case .capsule: return CapsuleFace()
        case .lumen: return LumenFace()
        case .folio: return FolioFace()
        }
    }
}
