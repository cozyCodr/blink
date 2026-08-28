import SwiftUI

/// capsule — Nocturne. The default face, and the only palette the web ships
/// (AGENT.md, planner P10-00).
///
/// Every value is transcribed from `src/web/css/tokens.css`, whose `:root`
/// block IS the capsule face, plus the `:where(html[data-face="capsule"])`
/// rules in `src/web/css/face.css`. Line citations are exact so the two
/// implementations can be diffed.
public struct CapsuleFace: FaceTokens {
    public init() {}

    public let id: FaceID = .capsule
    public let displayName = "Capsule"
    public let tagline = "Quiet dark, green glow. The one you know."

    // MARK: Grounds — tokens.css:19-32
    public let ground = Color.hex("#14181d")        // tokens.css:19  --ground
    public let surface = Color.hex("#1d232b")       // tokens.css:20  --surface
    public let control = Color.hex("#232a33")       // tokens.css:32  --control

    // MARK: Ink — tokens.css:21-24
    public let ink = Color.hex("#cad2d9")           // tokens.css:21  --text
    public let muted = Color.hex("#97a1aa")         // tokens.css:22  --muted
    public let faint = Color.hex("#6c757e")         // tokens.css:23  --faint
    public let line = Color.hex("#2a323b")          // tokens.css:24  --line

    // MARK: Signals — tokens.css:33-37
    public let accent = Color.hex("#8fbba3")        // tokens.css:33  --accent
    public let accentBright = Color.hex("#cdeeda")  // tokens.css:34  --accent-bright
    public let warm = Color.hex("#d9a05b")          // tokens.css:36  --warm
    public let alert = Color.hex("#d07a63")         // tokens.css:37  --alert

    // MARK: The eyes — tokens.css:39-42
    // Capsule is the one face that paints a vertical gradient through all
    // three stops; the flat-ink faces collapse them to one value.
    public let eyeInkTop = Color.hex("#cdeeda")     // tokens.css:39  --eye-top
    public let eyeInkMid = Color.hex("#8fbba3")     // tokens.css:40  --eye-mid
    public let eyeInkBottom = Color.hex("#6fa088")  // tokens.css:41  --eye-bot
    // Capsule has no dim-ink token: dim beats (sorry face.css:203, sleepy
    // face.css:240) drop `--glow` instead of changing the fill. The bottom
    // stop stands in wherever a flat dim ink is genuinely needed.
    public let eyeInkDim = Color.hex("#6fa088")     // tokens.css:41  --eye-bot
    // Capsule's heart is the eye body itself, rotated into lobes
    // (face.css:222-223). No separate heart token exists, so the accent is it.
    public let heart = Color.hex("#8fbba3")         // tokens.css:33  --accent
    public let glow = Color.rgba(143, 187, 163)     // tokens.css:42  --glow-rgb
    public let castsGlow = true                     // face.css:14-25  .ambient halo
    public let restingGlow: CGFloat = 0.5           // face.css:120  [data-state="idle"]
    public let ambientOpacity = 0.16                // face.css:19   0.16 * var(--glow)

    // MARK: Type — tokens.css:50-52
    // No font files ship in this repo, so the stacks below are the CSS stacks
    // verbatim and the resolver reports what actually landed.
    static let typography = FaceTypography(
        faceID: .capsule,
        display: ["Newsreader", "Georgia", "Times New Roman"],   // tokens.css:51 --serif
        body: ["Hanken Grotesk", "Segoe UI"],                    // tokens.css:50 --sans
        mono: ["IBM Plex Mono", "SF Mono", "Menlo"],             // tokens.css:52 --mono
        displayDesign: .serif,
        bodyDesign: .default
    )
    public var displayFont: Font { Self.typography.font(.display, size: 28, relativeTo: .title) }
    public var bodyFont: Font { Self.typography.font(.body, size: 17, relativeTo: .body) }
    public var monoFont: Font { Self.typography.font(.mono, size: 15, relativeTo: .callout) }
    public var fontResolutions: [FontResolution] { Self.typography.resolutions }

    // MARK: Shape
    // docs/COMPANION_SCREENS.md, "Face variation summary" → Corners: rounded 20.
    // (The web's capsule chrome uses 10px cards, clarify.css:49; the companion
    // spec asks for a softer native radius, so 20 is deliberate, not a typo.)
    public let cornerStyle: CornerStyle = .rounded(20)
    public let eyeShape: EyeShape = .capsule(cornerRadius: 40)  // face.css:96 border-radius: 40px
    public let celebration: Celebration = .heartBurst           // COMPANION_SCREENS.md

    public let eyeGeometry = EyeGeometry(
        // P15-01's citations for this block were each one line high; corrected
        // against tokens.css:57-63 while adding the three new measurements.
        eyeWidth: 74,      // tokens.css:57  --eye-w
        eyeHeight: 108,    // tokens.css:58  --eye-h
        eyesGap: 46,       // tokens.css:59  --eyes-gap
        faceGap: 46,       // tokens.css:60  --face-gap
        spreadGap: 64,     // face.css:52    .eyes.spread
        parkScale: 0.42,   // tokens.css:63  --park-scale
        ambientSize: 640,  // tokens.css:61  --ambient-size
        glintSize: 20,     // face.css:110   width/height: 20px
        glintInset: CGSize(width: 27, height: 38)  // face.css:110  left/top
    )

    /// The size the corner percentages resolve against.
    private static let eyeBox = CGSize(width: 74, height: 108)

    // MARK: Motion
    public let motion = FaceMotion(
        breathePeriod: 6.0,          // face.css:66   animation: breathe 6s
        breatheRise: 7,              // face.css:70   translateY(-7px)
        blinkDuration: 0.12,         // face.css:107  transition: transform 0.12s
        blinkHold: 0.11,             // app.js:184    setTimeout(…, 110)
        blinkInterval: 2.6...6.2,    // app.js:186    2600 + random * 3600
        blinkSquash: 0.06,           // face.css:117  --blink-sy
        blinkStretch: 1.0,           // capsule does not widen on blink
        emotionDuration: 0.25,       // face.css:184
        emotionCurve: TimingCurve(0.34, 1.3, 0.64, 1),  // face.css:184
        releaseDuration: 0.30,       // app.js:284    emote-ease window
        boilPeriod: nil,             // capsule does not boil
        boilSteps: 0,
        celebrationHold: 1.4,        // app.js:5939   emote("celebrate", 1400)
        heartHold: 0.9,              // app.js:5753   emote("heart", 900)
        haptic: .warmDouble,         // COMPANION_SCREENS.md
        idle: IdleMotion(
            glanceInterval: 4.5...9.0,   // app.js:213  coarse pointer branch
            glanceDuration: 2.4,         // face.css:1509 glance 2.4s
            glanceDrift: [-7, 5],        // face.css:1512-1513
            thinkLookPeriod: 2.4,        // face.css:141  lookAround 2.4s
            thinkLookDrift: [-10, 8, -3],// face.css:147-149
            shimmerPeriod: 1.3,          // face.css:142  shimmer 1.3s
            shimmerPeak: 1.22,           // face.css:157  brightness(1.22)
            haloPeriod: 7.0,             // face.css:27   haloBreath 7s
            haloScale: 1.08              // face.css:31   scale(1.08)
        ),
        beats: BeatMotion(
            bouncePeriod: 0.45,          // face.css:274  eyesBounce 0.45s
            bounceCount: 2,              // face.css:274  the trailing 2
            bounceRise: [-10, 2],        // face.css:283-284
            slowBlinkClose: 0.45,        // app.js:305    transform 0.45s
            slowBlinkHold: 0.48,         // app.js:309    setTimeout(…, 480)
            doubleBlinkGap: 0.18         // app.js:181    setTimeout(…, 180)
        )
    )

    // MARK: The twelve emotions plus the heart
    //
    // Transcribed line by line from the capsule block of src/web/css/face.css.
    // Corner percentages keep the stylesheet's own shorthand order and units:
    // `border-radius: h1 h2 h3 h4 / v1 v2 v3 v4`, top-left first.
    public let emotionPoses = EmotionPoseTable(
        // face.css:96 — border-radius: 40px
        resting: EyePose(both: EyeChannels(corners: .points(40, in: CapsuleFace.eyeBox))),
        poses: [
            // thinking — the squint, plus the look-around and shimmer the rig
            // runs. A STATE, not a class (face.css:141-143).
            .thinking: EyePose(
                both: EyeChannels(
                    scaleY: 0.56,                                   // face.css:142
                    corners: .points(40, in: CapsuleFace.eyeBox),
                    glintOpacity: 0.3                               // face.css:143
                ),
                glow: 0.8,                                          // face.css:122
                brightness: 1.02                                    // face.css:142
            ),

            // happy — the upward crescent (face.css:190-195)
            .happy: EyePose(
                both: EyeChannels(
                    scaleY: 0.55,
                    translateY: -3,
                    corners: .percent((50, 50, 12, 12), (62, 62, 6, 6))
                ),
                brightness: 1.1
            ),

            // wide — the alert open-up (face.css:198-199)
            .wide: EyePose(
                both: EyeChannels(
                    scaleX: 1.04,
                    scaleY: 1.1,
                    corners: .points(40, in: CapsuleFace.eyeBox)
                )
            ),

            // sorry — the droop, dimmed, glints sinking (face.css:203-207)
            .sorry: EyePose(
                left: EyeChannels(
                    translateY: 4,
                    corners: .percent((55, 45, 45, 45), (72, 40, 34, 40)),
                    glintOffset: CGSize(width: 0, height: 7),
                    glintOpacity: 0.45
                ),
                right: EyeChannels(
                    translateY: 4,
                    corners: .percent((45, 55, 45, 45), (40, 72, 40, 34)),
                    glintOffset: CGSize(width: 0, height: 7),
                    glintOpacity: 0.45
                ),
                glow: 0.38
            ),

            // curious — asymmetric raise plus a head tilt (face.css:210-212)
            .curious: EyePose(
                left: EyeChannels(scaleY: 1.08, corners: .points(40, in: CapsuleFace.eyeBox)),
                right: EyeChannels(scaleY: 0.8, corners: .points(40, in: CapsuleFace.eyeBox)),
                pairRotation: .degrees(2.5)
            ),

            // heart — the two lobes. The pair closes to a 2pt gap, the right
            // eye pulls 26pt left, and each body keeps only its top corners
            // round while rotating out by 40 degrees, so the lobes sit
            // outer-top and the tips cross at the point (face.css:220-224).
            .heart: EyePose(
                left: EyeChannels(
                    rotation: .degrees(-40),
                    corners: .points((37, 37, 6, 6), in: CapsuleFace.eyeBox),
                    glintOpacity: 0
                ),
                right: EyeChannels(
                    rotation: .degrees(40),
                    corners: .points((37, 37, 6, 6), in: CapsuleFace.eyeBox),
                    glintOpacity: 0
                ),
                gap: 2,
                rightEyeInset: -26
            ),

            // surprised — the fast open-up (face.css:230-236)
            .surprised: EyePose(
                both: EyeChannels(
                    scaleX: 1.06,
                    scaleY: 1.16,
                    translateY: -2,
                    corners: .points(40, in: CapsuleFace.eyeBox),
                    glintScale: 0.8
                ),
                brightness: 1.12
            ),

            // sleepy — heavy lids (face.css:240-245)
            .sleepy: EyePose(
                both: EyeChannels(
                    scaleY: 0.42,
                    translateY: 5,
                    corners: .percent((26, 26, 45, 45), (18, 18, 60, 60)),
                    glintOffset: CGSize(width: 0, height: 8),
                    glintOpacity: 0.3
                ),
                glow: 0.35
            ),

            // proud — a softer crescent than happy (face.css:249-254)
            .proud: EyePose(
                both: EyeChannels(
                    scaleY: 0.72,
                    translateY: -2,
                    corners: .percent((48, 48, 22, 22), (58, 58, 14, 14)),
                    glintOpacity: 0.95
                ),
                brightness: 1.07
            ),

            // sheepish — narrow, leaning away, glints aside (face.css:258-260)
            .sheepish: EyePose(
                both: EyeChannels(
                    scaleX: 0.85,
                    translateY: 2,
                    corners: .points(40, in: CapsuleFace.eyeBox),
                    glintOffset: CGSize(width: 7, height: 3),
                    glintOpacity: 0.6
                ),
                pairRotation: .degrees(-2)
            ),

            // worried — inner-top corners raised into brows (face.css:264-268)
            .worried: EyePose(
                left: EyeChannels(
                    scaleY: 0.9,
                    corners: .percent((45, 55, 48, 48), (36, 66, 42, 42)),
                    glintOffset: CGSize(width: 0, height: -3),
                    glintOpacity: 0.8
                ),
                right: EyeChannels(
                    scaleY: 0.9,
                    corners: .percent((55, 45, 48, 48), (66, 36, 42, 42)),
                    glintOffset: CGSize(width: 0, height: -3),
                    glintOpacity: 0.8
                ),
                glow: 0.5
            ),

            // celebrate — the happy crescent plus a two-beat bounce
            // (face.css:274-280)
            .celebrate: EyePose(
                both: EyeChannels(
                    scaleY: 0.55,
                    translateY: -3,
                    corners: .percent((50, 50, 12, 12), (62, 62, 6, 6))
                ),
                brightness: 1.14,
                bounces: true
            )

            // satisfied is deliberately absent: it has no pose. It is one
            // deliberate slow blink over whatever is already showing
            // (app.js:296-311), driven by the rig's blink channel.
        ]
    )
}
