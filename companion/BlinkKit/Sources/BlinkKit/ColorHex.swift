import SwiftUI

// The one place a CSS hex string becomes a SwiftUI Color.
//
// This file holds NO colour values of its own. Every literal lives in exactly
// three files (CapsuleFace.swift, LumenFace.swift, FolioFace.swift), each one
// transcribed from the stylesheet line cited beside it. That is what keeps the
// web and the companion diffable, and what the P15-01 grep check enforces.

extension Color {
    /// `#rrggbb` or `#rrggbbaa`, the way the stylesheets write it.
    /// Returns a clear colour for a malformed string rather than guessing a
    /// plausible one (degrade, never fabricate).
    static func hex(_ value: String) -> Color {
        var digits = value
        if digits.hasPrefix("#") { digits.removeFirst() }
        guard digits.count == 6 || digits.count == 8,
              let packed = UInt64(digits, radix: 16) else {
            return .clear
        }
        let hasAlpha = digits.count == 8
        let r = Double((packed >> (hasAlpha ? 24 : 16)) & 0xFF) / 255
        let g = Double((packed >> (hasAlpha ? 16 : 8)) & 0xFF) / 255
        let b = Double((packed >> (hasAlpha ? 8 : 0)) & 0xFF) / 255
        let a = hasAlpha ? Double(packed & 0xFF) / 255 : 1
        return Color(.sRGB, red: r, green: g, blue: b, opacity: a)
    }

    /// `rgba(r, g, b, a)` with 0-255 channels, the way `--glow-rgb` and the
    /// shadow tokens are written.
    static func rgba(_ r: Int, _ g: Int, _ b: Int, _ a: Double = 1) -> Color {
        Color(.sRGB, red: Double(r) / 255, green: Double(g) / 255, blue: Double(b) / 255, opacity: a)
    }
}
