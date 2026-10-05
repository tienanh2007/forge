#!/usr/bin/env bash
# Idempotent installer for forge. Never writes secrets.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
FORGE_HOME="${FORGE_HOME:-$HOME/agent-projects}"
ENV_DIR="$HOME/.config/forge"
ENV_FILE="$ENV_DIR/env"

ok()   { printf '  [ok]   %s\n' "$*"; }
warn() { printf '  [warn] %s\n' "$*"; }
die()  { printf '  [fail] %s\n' "$*" >&2; exit 1; }

echo "forge: checking prerequisites"
command -v python3 >/dev/null || die "python3 not found"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' \
  && ok "python3 $(python3 -c 'import platform; print(platform.python_version())')" \
  || warn "python3 < 3.12; forge targets 3.12+"
command -v git >/dev/null && ok "git" || die "git not found"
command -v claude >/dev/null && ok "claude" || die "claude (Claude Code CLI) not found"
if command -v gh >/dev/null; then
  gh auth status >/dev/null 2>&1 && ok "gh (authenticated)" || warn "gh not authenticated: run 'gh auth login'"
else
  warn "gh not found: PR/CI features will not work (brew install gh)"
fi
command -v git-spice >/dev/null && ok "git-spice" || warn "git-spice not found: stacked PRs unavailable (brew install git-spice)"

echo "forge: CLI"
chmod +x "$REPO/bin/forge" 2>/dev/null || warn "bin/forge not present yet"
mkdir -p "$BIN_DIR"
if [ "$(readlink "$BIN_DIR/forge" 2>/dev/null || true)" = "$REPO/bin/forge" ]; then
  ok "$BIN_DIR/forge already linked"
elif [ -e "$BIN_DIR/forge" ] && [ ! -L "$BIN_DIR/forge" ]; then
  warn "$BIN_DIR/forge exists and is not a symlink; leaving it alone"
else
  ln -sfn "$REPO/bin/forge" "$BIN_DIR/forge" && ok "linked $BIN_DIR/forge -> $REPO/bin/forge"
fi
case ":$PATH:" in
  *":$BIN_DIR:"*) ok "$BIN_DIR is on PATH" ;;
  *) warn "$BIN_DIR is not on PATH; add: export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac

echo "forge: state + credentials"
mkdir -p "$FORGE_HOME" && ok "FORGE_HOME $FORGE_HOME"
mkdir -p "$ENV_DIR" && chmod 700 "$ENV_DIR"
if [ ! -f "$ENV_FILE" ]; then
  umask 177
  cat > "$ENV_FILE" <<'TEMPLATE'
# forge credentials (KEY=VALUE). Keep this file chmod 600. Env vars take precedence.
CLICKUP_TOKEN=
SONAR_TOKEN=
# CLICKUP_TEAM_ID=
TEMPLATE
  ok "created template $ENV_FILE (fill in tokens)"
else
  ok "$ENV_FILE exists (not modified)"
fi
chmod 600 "$ENV_FILE"

echo "forge: Claude Code plugin"
if claude plugin marketplace list --json 2>/dev/null | grep -q '"name": *"forge"'; then
  ok "marketplace 'forge' already added"
else
  claude plugin marketplace add "$REPO" && ok "added marketplace 'forge' ($REPO)"
fi
installed="$(claude plugin list --json 2>/dev/null || true)"
if printf '%s' "$installed" | grep -q '"forge@forge"'; then
  ok "forge@forge already installed"
else
  claude plugin install forge@forge && ok "installed forge@forge"
fi
if printf '%s' "$installed" | grep -q '"superpowers@'; then
  ok "superpowers already installed"
else
  claude plugin install superpowers@claude-plugins-official && ok "installed superpowers"
fi

echo "forge: done. Restart Claude Code to load skills/hooks; then try /forge:start-project."
