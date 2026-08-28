import SwiftUI

/// lumen — porcelain. White vinyl, two ink dots joined by a hairline.
///
/// Transcribed from the `html[data-face="lumen"]` block in
/// `src/web/css/face.css` (lines 365-405) and the lumen rules under it.
public struct LumenFace: FaceTokens {
    public init() {}

    public let id: FaceID = .lumen
    public let displayName = "Lumen"
    public let tagline = "White porcelain, two calm dots."

    // MARK: Grounds — face.css:369-375
    public let ground = Color.hex("#ffffff")        // face.css:369  --ground
    public let surface = Color.hex("#ffffff")       // face.css:370  --surface
    public let control = Color.hex("#f2f3f4")       // face.css:375  --control

    // MARK: Ink — face.css:378-381
    public let ink = Color.hex("#37393b")           // face.css:378  --text
    public let muted = Color.hex("#74777a")         // face.css:379  --muted
    public let faint = Color.hex("#9a9da0")         // face.css:380  --faint
    public let line = Color.hex("#e4e6e8")          // face.css:381  --line

    // MARK: Signals — face.css:382-383, tokens.css:36-37
    public let accent = Color.hex("#4f7a63")        // face.css:382  --accent
    public let accentBright = Color.hex("#3c614e")  // face.css:383  --accent-bright
    // lumen does not redeclare --warm / --alert, so it inherits them from the
    // base token set.
    public let warm = Color.hex("#d9a05b")          // tokens.css:36 --warm (inherited)
    public let alert = Color.hex("#d07a63")         // tokens.css:37 --alert (inherited)

    // MARK: The eyes — face.css:393-396
    // Flat ink: all three gradient stops collapse to one value.
    public let eyeInkTop = Color.hex("#2b2a27")     // face.css:393  --lumen-ink
    public let eyeInkMid = Color.hex("#2b2a27")     // face.css:393  --lumen-ink
    public let eyeInkBottom = Color.hex("#2b2a27")  // face.css:393  --lumen-ink
    public let eyeInkDim = Color.hex("#8b8e91")     // face.css:394  --lumen-ink-dim
    public let heart = Color.hex("#d97b6c")         // face.css:395  --lumen-heart
    public let glow = Color.rgba(108, 111, 114)     // face.css:386  --glow-rgb
    public let castsGlow = false                    // face.css:420  .ambient { display: none }
    // Lumen declares no `--glow` states: porcelain does not brighten or dim,
    // its dim beats change the INK instead (face.css:631). Held at 1 so
    // nothing reads as a glow that is not there.
    public let restingGlow: CGFloat = 1
    public let ambientOpacity = 0.0                 // face.css:420  no halo to fade

    // MARK: Type
    // docs/COMPANION_SCREENS.md → Display type: Hanken Grotesk.
    static let typography = FaceTypography(
        faceID: .lumen,
        display: ["Hanken Grotesk", "Segoe UI"],                 // tokens.css:50 --sans
        body: ["Hanken Grotesk", "Segoe UI"],                    // tokens.css:50 --sans
        mono: ["IBM Plex Mono", "SF Mono", "Menlo"],             // tokens.css:52 --mono
        displayDesign: .default,
        bodyDesign: .default
    )
    public var displayFont: Font { Self.typography.font(.display, size: 28, relativeTo: .title) }
    public var bodyFont: Font { Self.typography.font(.body, size: 17, relativeTo: .body) }
    public var monoFont: Font { Self.typography.font(.mono, size: 15, relativeTo: .callout) }
    // P15-04 named type roles. The SIZES come from the web's shared chrome
    // (now.css), which is not face-scoped; the FAMILY is this face's, because
    // `typography` is. So a role reads in this face's voice without any view
    // knowing which face it is wearing.
    public var cardTitleFont: Font { Self.typography.font(.display, size: 22, relativeTo: .title3) }   // now.css:30
    public var labelFont: Font { Self.typography.font(.body, size: 12, relativeTo: .caption) }         // now.css:22
    public var numberFont: Font { Self.typography.font(.mono, size: 48, relativeTo: .largeTitle) }     // now.css:44, :89
    public var metaFont: Font { Self.typography.font(.mono, size: 12, relativeTo: .caption) }          // now.css:60
    public var secondaryFont: Font { Self.typography.font(.body, size: 14, relativeTo: .subheadline) } // now.css:73
    public var fontResolutions: [FontResolution] { Self.typography.resolutions }

    // MARK: Shape
    public let cornerStyle: CornerStyle = .squared(9)   // face.css:1203 border-radius: 9px
    // Shared web chrome; see FaceLayout. This face does not depart from it.
    public let layout = FaceLayout.webChrome
    public let eyeShape: EyeShape = .dot(joinedByHairline: true)  // face.css:445-461
    public let celebration: Celebration = .confetti     // COMPANION_SCREENS.md

    public let eyeGeometry = EyeGeometry(
        eyeWidth: 100,     // face.css:400  --eye-w
        eyeHeight: 100,    // face.css:401  --eye-h
        eyesGap: 120,      // face.css:402  --eyes-gap
        faceGap: 40,       // face.css:403  --face-gap
        spreadGap: 160,    // face.css:462  .eyes.spread
        parkScale: 0.45,   // face.css:404  --park-scale
        ambientSize: 0,    // face.css:420  the halo is hidden outright
        glintSize: 0,      // face.css:487  .glint { display: none }
        glintInset: .zero,
        hairlineThickness: 3,   // face.css:454  height: 3px
        hairlineUnderlap: 12    // face.css:453  each end runs 12px UNDER its dot
    )

    // MARK: Motion
    public let motion = FaceMotion(
        breathePeriod: 7.0,          // face.css:425  animation: lumen-breathe 7s
        breatheRise: 4,              // face.css:429  translateY(-4px)
        blinkDuration: 0.12,         // face.css:479  transition: transform 0.12s
        blinkHold: 0.11,             // app.js:184    setTimeout(…, 110)
        blinkInterval: 2.6...6.2,    // app.js:186    2600 + random * 3600
        blinkSquash: 0.12,           // face.css:489  --blink-sy
        blinkStretch: 1.0,           // lumen does not widen on blink
        emotionDuration: 0.25,       // face.css:616
        emotionCurve: TimingCurve(0.34, 1.3, 0.64, 1),  // face.css:616
        releaseDuration: 0.30,       // app.js:284    emote-ease window
        boilPeriod: nil,             // lumen does not boil
        boilSteps: 0,
        celebrationHold: 1.4,        // app.js:5939   emote("celebrate", 1400)
        heartHold: 0.9,              // app.js:5753   emote("heart", 900)
        haptic: .crispSingle,        // COMPANION_SCREENS.md
        idle: IdleMotion(
            glanceInterval: 4.5...9.0,    // app.js:213  coarse pointer branch
            glanceDuration: 2.4,          // face.css:1509 .eyes.glance is unscoped,
            glanceDrift: [-7, 5],         // face.css:1512-1513 so all faces share it
            thinkLookPeriod: 2.4,         // face.css:540  lumen-look 2.4s
            thinkLookDrift: [-14, 10, -4],// face.css:544-547
            shimmerPeriod: nil,           // lumen thinks in the LINE (face.css:542),
            shimmerPeak: 1,               // not in the dots' brightness
            haloPeriod: 7.0,              // unused: no halo (face.css:420)
            haloScale: 1
        ),
        beats: BeatMotion(
            bouncePeriod: 0.45,           // face.css:697  lumen-bounce 0.45s
            bounceCount: 2,               // face.css:697  the trailing 2
            bounceRise: [-18, 4],         // face.css:700-701
            slowBlinkClose: 0.45,         // app.js:305    transform 0.45s
            slowBlinkHold: 0.48,          // app.js:309    setTimeout(…, 480)
            doubleBlinkGap: 0.18          // app.js:181    setTimeout(…, 180)
        )
    )

    // MARK: The vocabulary, in Lumen's language
    //
    // The four transform channels and the pair transforms, transcribed from
    // face.css:620-706, plus (P15-08) the parts that are not per-eye channels:
    // what the connecting hairline does during each beat, the dim-by-colour
    // ink swaps, and the heart's two round lobes. Dim beats grey the INK and
    // never the opacity (face.css:394), because the underlapped line must not
    // show through a translucent dot.
    public let emotionPoses = EmotionPoseTable(
        resting: EyePose(both: EyeChannels(corners: .percent((50, 50, 50, 50), (50, 50, 50, 50)))),
        poses: [
            // face.css:541 — the squint-slits
            .thinking: EyePose(both: EyeChannels(scaleY: 0.5)),
            // face.css:622-626
            .happy: EyePose(both: EyeChannels(
                scaleY: 0.5, translateY: -8,
                corners: .percent((50, 50, 14, 14), (68, 68, 10, 10))
            )),
            // face.css:628
            .wide: EyePose(both: EyeChannels(scaleX: 1.14, scaleY: 1.14)),
            // face.css:631-632 — the dots sink and grey (colour, not opacity);
            // the line drops with them and fades
            .sorry: EyePose(
                both: EyeChannels(scaleY: 0.78, translateY: 12),
                ink: .dim,
                line: HairlineState(offsetY: 9, opacity: 0.5)
            ),
            // face.css:635-637
            .curious: EyePose(
                left: EyeChannels(scaleX: 1.12, scaleY: 1.12),
                right: EyeChannels(scaleX: 0.84, scaleY: 0.84),
                pairRotation: .degrees(3)
            ),
            // face.css:641-654 — coral hearts: rotated body + the two lobes,
            // and the line stays and blushes coral
            .heart: EyePose(
                both: EyeChannels(
                    scaleX: 0.85, scaleY: 0.85, rotation: .degrees(-45),
                    corners: .percent((0, 0, 0, 12), (0, 0, 0, 12))
                ),
                ink: .heart,
                heartLobes: true,
                line: HairlineState(ink: .heart)
            ),
            // face.css:656-658 — the pop; the line contracts a touch
            .surprised: EyePose(
                both: EyeChannels(scaleX: 1.24, scaleY: 1.24, translateY: -6),
                line: HairlineState(scaleX: 0.85)
            ),
            // face.css:666-667 — the pills sit LEVEL with the line, full ink
            .sleepy: EyePose(
                both: EyeChannels(scaleY: 0.26),
                line: HairlineState(opacity: 0.6)
            ),
            // face.css:670-674
            .proud: EyePose(
                both: EyeChannels(
                    scaleY: 0.66, translateY: -5,
                    corners: .percent((50, 50, 30, 30), (62, 62, 26, 26))
                ),
                line: HairlineState(offsetY: -5)
            ),
            // face.css:682-684
            .sheepish: EyePose(
                both: EyeChannels(scaleX: 0.8, scaleY: 0.8),
                pairRotation: .degrees(-3),
                pairOffset: CGSize(width: 16, height: 0),
                line: HairlineState(opacity: 0.6)
            ),
            // face.css:677-679 — the dots drift far apart, the line sags
            .worried: EyePose(
                both: EyeChannels(scaleY: 0.85),
                gap: 200,
                line: HairlineState(offsetY: 18, opacity: 0.8)
            ),
            // face.css:687-694
            .celebrate: EyePose(
                both: EyeChannels(
                    scaleY: 0.5, translateY: -8,
                    corners: .percent((50, 50, 14, 14), (68, 68, 10, 10))
                ),
                bounces: true
            )
        ]
    )
}
