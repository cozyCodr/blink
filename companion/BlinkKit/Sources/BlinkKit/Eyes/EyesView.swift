import SwiftUI

/// The eyes. The whole face, in fact: the ambient halo, the breath, the pair,
/// and the two eye bodies with their highlights — plus, where the face's
/// tokens say so, the connecting hairline (lumen), the line boil and the
/// stamped star (folio), and the heart's two lobes (both flat-ink faces).
///
/// The view asks the face for tokens and the rig for channel values, and
/// composes. It never asks WHICH face it is wearing: the hairline exists
/// because `eyeShape` says `.dot(joinedByHairline: true)`, the boil runs
/// because `motion.boilLoop` is non-nil, the star stamps because
/// `beats.stamp` is non-nil. Adding a fourth face stays a table, not a branch.
public struct EyesView: View {
    @Environment(\.face) private var face
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private let rig: EyeRig
    /// Scale for the whole rig, so a parked or watch-sized face is the same
    /// view at a different size.
    private let scale: CGFloat

    @State private var breathPhase = false
    @State private var haloPhase = false

    public init(rig: EyeRig, scale: CGFloat = 1) {
        self.rig = rig
        self.scale = scale
    }

    private var pose: EyePose {
        let raw = face.emotionPoses.pose(for: rig.emotion)
        guard reduceMotion, let name = rig.emotion else { return raw }
        return raw.reducedMotionVariant(for: name)
    }

    private var geometry: EyeGeometry { face.eyeGeometry }
    private var glowLevel: CGFloat { pose.glow ?? face.restingGlow }

    /// The boil this beat runs: the beat's own override (folio's worried
    /// tremble), or the face's base boil, or nothing. Reduced Motion stills
    /// the boil outright — the drawn irregular resting radius keeps the
    /// hand-inked read (face.css:1133-1139).
    private var activeBoil: BoilLoop? {
        guard !reduceMotion else { return nil }
        return pose.boilOverride ?? face.motion.boilLoop
    }

    public var body: some View {
        Group {
            if let boil = activeBoil {
                // Discrete held frames, exactly the web's steps(1, end):
                // each tick swaps the pose with no tween between frames —
                // the 6-8fps stop-motion wobble (face.css:849-851).
                TimelineView(.periodic(from: .now, by: boil.frameDuration)) { context in
                    pair(boil: boilFrame(of: boil, at: context.date))
                }
            } else {
                pair(boil: nil)
            }
        }
        .offset(y: breathOffset)
        // The halo is 640pt across, far wider than the pair. As a
        // background it paints behind the eyes without reporting a size,
        // so it can never push the layout around the way `--ambient-size`
        // cannot push the web's, where it is absolutely positioned.
        .background {
            if face.castsGlow {
                halo
            }
        }
        .scaleEffect(scale)
        .onAppear {
            rig.update(motion: face.motion, reduceMotion: reduceMotion)
            rig.start()
            startAmbientLoops()
        }
        .onDisappear { rig.stop() }
        .onChange(of: reduceMotion) { _, _ in
            rig.update(motion: face.motion, reduceMotion: reduceMotion)
            startAmbientLoops()
        }
        .onChange(of: face.id) { _, _ in
            // The face can change under a running rig. Motion is a token,
            // so the rig re-reads it rather than the view branching on
            // identity.
            rig.update(motion: face.motion, reduceMotion: reduceMotion)
            startAmbientLoops()
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(accessibilityLabel)
    }

    private func boilFrame(of loop: BoilLoop, at date: Date) -> BoilPose {
        let frame = Int(date.timeIntervalSinceReferenceDate / loop.frameDuration)
        return loop.poses[((frame % loop.poses.count) + loop.poses.count) % loop.poses.count]
    }

    // MARK: The pair

    private func pair(boil: BoilPose?) -> some View {
        ZStack {
            // The connecting hairline paints BENEATH the eye bodies and each
            // end runs under its dot, so no state — drift, tilt, scale, a
            // transition in flight — can ever open a gap between line and
            // dot. Safe because the dots are always fully opaque: dim beats
            // grey the INK, never the opacity (face.css:445-455).
            if hasHairline {
                hairline
            }
            HStack(spacing: pose.gap ?? geometry.eyesGap) {
                eye(pose.left, isRight: false, boil: boil)
                eye(pose.right, isRight: true, boil: boil)
                    .offset(x: pose.rightEyeInset)
            }
        }
        .rotationEffect(pose.pairRotation)
        // The stamped star lands ABOVE the pair (folio's celebrate,
        // face.css:1114-1126). It exists only while the beat holds, and only
        // on a face whose tokens declare one.
        .overlay(alignment: .top) {
            if rig.emotion == .celebrate, let stamp = face.motion.beats.stamp {
                StampedStar(stamp: stamp, color: face.heart, reduceMotion: reduceMotion)
                    .offset(y: -stamp.rise)   // face.css:1115  top: -84px
            }
        }
        .offset(
            x: pose.pairOffset.width + rig.glanceOffset + rig.thinkOffset,
            y: pose.pairOffset.height + rig.bounceOffset
        )
    }

    private var hasHairline: Bool {
        if case .dot(joinedByHairline: true) = face.eyeShape,
           geometry.hairlineThickness > 0 {
            return true
        }
        return false
    }

    /// face.css:445-461 — a 3pt ink line between the dots, each end
    /// underlapping its dot, tracking the gap as it animates.
    private var hairline: some View {
        Capsule()
            .fill(ink(for: pose.line.ink))
            .frame(
                width: (pose.gap ?? geometry.eyesGap) + 2 * geometry.hairlineUnderlap,
                height: geometry.hairlineThickness
            )
            .scaleEffect(x: pose.line.scaleX)          // face.css:658
            .offset(y: pose.line.offsetY)              // face.css:632, :674, :679
            .opacity(pose.line.opacity)                // the LINE may fade; dots never do
            .allowsHitTesting(false)
    }

    private func eye(_ channels: EyeChannels, isRight: Bool, boil: BoilPose?) -> some View {
        let size = CGSize(width: geometry.eyeWidth, height: geometry.eyeHeight)
        let resting = restingCorners(isRight: isRight, in: size)
        var corners = channels.corners ?? resting
        var rotation = channels.rotation
        if let boil {
            // The boil's corner nudge applies to the BASE radius only: a pose
            // that overrides its corners outright keeps them, exactly as a
            // CSS class's border-radius beats the base calc while --boil-rot
            // still rides the composed transform (face.css:842, :1007).
            if let boiled = boil.corners, corners == resting {
                corners = boiled
            }
            rotation += boil.rotation
        }
        return ZStack(alignment: .topLeading) {
            // The heart's two round lobes, drawn the way the web draws them:
            // two full-size circles on the body, one above, one to the right,
            // sharing the body's ink (face.css:648-654, :1050-1056).
            if pose.heartLobes {
                Ellipse()
                    .fill(ink(for: pose.ink))
                    .frame(width: size.width, height: size.height)
                    .offset(y: -size.height / 2)       // ::before  top: -50%
                Ellipse()
                    .fill(ink(for: pose.ink))
                    .frame(width: size.width, height: size.height)
                    .offset(x: size.width / 2)         // ::after   left: 50%
            }

            EyeBodyShape(radii: corners)
                .fill(bodyStyle)
                .modifier(EyeGlow(
                    face: face,
                    level: glowLevel,
                    // The heart drops the resting shadow (face.css:647).
                    shadowed: !pose.heartLobes
                ))

            if geometry.glintSize > 0 {
                glint(channels)
            }
        }
        .frame(width: size.width, height: size.height)
        // folio's sorry smudge: a thumb dragged through wet ink
        // (face.css:1033). A CSS blur radius is about twice SwiftUI's.
        .blur(radius: pose.blur / 2)
        // CSS applies `translateY() rotate() scaleX() scaleY()` right to left,
        // so the scale lands first, then the rotation, then the travel.
        .scaleEffect(
            x: channels.scaleX * rig.blinkScaleX,
            y: channels.scaleY * rig.blinkScaleY
        )
        .rotationEffect(rotation)
        .offset(y: channels.translateY)
        .brightness(pose.brightness - 1 + rig.shimmer)
        .accessibilityHidden(true)
        .id(isRight ? "right" : "left")
    }

    /// The eye bodies' fill. The gradient is capsule's; a beat that re-inks
    /// (dim, heart) paints flat, and dimming is a COLOUR change with the
    /// shape kept fully opaque (face.css:394, :767-768).
    private var bodyStyle: AnyShapeStyle {
        switch pose.ink {
        case .standard:
            return AnyShapeStyle(
                LinearGradient(
                    // face.css:97 — top, mid at 55%, bottom.
                    stops: [
                        .init(color: face.eyeInkTop, location: 0),
                        .init(color: face.eyeInkMid, location: 0.55),
                        .init(color: face.eyeInkBottom, location: 1)
                    ],
                    startPoint: .top,
                    endPoint: .bottom
                )
            )
        case .dim:
            return AnyShapeStyle(face.eyeInkDim)
        case .heart:
            return AnyShapeStyle(face.heart)
        }
    }

    /// The flat colour an ink role reads as, for pieces that never gradient
    /// (the hairline, the lobes).
    private func ink(for role: EyeInkRole) -> Color {
        switch role {
        case .standard: return face.eyeInkTop
        case .dim: return face.eyeInkDim
        case .heart: return face.heart
        }
    }

    /// face.css:110 — a soft white dot near the top-left of the eye body.
    private func glint(_ channels: EyeChannels) -> some View {
        Circle()
            .fill(Color.white.opacity(0.85))
            .blur(radius: 1)
            .frame(width: geometry.glintSize, height: geometry.glintSize)
            .scaleEffect(channels.glintScale)
            .offset(
                x: geometry.glintInset.width + channels.glintOffset.width,
                y: geometry.glintInset.height + channels.glintOffset.height
            )
            .opacity(channels.glintOpacity)
    }

    private func restingCorners(isRight: Bool, in size: CGSize) -> CornerRadii {
        let resting = face.emotionPoses.resting
        return (isRight ? resting.right.corners : resting.left.corners)
            ?? CornerRadii.points(face.cornerStyle.nominalRadius, in: size)
    }

    // MARK: Ambient halo

    private var halo: some View {
        Circle()
            .fill(
                RadialGradient(
                    // face.css:19 — the halo fades out entirely by 62%.
                    stops: [
                        .init(color: face.glow.opacity(face.ambientOpacity * Double(glowLevel)),
                              location: 0),
                        .init(color: face.glow.opacity(0), location: 0.62)
                    ],
                    center: .center,
                    startRadius: 0,
                    endRadius: geometry.ambientSize / 2
                )
            )
            .frame(width: geometry.ambientSize, height: geometry.ambientSize)
            .blur(radius: 12)                                // face.css:20
            .scaleEffect(haloPhase ? face.motion.idle.haloScale : 1)
            .allowsHitTesting(false)
    }

    // MARK: Loops that belong to the view

    private var breathOffset: CGFloat {
        breathPhase ? -face.motion.breatheRise : 0
    }

    private func startAmbientLoops() {
        guard !reduceMotion else {
            // face.css:1517 stands every ambient loop down. The eyes stay
            // exactly where they are, which is the point: legible still.
            withAnimation(nil) {
                breathPhase = false
                haloPhase = false
            }
            return
        }
        withAnimation(face.motion.breatheAnimation) { breathPhase = true }
        withAnimation(
            .easeInOut(duration: face.motion.idle.haloPeriod / 2)
                .repeatForever(autoreverses: true)
        ) { haloPhase = true }
    }

    // MARK: Honesty

    /// VoiceOver gets the beat by name, and nothing more. The face is
    /// expression, not information, so it never narrates a claim the rest of
    /// the screen has not already made.
    private var accessibilityLabel: String {
        guard let emotion = rig.emotion else { return "Blink is here, resting" }
        return "Blink looks \(emotion.rawValue)"
    }
}

// MARK: - The glow

/// Capsule's two-layer outer glow, scaled by `--glow`. Faces that do not glow
/// get a soft drop shadow instead of nothing, because a flat-ink eye on a
/// light ground still needs to sit on the page (face.css:420, face.css:794).
private struct EyeGlow: ViewModifier {
    let face: any FaceTokens
    let level: CGFloat
    /// The heart drops the resting shadow (face.css:647 `box-shadow: none`).
    var shadowed: Bool = true

    func body(content: Content) -> some View {
        if face.castsGlow {
            // face.css:98-99 — a 30px and a 70px shadow, both scaled by
            // --glow. A CSS blur radius is about twice SwiftUI's.
            content
                .shadow(color: face.glow.opacity(0.75 * Double(level)), radius: 15 * level)
                .shadow(color: face.glow.opacity(0.40 * Double(level)), radius: 35 * level)
        } else if shadowed {
            content
                .shadow(color: face.glow.opacity(0.18), radius: 6, y: 3)
        } else {
            content
        }
    }
}

// MARK: - The stamped star

/// folio's celebrate: a five-pointed star thunks down like a rubber stamp —
/// hard held poses, no tween between them (face.css:1114-1126, `folio-stamp
/// 0.32s steps(2, end) both`). Reduced Motion shows it landed at once, which
/// is exactly what killing the CSS animation leaves visible (face.css:1133).
private struct StampedStar: View {
    let stamp: StampBeat
    let color: Color
    let reduceMotion: Bool

    /// 0 is the start pose, `stamp.steps` is landed. Advanced in hard steps.
    @State private var step = 0

    var body: some View {
        StarShape()
            .fill(color)
            .frame(width: stamp.size, height: stamp.size)
            .scaleEffect(scale)
            .rotationEffect(rotation)
            .opacity(opacity)
            .allowsHitTesting(false)
            .task {
                guard !reduceMotion else {
                    step = stamp.steps
                    return
                }
                step = 0
                let frame = stamp.period / Double(max(stamp.steps, 1))
                for next in 1...max(stamp.steps, 1) {
                    try? await Task.sleep(for: .seconds(frame))
                    guard !Task.isCancelled else { return }
                    // A hard pose swap, deliberately unanimated: steps(), not
                    // a tween. That IS the thunk.
                    step = next
                }
            }
    }

    /// How far down the stamp this pose is: 0 at the start keyframe, 1 landed.
    private var progress: Double {
        min(1, Double(step) / Double(max(stamp.steps, 1)))
    }

    private var scale: CGFloat {
        stamp.startScale + (1 - stamp.startScale) * progress
    }

    private var rotation: Angle {
        .degrees(stamp.startRotation.degrees
                 + (stamp.landedRotation.degrees - stamp.startRotation.degrees) * progress)
    }

    private var opacity: Double { progress }   // face.css:1124  from opacity: 0
}

/// The star polygon, point for point from the web's clip-path
/// (face.css:1118-1119), as fractions of the box.
private struct StarShape: Shape {
    static let points: [CGPoint] = [
        CGPoint(x: 0.50, y: 0.00), CGPoint(x: 0.61, y: 0.35),
        CGPoint(x: 0.98, y: 0.35), CGPoint(x: 0.68, y: 0.57),
        CGPoint(x: 0.79, y: 0.91), CGPoint(x: 0.50, y: 0.70),
        CGPoint(x: 0.21, y: 0.91), CGPoint(x: 0.32, y: 0.57),
        CGPoint(x: 0.02, y: 0.35), CGPoint(x: 0.39, y: 0.35)
    ]

    func path(in rect: CGRect) -> Path {
        var path = Path()
        let scaled = Self.points.map {
            CGPoint(x: rect.minX + $0.x * rect.width, y: rect.minY + $0.y * rect.height)
        }
        guard let first = scaled.first else { return path }
        path.move(to: first)
        for point in scaled.dropFirst() {
            path.addLine(to: point)
        }
        path.closeSubpath()
        return path
    }
}
