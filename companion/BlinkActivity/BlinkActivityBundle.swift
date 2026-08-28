import WidgetKit
import SwiftUI

// The BlinkActivity extension's entry point. It carries the focus-session Live
// Activity today; P15-09's home and lock-screen widgets join this bundle later.
@main
struct BlinkActivityBundle: WidgetBundle {
    var body: some Widget {
        BlinkFocusLiveActivity()
    }
}
