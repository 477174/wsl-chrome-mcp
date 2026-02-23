"""Configuration management for WSL Chrome MCP.

Stores user preferences in ~/.config/wsl-chrome-mcp/config.toml.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import tomli_w

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


CONFIG_DIR = Path.home() / ".config" / "wsl-chrome-mcp"
CONFIG_FILE = CONFIG_DIR / "config.toml"


@dataclass
class ChromeConfig:
    """Chrome browser settings."""

    debug_port: int = 9222
    headless: bool = False
    profile_mode: str = "isolated"  # "isolated" | "profile"
    profile_name: str = ""


@dataclass
class NetworkConfig:
    """WSL network settings."""

    mirrored_networking: bool = True


@dataclass
class CdpConfig:
    """Chrome DevTools Protocol settings."""

    always_on: bool = False


@dataclass
class PluginConfig:
    """OpenCode plugin settings."""

    installed: bool = True


@dataclass
class AppConfig:
    """Root configuration object."""

    chrome: ChromeConfig = field(default_factory=ChromeConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    cdp: CdpConfig = field(default_factory=CdpConfig)
    plugin: PluginConfig = field(default_factory=PluginConfig)


def _dataclass_from_dict(cls: type, data: dict[str, Any]) -> Any:
    """Recursively create a dataclass instance from a dict."""
    nested_types: dict[str, type] = {
        "chrome": ChromeConfig,
        "network": NetworkConfig,
        "cdp": CdpConfig,
        "plugin": PluginConfig,
    }
    valid_fields = {f.name for f in fields(cls)}
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        if key not in valid_fields:
            continue
        if key in nested_types and isinstance(value, dict):
            kwargs[key] = _dataclass_from_dict(nested_types[key], value)
        else:
            kwargs[key] = value
    return cls(**kwargs)


def _dataclass_to_dict(obj: Any) -> dict[str, Any]:
    """Recursively convert a dataclass to a dict."""
    result: dict[str, Any] = {}
    for f in fields(obj):
        value = getattr(obj, f.name)
        if hasattr(value, "__dataclass_fields__"):
            result[f.name] = _dataclass_to_dict(value)
        else:
            result[f.name] = value
    return result


def load_config() -> AppConfig:
    """Load config from disk, returning defaults if file doesn't exist."""
    if not CONFIG_FILE.exists():
        return AppConfig()

    try:
        with open(CONFIG_FILE, "rb") as f:
            data = tomllib.load(f)
        return _dataclass_from_dict(AppConfig, data)
    except Exception:
        # Corrupted config — return defaults
        return AppConfig()


def save_config(config: AppConfig) -> None:
    """Save config to disk."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = _dataclass_to_dict(config)
    with open(CONFIG_FILE, "wb") as f:
        tomli_w.dump(data, f)


def reset_config() -> AppConfig:
    """Reset config to defaults."""
    config = AppConfig()
    save_config(config)
    return config
