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

### The model rejects `effort: "none"` (reasoning is mandatory)

Some models cannot have reasoning disabled at all. Check the catalogue first:

```bash
curl -s https://openrouter.ai/api/v1/models \
  | jq '.data[] | select(.id=="<vendor/model>") | .reasoning'
```

`"mandatory": true` means **do not send `effort: "none"`** — it is rejected,
not ignored. Drop `"off"` from `reasoningEfforts`, declare only the levels in
`supported_efforts`, and default `reasoning:` to the lowest one.

Two traps here: `supported_efforts` may omit `medium` (common, and invalid on
models that do not list it), and `default_effort` is often `max` — so an
omitted reasoning field buys the *most expensive* level rather than none. See
[09-dsh-0.1.1-and-mandatory-reasoning.md](09-dsh-0.1.1-and-mandatory-reasoning.md).

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

## Web search: `DeepSeek search has no API key for "DEEPSEEK_API_KEY"`

**Do not go looking for a DeepSeek key.** The message names the wrong cause.
dsh's stock search provider cannot work over OpenRouter even with a valid
DeepSeek key: it requires `web_search_tool_result` blocks, and OpenRouter
returns the same data as `citations[]` on text blocks instead. Native search
does run — `server_tool_use` is present in the response — so this is a shape
mismatch wearing a credential error's clothing.

The provider reports itself as available with no credential at all
(`available()` returns true whenever `resolveApiKey` is defined, which is
always), so the web seam selects it and the failure surfaces at search time
rather than as "no usable search provider".

Fix: disable the stock provider, register one that reads `citations[]`, and
point the web seam at it. See [`08-web-search.md`](08-web-search.md) and
[`providers/dsh-web-search-openrouter/`](../providers/dsh-web-search-openrouter/).

If you have already done that and still see this error, check for a **later
patch layer** re-enabling `web-search-deepseek` or setting
`searchProvider: null`. Later entries win, silently.

---

## Web search: `the model did not invoke native web search`

The query reached the model but it answered from memory instead of searching.
Send the query wrapped, not bare:

```js
content: [{ type: "text", text: `Perform a web search for the query: ${query}` }]
```

Measured 2026-08-19: `"What is the capital of Denmark?"` sent bare returns
`[thinking, text]` with no `server_tool_use` and 0 citations; wrapped, it
returns 5. This hits hardest on queries the model believes it already knows.

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

**Do not trust `supports_implicit_caching: false` to mean "no caching".**
Measured on `z-ai/glm-5.3-flash` via Novita, which reports that flag on every
endpoint: two identical 7,144-token requests went 0% → **99%** cached,
$0.000537 → $0.000110. The flag means "does not advertise implicit caching".
Send the same prompt twice and measure.

---

## `NO_ADAPTER: no adapter registered for provider "<x>"`

The route was refused during validation, so it never registered. The message
names the route, not the reason.

On dsh 0.1.1+, the most likely reason is a **withheld compat field**: setting
`openRouterRouting` on an unpatched build rejects the whole profile. Confirm:

```bash
grep -n 'openRouterRouting:' \
  "$(npm root -g)"/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-llm-pi-ai/lib/index.js
```

`"withhold"` → run `scripts/patch-openrouter.sh`. `z.any()` and `"offer"` →
already patched, look elsewhere.

Other causes: a `reasoningEfforts` level the model does not accept, or a
`compat` key misspelled. Each raises a specific message — surface it by
validating the profile directly rather than reading dsh's summary.

---

## The patch script says an anchor was not found

Your dsh version differs from the ones the patch targets (0.1.0-rc.7 and
0.1.1-rc.2). The script writes nothing in this case, which is the intended
behaviour — a patch that half-applies is worse than one that refuses.

First check whether you still need it:

```bash
grep -n "openRouterRouting" \
  "$(npm root -g)"/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-llm-pi-ai/lib/index.js
```

Read the **disposition**, not just the presence of the name:

| what you see | meaning | action |
|---|---|---|
| nothing | field unknown to dsh | patch (pre-0.1.1 layout) |
| `openRouterRouting: "withhold"` | known and **refused** | patch (0.1.1+ layout) |
| `openRouterRouting: "offer"` | patched already | none |
| `openRouterRouting: z.any()` | patched already (schema) | none |
| a real `z.object({…})` shape | genuinely fixed upstream | drop the patch step |

**`"withhold"` is a refusal, not a fix.** An earlier version of this document
and of `patch-openrouter.sh` treated any occurrence of the name as an upstream
fix, printed "GOOD NEWS … drop this script", and left users with no pin at all.
See [09-dsh-0.1.1-and-mandatory-reasoning.md](09-dsh-0.1.1-and-mandatory-reasoning.md).

Otherwise see "Porting to a newer dsh" in
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
