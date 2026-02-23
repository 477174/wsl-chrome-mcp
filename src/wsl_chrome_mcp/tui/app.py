from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Footer, Header, Input, Label, Select, Static, Switch

from ..config import load_config, save_config
from ..state import (
    OPENCODE_PLUGIN_DIR,
    PLUGIN_FILENAME,
    detect_system_state,
    discover_chrome_profiles,
)
from ..wslconfig import set_always_on_cdp, set_mirrored_networking

_SESSION_MODES: list[tuple[str, str]] = [
    ("Isolated (fresh context per session)", "isolated"),
    ("Profile (use existing Chrome profile)", "profile"),
]


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    return here.parent.parent.parent.parent


class ConfigRow(Horizontal):
    DEFAULT_CSS = """
    ConfigRow {
        height: auto;
        padding: 0 2;
        min-height: 1;
    }
    ConfigRow:hover {
        background: $surface-lighten-1;
    }
    ConfigRow .config-label {
        width: 24;
        padding: 0 1 0 0;
        color: $text-muted;
    }
    ConfigRow .config-value {
        width: 1fr;
    }
    """


class ConfigApp(App[None]):
    CSS_PATH = "app.tcss"
    TITLE = "WSL Chrome MCP"

    BINDINGS: list[BindingType] = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh_state", "Refresh"),
        Binding("s", "save", "Save"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._config = load_config()
        self._state = detect_system_state(self._config.chrome.debug_port)

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="main-scroll"):
            yield from self._compose_installation()
            yield from self._compose_network()
            yield from self._compose_chrome()
            yield from self._compose_sessions()
        yield Static("", id="status-bar")
        yield Footer()

    def _compose_installation(self) -> ComposeResult:
        yield Static(" INSTALLATION", classes="section-title")
        with ConfigRow():
            yield Label("MCP Server", classes="config-label")
            yield Static(self._mcp_status(), id="mcp-status", classes="config-value")
        with ConfigRow(id="plugin-row"):
            yield Label("OpenCode Plugin", classes="config-label")
            yield Static(self._plugin_status(), id="plugin-status", classes="config-value")
            if self._state.install.plugin_installed:
                yield Button("Uninstall", id="btn-uninstall-plugin", variant="error")
            else:
                yield Button("Install", id="btn-install-plugin", variant="success")

    def _compose_network(self) -> ComposeResult:
        yield Static(" NETWORK", classes="section-title")
        with ConfigRow():
            yield Label("WSL Version", classes="config-label")
            yield Static(self._state.wsl.version, classes="config-value")
        if self._state.wsl.windows_build:
            with ConfigRow():
                yield Label("Windows Build", classes="config-label")
                yield Static(self._state.wsl.windows_build, classes="config-value")
        with ConfigRow():
            yield Label("Mirrored Networking", classes="config-label")
            yield Switch(
                value=self._config.network.mirrored_networking,
                id="sw-mirrored",
            )

    def _compose_chrome(self) -> ComposeResult:
        yield Static(" CHROME", classes="section-title")
        with ConfigRow():
            yield Label("Status", classes="config-label")
            yield Static(self._chrome_status(), id="chrome-status", classes="config-value")
        with ConfigRow():
            yield Label("Debug Port", classes="config-label")
            yield Input(
                str(self._config.chrome.debug_port),
                id="port-input",
                type="integer",
                max_length=5,
            )
        with ConfigRow():
            yield Label("Headless", classes="config-label")
            yield Switch(value=self._config.chrome.headless, id="sw-headless")
        with ConfigRow():
            yield Label("Session Mode", classes="config-label")
            yield Select[str](
                _SESSION_MODES,
                id="session-mode-select",
                value=self._config.chrome.profile_mode,
                allow_blank=False,
            )
        profiles = discover_chrome_profiles()
        profile_options: list[tuple[str, str]] = [
            (f"{p.display_name} ({p.dir_name})", p.dir_name) for p in profiles
        ]
        with ConfigRow(id="profile-row"):
            yield Label("Profile", classes="config-label")
            if profile_options:
                profile_select: Select[str] = Select(
                    profile_options,
                    id="profile-select",
                    prompt="Select profile",
                )
                if self._config.chrome.profile_name:
                    profile_select.value = self._config.chrome.profile_name
                yield profile_select
            else:
                yield Static("[dim]No Chrome profiles found[/dim]", classes="config-value")
        with ConfigRow():
            yield Label("Always-on CDP", classes="config-label")
            yield Switch(value=self._config.cdp.always_on, id="sw-cdp-always-on")

    def _compose_sessions(self) -> ComposeResult:
        yield Static(" SESSIONS", classes="section-title")
        with ConfigRow():
            yield Label("Chrome Targets", classes="config-label")
            count = self._state.chrome.active_targets
            text = (
                f"{count} page(s)"
                if self._state.chrome.running
                else "[dim]Chrome not running[/dim]"
            )
            yield Static(text, id="sessions-count", classes="config-value")

    def _mcp_status(self) -> str:
        s = self._state.install
        if s.mcp_installed:
            ver = f" ({s.mcp_version})" if s.mcp_version else ""
            return f"[green]Installed{ver}[/green]  [dim]{s.mcp_path}[/dim]"
        return "[red]Not installed[/red]"

    def _plugin_status(self) -> str:
        if self._state.install.plugin_installed:
            return f"[green]Installed[/green]  [dim]{self._state.install.plugin_path}[/dim]"
        return "[yellow]Not installed[/yellow]"

    def _chrome_status(self) -> str:
        c = self._state.chrome
        if c.running:
            ver = f"  [dim]{c.version}[/dim]" if c.version else ""
            return f"[green]Running[/green] on port {c.port}{ver}"
        return f"[dim]Not running[/dim] (port {c.port})"

    def on_mount(self) -> None:
        profile_row = self.query_one("#profile-row", ConfigRow)
        profile_row.display = self._config.chrome.profile_mode == "profile"

    def on_switch_changed(self, event: Switch.Changed) -> None:
        switch_id = event.switch.id
        if switch_id == "sw-mirrored":
            self._config.network.mirrored_networking = event.value
            self._auto_save()
            result = set_mirrored_networking(event.value)
            self._set_status(result)
        elif switch_id == "sw-headless":
            self._config.chrome.headless = event.value
            self._set_status("Headless " + ("enabled" if event.value else "disabled"))
            self._auto_save()
        elif switch_id == "sw-cdp-always-on":
            self._config.cdp.always_on = event.value
            self._auto_save()
            result = set_always_on_cdp(event.value, port=self._config.chrome.debug_port)
            self._set_status(result)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "session-mode-select":
            if event.value is not Select.BLANK:
                self._config.chrome.profile_mode = str(event.value)
                profile_row = self.query_one("#profile-row", ConfigRow)
                profile_row.display = event.value == "profile"
                self._set_status(f"Session mode: {self._config.chrome.profile_mode}")
                self._auto_save()
        elif event.select.id == "profile-select":
            if event.value is not Select.BLANK:
                self._config.chrome.profile_name = str(event.value)
                self._set_status(f"Profile: {self._config.chrome.profile_name}")
                self._auto_save()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-install-plugin":
            self._install_plugin()
            self._swap_plugin_button(installed=True)
        elif event.button.id == "btn-uninstall-plugin":
            self._uninstall_plugin()
            self._swap_plugin_button(installed=False)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "port-input":
            try:
                port = int(event.value)
                if 1024 <= port <= 65535:
                    self._config.chrome.debug_port = port
                    self._set_status(f"Debug port: {port}")
                    self._auto_save()
            except ValueError:
                pass

    def action_refresh_state(self) -> None:
        self._state = detect_system_state(self._config.chrome.debug_port)
        self.query_one("#mcp-status", Static).update(self._mcp_status())
        self.query_one("#plugin-status", Static).update(self._plugin_status())
        self.query_one("#chrome-status", Static).update(self._chrome_status())
        count = self._state.chrome.active_targets
        text = f"{count} page(s)" if self._state.chrome.running else "[dim]Chrome not running[/dim]"
        self.query_one("#sessions-count", Static).update(text)
        self._set_status("State refreshed")

    def action_save(self) -> None:
        self._auto_save()
        self._set_status("Configuration saved")

    def _auto_save(self) -> None:
        save_config(self._config)

    def _set_status(self, message: str) -> None:
        self.query_one("#status-bar", Static).update(f" {message}")

    def _swap_plugin_button(self, *, installed: bool) -> None:
        for btn in self.query("Button"):
            if btn.id in ("btn-install-plugin", "btn-uninstall-plugin"):
                btn.remove()
                break

        row = self.query_one("#plugin-row", ConfigRow)
        if installed:
            row.mount(Button("Uninstall", id="btn-uninstall-plugin", variant="error"))
        else:
            row.mount(Button("Install", id="btn-install-plugin", variant="success"))

    def _install_plugin(self) -> None:
        src = _repo_root() / "opencode-plugin" / PLUGIN_FILENAME
        if not src.exists():
            self._set_status(f"Plugin source not found: {src}")
            return
        dest = OPENCODE_PLUGIN_DIR / PLUGIN_FILENAME
        OPENCODE_PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
        dest.write_text(src.read_text())
        self._set_status(f"Plugin installed to {dest}")
        self._state = detect_system_state(self._config.chrome.debug_port)
        self.query_one("#plugin-status", Static).update(self._plugin_status())

    def _uninstall_plugin(self) -> None:
        dest = OPENCODE_PLUGIN_DIR / PLUGIN_FILENAME
        if dest.exists():
            dest.unlink()
            self._set_status("Plugin uninstalled")
        else:
            self._set_status("Plugin not found")
        self._state = detect_system_state(self._config.chrome.debug_port)
        self.query_one("#plugin-status", Static).update(self._plugin_status())


def run_config_tui() -> None:
    app = ConfigApp()
    app.run()


if __name__ == "__main__":
    run_config_tui()
