import SwiftUI

/// The eye body: a rectangle whose four corners each carry their own
/// horizontal and vertical radius, exactly as CSS `border-radius` does.
///
/// This shape exists for one reason. On the web, the eyes tween into heart
/// lobes because CSS interpolates `border-radius` for free: the shape goes
/// from `40px` all round to `37px 37px 6px 6px` while the pair rotates and
/// closes, and the browser does the in-between frames. SwiftUI will not
/// interpolate a `RoundedRectangle`'s corner set, so the eight numbers become
/// `animatableData` and SwiftUI tweens them here instead. Every shape-changing
/// beat rides this: the happy crescent, the sorry droop, the sleepy lids, the
/// worried brows, and the heart.
///
/// Radii arrive as fractions of the box (see `CornerRadii`) and are resolved
/// against the rect at draw time, so the same pose reads correctly at any
/// size, including under the park scale.
public struct EyeBodyShape: Shape {
    public var radii: CornerRadii

    public init(radii: CornerRadii) {
        self.radii = radii
    }

    public var animatableData: CornerRadii {
        get { radii }
        set { radii = newValue }
    }

    public func path(in rect: CGRect) -> Path {
        // Resolve fractions to points.
        var tl = CGSize(width: radii.topLeft.width * rect.width,
                        height: radii.topLeft.height * rect.height)
        var tr = CGSize(width: radii.topRight.width * rect.width,
                        height: radii.topRight.height * rect.height)
        var br = CGSize(width: radii.bottomRight.width * rect.width,
                        height: radii.bottomRight.height * rect.height)
        var bl = CGSize(width: radii.bottomLeft.width * rect.width,
                        height: radii.bottomLeft.height * rect.height)

        for r in [tl, tr, br, bl] where r.width < 0 || r.height < 0 {
            _ = r
            return Path(rect)
        }

        // CSS overlap rule: if the radii along any edge exceed that edge, all
        // eight scale down by the same factor. Without this, a pose like
        // `62% 62% 6% 6%` vertically would push corners past each other and
        // the outline would fold in on itself mid-tween.
        let factors: [CGFloat] = [
            rect.width / max(tl.width + tr.width, .leastNonzeroMagnitude),
            rect.width / max(bl.width + br.width, .leastNonzeroMagnitude),
            rect.height / max(tl.height + bl.height, .leastNonzeroMagnitude),
            rect.height / max(tr.height + br.height, .leastNonzeroMagnitude)
        ]
        let f = min(1, factors.min() ?? 1)
        if f < 1 {
            tl = CGSize(width: tl.width * f, height: tl.height * f)
            tr = CGSize(width: tr.width * f, height: tr.height * f)
            br = CGSize(width: br.width * f, height: br.height * f)
            bl = CGSize(width: bl.width * f, height: bl.height * f)
        }

        // A quarter ellipse as one cubic. kappa is the standard circular
        // approximation constant; it holds for ellipses because the two axes
        // scale independently.
        let k: CGFloat = 0.5522847498307936

        var path = Path()
        path.move(to: CGPoint(x: rect.minX + tl.width, y: rect.minY))

        path.addLine(to: CGPoint(x: rect.maxX - tr.width, y: rect.minY))
        path.addCurve(
            to: CGPoint(x: rect.maxX, y: rect.minY + tr.height),
            control1: CGPoint(x: rect.maxX - tr.width + tr.width * k, y: rect.minY),
            control2: CGPoint(x: rect.maxX, y: rect.minY + tr.height - tr.height * k)
        )

        path.addLine(to: CGPoint(x: rect.maxX, y: rect.maxY - br.height))
        path.addCurve(
            to: CGPoint(x: rect.maxX - br.width, y: rect.maxY),
            control1: CGPoint(x: rect.maxX, y: rect.maxY - br.height + br.height * k),
            control2: CGPoint(x: rect.maxX - br.width + br.width * k, y: rect.maxY)
        )

        path.addLine(to: CGPoint(x: rect.minX + bl.width, y: rect.maxY))
        path.addCurve(
            to: CGPoint(x: rect.minX, y: rect.maxY - bl.height),
            control1: CGPoint(x: rect.minX + bl.width - bl.width * k, y: rect.maxY),
            control2: CGPoint(x: rect.minX, y: rect.maxY - bl.height + bl.height * k)
        )

        path.addLine(to: CGPoint(x: rect.minX, y: rect.minY + tl.height))
        path.addCurve(
            to: CGPoint(x: rect.minX + tl.width, y: rect.minY),
            control1: CGPoint(x: rect.minX, y: rect.minY + tl.height - tl.height * k),
            control2: CGPoint(x: rect.minX + tl.width - tl.width * k, y: rect.minY)
        )

        path.closeSubpath()
        return path
    }
}
