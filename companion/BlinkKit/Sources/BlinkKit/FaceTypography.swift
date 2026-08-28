import SwiftUI
#if canImport(UIKit)
import UIKit
#endif

// Type, resolved out loud.
//
// No font files ship in this repo (checked: zero .ttf/.otf/.woff anywhere),
// and P15-01 says not to download any. So each face declares the same family
// stack the web declares, and the resolver walks it and reports what actually
// landed. A fallback to San Francisco is allowed; a SILENT one is a bug, so
// `FontResolution` records it, the debug swatch screen prints it, and
// companion/README.md names it.

public enum FontRole: String, Sendable, CaseIterable {
    case display
    case body
    case mono
}

/// What a face asked for, and what the device gave it.
public struct FontResolution: Sendable, Identifiable, Equatable {
    public var id: String { faceID.rawValue + "." + role.rawValue }
    public let faceID: FaceID
    public let role: FontRole
    /// The family stack, in the order the stylesheet lists it.
    public let requested: [String]
    /// The family that actually resolved, or nil when nothing in the stack
    /// exists on this device and the system face stood in.
    public let resolved: String?

    public var isFallback: Bool { resolved != requested.first }
    public var usedSystemFont: Bool { resolved == nil }

    /// One honest line for the debug screen.
    public var summary: String {
        guard let resolved else {
            return "\(requested.first ?? "?") is missing, so this is the system face."
        }
        if resolved == requested.first { return "\(resolved), as designed." }
        return "\(requested.first ?? "?") is missing, so this is \(resolved)."
    }
}

/// A face's three type roles, declared as family stacks exactly as the CSS
/// declares them.
public struct FaceTypography: Sendable {
    public let faceID: FaceID
    public let display: [String]
    public let body: [String]
    public let mono: [String]
    /// The system design each stack falls back to when nothing resolves.
    public let displayDesign: Font.Design
    public let bodyDesign: Font.Design

    public init(
        faceID: FaceID,
        display: [String],
        body: [String],
        mono: [String],
        displayDesign: Font.Design,
        bodyDesign: Font.Design
    ) {
        self.faceID = faceID
        self.display = display
        self.body = body
        self.mono = mono
        self.displayDesign = displayDesign
        self.bodyDesign = bodyDesign
    }

    public func font(_ role: FontRole, size: CGFloat, relativeTo style: Font.TextStyle) -> Font {
        let stack = stack(for: role)
        if let family = Self.firstAvailable(in: stack) {
            return .custom(family, size: size, relativeTo: style)
        }
        return .system(style, design: design(for: role))
    }

    public var resolutions: [FontResolution] {
        FontRole.allCases.map { role in
            FontResolution(
                faceID: faceID,
                role: role,
                requested: stack(for: role),
                resolved: Self.firstAvailable(in: stack(for: role))
            )
        }
    }

    private func stack(for role: FontRole) -> [String] {
        switch role {
        case .display: return display
        case .body: return body
        case .mono: return mono
        }
    }

    private func design(for role: FontRole) -> Font.Design {
        switch role {
        case .display: return displayDesign
        case .body: return bodyDesign
        case .mono: return .monospaced
        }
    }

    /// Walks the stack and returns the first family this device can actually
    /// draw. Generic CSS keywords (`serif`, `sans-serif`, `system-ui`, …) are
    /// not family names, so they never resolve here; they land on the system
    /// design instead, which is what they mean.
    static func firstAvailable(in stack: [String]) -> String? {
        #if canImport(UIKit)
        for family in stack where UIFont(name: family, size: 12) != nil {
            return family
        }
        #endif
        return nil
    }
}
