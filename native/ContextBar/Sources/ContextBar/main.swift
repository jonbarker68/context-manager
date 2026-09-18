import AppKit
import Foundation

struct ContextStatus: Decodable {
    let currentSpace: String?
    let currentContext: String?
    let contexts: [OpenContext]

    enum CodingKeys: String, CodingKey {
        case currentSpace = "current_space"
        case currentContext = "current_context"
        case contexts
    }
}

struct OpenContext: Decodable {
    let id: String
    let name: String
    let description: String?
    let space: String
    let current: Bool
}

enum CtxError: LocalizedError {
    case executableNotFound
    case commandFailed(String)
    case invalidOutput(String)

    var errorDescription: String? {
        switch self {
        case .executableNotFound:
            return "Could not find the ctx executable."
        case .commandFailed(let message):
            return message
        case .invalidOutput(let message):
            return message
        }
    }
}

final class CtxRunner: @unchecked Sendable {
    private let executableURL: URL

    init() throws {
        guard let url = Self.findExecutable() else {
            throw CtxError.executableNotFound
        }
        executableURL = url
    }

    func status() throws -> ContextStatus {
        let data = try run(["status", "--json"])
        do {
            return try JSONDecoder().decode(ContextStatus.self, from: data)
        } catch {
            let text = String(data: data, encoding: .utf8) ?? "<non-UTF8 output>"
            throw CtxError.invalidOutput("ctx status --json returned invalid JSON:\n\(text)")
        }
    }

    func switchContext(id: String) throws {
        _ = try run(["switch", id])
    }

    func closeContext(id: String) throws {
        _ = try run(["close", id])
    }

    func repairSpaces() throws {
        _ = try run(["repair"])
    }

    private func run(_ arguments: [String]) throws -> Data {
        let process = Process()
        let stdout = Pipe()
        let stderr = Pipe()

        process.executableURL = executableURL
        process.arguments = arguments
        process.standardOutput = stdout
        process.standardError = stderr

        // Login Items are launched with a minimal GUI environment rather than
        // the user's interactive shell environment. ctx itself launches tools
        // such as yabai by name, so give it the standard executable paths it
        // would normally see from a shell.
        var environment = ProcessInfo.processInfo.environment
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let requiredPaths = [
            "\(home)/.local/bin",
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin"
        ]
        let inheritedPath = environment["PATH"] ?? ""
        environment["PATH"] = (requiredPaths + [inheritedPath])
            .filter { !$0.isEmpty }
            .joined(separator: ":")
        process.environment = environment

        try process.run()
        process.waitUntilExit()

        let output = stdout.fileHandleForReading.readDataToEndOfFile()
        let errorData = stderr.fileHandleForReading.readDataToEndOfFile()

        guard process.terminationStatus == 0 else {
            let message = String(data: errorData, encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines)
            throw CtxError.commandFailed(
                message?.isEmpty == false
                ? message!
                : "ctx exited with status \(process.terminationStatus)."
            )
        }

        return output
    }

    private static func findExecutable() -> URL? {
        let home = FileManager.default.homeDirectoryForCurrentUser
        let candidates = [
            home.appendingPathComponent(".local/bin/ctx"),
            home.appendingPathComponent("bin/ctx"),
            URL(fileURLWithPath: "/opt/homebrew/bin/ctx"),
            URL(fileURLWithPath: "/usr/local/bin/ctx")
        ]

        for candidate in candidates where FileManager.default.isExecutableFile(atPath: candidate.path) {
            return candidate
        }

        let process = Process()
        let stdout = Pipe()
        process.executableURL = URL(fileURLWithPath: "/bin/zsh")
        process.arguments = [
            "-lc",
            #"PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"; command -v ctx"#
        ]
        process.standardOutput = stdout
        process.standardError = FileHandle.nullDevice

        do {
            try process.run()
            process.waitUntilExit()
            guard process.terminationStatus == 0 else { return nil }

            let data = stdout.fileHandleForReading.readDataToEndOfFile()
            guard let path = String(data: data, encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines),
                  !path.isEmpty else { return nil }

            let url = URL(fileURLWithPath: path)
            return FileManager.default.isExecutableFile(atPath: url.path) ? url : nil
        } catch {
            return nil
        }
    }
}

@MainActor
final class ContextBarController: NSObject, NSApplicationDelegate {
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)

    private var runner: CtxRunner?
    private var status: ContextStatus?
    private var errorMessage: String?
    private var timer: Timer?
    private var refreshInProgress = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        if let button = statusItem.button {
            button.image = NSImage(
                systemSymbolName: "square.grid.2x2",
                accessibilityDescription: "Context"
            )
            button.imagePosition = .imageLeading
            button.title = "ctx"
        }

        do {
            runner = try CtxRunner()
        } catch {
            errorMessage = error.localizedDescription
        }

        rebuildMenu()
        refresh()

        timer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        timer?.invalidate()
    }

    private func refresh() {
        guard !refreshInProgress, let runner else {
            rebuildMenu()
            return
        }

        refreshInProgress = true

        Task.detached { [weak self, runner] in
            do {
                let newStatus = try runner.status()
                await MainActor.run {
                    self?.status = newStatus
                    self?.errorMessage = nil
                    self?.refreshInProgress = false
                    self?.updateStatusItem()
                    self?.rebuildMenu()
                }
            } catch {
                await MainActor.run {
                    self?.errorMessage = error.localizedDescription
                    self?.refreshInProgress = false
                    self?.rebuildMenu()
                }
            }
        }
    }

    private func updateStatusItem() {
        guard let button = statusItem.button else { return }

        if let current = status?.contexts.first(where: { $0.current }) {
            button.title = current.name
        } else if let space = status?.currentSpace, !space.isEmpty {
            button.title = space
        } else {
            button.title = "ctx"
        }
    }

    private func rebuildMenu() {
        let menu = NSMenu()

        if let errorMessage {
            let item = NSMenuItem(title: errorMessage, action: nil, keyEquivalent: "")
            item.isEnabled = false
            menu.addItem(item)
            menu.addItem(.separator())
        }

        if let status {
            if status.contexts.isEmpty {
                let item = NSMenuItem(title: "No open contexts", action: nil, keyEquivalent: "")
                item.isEnabled = false
                menu.addItem(item)
            } else {
                for context in status.contexts {
                    let item = NSMenuItem(
                        title: context.name,
                        action: context.current ? nil : #selector(switchContext(_:)),
                        keyEquivalent: ""
                    )
                    item.representedObject = context.id
                    if context.current {
                        item.state = .on
                        item.isEnabled = false
                    }
                    menu.addItem(item)
                }
            }

            if let current = status.contexts.first(where: { $0.current }) {
                menu.addItem(.separator())
                let closeItem = NSMenuItem(
                    title: "Close \(current.name)",
                    action: #selector(closeCurrent(_:)),
                    keyEquivalent: ""
                )
                closeItem.representedObject = current.id
                menu.addItem(closeItem)
            }
        }

        menu.addItem(.separator())

        let refreshItem = NSMenuItem(title: "Refresh", action: #selector(refreshNow(_:)), keyEquivalent: "r")
        refreshItem.target = self
        menu.addItem(refreshItem)

        let repairItem = NSMenuItem(title: "Repair Spaces…", action: #selector(repairSpaces(_:)), keyEquivalent: "")
        repairItem.target = self
        menu.addItem(repairItem)

        menu.addItem(.separator())

        let quitItem = NSMenuItem(title: "Quit ContextBar", action: #selector(quit(_:)), keyEquivalent: "q")
        quitItem.target = self
        menu.addItem(quitItem)

        for item in menu.items where item.action != nil && item.target == nil {
            item.target = self
        }

        statusItem.menu = menu
    }

    @objc private func switchContext(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String, let runner else { return }
        runCommand { try runner.switchContext(id: id) }
    }

    @objc private func closeCurrent(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String, let runner else { return }
        runCommand { try runner.closeContext(id: id) }
    }

    @objc private func refreshNow(_ sender: NSMenuItem) {
        refresh()
    }

    @objc private func repairSpaces(_ sender: NSMenuItem) {
        guard let runner else { return }
        runCommand { try runner.repairSpaces() }
    }

    @objc private func quit(_ sender: NSMenuItem) {
        NSApp.terminate(nil)
    }

    private func runCommand(_ command: @escaping @Sendable () throws -> Void) {
        guard !refreshInProgress else { return }
        refreshInProgress = true

        Task.detached { [weak self] in
            do {
                try command()
                await MainActor.run {
                    self?.refreshInProgress = false
                    self?.refresh()
                }
            } catch {
                await MainActor.run {
                    self?.errorMessage = error.localizedDescription
                    self?.refreshInProgress = false
                    self?.rebuildMenu()

                    let alert = NSAlert()
                    alert.alertStyle = .warning
                    alert.messageText = "Context Manager"
                    alert.informativeText = error.localizedDescription
                    alert.addButton(withTitle: "OK")
                    NSApp.activate(ignoringOtherApps: true)
                    alert.runModal()
                }
            }
        }
    }
}

let app = NSApplication.shared
let delegate = ContextBarController()
app.delegate = delegate
app.run()
