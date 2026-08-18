# Troubleshooting

Start here:

```bash
python3 scripts/verify-wire.py
```

It makes two real calls and reports which link in the chain is broken. Each
failure below is keyed to what that tool prints.

---

## `404 "No endpoints found for <model>"`

**The model is not gone.** With a provider pin, your own limits filter the
candidate list, and if nothing survives you get a 404 that names the model.

Causes, in order of likelihood:

1. **`maxTokens` exceeds the pinned provider's ceiling.** The model-level
   catalogue reports the *best* endpoint's numbers. Measured: setting
   `maxTokens: 393216` (the catalogue figure) with DeepInfra pinned produced
   this 404, because DeepInfra's real ceiling is 384000.

2. **`quantizations` excludes every endpoint your `order` lists.**

3. **`allow_fallbacks: false` plus a provider that is briefly down.**

4. **A typo in the model id.** Check the exact string:
   ```bash
   python3 scripts/probe-endpoints.py <model>
   ```

Fix: get the real per-provider numbers and use those.

```bash
python3 scripts/probe-endpoints.py deepseek/deepseek-v4-flash-0731 --quant fp8
```

---

## Reasoning tokens billed even though reasoning is off

`verify-wire.py` prints:

```
[FAIL] reasoning off -- 5502 reasoning tokens billed
```

Check in this order:

1. **Does the model declare `reasoningEfforts`?** Without it dsh sets
   `model.reasoning = false`, pi-ai's branch never runs, **no reasoning field
   is sent**, and the provider defaults to reasoning on. This is the most
   common cause.

2. **Is `off` quoted, in both places?**
   ```yaml
   reasoning: "off"         # route level
   reasoningEfforts:
     "off": none            # map key
   ```
   Bare `off` is boolean `false` in YAML 1.1.

3. **Is `compat.thinkingFormat: openrouter` set?** Without it the request goes
   out with `reasoning: null` and the provider bills anyway (measured: 76
   tokens).

4. **Is `api: openai-completions` on the route?** The compat switches are
   rejected on any other api.

Full mechanism: [`04-reasoning.md`](04-reasoning.md).

---

## Requests served by the wrong provider

`verify-wire.py` prints:

```
[FAIL] provider pin -- served by SomeoneElse, expected DeepInfra
```

1. **Is the patch applied?**
   ```bash
   ./scripts/patch-openrouter.sh          # prints "already patched" if so
   ```

2. **Did `npm update` revert it?** This is the usual cause of a setup that
   worked yesterday. npm replaces the patched file on every upgrade. Re-run the
   patch, or use `./scripts/dsh-update.sh`.

3. **Is it a fallback rather than a failure?** With `allow_fallbacks: true`,
   landing on your second choice is the system working. `verify-wire.py`
   reports this as `[INFO] fallback`, not a failure. Use
   `--expect-provider DeepInfra` to make it strict.

4. **Are you setting routing by header or model suffix?** Neither works.
   Measured: the header routed to "Io Net", the `:nitro` suffix to "Wafer".
   Only the request body's `provider` object is honoured.

---

## `settings.yaml` fails validation

### `reasoning` must be one of …

`off` is unquoted somewhere. YAML 1.1 turns it into boolean `false`.

### `model "<id>" sets compat reasoning switches, but its api is "<x>"`

Set `api: openai-completions` on the route. `thinkingFormat` and
`supportsReasoningEffort` exist only on that api.

### `model "<id>" has an empty reasoningEfforts`

Declare the levels, or set `reasoningEfforts: false` for a genuinely
non-reasoning model. An empty map is rejected deliberately.

### `reasoningEfforts.<level> needs the wire value dispatch should send`

Only `off` may be valueless. Every other level needs its wire spelling:

```yaml
reasoningEfforts:
  "off": none
  low: low        # not `low:` with nothing after it
```

### `reasoningEfforts offers no level beyond "off"`

The map must offer at least one real thinking level. For a non-reasoning model
use `reasoningEfforts: false`.

---

## The model does not appear in the Web UI picker

1. Restart dsh — settings are read at boot.
2. Confirm the file parses:
   ```bash
   python3 -c "import yaml,sys; yaml.safe_load(open(sys.argv[1]))" ~/.dsh/settings.yaml
   ```
3. Check the route key nests correctly under
   `llm-pi-ai.providers.<key>.models[]`.
4. Look at dsh's startup output — schema errors are reported at boot, not at
   first use.

---

## Headless works but the Web UI uses the wrong model (or vice versa)

The Web UI's model picker **writes** the `agent-default-model` block. If you
picked a different model there, it overwrote your config. Re-run
`./scripts/install.sh` (safe and idempotent), or fix the block by hand:

```yaml
agent-default-model:
  provider: openrouter
  model: deepseek/deepseek-v4-flash-0731
```

---

## `401 Unauthorized`

- Is `OPENROUTER_API_KEY` exported in the shell that launched dsh? A key set in
  your terminal is not visible to a desktop-launched process.
- Does `apiKeyEnv` name the right variable? It is the variable *name*, not the
  key.
- Is the key still valid? Check https://openrouter.ai/keys

---

## `429` / `engine_overloaded`

The pinned provider is rate-limiting or busy.

- With `allow_fallbacks: true` this mostly self-heals — you land on the next
  provider in `order`.
- With `allow_fallbacks: false` it is a hard failure by design.
- Concurrent runs against the same key make this much more likely. Measured:
  running two experiments simultaneously produced `engine_overloaded
  (upstream_provider_shared_pool)` on a single-word prompt.

If you need `allow_fallbacks: false` for receipt fidelity, add retry with
backoff that honours `Retry-After` rather than removing the pin.

---

## Cache hit rate is 0%

1. **Cache is per-provider.** If consecutive requests land on different
   providers there is nothing to hit. This is the real cost of
   `allow_fallbacks: true`, and why `verify-wire.py` pins hard before measuring
   cache.
2. **Short prompts may not be cached at all.** Providers set a minimum prefix
   length.
3. **The prefix must be stable.** Anything varying near the start of the prompt
   — a timestamp, a session id — invalidates the prefix every turn.
4. **Is `cacheRetention: long` set?**

Expect warm-up rather than an instant hit. Measured curve over a real run:
0% → 74% → 86% → 98% across the first four exchanges.

---

## The patch script says an anchor was not found

Your dsh version differs from 0.1.0-rc.7. The script writes nothing in this
case.

First check whether you still need it:

```bash
grep -n "openRouterRouting" \
  "$(npm root -g)"/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-llm-pi-ai/lib/index.js
```

If dsh now declares it in its own schema, **delete the patch step** — it was
fixed upstream. Otherwise see "Porting to a newer dsh" in
[`03-provider-pinning.md`](03-provider-pinning.md).

---

## Everything passes but something still feels wrong

Record a whole session and look at every request:

```bash
python3 scripts/watch-proxy.py 8799 ~/.local/share/dsh/watch
# set baseURL: http://127.0.0.1:8799/v1 in settings.yaml, then use dsh normally
python3 scripts/watch-report.py ~/.local/share/dsh/watch
```

The report gives per-exchange provider, TTFT, total latency, cache percentage,
output tokens and cost, then flags silent fallbacks and stray reasoning tokens.
Both bugs documented in this repo were found this way, not by reading code.

Remember to restore `baseURL` to `https://openrouter.ai/api/v1` afterwards.
