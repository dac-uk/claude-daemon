"""OS-specific service file generation for systemd and launchd."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

SYSTEMD_UNIT = """\
[Unit]
Description=Claude Code Daemon
After=network.target

[Service]
Type=simple
ExecStart={binary} start --foreground
Restart=on-failure
RestartSec=10
Environment=HOME={home}

[Install]
WantedBy=default.target
"""

LAUNCHD_PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.claude-daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>{binary}</string>
        <string>start</string>
        <string>--foreground</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_dir}/daemon.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/daemon.stderr.log</string>
</dict>
</plist>
"""


def install_systemd_service(log_dir: Path) -> Path:
    """Generate and install a systemd user unit file. Returns the path."""
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    path = unit_dir / "claude-daemon.service"

    binary = shutil.which("claude-daemon") or "claude-daemon"
    content = SYSTEMD_UNIT.format(binary=binary, home=Path.home())
    path.write_text(content)
    return path


def install_launchd_service(log_dir: Path) -> Path:
    """Generate and install a launchd LaunchAgent plist. Returns the path."""
    agent_dir = Path.home() / "Library" / "LaunchAgents"
    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / "com.claude-daemon.plist"

    binary = shutil.which("claude-daemon") or "claude-daemon"
    content = LAUNCHD_PLIST.format(binary=binary, log_dir=log_dir)
    path.write_text(content)
    return path


def install_service(log_dir: Path) -> Path:
    """Install the appropriate service file for the current platform."""
    if sys.platform == "darwin":
        return install_launchd_service(log_dir)
    return install_systemd_service(log_dir)
