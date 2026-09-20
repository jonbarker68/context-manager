const vscode = require("vscode");
const { execFile } = require("child_process");

function ctxCommand() {
    return vscode.workspace.getConfiguration("ctx").get("command", "ctx");
}

function runCtx(args) {
    return new Promise((resolve, reject) => {
        execFile(
            ctxCommand(),
            args,
            { maxBuffer: 10 * 1024 * 1024 },
            (error, stdout, stderr) => {
                if (error) {
                    const message = (stderr || stdout || error.message).trim();
                    reject(new Error(message));
                    return;
                }
                resolve(stdout);
            }
        );
    });
}

async function canonicalNotes() {
    const stdout = await runCtx(["_notes"]);
    return stdout
        .split(/\r?\n/)
        .map((line) => line.trim())
        .filter(Boolean);
}

async function fileTodo() {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
        vscode.window.showErrorMessage("CTX: No active editor.");
        return;
    }

    if (editor.document.isUntitled) {
        vscode.window.showErrorMessage("CTX: Save the source note before filing a TODO.");
        return;
    }

    // ctx uses one-based line numbers; VS Code uses zero-based positions.
    const source = editor.document.uri.fsPath;
    const line = editor.selection.active.line + 1;

    let notes;
    try {
        notes = await canonicalNotes();
    } catch (error) {
        vscode.window.showErrorMessage(`CTX: Could not load notes: ${error.message}`);
        return;
    }

    if (notes.length === 0) {
        vscode.window.showWarningMessage("CTX: No notes found.");
        return;
    }

    const destination = await vscode.window.showQuickPick(notes, {
        title: "CTX: File TODO",
        placeHolder: "Choose the destination note",
        matchOnDescription: true,
        matchOnDetail: true
    });

    if (!destination) {
        return;
    }

    // Save first so ctx operates on exactly what the user can see.
    if (editor.document.isDirty) {
        const saved = await editor.document.save();
        if (!saved) {
            vscode.window.showErrorMessage("CTX: Could not save the source note.");
            return;
        }
    }

    try {
        const stdout = await runCtx([
            "todo",
            "file",
            "--source",
            source,
            "--line",
            String(line),
            "--destination",
            destination
        ]);

        // ctx edits the file externally. Reload the active document so the
        // newly-created ghost is visible immediately.
        await vscode.commands.executeCommand("workbench.action.files.revert");

        const message = stdout.trim() || `Filed TODO to ${destination}`;
        vscode.window.showInformationMessage(`CTX: ${message}`);
    } catch (error) {
        vscode.window.showErrorMessage(`CTX: ${error.message}`);
    }
}

async function syncTodos() {
    const editor = vscode.window.activeTextEditor;

    // Avoid overwriting unsaved Markdown edits when ctx scans/writes notes.
    if (editor && editor.document.isDirty) {
        const saved = await editor.document.save();
        if (!saved) {
            vscode.window.showErrorMessage("CTX: Could not save the active document.");
            return;
        }
    }

    try {
        const stdout = await runCtx(["todo", "sync"]);

        // Sync may have changed the active note or todo.md externally.
        if (editor) {
            await vscode.commands.executeCommand("workbench.action.files.revert");
        }

        vscode.window.showInformationMessage(
            `CTX: ${stdout.trim() || "TODO sync complete."}`
        );
    } catch (error) {
        vscode.window.showErrorMessage(`CTX: ${error.message}`);
    }
}

function activate(context) {
    context.subscriptions.push(
        vscode.commands.registerCommand("ctx.fileTodo", fileTodo),
        vscode.commands.registerCommand("ctx.syncTodos", syncTodos)
    );
}

function deactivate() {}

module.exports = {
    activate,
    deactivate
};
