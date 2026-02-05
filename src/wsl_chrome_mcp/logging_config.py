"""Logging configuration with date-based directory structure and time rotation.

Log files are written to: {repo_root}/logs/{YYYY}/{MM}/{DD}/wsl-chrome-mcp.log
Rotation happens at midnight via TimedRotatingFileHandler.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler


def setup_logging() -> None:
    """Configure logging with file and stderr handlers.

    File handler:
    - Path: logs/{year}/{month}/{day}/wsl-chrome-mcp.log
    - Rotation: midnight, keeps 7 rotated files per directory
    - Level: DEBUG (captures everything including raw CDP responses)

    Stderr handler:
    - Level: INFO (keeps stderr clean)
    """
    # Repo root: this file is src/wsl_chrome_mcp/logging_config.py
    # Go up 3 levels: logging_config.py -> wsl_chrome_mcp/ -> src/ -> repo_root/
    this_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(os.path.dirname(this_dir))

    now = datetime.now()
    log_dir = os.path.join(
        repo_root,
        "logs",
        now.strftime("%Y"),
        now.strftime("%m"),
        now.strftime("%d"),
    )
    os.makedirs(log_dir, exist_ok=True)

    log_file = os.path.join(log_dir, "wsl-chrome-mcp.log")

    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    # File handler: DEBUG level, rotate at midnight, keep 7 backups
    file_handler = TimedRotatingFileHandler(
        filename=log_file,
        when="midnight",
        interval=1,
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    # Stderr handler: INFO level
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.INFO)
    stderr_handler.setFormatter(formatter)

    # Configure root logger
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(file_handler)
    root.addHandler(stderr_handler)
