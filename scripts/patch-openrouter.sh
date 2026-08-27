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

# ------------------------------------------------- upstream-fix detection
# If a future dsh declares openRouterRouting in its OWN schema, this patch is
# obsolete. Distinguish that from "we patched it" by checking for our exact
# permissive form: upstream would almost certainly declare a real shape
# (z.object({...})), not z.any().
#
# CAUTION -- knowing the field is NOT the same as offering it. dsh 0.1.1-rc.2
# added a compat gate that names openRouterRouting and REFUSES it:
#
#     openRouterRouting: "withhold",     <- in COMPLETIONS_COMPAT_GATE
#
# A bare `grep -q openRouterRouting` matches that refusal. This script used to
# read it as an upstream fix and print "GOOD NEWS ... drop this script", which
# is the worst possible advice: the field is rejected, the route is refused
# wholesale, and the pin silently stops reaching the wire. Check the gate's
# disposition, not merely the field's presence.
if grep -q 'openRouterRouting: "withhold"' "$TARGET"; then
  : # upstream knows the field and refuses it -- the patch below flips that.
elif grep -q "openRouterRouting" "$TARGET" && ! grep -q "openRouterRouting: z.any()" "$TARGET"; then
  echo
  echo "GOOD NEWS: this dsh already declares openRouterRouting in its own schema:"
  grep -n "openRouterRouting" "$TARGET" | head -5 | sed 's/^/    /'
  echo
  echo "The patch is NOT needed on this version. Use the config as-is and drop"
  echo "this script from your workflow. Confirm with:  scripts/verify-wire.py"
  exit 0
fi

# ---------------------------------------------------------------- idempotency
# Two shapes count as patched, because the two dsh layouts need different work:
#   pre-0.1.1  three edits, ending in the spread into the compat block
#   0.1.1+     two edits; resolveModelCompat is gate-driven and needs no spread
if grep -q "openRouterRouting: z.any()" "$TARGET" \
   && { grep -q "openRouterRouting === void 0 ? {} : { openRouterRouting }" "$TARGET" \
        || grep -q 'openRouterRouting: "offer"' "$TARGET"; }; then
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

// Two dsh layouts need different work, so the edits are selected by which one
// this file is, rather than applied blindly:
//
//   pre-0.1.1   compatProfile has 3 fields; resolveModelCompat threads each
//               compat switch by hand, so the value must be read, guarded and
//               spread -- three edits.
//   0.1.1+      compatProfile has ~20 fields; resolveModelCompat is
//               gate-driven and threads any OFFERED field on its own, so only
//               the schema and the gate need touching -- two edits.
//
// Verified against 0.1.0-rc.7 and 0.1.1-rc.2.
const GATED = s.includes('openRouterRouting: "withhold"');

if (GATED) {
  // ---- dsh 0.1.1+ ----------------------------------------------------------
  // EDIT 1 -- schema. Anchored on the LAST field of compatProfile rather than
  // the whole object: that list grew from 3 fields to 20 between rc.7 and
  // rc.2, and anchoring on the full literal is what broke this patch.
  edit("schema: accept openRouterRouting",
    `	supportsStrictTools: z.boolean()
});`,
    `	supportsStrictTools: z.boolean(),
	openRouterRouting: z.any()
});`);

  // EDIT 2 -- the compat gate. This is the whole patch on this layout.
  //
  // Upstream marks the field "withhold": known, deliberately not configurable,
  // on the stated rationale that "pi-ai's installed catalog sets it for the
  // vendors that need it, so name that provider as the route instead."
  //
  // That rationale does not survive contact with the catalog: pi-ai ships NO
  // openRouterRouting for ANY model (checked 2026-08-27 against
  // dist/models.generated.js). Withholding the field therefore leaves no way
  // to express a provider pin at all -- and OpenRouter honours pinning ONLY as
  // a `provider` object in the request body.
  //
  // "offer" is the disposition resolveModelCompat reads to admit a configured
  // field, so this one word is what puts the pin back on the wire.
  edit("gate: offer openRouterRouting",
    `	openRouterRouting: "withhold",`,
    `	openRouterRouting: "offer",`);
} else {
  // ---- dsh pre-0.1.1 -------------------------------------------------------
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
}

fs.writeFileSync(file, s);
console.log(`applied ${applied} edit(s)`);
NODE

# ------------------------------------------------------------------- verify
echo
echo "verifying..."
node -e '
const s = require("fs").readFileSync(process.argv[1], "utf8");
// Checked per layout: the 0.1.1+ resolver is gate-driven, so the guard and
// spread that the older layout needs do not exist there and must not be
// required. Demanding them would fail a correctly patched 0.1.1+ file.
const gated = s.includes("openRouterRouting: \"offer\"");
const checks = gated
  ? [
      ["schema declares openRouterRouting", "openRouterRouting: z.any()"],
      ["compat gate offers openRouterRouting", "openRouterRouting: \"offer\""],
    ]
  : [
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
