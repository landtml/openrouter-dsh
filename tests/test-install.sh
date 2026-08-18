#!/usr/bin/env bash
#
# test-install.sh -- end-to-end test of the whole repo against a throwaway
# DSH_HOME, so it never touches your real ~/.dsh.
#
#   ./tests/test-install.sh
#
# Covers: patch on a pristine file, idempotency, merge onto empty and onto an
# existing file with unrelated keys, YAML validity, the `off` boolean trap,
# and -- if OPENROUTER_API_KEY is set -- a real call through dsh proving the
# pin and reasoning setting reach the wire.
#
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }
head_() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

head_ "1. Shell scripts parse"
for f in "$REPO"/scripts/*.sh "$REPO"/tests/*.sh; do
  bash -n "$f" 2>/dev/null && ok "$(basename "$f")" || bad "$(basename "$f") has a syntax error"
done

head_ "2. Python scripts parse"
for f in "$REPO"/scripts/*.py; do
  python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$f" 2>/dev/null \
    && ok "$(basename "$f")" || bad "$(basename "$f") has a syntax error"
done

head_ "3. Patch applies to a pristine pi-ai"
PRISTINE="$TMP/pristine.js"
cat > "$PRISTINE" <<'EOF'
function resolveModelCompat(provider, entry, route, base, api) {
	const thinkingFormat = entry.compat?.thinkingFormat ?? route?.thinkingFormat;
	const supportsReasoningEffort = entry.compat?.supportsReasoningEffort ?? route?.supportsReasoningEffort;
	if (thinkingFormat === void 0 && supportsReasoningEffort === void 0) return {};
	return { compat: {
		...thinkingFormat === void 0 ? {} : { thinkingFormat },
		...supportsReasoningEffort === void 0 ? {} : { supportsReasoningEffort }
	} };
}
const compatProfile = z.object({
	thinkingFormat: z.union(SUPPORTED_THINKING_FORMATS),
	supportsReasoningEffort: z.boolean()
});
EOF
if bash "$REPO/scripts/patch-openrouter.sh" "$PRISTINE" >/dev/null 2>&1; then
  grep -q "openRouterRouting: z.any()" "$PRISTINE" && ok "schema widened" || bad "schema not widened"
  grep -q "openRouterRouting === void 0 ? {} : { openRouterRouting }" "$PRISTINE" \
    && ok "compat spread added" || bad "compat spread missing"
  [ -f "$PRISTINE.orig" ] && ok "backup created" || bad "no .orig backup"
else
  bad "patch script failed on a pristine file"
fi

head_ "4. Patch is idempotent"
BEFORE="$(md5sum "$PRISTINE" | cut -d' ' -f1)"
bash "$REPO/scripts/patch-openrouter.sh" "$PRISTINE" >/dev/null 2>&1
AFTER="$(md5sum "$PRISTINE" | cut -d' ' -f1)"
[ "$BEFORE" = "$AFTER" ] && ok "second run changed nothing" || bad "second run modified the file"

head_ "5. Patch refuses an unknown file rather than corrupting it"
echo "function somethingElse() { return 1; }" > "$TMP/wrong.js"
if bash "$REPO/scripts/patch-openrouter.sh" "$TMP/wrong.js" >/dev/null 2>&1; then
  bad "patch claimed success on a file with no anchors"
else
  grep -q "openRouterRouting" "$TMP/wrong.js" && bad "patch wrote to an unknown file" \
    || ok "refused, left the file untouched"
fi

head_ "6. Merge onto an empty settings file"
python3 "$REPO/scripts/merge_settings.py" "$TMP/fresh.yaml" \
        "$REPO/config/deepseek-v4-flash.yaml" >/dev/null 2>&1 \
  && ok "merge succeeded" || bad "merge failed"
grep -q "openRouterRouting" "$TMP/fresh.yaml" && ok "routing block present" || bad "routing block missing"
C="$(grep -c '#' "$TMP/fresh.yaml")"
[ "$C" -gt 50 ] && ok "comments preserved ($C lines)" || bad "comments stripped ($C)"

head_ "7. Merge preserves unrelated keys"
cat > "$TMP/existing.yaml" <<'EOF'
ui-onboarding:
  welcomeNoticeVersion: 2026-08-13.1
my-other-plugin:
  keepMe: true
  nested:
    deep: value
EOF
python3 "$REPO/scripts/merge_settings.py" "$TMP/existing.yaml" \
        "$REPO/config/deepseek-v4-flash.yaml" >/dev/null 2>&1
grep -q "keepMe: true" "$TMP/existing.yaml" && ok "unrelated plugin survived" || bad "unrelated plugin lost"
grep -q "deep: value" "$TMP/existing.yaml" && ok "nested value survived" || bad "nested value lost"
grep -q "welcomeNoticeVersion" "$TMP/existing.yaml" && ok "ui-onboarding survived" || bad "ui-onboarding lost"
grep -q "deepseek-v4-flash-0731" "$TMP/existing.yaml" && ok "model added" || bad "model not added"

head_ "7b. REGRESSION: a sibling provider inside llm-pi-ai survives"
# This is the case the first version of merge_settings.py destroyed: every dsh
# provider is nested inside the SINGLE top-level key `llm-pi-ai`, so a naive
# top-level block swap deletes them all.
cat > "$TMP/multi.yaml" <<'EOF'
# my carefully tuned anthropic setup, do not touch
llm-pi-ai:
  providers:
    anthropic:
      apiKeyEnv: ANTHROPIC_API_KEY
      models:
        - id: claude-x
    openrouter:
      apiKeyEnv: OLD_KEY_NAME
      models:
        - id: some-other-model
agent-default-model:
  provider: anthropic
  model: claude-x
EOF
python3 "$REPO/scripts/merge_settings.py" "$TMP/multi.yaml" \
        "$REPO/config/deepseek-v4-flash.yaml" >/dev/null 2>&1
python3 - "$TMP/multi.yaml" <<'PY'
import sys
try:
    import yaml
except ImportError:
    print("  SKIP PyYAML not installed"); sys.exit(0)
d = yaml.safe_load(open(sys.argv[1]))
p = d["llm-pi-ai"]["providers"]
raw = open(sys.argv[1]).read()
checks = [
    ("sibling provider `anthropic` survived", "anthropic" in p),
    ("its apiKeyEnv intact",
     p.get("anthropic", {}).get("apiKeyEnv") == "ANTHROPIC_API_KEY"),
    ("its models list intact",
     p.get("anthropic", {}).get("models") == [{"id": "claude-x"}]),
    ("openrouter was replaced",
     p["openrouter"]["models"][0]["id"] == "deepseek/deepseek-v4-flash-0731"),
    ("routing pin present",
     bool(p["openrouter"]["models"][0]["compat"]["openRouterRouting"]["order"])),
    ("user's own comment survived", "do not touch" in raw),
]
bad = 0
for label, good in checks:
    print(("  \033[32mPASS\033[0m " if good else "  \033[31mFAIL\033[0m ") + label)
    bad += 0 if good else 1
sys.exit(1 if bad else 0)
PY
if [ $? -eq 0 ]; then PASS=$((PASS+6)); else FAIL=$((FAIL+1)); fi

head_ "7c. REGRESSION: duplicate top-level keys are refused, not duplicated"
printf 'llm-pi-ai:\n  providers:\n    a:\n      apiKeyEnv: X\nfoo: 1\nllm-pi-ai:\n  providers:\n    b:\n      apiKeyEnv: Y\n' > "$TMP/dup.yaml"
if python3 "$REPO/scripts/merge_settings.py" "$TMP/dup.yaml" \
           "$REPO/config/deepseek-v4-flash.yaml" >/dev/null 2>&1; then
  bad "accepted a file with duplicate top-level keys"
else
  N="$(grep -c 'displayName: OpenRouter' "$TMP/dup.yaml" || true)"
  [ "$N" -eq 0 ] && ok "refused and left the file untouched" \
                 || bad "wrote the fragment $N time(s) into a malformed file"
fi

head_ "8. Result is valid YAML with the right values"
python3 - "$TMP/fresh.yaml" <<'PY'
import sys
try:
    import yaml
except ImportError:
    print("  SKIP PyYAML not installed"); sys.exit(0)
d = yaml.safe_load(open(sys.argv[1]))
m = d["llm-pi-ai"]["providers"]["openrouter"]["models"][0]
checks = [
    ("valid YAML mapping", isinstance(d, dict)),
    ("model id correct", m["id"] == "deepseek/deepseek-v4-flash-0731"),
    ("contextWindow is DeepInfra's", m["contextWindow"] == 1048576),
    ("maxTokens is DeepInfra's", m["maxTokens"] == 384000),
    ('route reasoning is the string "off"',
     d["llm-pi-ai"]["providers"]["openrouter"]["reasoning"] == "off"),
    ("reasoningEfforts.off is 'none', NOT boolean False",
     m["reasoningEfforts"].get("off") == "none"),
    ("thinkingFormat is openrouter",
     m["compat"]["thinkingFormat"] == "openrouter"),
    ("routing order starts with DeepInfra",
     m["compat"]["openRouterRouting"]["order"][0] == "DeepInfra"),
    ("quantizations is [fp8]",
     m["compat"]["openRouterRouting"]["quantizations"] == ["fp8"]),
    ("agent-default-model points at this model",
     d["agent-default-model"]["model"] == "deepseek/deepseek-v4-flash-0731"),
]
bad = 0
for label, good in checks:
    print(("  \033[32mPASS\033[0m " if good else "  \033[31mFAIL\033[0m ") + label)
    bad += 0 if good else 1
sys.exit(1 if bad else 0)
PY
if [ $? -eq 0 ]; then PASS=$((PASS+10)); else FAIL=$((FAIL+1)); fi

head_ "9. Live: config drives a real dsh call"
if [ -z "${OPENROUTER_API_KEY:-}" ]; then
  echo "  SKIP  OPENROUTER_API_KEY not set"
elif ! command -v dsh >/dev/null 2>&1; then
  echo "  SKIP  dsh not installed"
else
  export DSH_HOME="$TMP/dshhome"
  mkdir -p "$DSH_HOME"
  cp "$TMP/fresh.yaml" "$DSH_HOME/settings.yaml"
  [ -d "$HOME/.dsh/profiles" ] && cp -r "$HOME/.dsh/profiles" "$DSH_HOME/" 2>/dev/null
  OUT="$(timeout 180 dsh --profile headless "Reply with exactly: TESTOK" 2>&1 | tail -3)"
  echo "$OUT" | grep -q "TESTOK" \
    && ok "dsh answered through the generated config" \
    || bad "dsh call failed: $OUT"
fi

printf '\n\033[1m%d passed, %d failed\033[0m\n\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
