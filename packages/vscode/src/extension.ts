import * as vscode from 'vscode';
import * as cp from 'child_process';
import * as path from 'path';
import * as fs from 'fs';

// ── helpers ───────────────────────────────────────────────────────────────────

function uotaPath(): string {
    return vscode.workspace.getConfiguration('micro-ota').get<string>('uotaPath', 'uota');
}

function workspaceRoot(): string | undefined {
    return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
}

/** Return path to ota.json in the workspace, or undefined. */
function findOtaJson(root: string): string | undefined {
    const p = path.join(root, 'ota.json');
    return fs.existsSync(p) ? p : undefined;
}

/**
 * Run a uota command in the VS Code integrated terminal.
 * The terminal is reused across calls (one terminal per session).
 */
let _terminal: vscode.Terminal | undefined;
function runInTerminal(args: string[], cwd?: string): void {
    if (!_terminal || _terminal.exitStatus !== undefined) {
        _terminal = vscode.window.createTerminal({
            name: 'micro-ota',
            cwd: cwd ?? workspaceRoot(),
        });
    }
    _terminal.show(true);
    _terminal.sendText([uotaPath(), ...args].join(' '));
}

/** Ask the user for a file path via QuickPick of .bin files in the workspace. */
async function pickBinFile(): Promise<string | undefined> {
    const root = workspaceRoot();
    if (!root) { return undefined; }
    const uris = await vscode.workspace.findFiles('**/*.bin', '**/node_modules/**', 20);
    if (uris.length === 0) {
        return vscode.window.showInputBox({ prompt: 'Path to .bin firmware file' });
    }
    const items = uris.map(u => ({ label: path.relative(root, u.fsPath), uri: u }));
    const pick  = await vscode.window.showQuickPick(items, { placeHolder: 'Select firmware .bin' });
    return pick?.label;
}

// ── status bar ────────────────────────────────────────────────────────────────

interface BarButton {
    text: string;
    tooltip: string;
    command: string;
    priority: number;
}

const BAR_BUTTONS: BarButton[] = [
    { text: '$(zap) Fast',          tooltip: 'micro-ota: Fast OTA Push',        command: 'micro-ota.fast',      priority: 106 },
    { text: '$(cloud-upload) Full', tooltip: 'micro-ota: Full OTA Push',        command: 'micro-ota.full',      priority: 105 },
    { text: '$(terminal) Shell',    tooltip: 'micro-ota: Open Device Terminal', command: 'micro-ota.terminal',  priority: 104 },
    { text: '$(tag) Version',       tooltip: 'micro-ota: Read Device Version',  command: 'micro-ota.version',   priority: 103 },
    { text: '$(radio-tower) Listen',tooltip: 'micro-ota: RemoteIO Listen',      command: 'micro-ota.listen',    priority: 102 },
    { text: '$(plug) Bootstrap',    tooltip: 'micro-ota: Bootstrap Device (Serial)', command: 'micro-ota.bootstrap', priority: 101 },
];

function createStatusBarItems(): vscode.StatusBarItem[] {
    return BAR_BUTTONS.map(btn => {
        const item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, btn.priority);
        item.text    = btn.text;
        item.tooltip = btn.tooltip;
        item.command = btn.command;
        return item;
    });
}

// ── command handlers ──────────────────────────────────────────────────────────

async function cmdInit(): Promise<void> {
    const root = workspaceRoot();
    if (!root) {
        vscode.window.showErrorMessage('micro-ota: Open a folder first.');
        return;
    }
    if (findOtaJson(root)) {
        const ok = await vscode.window.showWarningMessage(
            'ota.json already exists. Re-initialize?', 'Yes', 'No'
        );
        if (ok !== 'Yes') { return; }
        runInTerminal(['init', '--force'], root);
    } else {
        runInTerminal(['init'], root);
    }
}

function cmdBootstrap(): void {
    runInTerminal(['bootstrap']);
}

async function cmdFast(): Promise<void> {
    const transport = vscode.workspace.getConfiguration('micro-ota').get<string>('transport', 'wifi_tcp');
    runInTerminal(['fast', '--transport', transport]);
}

async function cmdFull(): Promise<void> {
    const wipe = await vscode.window.showQuickPick(
        ['No — keep existing files', 'Yes — wipe device first'],
        { placeHolder: 'Wipe device before upload?' }
    );
    if (wipe === undefined) { return; }
    const args = ['full'];
    if (wipe.startsWith('Yes')) { args.push('--wipe'); }
    const transport = vscode.workspace.getConfiguration('micro-ota').get<string>('transport', 'wifi_tcp');
    args.push('--transport', transport);
    runInTerminal(args);
}

function cmdTerminal(): void {
    const transport = vscode.workspace.getConfiguration('micro-ota').get<string>('transport', 'wifi_tcp');
    runInTerminal(['terminal', '--transport', transport]);
}

function cmdVersion(): void {
    runInTerminal(['version']);
}

async function cmdFlash(): Promise<void> {
    const firmware = await pickBinFile();
    if (!firmware) { return; }
    runInTerminal(['flash', firmware]);
}

function cmdServe(): void {
    runInTerminal(['serve']);
}

function cmdBundle(): void {
    runInTerminal(['bundle', '--zip']);
}

function cmdListen(): void {
    runInTerminal(['remoteio', 'listen']);
}

// ── activation ────────────────────────────────────────────────────────────────

export function activate(context: vscode.ExtensionContext): void {
    const statusBarItems = createStatusBarItems();
    statusBarItems.forEach(item => context.subscriptions.push(item));

    // Show status bar buttons only when ota.json is present
    const updateBar = () => {
        const root = workspaceRoot();
        const visible = !!(root && findOtaJson(root));
        statusBarItems.forEach(item => visible ? item.show() : item.hide());
    };
    updateBar();

    const watcher = vscode.workspace.createFileSystemWatcher('**/ota.json');
    watcher.onDidCreate(() => updateBar());
    watcher.onDidDelete(() => updateBar());
    context.subscriptions.push(watcher);

    // Register commands
    const reg = (id: string, fn: () => void | Promise<void>) =>
        context.subscriptions.push(vscode.commands.registerCommand(id, fn));

    reg('micro-ota.init',      cmdInit);
    reg('micro-ota.bootstrap', cmdBootstrap);
    reg('micro-ota.fast',      cmdFast);
    reg('micro-ota.full',      cmdFull);
    reg('micro-ota.terminal',  cmdTerminal);
    reg('micro-ota.version',   cmdVersion);
    reg('micro-ota.flash',     cmdFlash);
    reg('micro-ota.serve',     cmdServe);
    reg('micro-ota.bundle',    cmdBundle);
    reg('micro-ota.listen',    cmdListen);

    console.log('micro-ota extension activated');
}

export function deactivate(): void {
    _terminal?.dispose();
}
