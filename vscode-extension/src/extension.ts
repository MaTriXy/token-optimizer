// Entry point: wire DataSource -> StatusBar, register commands. Rate limits come
// straight from the statusline sidecar on the snapshot (no network lookups).
import * as vscode from 'vscode';
import { resolvePathsForRuntime } from './paths';
import { DataSource } from './dataSource';
import { StatusBar } from './statusBar';
import { StatusPanel, PanelAction } from './statusPanel';
import { registerCommands } from './commands';
import { buildPanelModel } from './format';
import { Snapshot } from './types';

export function activate(context: vscode.ExtensionContext): void {
  const statusBar = new StatusBar();

  const cfg = () => vscode.workspace.getConfiguration('tokenOptimizer');
  const staleAfter = () => cfg().get<number>('staleAfterSeconds', 180);
  const runtime = () => cfg().get<string>('runtime', 'auto');

  let disposed = false;

  const statusPanel = new StatusPanel((action: PanelAction) => {
    void vscode.commands.executeCommand(`tokenOptimizer.${action}`);
  });

  const renderFrom = (snap: Snapshot): void => {
    if (disposed) return; // a queued render may resume mid-disposal
    try {
      statusBar.render(snap);
      statusPanel.update(buildPanelModel(snap, { nowMs: Date.now() }));
    } catch {
      // Rendering must never break the editor.
    }
  };

  // Build a DataSource for the current runtime setting.
  // Called once at activation and again whenever tokenOptimizer.runtime changes
  // so the new paths/session resolver take effect immediately.
  let currentRuntime = runtime();

  function createDataSource(): DataSource {
    const paths = resolvePathsForRuntime(currentRuntime);
    return new DataSource(paths, staleAfter, renderFrom);
  }

  let currentDataSource: DataSource = createDataSource();

  // Re-read from disk and recompute transcript estimates on explicit refresh.
  const refreshNow = () => currentDataSource.refresh(true);

  // Pass a getter so openDashboard resolves paths at call time, not at
  // activation time.  If the user switches runtime after activation the command
  // will use the new paths rather than the stale closure from startup.
  registerCommands(context, { getPaths: () => resolvePathsForRuntime(currentRuntime), onConfigChanged: refreshNow });

  // Clicking the status bar opens the expanded panel.
  context.subscriptions.push(
    vscode.commands.registerCommand('tokenOptimizer.showStatus', () => {
      refreshNow();
      statusPanel.show();
    })
  );

  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (!e.affectsConfiguration('tokenOptimizer')) return;
      const newRuntime = runtime();
      if (newRuntime !== currentRuntime) {
        // Runtime changed: tear down the old DataSource and start a new one.
        currentDataSource.dispose();
        currentRuntime = newRuntime;
        currentDataSource = createDataSource();
        currentDataSource.start();
      } else {
        currentDataSource.refresh(false);
      }
    })
  );

  // Disposal order (reverse of push): the `disposed` flag flips FIRST, before
  // dataSource and statusBar are torn down, so an in-flight renderFrom bails out
  // before touching a disposed status bar item.
  context.subscriptions.push(statusBar);
  context.subscriptions.push({ dispose: () => statusPanel.dispose() });
  context.subscriptions.push({ dispose: () => currentDataSource.dispose() });
  context.subscriptions.push({ dispose: () => { disposed = true; } });

  currentDataSource.start();
}

export function deactivate(): void {
  // Disposables registered on context.subscriptions are cleaned up by VS Code.
}
