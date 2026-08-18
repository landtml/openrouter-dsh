# Provider pinning: why it needs a patch, and what the patch does

## The problem in one paragraph

OpenRouter serves most models from a dozen different providers at different
prices, quantizations, context limits and latencies. Which one you get is
OpenRouter's choice unless you say otherwise. Saying otherwise requires putting
a `provider` object in the request **body** — and dsh 0.1.0-rc.7's settings
schema silently discards the config key that would produce it.

---

## Measured: only the body works

Three documented ways to influence routing, all tested live against
`deepseek/deepseek-v4-flash-0731`:

| Method | Requested | Actually served by | Verdict |
|---|---|---|---|
| body `{"provider":{"order":["DeepInfra"]}}` | DeepInfra | **DeepInfra** | works |
| header `X-OR-Provider-Order: DeepInfra` | DeepInfra | Io Net | ignored |
| model suffix `deepseek-v4-flash-0731:nitro` | fastest | Wafer | ignored |

The two failing methods return a normal 200 with a normal completion. Nothing
signals that your routing preference was dropped. You simply pay a different
price for a different quantization at a different latency.

---

## Where the field gets lost

The chain from your settings file to the wire has four links. Link 3 was
broken.

```
  ~/.dsh/settings.yaml
        │  compat.openRouterRouting: {order: [...], ...}
        ▼
  [1] schemastery validates against `compatProfile`
        │  ← DROPS unknown keys. openRouterRouting was not declared.
        ▼
  [2] resolveModelCompat()  (dsh-llm-pi-ai/lib/index.js:1105)
        │  ← builds the model's compat block
        ▼
  [3] materialized model.compat
        │
        ▼
  [4] pi-ai openai-completions.js:643
        if (model.compat?.openRouterRouting)
            params.provider = model.compat.openRouterRouting;   ← already worked!
        ▼
      request body
```

**The capability was never missing.** Upstream `@earendil-works/pi-ai` has
emitted this field all along. dsh's own schema layer just never declared the
key, so schemastery — which strips anything undeclared — removed it at step 1,
and steps 2–4 never saw it.

This is why the fix is three lines rather than a feature implementation.

---

## The three edits

`scripts/patch-openrouter.sh` applies all three. Each is an exact-substring
replacement, idempotent, with a `.orig` backup taken on first run.

### Edit 1 — declare the key (the actual fix)

`dsh-llm-pi-ai/lib/index.js:1371`

```diff
 const compatProfile = z.object({
     thinkingFormat: z.union(SUPPORTED_THINKING_FORMATS),
-    supportsReasoningEffort: z.boolean()
+    supportsReasoningEffort: z.boolean(),
+    openRouterRouting: z.any()
 });
```

`z.any()` is deliberate. OpenRouter's routing object has a dozen optional
fields (`order`, `only`, `ignore`, `sort`, `allow_fallbacks`, `quantizations`,
`data_collection`, `require_parameters`, `max_price`, …) and they change as
OpenRouter ships features. Mirroring that schema here would mean re-patching
whenever OpenRouter adds a field. The object is passed through verbatim to an
API that validates it properly, so a permissive local schema costs nothing and
survives upstream change.

### Edit 2 — read it, and fix the early return

`resolveModelCompat()`, same file, ~line 1106

```diff
 const supportsReasoningEffort = entry.compat?.supportsReasoningEffort ?? route?.supportsReasoningEffort;
+const openRouterRouting = entry.compat?.openRouterRouting ?? route?.openRouterRouting;
-if (thinkingFormat === void 0 && supportsReasoningEffort === void 0) return {};
+if (thinkingFormat === void 0 && supportsReasoningEffort === void 0 && openRouterRouting === void 0) return {};
```

The guard clause matters. Without it, a model that sets **only**
`openRouterRouting` and no reasoning switches hits the early `return {}` and
loses the routing again — a subtle partial failure that would work for anyone
who happened to also set `thinkingFormat` and break for anyone who didn't.

Note both lines use `entry.compat?.X ?? route?.X`, so routing can be set
per-model or once at the route level.

### Edit 3 — emit it

Same function, the return statement

```diff
     ...supportsReasoningEffort === void 0 ? {} : { supportsReasoningEffort },
+    ...openRouterRouting === void 0 ? {} : { openRouterRouting }
 } };
```

Now `model.compat.openRouterRouting` exists, and pi-ai's line 643 does the rest.

---

## Applying it

```bash
./scripts/patch-openrouter.sh
```

Output on success:

```
target: /path/to/dsh-llm-pi-ai/lib/index.js
backup: /path/to/dsh-llm-pi-ai/lib/index.js.orig
  [ok]   schema: accept openRouterRouting
  [ok]   resolve: read + guard
  [ok]   emit: spread into compat
applied 3 edit(s)

verifying...
  OK   schema declares openRouterRouting
  OK   guard reads openRouterRouting
  OK   compat spreads openRouterRouting

PATCHED. The provider pin will now reach the request body.
```

Re-running prints `already patched -- nothing to do`.

If an anchor is not found, the script **exits without writing anything** and
tells you which edit failed. That means your dsh version differs from
0.1.0-rc.7; see "Porting" below.

### Reverting

```bash
cp <target>.orig <target>
# or simply
npm install -g @deepseek-ai/dsh --force
```

---

## npm will undo this

**Every `npm update -g @deepseek-ai/dsh` replaces the patched file.** The
routing then goes back to being silently dropped — no error, just different
providers and different bills.

Use the wrapper, which updates and re-patches in one step:

```bash
./scripts/dsh-update.sh
```

Or re-patch manually after any upgrade, then confirm:

```bash
./scripts/patch-openrouter.sh
python3 scripts/verify-wire.py
```

---

## Porting to a newer dsh

If `patch-openrouter.sh` reports a missing anchor, the code moved. To re-derive
the patch:

1. Confirm upstream still emits the field:
   ```bash
   grep -n "openRouterRouting" \
     "$(npm root -g)"/@deepseek-ai/dsh/node_modules/@earendil-works/pi-ai/dist/api/openai-completions.js
   ```
   If this prints `params.provider = model.compat.openRouterRouting`, the
   approach still holds.

2. Check whether dsh fixed it upstream:
   ```bash
   grep -n "openRouterRouting" \
     "$(npm root -g)"/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-llm-pi-ai/lib/index.js
   ```
   If the schema already declares it, **you do not need this patch** — delete
   the step from your workflow and just use the config.

3. Otherwise find the new locations of `compatProfile` and
   `resolveModelCompat` and update the three anchor strings in the script.

---

## Proving it worked

Schema-level checks confirm the file changed. Only the wire confirms the
behaviour:

```bash
python3 scripts/verify-wire.py
```

```
[PASS] provider pin -- served by DeepInfra (in pinned order [...])
[PASS] reasoning off -- 0 reasoning tokens billed
[PASS] prompt caching -- 256/377 tokens cached (67.9%) on the 2nd request
[PASS] clean completion -- finish_reason=stop
[PASS] model responded -- 'OK'
```

For a full session, `scripts/watch-proxy.py` records every exchange dsh makes
and flags silent fallbacks, stray reasoning tokens and wrong quantization. It
is how the two silent misconfigurations documented in this repo were found in
the first place.
