/**
 * OpenCode plugin that auto-injects the current session ID into all
 * wsl-chrome-mcp tool calls. This allows the MCP server to route each
 * chat session to its own Chrome window without the LLM needing to
 * know about sessions.
 *
 * Install: copy this file to ~/.config/opencode/plugins/
 */

import type { Plugin } from "@opencode-ai/plugin"

// New tool names (ChromeDevTools convention)
const NEW_TOOLS = [
  // Navigation
  "navigate_page",
  "list_pages",
  "select_page",
  "new_page",
  "close_page",
  "wait_for",
  // Input
  "click",
  "click_at",
  "fill",
  "fill_form",
  "hover",
  "drag",
  "press_key",
  "scroll",
  "handle_dialog",
  "upload_file",
  // Snapshot & Screenshot
  "take_snapshot",
  "take_screenshot",
  "generate_pdf",
  // Monitoring
  "get_console",
  "get_console_message",
  "get_network",
  "get_network_request",
  // Script
  "evaluate",
  "get_html",
  // Emulation
  "emulate",
  "resize_page",
  // Performance
  "performance_start_trace",
  "performance_stop_trace",
  "performance_analyze_insight",
]

// Build the full tool name set for matching
const CHROME_TOOLS = new Set(NEW_TOOLS.map((t) => `wsl-chrome-mcp_${t}`))

export const ChromeSessionPlugin: Plugin = async (_ctx) => {
  return {
    "tool.execute.before": async (
      input: { tool: string; sessionID: string; callID: string },
      output: { args: any },
    ) => {
      // Inject session_id for chrome MCP tools (old and new naming)
      if (
        input.tool.startsWith("wsl-chrome-mcp_chrome_") ||
        CHROME_TOOLS.has(input.tool)
      ) {
        output.args.session_id = input.sessionID
      }
    },
  }
}
