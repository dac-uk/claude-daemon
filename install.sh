#!/usr/bin/env bash
# install.sh — One-line installer for claude-daemon
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/dac-uk/claude-daemon/main/install.sh | bash
#   OR
#   git clone git@github.com:dac-uk/claude-daemon.git && cd claude-daemon && ./install.sh
#
# Idempotent: safe to run multiple times. Never overwrites existing config or .env.

set -euo pipefail

# -- Colours ------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Colour

info()  { printf "${CYAN}[info]${NC}  %s\n" "$*"; }
ok()    { printf "${GREEN}[ok]${NC}    %s\n" "$*"; }
warn()  { printf "${YELLOW}[warn]${NC}  %s\n" "$*"; }
fail()  { printf "${RED}[error]${NC} %s\n" "$*"; exit 1; }
step()  { printf "\n${BOLD}==> %s${NC}\n" "$*"; }

# -- Configuration ------------------------------------------------------------
REPO_URL="git@github.com:dac-uk/claude-daemon.git"
REPO_URL_HTTPS="https://github.com/dac-uk/claude-daemon.git"
INSTALL_DIR="${CLAUDE_DAEMON_INSTALL_DIR:-$HOME/.local/share/claude-daemon}"
CONFIG_DIR="${CLAUDE_DAEMON_DATA_DIR:-$HOME/.config/claude-daemon}"

# -- 1. Preflight checks ------------------------------------------------------
step "Preflight checks"

# Python >= 3.10
if command -v python3 &>/dev/null; then
    PYTHON=python3
elif command -v python &>/dev/null; then
    PYTHON=python
else
    fail "Python 3.10+ is required but not found. Install it first."
fi

PY_VERSION=$($PYTHON -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$($PYTHON -c 'import sys; print(sys.version_info.major)')
PY_MINOR=$($PYTHON -c 'import sys; print(sys.version_info.minor)')

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    fail "Python >= 3.10 required (found $PY_VERSION)"
fi
ok "Python $PY_VERSION"

# pip
if $PYTHON -m pip --version &>/dev/null; then
    PIP="$PYTHON -m pip"
elif command -v pip3 &>/dev/null; then
    PIP=pip3
elif command -v pip &>/dev/null; then
    PIP=pip
else
    fail "pip is required but not found. Install it with: $PYTHON -m ensurepip"
fi
ok "pip available"

# git
if ! command -v git &>/dev/null; then
    fail "git is required but not found."
fi
ok "git available"

# claude CLI (warn only — not strictly required for install)
if command -v claude &>/dev/null; then
    ok "claude CLI found"
else
    warn "claude CLI not found — install it later (npm install -g @anthropic-ai/claude-code)"
fi

# -- 2. Clone or locate repo --------------------------------------------------
step "Locating source code"

# Detect if we're already inside the repo
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
if [ -f "$SCRIPT_DIR/pyproject.toml" ] && grep -q 'claude-daemon' "$SCRIPT_DIR/pyproject.toml" 2>/dev/null; then
    REPO_DIR="$SCRIPT_DIR"
    ok "Running from repo at $REPO_DIR"
elif [ -f "$INSTALL_DIR/pyproject.toml" ] && grep -q 'claude-daemon' "$INSTALL_DIR/pyproject.toml" 2>/dev/null; then
    REPO_DIR="$INSTALL_DIR"
    info "Updating existing clone at $REPO_DIR"
    git -C "$REPO_DIR" pull --ff-only || warn "git pull failed — using existing code"
    ok "Source updated"
else
    info "Cloning to $INSTALL_DIR"
    mkdir -p "$(dirname "$INSTALL_DIR")"
    # Try SSH first, fall back to HTTPS
    if git clone "$REPO_URL" "$INSTALL_DIR" 2>/dev/null; then
        ok "Cloned via SSH"
    elif git clone "$REPO_URL_HTTPS" "$INSTALL_DIR" 2>/dev/null; then
        ok "Cloned via HTTPS"
    else
        fail "Failed to clone repository. Check your GitHub access."
    fi
    REPO_DIR="$INSTALL_DIR"
fi

# -- 3. Install Python package ------------------------------------------------
step "Installing claude-daemon"

cd "$REPO_DIR"
$PIP install -e ".[all]" --quiet 2>&1 | tail -5 || fail "pip install failed"

# Verify the command is available
DAEMON_BIN=$(command -v claude-daemon 2>/dev/null || true)
if [ -z "$DAEMON_BIN" ]; then
    # Check common pip install locations
    for candidate in \
        "$HOME/.local/bin/claude-daemon" \
        "$($PYTHON -c 'import sysconfig; print(sysconfig.get_path("scripts"))')/claude-daemon" \
    ; do
        if [ -x "$candidate" ]; then
            DAEMON_BIN="$candidate"
            break
        fi
    done
fi

if [ -z "$DAEMON_BIN" ]; then
    warn "claude-daemon installed but not on PATH. Add ~/.local/bin to your PATH."
    DAEMON_BIN="$HOME/.local/bin/claude-daemon"
else
    ok "claude-daemon installed at $DAEMON_BIN"
fi

# -- 4. Create config directory ------------------------------------------------
step "Setting up configuration"

mkdir -p "$CONFIG_DIR"

# Copy config template (never overwrite)
if [ -f "$CONFIG_DIR/config.yaml" ]; then
    ok "config.yaml already exists — skipping"
else
    cp "$REPO_DIR/config.example.yaml" "$CONFIG_DIR/config.yaml"
    ok "Created $CONFIG_DIR/config.yaml"
fi

# Copy .env template (never overwrite)
if [ -f "$CONFIG_DIR/.env" ]; then
    ok ".env already exists — skipping"
else
    cp "$REPO_DIR/.env.example" "$CONFIG_DIR/.env"
    ok "Created $CONFIG_DIR/.env"
fi

# -- 5. Install system service -------------------------------------------------
step "Installing system service"

OS="$(uname -s)"

if [ "$OS" = "Linux" ]; then
    SERVICE_DIR="$HOME/.config/systemd/user"
    SERVICE_FILE="$SERVICE_DIR/claude-daemon.service"
    mkdir -p "$SERVICE_DIR"

    # Copy and patch ExecStart with full binary path
    sed "s|ExecStart=claude-daemon|ExecStart=$DAEMON_BIN|g" \
        "$REPO_DIR/service/claude-daemon.service" > "$SERVICE_FILE"
    ok "Installed systemd unit at $SERVICE_FILE"

    # Reload, enable, start
    systemctl --user daemon-reload
    systemctl --user enable claude-daemon 2>/dev/null || true
    ok "Service enabled"

    if systemctl --user is-active claude-daemon &>/dev/null; then
        info "Service already running — restarting"
        systemctl --user restart claude-daemon
    else
        systemctl --user start claude-daemon 2>/dev/null || warn "Service start failed — edit .env first, then: systemctl --user start claude-daemon"
    fi

    # Enable linger so user services survive logout
    if command -v loginctl &>/dev/null; then
        loginctl enable-linger "$(whoami)" 2>/dev/null || true
    fi

elif [ "$OS" = "Darwin" ]; then
    AGENTS_DIR="$HOME/Library/LaunchAgents"
    PLIST_FILE="$AGENTS_DIR/com.claude-daemon.plist"
    mkdir -p "$AGENTS_DIR"

    # Copy and patch ProgramArguments with full binary path
    sed "s|<string>claude-daemon</string>|<string>$DAEMON_BIN</string>|g" \
        "$REPO_DIR/service/com.claude-daemon.plist" > "$PLIST_FILE"
    ok "Installed launchd plist at $PLIST_FILE"

    # Unload if already loaded, then load
    launchctl unload "$PLIST_FILE" 2>/dev/null || true
    launchctl load "$PLIST_FILE" 2>/dev/null || warn "launchctl load failed — edit .env first, then: launchctl load $PLIST_FILE"
    ok "Service loaded"

else
    warn "Unsupported OS ($OS) — skipping service install. Start manually: claude-daemon start --foreground"
fi

# -- 6. Summary ----------------------------------------------------------------
step "Installation complete"

printf "\n"
printf "  ${BOLD}Config:${NC}     %s/config.yaml\n" "$CONFIG_DIR"
printf "  ${BOLD}Env vars:${NC}   %s/.env\n" "$CONFIG_DIR"
printf "  ${BOLD}Source:${NC}     %s\n" "$REPO_DIR"
printf "  ${BOLD}Binary:${NC}     %s\n" "$DAEMON_BIN"
printf "\n"

printf "  ${YELLOW}Next steps:${NC}\n"
printf "    1. Edit ${BOLD}%s/.env${NC} with your bot tokens\n" "$CONFIG_DIR"
printf "       (TELEGRAM_BOT_TOKEN, DISCORD_BOT_TOKEN, GITHUB_TOKEN, etc.)\n"
printf "\n"
printf "    2. Edit ${BOLD}%s/config.yaml${NC} to enable integrations\n" "$CONFIG_DIR"
printf "       (api_enabled, dashboard_enabled, telegram, discord)\n"
printf "\n"
printf "    3. Check status:\n"
if [ "$OS" = "Linux" ]; then
    printf "       ${CYAN}systemctl --user status claude-daemon${NC}\n"
    printf "       ${CYAN}journalctl --user -u claude-daemon -f${NC}\n"
elif [ "$OS" = "Darwin" ]; then
    printf "       ${CYAN}launchctl list | grep claude-daemon${NC}\n"
    printf "       ${CYAN}tail -f /tmp/claude-daemon.stdout.log${NC}\n"
fi
printf "       ${CYAN}claude-daemon status${NC}\n"
printf "\n"
printf "    4. Dashboard (once api_enabled + dashboard_enabled are set):\n"
printf "       ${CYAN}http://localhost:8080/${NC}\n"
printf "\n"
