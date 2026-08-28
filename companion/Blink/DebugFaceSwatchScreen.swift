import SwiftUI
import BlinkKit

// DEBUG SCAFFOLDING — not product UI.
//
// One row per face, proving all three token sets resolve on device: the
// colours, the type (with an honest note when a family fell back), the corner
// style, and the motion numbers. P15-02 replaces this screen with the eyes.

struct DebugFaceSwatchScreen: View {
    @Environment(FaceProvider.self) private var faces
    @Environment(\.face) private var face

    var body: some View {
        ZStack {
            face.ground.ignoresSafeArea()

            ScrollView {
                VStack(alignment: .leading, spacing: 28) {
                    header
                    picker
                    ForEach(Array(Faces.all.enumerated()), id: \.offset) { _, tokens in
                        FaceCard(tokens: tokens)
                    }
                    footer
                }
                .padding(20)
            }
        }
        .animation(face.motion.emotionAnimation, value: faces.faceID)
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Face tokens")
                .font(face.displayFont)
                .foregroundStyle(face.ink)
            Text("Debug scaffolding for P15-01. Every swatch below reads straight off a FaceTokens conformance.")
                .font(face.bodyFont)
                .foregroundStyle(face.muted)
        }
    }

    private var picker: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("This screen is wearing")
                .font(face.bodyFont)
                .foregroundStyle(face.faint)
            HStack(spacing: 10) {
                ForEach(FaceID.allCases) { id in
                    Button {
                        faces.select(id)
                    } label: {
                        Text(Faces.tokens(for: id).displayName)
                            .font(face.bodyFont)
                            .foregroundStyle(faces.faceID == id ? face.ground : face.ink)
                            .padding(.horizontal, 14)
                            .padding(.vertical, 9)
                            .background(
                                RoundedRectangle(cornerRadius: face.cornerStyle.nominalRadius)
                                    .fill(faces.faceID == id ? face.accent : face.control)
                            )
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Wear the \(Faces.tokens(for: id).displayName) face")
                }
            }
        }
    }

    private var footer: some View {
        Text("The face preference is stored on this device for now. It moves onto the account in P15-08.")
            .font(face.bodyFont)
            .foregroundStyle(face.faint)
    }
}

// MARK: - One face

private struct FaceCard: View {
    let tokens: any FaceTokens

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 2) {
                Text(tokens.displayName)
                    .font(tokens.displayFont)
                    .foregroundStyle(tokens.ink)
                Text(tokens.tagline)
                    .font(tokens.bodyFont)
                    .foregroundStyle(tokens.muted)
            }

            swatchRow(
                label: "Ground",
                items: [
                    ("ground", tokens.ground),
                    ("surface", tokens.surface),
                    ("control", tokens.control),
                    ("line", tokens.line)
                ]
            )
            swatchRow(
                label: "Ink and signal",
                items: [
                    ("ink", tokens.ink),
                    ("muted", tokens.muted),
                    ("accent", tokens.accent),
                    ("warm", tokens.warm),
                    ("alert", tokens.alert)
                ]
            )
            swatchRow(
                label: "Eyes",
                items: [
                    ("top", tokens.eyeInkTop),
                    ("mid", tokens.eyeInkMid),
                    ("bottom", tokens.eyeInkBottom),
                    ("dim", tokens.eyeInkDim),
                    ("heart", tokens.heart),
                    ("glow", tokens.glow)
                ]
            )

            detail("Corners", cornerDescription)
            detail("Eyes", eyeDescription)
            detail("Celebration", celebrationDescription)
            detail("Breath", "\(fmt(tokens.motion.breathePeriod))s, rising \(fmt(Double(tokens.motion.breatheRise)))pt")
            detail("Blink", "\(fmt(tokens.motion.blinkDuration))s close, squash \(fmt(Double(tokens.motion.blinkSquash))), every \(fmt(tokens.motion.blinkInterval.lowerBound)) to \(fmt(tokens.motion.blinkInterval.upperBound))s")
            detail("Boil", boilDescription)
            detail("Haptic", hapticDescription)

            VStack(alignment: .leading, spacing: 3) {
                ForEach(tokens.fontResolutions) { resolution in
                    Text("\(resolution.role.rawValue): \(resolution.summary)")
                        .font(tokens.monoFont)
                        .foregroundStyle(resolution.isFallback ? tokens.warm : tokens.muted)
                }
            }
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: tokens.cornerStyle.nominalRadius)
                .fill(tokens.ground)
        )
        .overlay(
            RoundedRectangle(cornerRadius: tokens.cornerStyle.nominalRadius)
                .stroke(tokens.line, lineWidth: 1)
        )
    }

    private func swatchRow(label: String, items: [(String, Color)]) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(label)
                .font(tokens.monoFont)
                .foregroundStyle(tokens.faint)
            HStack(spacing: 8) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    VStack(spacing: 4) {
                        RoundedRectangle(cornerRadius: tokens.cornerStyle.nominalRadius / 2)
                            .fill(item.1)
                            .overlay(
                                RoundedRectangle(cornerRadius: tokens.cornerStyle.nominalRadius / 2)
                                    .stroke(tokens.line, lineWidth: 1)
                            )
                            .frame(width: 44, height: 34)
                        Text(item.0)
                            .font(tokens.monoFont)
                            .foregroundStyle(tokens.faint)
                            .lineLimit(1)
                            .minimumScaleFactor(0.6)
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel("\(tokens.displayName) \(item.0) swatch")
                }
            }
        }
    }

    private func detail(_ label: String, _ value: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(label)
                .font(tokens.monoFont)
                .foregroundStyle(tokens.faint)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
                .frame(width: 104, alignment: .leading)
            Text(value)
                .font(tokens.monoFont)
                .foregroundStyle(tokens.ink)
        }
    }

    private var cornerDescription: String {
        switch tokens.cornerStyle {
        case .rounded(let r): return "rounded \(fmt(Double(r)))"
        case .squared(let r): return "squared \(fmt(Double(r)))"
        case .handDrawn(let base, let jitter):
            return "hand drawn, \(fmt(Double(base))) give or take \(fmt(Double(jitter)))"
        }
    }

    private var eyeDescription: String {
        let size = "\(fmt(Double(tokens.eyeGeometry.eyeWidth)))x\(fmt(Double(tokens.eyeGeometry.eyeHeight)))"
        switch tokens.eyeShape {
        case .capsule(let radius): return "capsules \(size), corner \(fmt(Double(radius)))"
        case .dot(let joined): return joined ? "dots \(size), joined by a hairline" : "dots \(size)"
        case .inkBlot: return "ink blots \(size)"
        }
    }

    private var celebrationDescription: String {
        switch tokens.celebration {
        case .heartBurst: return "heart burst"
        case .confetti: return "confetti"
        case .stampedStar: return "stamped star"
        }
    }

    private var boilDescription: String {
        guard let period = tokens.motion.boilPeriod else { return "none, the outline holds still" }
        return "\(tokens.motion.boilSteps) poses every \(fmt(period))s"
    }

    private var hapticDescription: String {
        switch tokens.motion.haptic {
        case .warmDouble: return "warm double tap"
        case .crispSingle: return "crisp single"
        case .thunk: return "thunk"
        }
    }

    private func fmt(_ value: Double) -> String {
        value == value.rounded() ? String(Int(value)) : String(format: "%.2g", value)
    }
}
