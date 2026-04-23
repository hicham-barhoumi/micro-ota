import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import { execSync } from 'child_process';

// ── project root resolution ───────────────────────────────────────────────────

// The folder containing config/ota.json — may differ from workspace root when
// VS Code is opened at a parent directory (e.g. the whole repo).
let _projectRoot: string | undefined;

async function resolveProjectRoot(): Promise<string | undefined> {
    const wsRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    if (!wsRoot) { return undefined; }

    // Fast path: workspace root is directly a micro-ota project
    if (fs.existsSync(path.join(wsRoot, 'config', 'ota.json'))) {
        return wsRoot;
    }

    // Slow path: search anywhere in the workspace (e.g. examples/serial/)
    const uris = await vscode.workspace.findFiles(
        '**/config/ota.json', '**/node_modules/**', 1
    );
    if (uris.length > 0) {
        // grandparent of ota.json: …/config/ota.json → …/
        return path.dirname(path.dirname(uris[0].fsPath));
    }
    return undefined;
}

// ── helpers ───────────────────────────────────────────────────────────────────

function uotaPath(): string {
    return vscode.workspace.getConfiguration('micro-ota').get<string>('uotaPath', 'uota');
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
            cwd: cwd ?? _projectRoot ?? vscode.workspace.workspaceFolders?.[0]?.uri.fsPath,
        });
    }
    _terminal.show(true);
    _terminal.sendText([uotaPath(), ...args].join(' '));
}

/** Ask the user for a file path via QuickPick of .bin files in the workspace. */
async function pickBinFile(): Promise<string | undefined> {
    const root = _projectRoot ?? vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
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
    { text: '$(zap) Fast',          tooltip: 'micro-ota: Fast OTA Push',        command: 'micro-ota.fast',      priority: 107 },
    { text: '$(cloud-upload) Full', tooltip: 'micro-ota: Full OTA Push',        command: 'micro-ota.full',      priority: 106 },
    { text: '$(terminal) Shell',    tooltip: 'micro-ota: Open Device Terminal', command: 'micro-ota.terminal',  priority: 105 },
    { text: '$(info) Info',         tooltip: 'micro-ota: Device Info',          command: 'micro-ota.info',      priority: 104 },
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
    const root = _projectRoot ?? vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    if (!root) {
        vscode.window.showErrorMessage('micro-ota: Open a folder first.');
        return;
    }
    if (fs.existsSync(path.join(root, 'config', 'ota.json'))) {
        const ok = await vscode.window.showWarningMessage(
            'config/ota.json already exists. Re-initialize?', 'Yes', 'No'
        );
        if (ok !== 'Yes') { return; }
        runInTerminal(['init', '--force'], root);
    } else {
        runInTerminal(['init'], root);
    }
}

function cmdBootstrap(): void { runInTerminal(['bootstrap']); }
function cmdInfo():      void { runInTerminal(['info']); }
function cmdVersion():   void { runInTerminal(['version']); }
function cmdServe():     void { runInTerminal(['serve']); }
function cmdBundle():    void { runInTerminal(['bundle', '--zip']); }
function cmdListen():    void { runInTerminal(['remoteio', 'listen']); }

function cmdFast(): void { runInTerminal(['fast']); }

async function cmdFull(): Promise<void> {
    const wipe = await vscode.window.showQuickPick(
        ['No — keep existing files', 'Yes — wipe device first'],
        { placeHolder: 'Wipe device before upload?' }
    );
    if (wipe === undefined) { return; }
    const args = ['full'];
    if (wipe.startsWith('Yes')) { args.push('--wipe'); }
    runInTerminal(args);
}

function cmdTerminal(): void { runInTerminal(['terminal']); }

async function cmdFlash(): Promise<void> {
    const firmware = await pickBinFile();
    if (!firmware) { return; }
    runInTerminal(['flash', firmware]);
}

// ── install check ─────────────────────────────────────────────────────────────

function isUotaInstalled(): boolean {
    try {
        // Use which/where to check PATH — avoids relying on uota's exit code
        const cmd = process.platform === 'win32' ? 'where uota' : 'which uota';
        execSync(cmd, { stdio: 'ignore', timeout: 3000 });
        return true;
    } catch {
        return false;
    }
}

async function promptInstall(context: vscode.ExtensionContext): Promise<void> {
    const wheelPath = context.asAbsolutePath(path.join('bin', 'micro_ota-latest.whl'));
    const bundled   = fs.existsSync(wheelPath);

    const action = await vscode.window.showWarningMessage(
        'micro-ota: the uota CLI is not installed.',
        bundled ? 'Install now' : 'Show instructions',
        'Ignore'
    );

    if (action === 'Install now') {
        const pip = process.platform === 'win32' ? 'pip' : 'pip3';
        const t = vscode.window.createTerminal({ name: 'micro-ota install' });
        t.show();
        t.sendText(`${pip} install "${wheelPath}"`);
    } else if (action === 'Show instructions') {
        vscode.env.openExternal(vscode.Uri.parse(
            'https://github.com/claudebarhoumi/micro-ota#install'
        ));
    }
}

// ── activation ────────────────────────────────────────────────────────────────

export async function activate(context: vscode.ExtensionContext): Promise<void> {
    const statusBarItems = createStatusBarItems();
    statusBarItems.forEach(item => context.subscriptions.push(item));

    const updateBar = async () => {
        _projectRoot = await resolveProjectRoot();
        statusBarItems.forEach(item => _projectRoot ? item.show() : item.hide());
    };

    await updateBar();

    const watcher = vscode.workspace.createFileSystemWatcher('**/config/ota.json');
    watcher.onDidCreate(() => updateBar());
    watcher.onDidDelete(() => updateBar());
    context.subscriptions.push(watcher);

    const reg = (id: string, fn: () => void | Promise<void>) =>
        context.subscriptions.push(vscode.commands.registerCommand(id, fn));

    reg('micro-ota.init',      cmdInit);
    reg('micro-ota.bootstrap', cmdBootstrap);
    reg('micro-ota.info',      cmdInfo);
    reg('micro-ota.fast',      cmdFast);
    reg('micro-ota.full',      cmdFull);
    reg('micro-ota.terminal',  cmdTerminal);
    reg('micro-ota.version',   cmdVersion);
    reg('micro-ota.flash',     cmdFlash);
    reg('micro-ota.serve',     cmdServe);
    reg('micro-ota.bundle',    cmdBundle);
    reg('micro-ota.listen',    cmdListen);

    if (!isUotaInstalled()) {
        promptInstall(context);
    }
}

export function deactivate(): void {
    _terminal?.dispose();
}
