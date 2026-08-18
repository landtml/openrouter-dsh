#!/usr/bin/env bash
#
# patch-openrouter.sh -- widen dsh's compat schema so OpenRouter provider
# routing reaches the wire.
#
# WHY THIS EXISTS
# ---------------
# OpenRouter accepts provider pinning ONLY as a `provider` object in the
# request BODY. Headers and model-name suffixes are silently ignored
# (measured -- see docs/03-provider-pinning.md).
#
# The upstream library dsh vendors, @earendil-works/pi-ai, ALREADY emits that
# body field. In dist/api/openai-completions.js:
#
#     // OpenRouter provider routing preferences
#     if (model.compat?.openRouterRouting) {
#         params.provider = model.compat.openRouterRouting;
#     }
#
# The capability was never missing. dsh's own settings schema simply did not
# declare `openRouterRouting` as a valid compat key, so schemastery stripped
# the field before it ever reached that code.
#
# This script widens that one schema and threads the value through the two
# functions between the settings file and pi-ai. Three edits, all idempotent.
#
# IMPORTANT: npm replaces the patched file on every `dsh` upgrade. Re-run this
# after `npm update`. `dsh-update` (scripts/dsh-update.sh) does it for you.
#
set -euo pipefail

TARGET="${1:-}"

find_target() {
  local roots=(
    "$(npm root -g 2>/dev/null || true)"
    "$HOME/.npm-global/lib/node_modules"
    "/usr/local/lib/node_modules"
    "/usr/lib/node_modules"
    "/opt/homebrew/lib/node_modules"
  )
  for r in "${roots[@]}"; do
    [ -n "$r" ] || continue
    local p="$r/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-llm-pi-ai/lib/index.js"
    [ -f "$p" ] && { echo "$p"; return 0; }
  done
  # Fallback: hunt for it anywhere under the global root.
  local gr; gr="$(npm root -g 2>/dev/null || true)"
  if [ -n "$gr" ] && [ -d "$gr" ]; then
    find "$gr" -path "*dsh-llm-pi-ai/lib/index.js" -print -quit 2>/dev/null && return 0
  fi
  return 1
}

if [ -z "$TARGET" ]; then
  TARGET="$(find_target || true)"
fi

if [ -z "$TARGET" ] || [ ! -f "$TARGET" ]; then
  echo "ERROR: could not locate dsh-llm-pi-ai/lib/index.js" >&2
  echo "       Is dsh installed?  npm ls -g @deepseek-ai/dsh" >&2
  echo "       Or pass the path explicitly:  $0 /path/to/lib/index.js" >&2
  exit 1
fi

echo "target: $TARGET"

# ---------------------------------------------------------------- idempotency
if grep -q "openRouterRouting: z.any()" "$TARGET" \
   && grep -q "openRouterRouting === void 0 ? {} : { openRouterRouting }" "$TARGET"; then
  echo "already patched -- nothing to do"
  exit 0
fi

# ------------------------------------------------------------------- backup
BACKUP="${TARGET}.orig"
if [ ! -f "$BACKUP" ]; then
  cp "$TARGET" "$BACKUP"
  echo "backup: $BACKUP"
fi

# --------------------------------------------------------------------- patch
# Node is used rather than sed because these are exact-substring replacements
# on minified-ish JS; sed's regex metacharacters make this needlessly fragile.
node - "$TARGET" <<'NODE'
const fs = require("fs");
const file = process.argv[2];
let s = fs.readFileSync(file, "utf8");
let applied = 0;

function edit(name, from, to) {
  if (s.includes(to)) { console.log(`  [skip] ${name} (already present)`); return; }
  if (!s.includes(from)) {
    console.error(`  [FAIL] ${name}: anchor not found.`);
    console.error(`         This dsh version differs from the one this patch targets.`);
    console.error(`         Expected to find:\n         ${from.slice(0, 120)}`);
    process.exit(2);
  }
  s = s.replace(from, to);
  applied++;
  console.log(`  [ok]   ${name}`);
}

// EDIT 1 -- schema. Without this, schemastery strips openRouterRouting from
// the parsed settings and nothing downstream ever sees it.
edit("schema: accept openRouterRouting",
  `const compatProfile = z.object({
	thinkingFormat: z.union(SUPPORTED_THINKING_FORMATS),
	supportsReasoningEffort: z.boolean()
});`,
  `const compatProfile = z.object({
	thinkingFormat: z.union(SUPPORTED_THINKING_FORMATS),
	supportsReasoningEffort: z.boolean(),
	openRouterRouting: z.any()
});`);

// EDIT 2 -- read the value and keep the early-return honest. Without the
// added guard clause, a model that sets ONLY openRouterRouting (no reasoning
// switches) returns {} here and the routing is dropped.
edit("resolve: read + guard",
  `	const supportsReasoningEffort = entry.compat?.supportsReasoningEffort ?? route?.supportsReasoningEffort;
	if (thinkingFormat === void 0 && supportsReasoningEffort === void 0) return {};`,
  `	const supportsReasoningEffort = entry.compat?.supportsReasoningEffort ?? route?.supportsReasoningEffort;
	const openRouterRouting = entry.compat?.openRouterRouting ?? route?.openRouterRouting;
	if (thinkingFormat === void 0 && supportsReasoningEffort === void 0 && openRouterRouting === void 0) return {};`);

// EDIT 3 -- emit it into the materialized model's compat block, which is what
// pi-ai reads at openai-completions.js:643.
edit("emit: spread into compat",
  `		...supportsReasoningEffort === void 0 ? {} : { supportsReasoningEffort }
	} };`,
  `		...supportsReasoningEffort === void 0 ? {} : { supportsReasoningEffort },
		...openRouterRouting === void 0 ? {} : { openRouterRouting }
	} };`);

fs.writeFileSync(file, s);
console.log(`applied ${applied} edit(s)`);
NODE

# ------------------------------------------------------------------- verify
echo
echo "verifying..."
node -e '
const s = require("fs").readFileSync(process.argv[1], "utf8");
const checks = [
  ["schema declares openRouterRouting", "openRouterRouting: z.any()"],
  ["guard reads openRouterRouting",     "openRouterRouting === void 0) return {}"],
  ["compat spreads openRouterRouting",  "{ openRouterRouting }"],
];
let bad = 0;
for (const [label, needle] of checks) {
  const ok = s.includes(needle);
  console.log(`  ${ok ? "OK  " : "MISS"} ${label}`);
  if (!ok) bad++;
}
process.exit(bad ? 1 : 0);
' "$TARGET"

echo
echo "PATCHED. The provider pin will now reach the request body."
echo "Prove it end-to-end with:  scripts/verify-wire.py"
