#!/usr/bin/env bash
#
# install.sh -- one command from a fresh dsh install to a working, verified
# DeepSeek V4 Flash 0731 setup pinned to DeepInfra via OpenRouter.
#
#   ./scripts/install.sh
#
# What it does, in order:
#   1. checks prerequisites (node, npm, python3)
#   2. installs dsh if missing (or reports the version if present)
#   3. backs up ~/.dsh/settings.yaml
#   4. merges config/deepseek-v4-flash.yaml into it, preserving your other keys
#   5. applies the OpenRouter routing patch to dsh's schema
#   6. verifies the result on the live wire
#
# Safe to re-run. Every step is idempotent.
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SETTINGS="${DSH_SETTINGS:-$HOME/.dsh/settings.yaml}"
FRAGMENT="$REPO/config/deepseek-v4-flash.yaml"

SKIP_VERIFY=0
for arg in "$@"; do
  case "$arg" in
    --no-verify) SKIP_VERIFY=1 ;;
    -h|--help)
      sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32mOK\033[0m   %s\n' "$*"; }
warn() { printf '    \033[33mWARN\033[0m %s\n' "$*"; }
die()  { printf '    \033[31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }

# ------------------------------------------------------------ 1. prereqs
say "Checking prerequisites"

command -v node >/dev/null   || die "node not found. Install Node.js 20+ from https://nodejs.org"
command -v npm  >/dev/null   || die "npm not found (ships with Node.js)"
command -v python3 >/dev/null || die "python3 not found (needed for verification)"

NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]')"
if [ "$NODE_MAJOR" -lt 20 ]; then
  die "node $(node -v) is too old; dsh needs 20+"
fi
ok "node $(node -v), npm $(npm -v), python3 $(python3 -V 2>&1 | cut -d' ' -f2)"

if [ -z "${OPENROUTER_API_KEY:-}" ]; then
  warn "OPENROUTER_API_KEY is not set."
  warn "Get a key at https://openrouter.ai/keys, then:"
  warn "    export OPENROUTER_API_KEY=sk-or-v1-..."
  warn "Add it to your shell profile to make it permanent."
  warn "Installation continues; the final verification step will be skipped."
  SKIP_VERIFY=1
else
  ok "OPENROUTER_API_KEY is set (${OPENROUTER_API_KEY:0:12}...)"
fi

# --------------------------------------------------------------- 2. dsh
say "Checking dsh"

if command -v dsh >/dev/null 2>&1; then
  ok "dsh present: $(dsh --version 2>/dev/null || echo 'version unknown')"
else
  warn "dsh not found -- installing @deepseek-ai/dsh globally"
  npm install -g @deepseek-ai/dsh || die "npm install failed. If this is a
    permissions error, configure a user-level npm prefix:
        npm config set prefix ~/.npm-global
        export PATH=\$HOME/.npm-global/bin:\$PATH"
  command -v dsh >/dev/null || die "dsh still not on PATH after install.
    Add your npm global bin to PATH:  export PATH=\$(npm bin -g):\$PATH"
  ok "installed $(dsh --version 2>/dev/null || echo dsh)"
fi

# ---------------------------------------------------------- 3. backup
say "Backing up settings"

mkdir -p "$(dirname "$SETTINGS")"
if [ -f "$SETTINGS" ]; then
  STAMP="$(date +%Y%m%d-%H%M%S)"
  cp "$SETTINGS" "${SETTINGS}.bak-${STAMP}"
  ok "backed up to ${SETTINGS}.bak-${STAMP}"
  # keep the 10 most recent backups
  ls -1t "${SETTINGS}.bak-"* 2>/dev/null | tail -n +11 | xargs -r rm --
else
  ok "no existing settings.yaml (fresh install)"
fi

# ----------------------------------------------------------- 4. merge
say "Merging OpenRouter configuration"

[ -f "$FRAGMENT" ] || die "config fragment missing: $FRAGMENT"

python3 "$REPO/scripts/merge_settings.py" "$SETTINGS" "$FRAGMENT" \
  || die "merge failed -- your settings.yaml is untouched (restore from the backup above if needed)"
ok "merged into $SETTINGS"

# ----------------------------------------------------------- 5. patch
say "Patching dsh schema for OpenRouter provider routing"

bash "$REPO/scripts/patch-openrouter.sh" || die "patch failed"

# ---------------------------------------------------------- 6. verify
if [ "$SKIP_VERIFY" -eq 1 ]; then
  say "Skipping wire verification"
  warn "Run it yourself once OPENROUTER_API_KEY is set:"
  warn "    python3 $REPO/scripts/verify-wire.py"
else
  say "Verifying on the live wire"
  python3 "$REPO/scripts/verify-wire.py" || die "wire verification failed -- see docs/05-troubleshooting.md"
fi

# ------------------------------------------------------------- done
cat <<EOF

$(printf '\033[1;32m✓ Installation complete.\033[0m')

  Settings:  $SETTINGS
  Model:     deepseek/deepseek-v4-flash-0731
  Pinned to: DeepInfra (fallbacks: GMICloud, BaseTen)
  Reasoning: off

Start the web UI:
    dsh --profile web --host 127.0.0.1 --port 3080

Or run headless:
    dsh --profile headless -p "your prompt"

$(printf '\033[1mIMPORTANT:\033[0m') npm replaces the patched file on every dsh upgrade.
After any \`npm update -g @deepseek-ai/dsh\`, re-run:
    $REPO/scripts/patch-openrouter.sh

Or use the wrapper that does both:
    $REPO/scripts/dsh-update.sh

EOF
