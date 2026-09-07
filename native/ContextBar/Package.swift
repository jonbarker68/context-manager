// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "ContextBar",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(name: "ContextBar")
    ]
)
