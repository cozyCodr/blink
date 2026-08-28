import SwiftUI

/// folio — ink on paper. Warm stock, hand-inked marks, a stamp-red accent,
/// and a line boil that never lets the outline sit still.
///
/// Transcribed from the `html[data-face="folio"]` block in
/// `src/web/css/face.css` (lines 738-779) and the folio rules under it.
public struct FolioFace: FaceTokens {
    public init() {}

    public let id: FaceID = .folio
    public let displayName = "Folio"
    public let tagline = "Ink on warm paper, drawn by hand."

    // MARK: Grounds — face.css:739-743
    public let ground = Color.hex("#f2ecdf")        // face.css:739  --ground
    public let surface = Color.hex("#faf6ec")       // face.css:740  --surface
    public let control = Color.hex("#fdfaf3")       // face.css:743  --control

    // MARK: Ink — face.css:746-749
    public let ink = Color.hex("#33291f")           // face.css:746  --text
    public let muted = Color.hex("#6f6353")         // face.css:747  --muted
    public let faint = Color.hex("#998b76")         // face.css:748  --faint
    public let line = Color.hex("#ddd3c0")          // face.css:749  --line

    // MARK: Signals — face.css:750-751, tokens.css:36-37
    public let accent = Color.hex("#b3402e")        // face.css:750  --accent (stamp red)
    public let accentBright = Color.hex("#8e2f21")  // face.css:751  --accent-bright
    // folio does not redeclare --warm / --alert either.
    public let warm = Color.hex("#d9a05b")          // tokens.css:36 --warm (inherited)
    public let alert = Color.hex("#d07a63")         // tokens.css:37 --alert (inherited)

    // MARK: The eyes — face.css:766-770
    public let eyeInkTop = Color.hex("#29211a")     // face.css:766  --folio-ink
    public let eyeInkMid = Color.hex("#29211a")     // face.css:766  --folio-ink
    public let eyeInkBottom = Color.hex("#29211a")  // face.css:766  --folio-ink
    public let eyeInkDim = Color.hex("#9a8d7c")     // face.css:767  --folio-ink-dim
    public let heart = Color.hex("#b3402e")         // face.css:769  --folio-red
    public let glow = Color.rgba(111, 96, 76)       // face.css:754  --glow-rgb
    public let castsGlow = false                    // face.css:794  .ambient { display: none }
    // Ink on paper does not glow. Folio's dim beats wash the ink toward grey
    // (face.css:1032) rather than turning a light down.
    public let restingGlow: CGFloat = 1
    public let ambientOpacity = 0.0                 // face.css:794  no halo to fade

    // MARK: Type — face.css:771  --folio-hand
    static let typography = FaceTypography(
        faceID: .folio,
        display: ["Caveat", "Bradley Hand", "Segoe Script", "Comic Sans MS"],  // face.css:771
        body: ["Caveat", "Bradley Hand", "Segoe Script", "Comic Sans MS"],     // face.css:771
        mono: ["IBM Plex Mono", "SF Mono", "Menlo"],                           // tokens.css:52
        displayDesign: .serif,
        bodyDesign: .serif
    )
    public var displayFont: Font { Self.typography.font(.display, size: 30, relativeTo: .title) }
    public var bodyFont: Font { Self.typography.font(.body, size: 19, relativeTo: .body) }
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
    // Hand-drawn corners: the web writes four different radii nudged by the
    // boil vars around a ~12.5px base, ±2px (face.css:1259-1263).
    public let cornerStyle: CornerStyle = .handDrawn(base: 12.5, jitter: 2)
    // Shared web chrome; see FaceLayout. This face does not depart from it.
    public let layout = FaceLayout.webChrome
    public let eyeShape: EyeShape = .inkBlot            // face.css:826-853
    public let celebration: Celebration = .stampedStar  // COMPANION_SCREENS.md

    public let eyeGeometry = EyeGeometry(
        eyeWidth: 80,      // face.css:774  --eye-w
        eyeHeight: 96,     // face.css:775  --eye-h
        eyesGap: 88,       // face.css:776  --eyes-gap
        faceGap: 42,       // face.css:777  --face-gap
        spreadGap: 120,    // face.css:819  .eyes.spread
        parkScale: 0.45,   // face.css:778  --park-scale
        ambientSize: 0,    // face.css:794  the halo is hidden outright
        glintSize: 0,      // face.css:855  .glint { display: none }
        glintInset: .zero
    )

    // MARK: The boil
    //
    // face.css:831-835 — the drawn mark's base border-radius is a calc over
    // two boil vars, so each held boil frame nudges every corner:
    //   h: 48%+a  52%-a  50%+b  50%-b
    //   v: 53%-b  47%+b  52%+a  48%-a
    // a and b are px against the 80x96 eye box; resolved to fractions here so
    // the same frame reads correctly at any rendered size (park scale, watch).
    static let eyeBox = CGSize(width: 80, height: 96)   // face.css:774-775
    static func boilCorners(a: CGFloat, b: CGFloat) -> CornerRadii {
        let w = eyeBox.width, h = eyeBox.height
        return CornerRadii(
            topLeft: CGSize(width: 0.48 + a / w, height: 0.53 - b / h),
            topRight: CGSize(width: 0.52 - a / w, height: 0.47 + b / h),
            bottomRight: CGSize(width: 0.50 + b / w, height: 0.52 + a / h),
            bottomLeft: CGSize(width: 0.50 - b / w, height: 0.48 - a / h)
        )
    }

    // MARK: Motion
    public let motion = FaceMotion(
        breathePeriod: 7.0,          // face.css:799  animation: folio-breathe 7s
        breatheRise: 5,              // face.css:803  translateY(-5px)
        blinkDuration: 0.12,         // face.css:845  transition: transform 0.12s
        blinkHold: 0.11,             // app.js:184    setTimeout(…, 110)
        blinkInterval: 2.6...6.2,    // app.js:186    2600 + random * 3600
        blinkSquash: 0.08,           // face.css:865  --blink-sy
        blinkStretch: 1.3,           // face.css:865  --blink-sx (cartoon widen)
        emotionDuration: 0.25,       // face.css:1010
        emotionCurve: TimingCurve(0.34, 1.3, 0.64, 1),  // face.css:1010
        releaseDuration: 0.30,       // app.js:284    emote-ease window
        boilPeriod: 0.42,            // face.css:851  folio-boil 0.42s steps(1, end)
        boilSteps: 3,                // face.css:858-863  three held poses (~7fps)
        boilPoses: [                 // face.css:859-861, resolved via boilCorners
            BoilPose(corners: boilCorners(a: 0, b: 2), rotation: .degrees(0)),
            BoilPose(corners: boilCorners(a: 3, b: -1), rotation: .degrees(0.9)),
            BoilPose(corners: boilCorners(a: -2, b: 1), rotation: .degrees(-0.8))
        ],
        celebrationHold: 1.4,        // app.js:5939   emote("celebrate", 1400)
        heartHold: 0.9,              // app.js:5753   emote("heart", 900)
        swapFade: 0.14,              // app.js:457-464  swapMode 140ms
        dealDuration: 0.26,          // clarify.css:229 clarifyDeal 0.26s
        dealStagger: 0.04,           // clarify.css:232-238  40ms apart
        revealRise: 6,               // clarify.css:240 translateY(6px)
        haptic: .thunk,              // COMPANION_SCREENS.md
        idle: IdleMotion(
            glanceInterval: 4.5...9.0,    // app.js:213  coarse pointer branch
            glanceDuration: 2.4,          // face.css:1509 .eyes.glance is unscoped,
            glanceDrift: [-7, 5],         // face.css:1512-1513 so all faces share it
            thinkLookPeriod: 2.4,         // face.css:912  folio-look 2.4s
            thinkLookDrift: [-12, 9, -4], // face.css:926-931
            shimmerPeriod: nil,           // folio thinks in the SCRIBBLED LOOP
            shimmerPeak: 1,               // (face.css:914-921), not in brightness
            haloPeriod: 7.0,              // unused: no halo (face.css:794)
            haloScale: 1
        ),
        beats: BeatMotion(
            bouncePeriod: 0.32,           // face.css:1120 folio-stamp 0.32s
            bounceCount: 1,
            bounceRise: nil,              // folio's celebrate stamps a star above
                                          // the pair (face.css:1114) and never
                                          // bounces it
            slowBlinkClose: 0.45,         // app.js:305    transform 0.45s
            slowBlinkHold: 0.48,          // app.js:309    setTimeout(…, 480)
            doubleBlinkGap: 0.18,         // app.js:181    setTimeout(…, 180)
            stamp: StampBeat(             // face.css:1114-1126
                size: 58,                       // face.css:1116
                rise: 84,                       // face.css:1115  top: -84px
                startScale: 2.1,                // face.css:1124
                startRotation: .degrees(-14),   // face.css:1124
                landedRotation: .degrees(-4),   // face.css:1125
                period: 0.32,                   // face.css:1121
                steps: 2                        // face.css:1121  steps(2, end)
            )
        )
    )

    // MARK: The vocabulary, in Folio's language
    //
    // The four transform channels and the corner sets, transcribed from
    // face.css:1012-1113, plus (P15-08) the parts that are not per-eye
    // channels: the dim-by-colour ink washes, the sorry smudge blur, the
    // worried tremble that replaces the base boil, and the heart lobes. The
    // boil itself and the stamped star live in `motion`, because they are
    // motion, not a pose.
    public let emotionPoses = EmotionPoseTable(
        // face.css:831-835 — the drawn mark's slightly irregular corners, at
        // the boil's rest pose (--boil-a 0px, --boil-b 2px, face.css:859)
        resting: EyePose(both: EyeChannels(
            corners: FolioFace.boilCorners(a: 0, b: 2)
        )),
        poses: [
            // face.css:913
            .thinking: EyePose(both: EyeChannels(scaleY: 0.52)),
            // face.css:1016-1019
            .happy: EyePose(both: EyeChannels(
                scaleY: 0.48, translateY: -7,
                corners: .percent((50, 50, 16, 14), (68, 72, 8, 10))
            )),
            // face.css:1022-1025
            .wide: EyePose(both: EyeChannels(
                scaleX: 1.16, scaleY: 1.1,
                corners: .percent((50, 48, 52, 50), (52, 50, 48, 50))
            )),
            // face.css:1030-1036 — the SMUDGE: droop, wash toward grey
            // (colour, never opacity), and a hair of wet-ink blur
            .sorry: EyePose(
                left: EyeChannels(
                    scaleY: 0.78, translateY: 10,
                    corners: .percent((58, 42, 46, 50), (74, 40, 34, 44))
                ),
                right: EyeChannels(
                    scaleY: 0.78, translateY: 10,
                    corners: .percent((42, 58, 50, 46), (40, 74, 44, 34))
                ),
                ink: .dim,          // face.css:1032
                blur: 1.4           // face.css:1033  filter: blur(1.4px)
            ),
            // face.css:1039-1041
            .curious: EyePose(
                left: EyeChannels(scaleX: 1.06, scaleY: 1.12, translateY: -6),
                right: EyeChannels(scaleY: 0.76, translateY: 3),
                pairRotation: .degrees(3)
            ),
            // face.css:1045-1056 — inked hearts in the stamp red: rotated
            // body + the two round lobes; the boil rot keeps them hand-held
            .heart: EyePose(
                both: EyeChannels(
                    scaleX: 0.78, scaleY: 0.66, rotation: .degrees(-45),
                    corners: .percent((0, 0, 0, 12), (0, 0, 0, 12))
                ),
                ink: .heart,
                heartLobes: true
            ),
            // face.css:1060-1063
            .surprised: EyePose(both: EyeChannels(
                scaleX: 0.6, scaleY: 0.52, translateY: -12,
                corners: .percent((50, 50, 50, 50), (50, 50, 50, 50))
            )),
            // face.css:1066-1070 — heavy lids, ink washed grey (colour, not
            // opacity)
            .sleepy: EyePose(
                both: EyeChannels(
                    scaleY: 0.3, translateY: 12,
                    corners: .percent((42, 42, 50, 50), (22, 22, 74, 74))
                ),
                ink: .dim               // face.css:1068
            ),
            // face.css:1073-1076
            .proud: EyePose(both: EyeChannels(
                scaleY: 0.6, translateY: -9,
                corners: .percent((50, 50, 32, 30), (62, 64, 26, 24))
            )),
            // face.css:1101-1104
            .sheepish: EyePose(
                left: EyeChannels(
                    scaleY: 0.42, translateY: 3,
                    corners: .percent((62, 38, 55, 45), (70, 52, 30, 48))
                ),
                right: EyeChannels(
                    scaleY: 0.42, translateY: 3,
                    corners: .percent((38, 62, 45, 55), (52, 70, 48, 30))
                ),
                pairRotation: .degrees(-2.5),
                pairOffset: CGSize(width: 10, height: 0)
            ),
            // face.css:1081-1097 — the marks knit inward, with a faster,
            // harder tremble whose steps() jitter replaces the base boil for
            // the hold. The pose overrides the corners outright, so only the
            // tremble's rotation frames carry (same as the web, where the
            // class's border-radius beats the base calc but --boil-rot still
            // rides the composed transform).
            .worried: EyePose(
                left: EyeChannels(
                    scaleY: 0.8, translateY: 3, rotation: .degrees(9),
                    corners: .percent((62, 24, 50, 52), (70, 26, 46, 50))
                ),
                right: EyeChannels(
                    scaleY: 0.8, translateY: 3, rotation: .degrees(-9),
                    corners: .percent((24, 62, 52, 50), (26, 70, 50, 46))
                ),
                boilOverride: BoilLoop(
                    period: 0.26,                     // face.css:1091
                    poses: [                          // face.css:1093-1097
                        BoilPose(corners: nil, rotation: .degrees(1.6)),
                        BoilPose(corners: nil, rotation: .degrees(-1.4)),
                        BoilPose(corners: nil, rotation: .degrees(0.6))
                    ]
                )
            ),
            // face.css:1110-1113 — the happy arcs; the stamped star is
            // motion, carried by `beats.stamp`
            .celebrate: EyePose(both: EyeChannels(
                scaleY: 0.48, translateY: -7,
                corners: .percent((50, 50, 16, 14), (68, 72, 8, 10))
            ))
        ]
    )
}
