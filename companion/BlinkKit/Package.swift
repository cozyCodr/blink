// swift-tools-version: 6.0
import PackageDescription

// BlinkKit — everything the companion targets share: face tokens, motion,
// models, formatting. It is a local Swift package so the iOS app, and later
// the widget and Live Activity extensions, all link the SAME token layer.
// Deployment target rationale lives in companion/README.md.
let package = Package(
    name: "BlinkKit",
    platforms: [.iOS(.v17)],
    products: [
        .library(name: "BlinkKit", targets: ["BlinkKit"])
    ],
    targets: [
        .target(name: "BlinkKit"),
        .testTarget(name: "BlinkKitTests", dependencies: ["BlinkKit"])
    ],
    // Language mode 5, matching the app target. Moving BlinkKit to strict
    // Swift 6 concurrency is a deliberate step, not something to inherit by
    // accident from the tools version.
    swiftLanguageModes: [.v5]
)
