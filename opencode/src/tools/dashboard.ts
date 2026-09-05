import { tool, type ToolDefinition } from "@opencode-ai/plugin";
import { writeDashboard } from "../dashboard/generator.js";

export function createDashboardTool(
  getDataDir: () => string,
  onBeforeGenerate?: () => void,
): ToolDefinition {
  return tool({
    description:
      "Generate and open the Token Optimizer dashboard. Shows quality trends, session history, " +
      "and daily stats in an interactive HTML page.",
    args: {
      days: tool.schema.number().optional().describe("Number of days to include (default 30)"),
    },
    async execute(args) {
      const dataDir = getDataDir();
      const days = Math.max(1, Math.min(args.days ?? 30, 365));

      try {
        // Roll live sessions into trends.db first so the dashboard is never
        // empty just because no session-end event has fired yet.
        try {
          onBeforeGenerate?.();
        } catch (err) {
          console.warn("[Token Optimizer] dashboard pre-flush failed:", err);
        }

        const outputPath = writeDashboard({ dataDir, days });

        const { execFileSync } = await import("node:child_process");
        const platform = process.platform;
        const hide = { windowsHide: true } as const;
        if (platform === "darwin") {
          execFileSync("open", [outputPath], hide);
        } else if (platform === "linux") {
          try { execFileSync("xdg-open", [outputPath], hide); } catch { execFileSync("sensible-browser", [outputPath], hide); }
        } else if (platform === "win32") {
          // NOT `cmd /c start "" <path>`. That hands the path to cmd.exe's
          // parser, and libuv's quote_cmd_arg quotes an argument only on
          // space/tab/quote -- never on & ^ | ( ). `&` is a legal Windows
          // account-name character, so C:\Users\R&D\...\dashboard.html arrived
          // unquoted, cmd split at the `&` and ran the tail as a second command
          // relative to the CWD. A bare image name `cmd` also resolves CWD-first.
          // rundll32 url.dll,FileProtocolHandler is the documented shell-open
          // trampoline: execFileSync passes the path as one real argv entry, so
          // no interpreter ever parses it. Addressed absolutely under %SystemRoot%.
          const systemRoot = process.env.SystemRoot || process.env.windir || "C:\\Windows";
          execFileSync(`${systemRoot}\\System32\\rundll32.exe`, ["url.dll,FileProtocolHandler", outputPath], hide);
        }

        return {
          title: "Dashboard Generated",
          output: `Dashboard written to ${outputPath} and opened in browser.\n\nShowing ${days} days of session data.`,
        };
      } catch (err) {
        return {
          title: "Dashboard Error",
          output: `Failed to generate dashboard: ${err instanceof Error ? err.message : String(err)}`,
        };
      }
    },
  });
}
