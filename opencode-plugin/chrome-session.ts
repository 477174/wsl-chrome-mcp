/**
 * OpenCode plugin that auto-injects the current session ID into all
 * wsl-chrome-mcp tool calls. This allows the MCP server to route each
 * chat session to its own Chrome window without the LLM needing to
 * know about sessions.
 *
 * Install: copy this file to ~/.config/opencode/plugins/
 */

import type { Plugin } from "@opencode-ai/plugin"

export const ChromeSessionPlugin: Plugin = async (_ctx) => {
  return {
    "tool.execute.before": async (
      input: { tool: string; sessionID: string; callID: string },
      output: { args: any },
    ) => {
      // Only inject session_id for chrome MCP tools
      if (input.tool.startsWith("wsl-chrome-mcp_chrome_")) {
        output.args.session_id = input.sessionID
      }
    },
  }
}
