#!/usr/bin/env bash
# install.sh — One-line installer for claude-daemon
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/dac-uk/claude-daemon/main/install.sh | bash
#   OR
#   git clone git@github.com:dac-uk/claude-daemon.git && cd claude-daemon && ./install.sh
#
# Idempotent: safe to run multiple times. Never overwrites existing config or .env.
#
# Flags:
#   --update    Skip interactive prompts, just pull + reinstall + re-patch service

set -euo pipefail

# Parse flags
UPDATE_ONLY=false
for arg in "$@"; do
    case "$arg" in
        --update) UPDATE_ONLY=true ;;
    esac
done

# -- Colours & logging --------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Colour

# Accumulate warnings and errors for final summary
WARNINGS=()
ERRORS=()
HAS_FATAL=false

info()  { printf "${CYAN}[info]${NC}  %s\n" "$*"; }
ok()    { printf "${GREEN}[ok]${NC}    %s\n" "$*"; }
warn()  { printf "${YELLOW}[warn]${NC}  %s\n" "$*"; WARNINGS+=("$*"); }
error() { printf "${RED}[error]${NC} %s\n" "$*"; ERRORS+=("$*"); }
fail()  { printf "${RED}[FATAL]${NC} %s\n" "$*"; ERRORS+=("$*"); HAS_FATAL=true; _print_summary; exit 1; }
step()  { printf "\n${BOLD}==> %s${NC}\n" "$*"; }

_print_summary() {
    printf "\n${BOLD}─── Install Summary ───${NC}\n"
    if [ ${#ERRORS[@]} -gt 0 ]; then
        printf "${RED}${BOLD}ERRORS (${#ERRORS[@]}):${NC}\n"
        for e in "${ERRORS[@]}"; do
            printf "  ${RED}✗${NC} %s\n" "$e"
        done
    fi
    if [ ${#WARNINGS[@]} -gt 0 ]; then
        printf "${YELLOW}${BOLD}WARNINGS (${#WARNINGS[@]}):${NC}\n"
        for w in "${WARNINGS[@]}"; do
            printf "  ${YELLOW}⚠${NC} %s\n" "$w"
        done
    fi
    if [ ${#ERRORS[@]} -eq 0 ] && [ ${#WARNINGS[@]} -eq 0 ]; then
        printf "  ${GREEN}✓ No errors or warnings${NC}\n"
    fi
    if [ "$HAS_FATAL" = true ]; then
        printf "\n${RED}${BOLD}RESULT: FAILED${NC}\n"
        printf "  Copy the output above and share it with Claude for diagnosis.\n"
    elif [ ${#ERRORS[@]} -gt 0 ]; then
        printf "\n${YELLOW}${BOLD}RESULT: COMPLETED WITH ERRORS${NC}\n"
        printf "  The daemon is installed but some features may not work.\n"
        printf "  Copy the output above and share it with Claude for diagnosis.\n"
    elif [ ${#WARNINGS[@]} -gt 0 ]; then
        printf "\n${YELLOW}${BOLD}RESULT: COMPLETED WITH WARNINGS${NC}\n"
        printf "  The daemon is installed. Warnings are non-critical but worth reviewing.\n"
    else
        printf "\n${GREEN}${BOLD}RESULT: SUCCESS${NC}\n"
    fi
    printf "\n"
}

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

# Python 3.14+ is too new — many packages lack wheels. Try to find 3.12 or 3.13.
if [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -ge 14 ]; then
    # Try to find a stable Python (3.13, 3.12, 3.11) before warning
    FOUND_STABLE=false
    for candidate in python3.13 python3.12 python3.11; do
        if command -v "$candidate" &>/dev/null; then
            STABLE_VER=$($candidate -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
            info "System Python is $PY_VERSION — switching to $candidate ($STABLE_VER)"
            PYTHON="$candidate"
            PY_VERSION="$STABLE_VER"
            FOUND_STABLE=true
            break
        fi
    done
    if [ "$FOUND_STABLE" = false ]; then
        warn "Python $PY_VERSION is very new — some dependencies may lack binary wheels."
        warn "No stable Python 3.11-3.13 found. Install may fail."
        info "Fix: brew install python@3.13 (macOS) or apt install python3.13 (Linux)"
    fi
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

# -- 3. Install Python package (in venv) --------------------------------------
step "Installing claude-daemon"

cd "$REPO_DIR"

# Always use a virtual environment — avoids PEP 668 "externally-managed-environment"
# errors on macOS Homebrew, modern Debian/Ubuntu, Fedora, etc.
VENV_DIR="$REPO_DIR/.venv"
NEED_VENV=false
if [ ! -d "$VENV_DIR" ]; then
    NEED_VENV=true
elif [ -x "$VENV_DIR/bin/python" ]; then
    # Recreate if venv Python version doesn't match the selected Python
    VENV_PY=$("$VENV_DIR/bin/python" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "unknown")
    if [ "$VENV_PY" != "$PY_VERSION" ]; then
        info "Venv Python ($VENV_PY) differs from selected Python ($PY_VERSION) — recreating"
        rm -rf "$VENV_DIR"
        NEED_VENV=true
    fi
fi
if [ "$NEED_VENV" = true ]; then
    info "Creating virtual environment at $VENV_DIR (Python $PY_VERSION)"
    if ! $PYTHON -m venv "$VENV_DIR" 2>&1; then
        fail "Failed to create virtual environment. Ensure python3-venv is installed."
    fi
    ok "Virtual environment created"
else
    ok "Virtual environment at $VENV_DIR (Python $PY_VERSION)"
fi
PYTHON="$VENV_DIR/bin/python"
PIP="$VENV_DIR/bin/pip"

PIP_LOG=$(mktemp)
if $PIP install -e ".[all]" 2>&1 | tee "$PIP_LOG" | grep -E '(ERROR|error:|WARNING|warning:|Successfully installed)'; then
    true  # grep found matches (or pip succeeded)
fi
if ! $PYTHON -c "import claude_daemon" 2>/dev/null; then
    printf "\n${RED}pip install output:${NC}\n"
    cat "$PIP_LOG"
    rm -f "$PIP_LOG"
    fail "pip install failed — claude_daemon module not importable. Full output above."
fi
rm -f "$PIP_LOG"
ok "Python package installed"

# Verify the command is available (check venv bin first)
DAEMON_BIN="$VENV_DIR/bin/claude-daemon"
if [ ! -x "$DAEMON_BIN" ]; then
    DAEMON_BIN=$(command -v claude-daemon 2>/dev/null || true)
fi
if [ -z "$DAEMON_BIN" ] || [ ! -x "$DAEMON_BIN" ]; then
    # Check common pip install locations
    for candidate in \
        "$VENV_DIR/bin/claude-daemon" \
        "$HOME/.local/bin/claude-daemon" \
        "$($PYTHON -c 'import sysconfig; print(sysconfig.get_path("scripts"))')/claude-daemon" \
    ; do
        if [ -x "$candidate" ]; then
            DAEMON_BIN="$candidate"
            break
        fi
    done
fi

if [ -z "$DAEMON_BIN" ] || [ ! -x "$DAEMON_BIN" ]; then
    warn "claude-daemon installed but binary not found. Check: $VENV_DIR/bin/"
    DAEMON_BIN="$VENV_DIR/bin/claude-daemon"
else
    ok "claude-daemon installed at $DAEMON_BIN"
fi

# Ensure claude-daemon is on PATH via symlink in ~/.local/bin
LOCAL_BIN="$HOME/.local/bin"
mkdir -p "$LOCAL_BIN"
SYMLINK="$LOCAL_BIN/claude-daemon"
if [ -x "$DAEMON_BIN" ]; then
    ln -sf "$DAEMON_BIN" "$SYMLINK"
    ok "Symlinked to $SYMLINK"
fi

# Check if ~/.local/bin is on PATH; if not, add it to shell profile
if ! echo "$PATH" | tr ':' '\n' | grep -qx "$LOCAL_BIN"; then
    SHELL_NAME="$(basename "$SHELL")"
    case "$SHELL_NAME" in
        zsh)  PROFILE="$HOME/.zshrc" ;;
        bash) PROFILE="$HOME/.bashrc" ;;
        *)    PROFILE="$HOME/.profile" ;;
    esac
    PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'
    if [ -f "$PROFILE" ] && grep -qF '.local/bin' "$PROFILE" 2>/dev/null; then
        info "PATH entry already in $PROFILE"
    else
        printf "\n# Added by claude-daemon installer\n%s\n" "$PATH_LINE" >> "$PROFILE"
        ok "Added ~/.local/bin to PATH in $PROFILE"
        warn "Run 'source $PROFILE' or open a new terminal for claude-daemon to work"
    fi
    # Also export for the rest of this script
    export PATH="$LOCAL_BIN:$PATH"
fi

# -- 4. Create config directory ------------------------------------------------
step "Setting up configuration"

mkdir -p "$CONFIG_DIR"
ENV_FILE="$CONFIG_DIR/.env"
YAML_FILE="$CONFIG_DIR/config.yaml"

# Copy config template (never overwrite)
if [ -f "$YAML_FILE" ]; then
    ok "config.yaml already exists — skipping"
else
    cp "$REPO_DIR/config.example.yaml" "$YAML_FILE"
    ok "Created $YAML_FILE"
fi

# Helper: set a value in the .env file (append or update)
_set_env() {
    local key="$1" value="$2"
    if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
        sed -i.bak "s|^${key}=.*|${key}=${value}|" "$ENV_FILE" && rm -f "${ENV_FILE}.bak"
    elif grep -q "^# *${key}=" "$ENV_FILE" 2>/dev/null; then
        sed -i.bak "s|^# *${key}=.*|${key}=${value}|" "$ENV_FILE" && rm -f "${ENV_FILE}.bak"
    else
        echo "${key}=${value}" >> "$ENV_FILE"
    fi
}

# Always ensure API is enabled (required for claude-daemon chat)
if grep -q '^\s*#\s*api_enabled:\s*true' "$YAML_FILE" 2>/dev/null; then
    sed -i.bak 's|^\(\s*\)#\s*api_enabled:\s*true|\1api_enabled: true|' "$YAML_FILE" && rm -f "${YAML_FILE}.bak"
    ok "HTTP API enabled in config.yaml"
elif grep -q '^\s*api_enabled:\s*false' "$YAML_FILE" 2>/dev/null; then
    sed -i.bak 's|^\(\s*\)api_enabled:\s*false|\1api_enabled: true|' "$YAML_FILE" && rm -f "${YAML_FILE}.bak"
    ok "HTTP API enabled in config.yaml"
elif ! grep -q 'api_enabled:\s*true' "$YAML_FILE" 2>/dev/null; then
    # Not present at all — add it
    sed -i.bak 's|^daemon:|daemon:\n  api_enabled: true|' "$YAML_FILE" && rm -f "${YAML_FILE}.bak"
    ok "HTTP API enabled in config.yaml"
fi

# Auto-generate API key if not set (secures the HTTP API)
if ! grep -q "^CLAUDE_DAEMON_API_KEY=" "$ENV_FILE" 2>/dev/null || \
   grep -q "^CLAUDE_DAEMON_API_KEY=$" "$ENV_FILE" 2>/dev/null; then
    AUTO_KEY=$($PYTHON -c 'import secrets; print(secrets.token_urlsafe(32))')
    _set_env "CLAUDE_DAEMON_API_KEY" "$AUTO_KEY"
    ok "Auto-generated API key (secures HTTP API + chat)"
fi

# Copy .env template (never overwrite)
if [ -f "$ENV_FILE" ]; then
    ok ".env already exists — skipping"
else
    cp "$REPO_DIR/.env.example" "$ENV_FILE"
    ok "Created $ENV_FILE"
fi

# -- 5. Interactive token setup ------------------------------------------------
# Only run interactive setup if stdin is a terminal (not piped) and not --update
INTERACTIVE=false
if [ -t 0 ] && [ "$UPDATE_ONLY" = false ]; then
    INTERACTIVE=true
fi

if [ "$INTERACTIVE" = true ]; then
    step "Integration setup (press Enter to skip any step)"
    printf "\n"

    # -- Telegram --
    printf "  ${BOLD}Telegram${NC}\n"
    printf "  To create a bot: open Telegram, search for @BotFather, send /newbot\n"
    printf "  BotFather will give you a token like: 7123456789:AAF1234...\n\n"
    printf "  Telegram bot token (or Enter to skip): "
    read -r TG_TOKEN
    if [ -n "$TG_TOKEN" ]; then
        _set_env "TELEGRAM_BOT_TOKEN" "$TG_TOKEN"
        ok "Telegram token saved"
    else
        info "Skipped Telegram — add later in $ENV_FILE"
    fi

    printf "\n"

    # -- Discord --
    printf "  ${BOLD}Discord${NC}\n"
    printf "  To create a bot: go to discord.com/developers/applications\n"
    printf "  Create an app > Bot > Reset Token > copy it\n"
    printf "  Also enable 'Message Content Intent' under Privileged Gateway Intents\n\n"
    printf "  Discord bot token (or Enter to skip): "
    read -r DC_TOKEN
    if [ -n "$DC_TOKEN" ]; then
        _set_env "DISCORD_BOT_TOKEN" "$DC_TOKEN"
        ok "Discord token saved"
    else
        info "Skipped Discord — add later in $ENV_FILE"
    fi

    printf "\n"

    # -- API key --
    printf "  ${BOLD}HTTP API${NC}\n"
    printf "  Set a secret key to protect the REST API and dashboard.\n"
    printf "  Leave empty to auto-generate one.\n\n"
    printf "  API key (or Enter to auto-generate): "
    read -r API_KEY
    if [ -z "$API_KEY" ]; then
        API_KEY=$($PYTHON -c 'import secrets; print(secrets.token_urlsafe(32))')
        info "Auto-generated API key"
    fi
    _set_env "CLAUDE_DAEMON_API_KEY" "$API_KEY"
    ok "API key saved"

    printf "\n"

    # -- GitHub token --
    printf "  ${BOLD}GitHub (for MCP tools)${NC}\n"
    printf "  Create a token at: github.com/settings/tokens (classic, repo scope)\n\n"
    printf "  GitHub token (or Enter to skip): "
    read -r GH_TOKEN
    if [ -n "$GH_TOKEN" ]; then
        _set_env "GITHUB_TOKEN" "$GH_TOKEN"
        ok "GitHub token saved"
    else
        info "Skipped GitHub — agents won't have GitHub access until configured"
    fi

    printf "\n"

    # -- Enable API + dashboard in config.yaml --
    printf "  ${BOLD}Dashboard${NC}\n"
    printf "  Enable the live web dashboard? Shows agents, status, streaming output.\n"
    printf "  Accessible at http://your-ip:8080/ (Tailscale/ZeroTier/LAN)\n\n"
    printf "  Enable dashboard? [Y/n]: "
    read -r ENABLE_DASH
    ENABLE_DASH="${ENABLE_DASH:-Y}"
    if [[ "$ENABLE_DASH" =~ ^[Yy] ]]; then
        # Uncomment api_enabled and dashboard_enabled in config.yaml
        sed -i.bak \
            -e 's|^  # api_enabled: true|  api_enabled: true|' \
            -e 's|^  # api_port: 8080|  api_port: 8080|' \
            -e 's|^  # dashboard_enabled: true|  dashboard_enabled: true|' \
            -e 's|^  # api_bind: "0.0.0.0"|  api_bind: "0.0.0.0"|' \
            "$YAML_FILE" && rm -f "${YAML_FILE}.bak"
        ok "Dashboard enabled at http://0.0.0.0:8080/"
    else
        info "Dashboard not enabled — uncomment in config.yaml later"
    fi

else
    info "Non-interactive mode — edit tokens in $ENV_FILE"
fi

# -- 6. Install system service -------------------------------------------------
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
        systemctl --user restart claude-daemon || error "Service restart failed. Check: systemctl --user status claude-daemon"
    else
        if ! systemctl --user start claude-daemon 2>/dev/null; then
            SVC_STATUS=$(systemctl --user status claude-daemon 2>&1 | tail -10 || true)
            error "Service failed to start. Status output:"
            printf "%s\n" "$SVC_STATUS"
            warn "Fix: ensure tokens are set in $ENV_FILE, then: systemctl --user start claude-daemon"
        fi
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
    launchctl load "$PLIST_FILE" 2>/dev/null || warn "launchctl load failed — check tokens are set, then: launchctl load $PLIST_FILE"
    ok "Service loaded"

else
    warn "Unsupported OS ($OS) — skipping service install. Start manually: claude-daemon start --foreground"
fi

# -- 7. Summary ----------------------------------------------------------------
step "Installation complete"

printf "\n"
printf "  ${BOLD}Config:${NC}     %s\n" "$YAML_FILE"
printf "  ${BOLD}Env vars:${NC}   %s\n" "$ENV_FILE"
printf "  ${BOLD}Source:${NC}     %s\n" "$REPO_DIR"
printf "  ${BOLD}Binary:${NC}     %s\n" "$DAEMON_BIN"
printf "\n"

if [ "$OS" = "Linux" ]; then
    printf "  ${BOLD}Service:${NC}\n"
    printf "    ${CYAN}systemctl --user status claude-daemon${NC}    # Check status\n"
    printf "    ${CYAN}systemctl --user restart claude-daemon${NC}   # Restart after config changes\n"
    printf "    ${CYAN}journalctl --user -u claude-daemon -f${NC}   # Stream logs\n"
elif [ "$OS" = "Darwin" ]; then
    printf "  ${BOLD}Service:${NC}\n"
    printf "    ${CYAN}launchctl list | grep claude-daemon${NC}     # Check status\n"
    printf "    ${CYAN}tail -f /tmp/claude-daemon.stdout.log${NC}   # Stream logs\n"
fi
printf "    ${CYAN}claude-daemon status${NC}                    # Quick check\n"
printf "    ${CYAN}claude-daemon stop${NC}                      # Stop the daemon\n"
printf "    ${CYAN}claude-daemon restart${NC}                   # Restart after config changes\n"
printf "    ${CYAN}claude-daemon chat${NC}                      # Chat from the terminal\n"
printf "\n"

printf "  ${BOLD}MCP Servers:${NC}\n"
printf "    41 MCP servers available. Zero-config servers enabled automatically.\n"
printf "    Set tokens to enable more:  ${CYAN}claude-daemon env set TAVILY_API_KEY=tvly-...${NC}\n"
printf "    View all servers:           ${CYAN}claude-daemon mcp list${NC}\n"
printf "\n"

if [ "$INTERACTIVE" = true ]; then
    printf "  ${YELLOW}What's next:${NC}\n"
    printf "    1. Set up channels — see the Channel Setup Guide in README.md\n"
    printf "    2. The bot will bootstrap 7 AI agents on first message\n"
    printf "    3. Just start talking — type naturally, no special commands needed\n"
else
    printf "  ${YELLOW}What's next:${NC}\n"
    printf "    1. Edit ${BOLD}%s${NC} with your bot tokens\n" "$ENV_FILE"
    printf "    2. Edit ${BOLD}%s${NC} to enable integrations\n" "$YAML_FILE"
    if [ "$OS" = "Darwin" ]; then
        printf "    3. Restart: ${CYAN}launchctl unload ~/Library/LaunchAgents/com.claude-daemon.plist && launchctl load ~/Library/LaunchAgents/com.claude-daemon.plist${NC}\n"
    else
        printf "    3. Restart: ${CYAN}systemctl --user restart claude-daemon${NC}\n"
    fi
    printf "    4. Chat:    ${CYAN}claude-daemon chat${NC}\n"
fi

# -- Final summary with all warnings/errors --
_print_summary
