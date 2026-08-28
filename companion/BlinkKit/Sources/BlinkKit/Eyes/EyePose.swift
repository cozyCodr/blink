import SwiftUI

// The channel model.
//
// docs/COMPANION_ARCHITECTURE.md §3: "the Swift model is the same four
// channels per eye (scaleX, scaleY, translateY, rotation), a table of target
// values per emotion per face, and one animation to drive them. Blink stays an
// independent channel and composes by multiplication."
//
// The CSS custom-property indirection is deliberately NOT ported. It exists
// because CSS has one `transform` property that any rule can clobber; these
// are plain stored properties, so nothing can clobber anything.

// MARK: - Corners

/// The four corners of an eye, each with its own horizontal and vertical
/// radius, held as FRACTIONS of the eye's width and height.
///
/// Fractions rather than points because that is how the CSS writes most of
/// them (`border-radius: 50% 50% 12% 12% / 62% 62% 6% 6%`), and because a
/// fraction interpolates cleanly whatever the eye's measured size turns out
/// to be. `width` is the horizontal radius, `height` the vertical one, in the
/// CSS's own before-slash / after-slash sense.
///
/// This is the type that makes the heart possible. SwiftUI will not
/// interpolate a `RoundedRectangle`'s corner set the way CSS interpolates
/// `border-radius`, so the radii become `animatableData` on a custom `Shape`
/// and SwiftUI tweens the eight numbers directly.
public struct CornerRadii: Sendable, Equatable, VectorArithmetic {
    /// Order matches CSS shorthand order: top-left, top-right, bottom-right,
    /// bottom-left.
    public var topLeft: CGSize
    public var topRight: CGSize
    public var bottomRight: CGSize
    public var bottomLeft: CGSize

    public init(topLeft: CGSize, topRight: CGSize, bottomRight: CGSize, bottomLeft: CGSize) {
        self.topLeft = topLeft
        self.topRight = topRight
        self.bottomRight = bottomRight
        self.bottomLeft = bottomLeft
    }

    /// `border-radius: h1 h2 h3 h4 / v1 v2 v3 v4`, as percentages, written in
    /// the same order and the same units the stylesheet writes them.
    public static func percent(
        _ h: (CGFloat, CGFloat, CGFloat, CGFloat),
        _ v: (CGFloat, CGFloat, CGFloat, CGFloat)
    ) -> CornerRadii {
        CornerRadii(
            topLeft: CGSize(width: h.0 / 100, height: v.0 / 100),
            topRight: CGSize(width: h.1 / 100, height: v.1 / 100),
            bottomRight: CGSize(width: h.2 / 100, height: v.2 / 100),
            bottomLeft: CGSize(width: h.3 / 100, height: v.3 / 100)
        )
    }

    /// `border-radius: a b c d` in points, converted against the eye it sits
    /// on. The CSS resolves a px radius against the box it is applied to, so
    /// the conversion needs that box.
    public static func points(
        _ values: (CGFloat, CGFloat, CGFloat, CGFloat),
        in size: CGSize
    ) -> CornerRadii {
        func corner(_ r: CGFloat) -> CGSize {
            CGSize(width: r / max(size.width, 1), height: r / max(size.height, 1))
        }
        return CornerRadii(
            topLeft: corner(values.0),
            topRight: corner(values.1),
            bottomRight: corner(values.2),
            bottomLeft: corner(values.3)
        )
    }

    /// `border-radius: <r>px` on all four corners.
    public static func points(_ radius: CGFloat, in size: CGSize) -> CornerRadii {
        points((radius, radius, radius, radius), in: size)
    }

    // MARK: VectorArithmetic

    public static let zero = CornerRadii(
        topLeft: .zero, topRight: .zero, bottomRight: .zero, bottomLeft: .zero
    )

    public static func + (lhs: CornerRadii, rhs: CornerRadii) -> CornerRadii {
        CornerRadii(
            topLeft: lhs.topLeft + rhs.topLeft,
            topRight: lhs.topRight + rhs.topRight,
            bottomRight: lhs.bottomRight + rhs.bottomRight,
            bottomLeft: lhs.bottomLeft + rhs.bottomLeft
        )
    }

    public static func - (lhs: CornerRadii, rhs: CornerRadii) -> CornerRadii {
        CornerRadii(
            topLeft: lhs.topLeft - rhs.topLeft,
            topRight: lhs.topRight - rhs.topRight,
            bottomRight: lhs.bottomRight - rhs.bottomRight,
            bottomLeft: lhs.bottomLeft - rhs.bottomLeft
        )
    }

    public mutating func scale(by rhs: Double) {
        topLeft = topLeft * rhs
        topRight = topRight * rhs
        bottomRight = bottomRight * rhs
        bottomLeft = bottomLeft * rhs
    }

    public var magnitudeSquared: Double {
        var total: Double = 0
        for corner in [topLeft, topRight, bottomRight, bottomLeft] {
            let w = Double(corner.width)
            let h = Double(corner.height)
            total += w * w + h * h
        }
        return total
    }
}

private func + (lhs: CGSize, rhs: CGSize) -> CGSize {
    CGSize(width: lhs.width + rhs.width, height: lhs.height + rhs.height)
}

private func - (lhs: CGSize, rhs: CGSize) -> CGSize {
    CGSize(width: lhs.width - rhs.width, height: lhs.height - rhs.height)
}

private func * (lhs: CGSize, rhs: Double) -> CGSize {
    CGSize(width: lhs.width * rhs, height: lhs.height * rhs)
}

// MARK: - One eye

/// The four transform channels for a single eye, plus the shape and highlight
/// the emotion also moves. Every field has a neutral default, so an emotion
/// declares only what its CSS block declares.
public struct EyeChannels: Sendable, Equatable {
    /// `--emo-sx`.
    public var scaleX: CGFloat = 1
    /// `--emo-sy`. Multiplies with the blink channel, never replaces it.
    public var scaleY: CGFloat = 1
    /// `--emo-ty`, points.
    public var translateY: CGFloat = 0
    /// `--emo-rot`.
    public var rotation: Angle = .zero
    /// The eye's outline. nil means the face's resting corners.
    public var corners: CornerRadii?
    /// The glint's `translate` channel, points.
    public var glintOffset: CGSize = .zero
    public var glintOpacity: Double = 1
    /// The glint's `scale` channel (surprised sharpens it to 0.8).
    public var glintScale: CGFloat = 1

    public init(
        scaleX: CGFloat = 1,
        scaleY: CGFloat = 1,
        translateY: CGFloat = 0,
        rotation: Angle = .zero,
        corners: CornerRadii? = nil,
        glintOffset: CGSize = .zero,
        glintOpacity: Double = 1,
        glintScale: CGFloat = 1
    ) {
        self.scaleX = scaleX
        self.scaleY = scaleY
        self.translateY = translateY
        self.rotation = rotation
        self.corners = corners
        self.glintOffset = glintOffset
        self.glintOpacity = glintOpacity
        self.glintScale = glintScale
    }
}

// MARK: - The pair

/// One emotion, fully described: both eyes, plus everything the CSS puts on
/// the pair rather than on an eye.
public struct EyePose: Sendable, Equatable {
    public var left: EyeChannels
    public var right: EyeChannels
    /// The pair's tilt (`.eyes.emote-curious { transform: rotate(2.5deg) }`).
    public var pairRotation: Angle = .zero
    /// The pair's own translate, points (sheepish leans away on some faces).
    public var pairOffset: CGSize = .zero
    /// An override for `--eyes-gap`, points. nil means the resting gap.
    public var gap: CGFloat?
    /// The right eye's `margin-left`, points. Negative overlaps the pair,
    /// which is how the heart closes.
    public var rightEyeInset: CGFloat = 0
    /// An override for `--glow`. nil means the face's `restingGlow`.
    public var glow: CGFloat?
    /// A CSS `filter: brightness(n)` on the eye bodies. 1 is untouched.
    public var brightness: Double = 1
    /// Whether this beat bounces the pair (celebrate).
    public var bounces: Bool = false

    public init(
        left: EyeChannels = EyeChannels(),
        right: EyeChannels = EyeChannels(),
        pairRotation: Angle = .zero,
        pairOffset: CGSize = .zero,
        gap: CGFloat? = nil,
        rightEyeInset: CGFloat = 0,
        glow: CGFloat? = nil,
        brightness: Double = 1,
        bounces: Bool = false
    ) {
        self.left = left
        self.right = right
        self.pairRotation = pairRotation
        self.pairOffset = pairOffset
        self.gap = gap
        self.rightEyeInset = rightEyeInset
        self.glow = glow
        self.brightness = brightness
        self.bounces = bounces
    }

    /// Both eyes doing the same thing, which is most emotions.
    public init(
        both: EyeChannels,
        pairRotation: Angle = .zero,
        pairOffset: CGSize = .zero,
        gap: CGFloat? = nil,
        rightEyeInset: CGFloat = 0,
        glow: CGFloat? = nil,
        brightness: Double = 1,
        bounces: Bool = false
    ) {
        self.init(
            left: both,
            right: both,
            pairRotation: pairRotation,
            pairOffset: pairOffset,
            gap: gap,
            rightEyeInset: rightEyeInset,
            glow: glow,
            brightness: brightness,
            bounces: bounces
        )
    }

    /// Reduced Motion does not flatten an emotion, it lands it instantly. Two
    /// beats are the exception, because the CSS says so at face.css:1527-1529:
    /// curious drops its pair tilt and the heart drops its lobe rotation. Both
    /// still change shape, so both stay legible as a still frame.
    public func reducedMotionVariant(for name: EmotionName) -> EyePose {
        var pose = self
        switch name {
        case .curious:
            pose.pairRotation = .zero              // face.css:1527
        case .heart:
            pose.left.rotation = .zero             // face.css:1528-1529
            pose.right.rotation = .zero
        case .celebrate:
            pose.bounces = false                   // face.css:287-289
        default:
            break
        }
        return pose
    }
}

// MARK: - The table

/// One face's whole emotion vocabulary. Adding a face means adding a table,
/// never editing a view.
public struct EmotionPoseTable: Sendable {
    /// Where the eyes sit when nothing is happening.
    public let resting: EyePose
    private let poses: [EmotionName: EyePose]

    public init(resting: EyePose, poses: [EmotionName: EyePose]) {
        self.resting = resting
        self.poses = poses
    }

    /// `satisfied` has no pose of its own by design: it is one slow blink over
    /// whatever the eyes were already doing, so it resolves to resting here
    /// and the rig drives the blink channel instead.
    public func pose(for name: EmotionName?) -> EyePose {
        guard let name else { return resting }
        return poses[name] ?? resting
    }

    /// Which beats this table actually describes. A face that has not been
    /// drawn yet can be honest about the gap instead of quietly showing
    /// resting eyes.
    public var describedEmotions: Set<EmotionName> { Set(poses.keys) }
}
