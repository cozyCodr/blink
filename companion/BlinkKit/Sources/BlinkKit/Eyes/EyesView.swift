import SwiftUI

/// The eyes. The whole face, in fact: the ambient halo, the breath, the pair,
/// and the two eye bodies with their highlights.
///
/// The view asks the face for tokens and the rig for channel values, and
/// composes. It never asks WHICH face it is wearing, which is the only reason
/// lumen and folio can drop in at P15-08 by adding a pose table rather than a
/// branch.
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

    public var body: some View {
        pair
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

    // MARK: The pair

    private var pair: some View {
        HStack(spacing: pose.gap ?? geometry.eyesGap) {
            eye(pose.left, isRight: false)
            eye(pose.right, isRight: true)
                .offset(x: pose.rightEyeInset)
        }
        .rotationEffect(pose.pairRotation)
        .offset(
            x: pose.pairOffset.width + rig.glanceOffset + rig.thinkOffset,
            y: pose.pairOffset.height + rig.bounceOffset
        )
    }

    private func eye(_ channels: EyeChannels, isRight: Bool) -> some View {
        let size = CGSize(width: geometry.eyeWidth, height: geometry.eyeHeight)
        let corners = channels.corners ?? restingCorners(in: size)
        return ZStack(alignment: .topLeading) {
            EyeBodyShape(radii: corners)
                .fill(
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
                .modifier(EyeGlow(face: face, level: glowLevel, corners: corners))

            if geometry.glintSize > 0 {
                glint(channels)
            }
        }
        .frame(width: size.width, height: size.height)
        // CSS applies `translateY() rotate() scaleX() scaleY()` right to left,
        // so the scale lands first, then the rotation, then the travel.
        .scaleEffect(
            x: channels.scaleX * rig.blinkScaleX,
            y: channels.scaleY * rig.blinkScaleY
        )
        .rotationEffect(channels.rotation)
        .offset(y: channels.translateY)
        .brightness(pose.brightness - 1 + rig.shimmer)
        .accessibilityHidden(true)
        .id(isRight ? "right" : "left")
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

    private func restingCorners(in size: CGSize) -> CornerRadii {
        face.emotionPoses.resting.left.corners
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
    let corners: CornerRadii

    func body(content: Content) -> some View {
        if face.castsGlow {
            // face.css:98-99 — a 30px and a 70px shadow, both scaled by
            // --glow. A CSS blur radius is about twice SwiftUI's.
            content
                .shadow(color: face.glow.opacity(0.75 * Double(level)), radius: 15 * level)
                .shadow(color: face.glow.opacity(0.40 * Double(level)), radius: 35 * level)
        } else {
            content
                .shadow(color: face.glow.opacity(0.18), radius: 6, y: 3)
        }
    }
}
