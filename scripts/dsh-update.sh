#!/usr/bin/env bash
#
# dsh-update.sh -- upgrade dsh and re-apply the OpenRouter patch.
#
# npm replaces the patched file on every upgrade, which silently reverts
# provider pinning. This wrapper makes that impossible to forget.
#
#   ./scripts/dsh-update.sh            upgrade, back up, re-patch, verify
#   ./scripts/dsh-update.sh --check    report the available version, change nothing
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKG="@deepseek-ai/dsh"
DSH_DIR="${DSH_HOME:-$HOME/.dsh}"
BACKUP_DIR="$DSH_DIR/backups"
KEEP=10

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32mOK\033[0m   %s\n' "$*"; }
warn() { printf '    \033[33mWARN\033[0m %s\n' "$*"; }
die()  { printf '    \033[31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

# ------------------------------------------------------------- versions
say "Checking versions"

CURRENT="$(npm ls -g --depth=0 "$PKG" --json 2>/dev/null \
  | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{
      try{const j=JSON.parse(s);console.log(j.dependencies?.["'"$PKG"'"]?.version??"none")}
      catch{console.log("none")}})' 2>/dev/null || echo none)"

LATEST="$(npm view "$PKG" version 2>/dev/null || echo unknown)"

echo "    installed: $CURRENT"
echo "    latest:    $LATEST"

if [ "$CHECK_ONLY" -eq 1 ]; then
  [ "$CURRENT" = "$LATEST" ] && ok "up to date" || warn "update available"
  exit 0
fi

if [ "$CURRENT" = "$LATEST" ]; then
  ok "already on the latest version"
  say "Re-verifying the patch anyway"
  bash "$REPO/scripts/patch-openrouter.sh"
  exit 0
fi

# ---------------------------------------------------------- running check
say "Checking for running dsh processes"

# NOTE: the bracket trick stops this grep from matching ITSELF, which is a
# classic way for a "is it running?" check to always answer yes.
RUNNING="$(ps -eo pid,cmd | grep "[d]sh --profile" || true)"
if [ -n "$RUNNING" ]; then
  warn "dsh appears to be running:"
  echo "$RUNNING" | sed 's/^/      /'
  warn "Upgrading under a running process can leave it on half-old code."
  printf '    Continue anyway? [y/N] '
  read -r reply
  case "$reply" in [yY]*) ;; *) die "aborted" ;; esac
else
  ok "no running dsh processes"
fi

# -------------------------------------------------------------- backup
say "Backing up $DSH_DIR"

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
TARBALL="$BACKUP_DIR/dsh-${CURRENT}-${STAMP}.tar.gz"
tar czf "$TARBALL" -C "$(dirname "$DSH_DIR")" \
    --exclude='backups' --exclude='sessions' \
    "$(basename "$DSH_DIR")" 2>/dev/null \
  && ok "backed up to $TARBALL" \
  || warn "backup failed (continuing)"

ls -1t "$BACKUP_DIR"/dsh-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm --

# ------------------------------------------------------------- upgrade
say "Upgrading $PKG -> $LATEST"
npm install -g "$PKG@$LATEST" || die "npm install failed"
ok "installed $(dsh --version 2>/dev/null || echo "$LATEST")"

# --------------------------------------------------------------- patch
say "Re-applying the OpenRouter patch"
bash "$REPO/scripts/patch-openrouter.sh" || die "patch failed on the new version.
    The upgrade succeeded but provider pinning is NOT active.
    See docs/03-provider-pinning.md -> 'Porting to a newer dsh'."

# ------------------------------------------------------- settings check
say "Checking settings still parse"

SETTINGS="$DSH_DIR/settings.yaml"
if [ -f "$SETTINGS" ]; then
  python3 - "$SETTINGS" <<'PY' || die "settings.yaml no longer parses"
import sys
try:
    import yaml
except ImportError:
    print("    (PyYAML not installed -- skipped)")
    sys.exit(0)
with open(sys.argv[1]) as f:
    d = yaml.safe_load(f)
m = d["llm-pi-ai"]["providers"]["openrouter"]["models"][0]
assert m["compat"]["openRouterRouting"]["order"], "routing order missing"
assert m["reasoningEfforts"]["off"] != False, 'reasoningEfforts.off is boolean false -- quote it'
print("    settings.yaml OK")
PY
  ok "settings intact"
else
  warn "no settings.yaml at $SETTINGS"
fi

# -------------------------------------------------------------- verify
if [ -n "${OPENROUTER_API_KEY:-}" ]; then
  say "Verifying on the wire"
  python3 "$REPO/scripts/verify-wire.py" || die "wire verification failed"
else
  warn "OPENROUTER_API_KEY not set -- skipping wire verification"
fi

printf '\n\033[1;32m✓ Updated to %s with the patch re-applied.\033[0m\n\n' "$LATEST"
