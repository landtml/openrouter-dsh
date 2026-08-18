# Attaching any OpenRouter model to dsh

This is the general procedure. For a complete worked example with measured
values, see [`01-deepseek-v4-flash.md`](01-deepseek-v4-flash.md).

Everything here was verified against **dsh 0.1.0-rc.7** and the
`@earendil-works/pi-ai` it vendors.

Source line numbers are cited so you can check the claims yourself. They refer
to the **unpatched** files as npm installs them, and they will drift across
versions — search for the function name rather than jumping to the line if the
number no longer matches. The function names and the logic have been stable.

---

## 1. The shape of a provider route

An OpenRouter model lives under `llm-pi-ai.providers.<routeKey>` in
`~/.dsh/settings.yaml`. The route key is yours to choose — it is the name you
select in the Web UI, not something OpenRouter defines.

```yaml
llm-pi-ai:
  providers:
    openrouter:                  # <- your route key, arbitrary
      displayName: OpenRouter    # what the Web UI shows
      api: openai-completions    # OpenRouter speaks OpenAI's dialect
      baseURL: https://openrouter.ai/api/v1
      apiKeyEnv: OPENROUTER_API_KEY
      models:
        - id: vendor/model-name  # exactly as OpenRouter lists it
          name: Human Readable Name
```

That minimum works. Everything below is about making it work *correctly*.

### `api: openai-completions` is load-bearing

This is not just a protocol hint. `resolveModelCompat()` refuses reasoning
switches on any other api:

```js
if (api !== "openai-completions") {
    if (entry.compat?.thinkingFormat !== void 0 || ...)
        invalid(provider, `model "${entry.id}" sets compat reasoning switches,
                 but its api is "${api}"; thinkingFormat and
                 supportsReasoningEffort exist only on openai-completions`);
    return {};
}
```

### `apiKeyEnv`, not `apiKey`

Name the environment variable; never put the key in the file. You will commit
this file eventually.

---

## 2. Getting the model's real parameters

**Do not use the numbers from OpenRouter's model page.** They describe the
*best* endpoint on the board, which may not be the one that serves you.

Ask the endpoints API instead:

```bash
curl -s "https://openrouter.ai/api/v1/models/vendor/model-name/endpoints" \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" | python3 -m json.tool
```

Each entry in `.data.endpoints` is one provider serving that model, with its
**own** limits:

| Field | Meaning | Maps to |
|---|---|---|
| `provider_name` | the name you use in `order` | — |
| `context_length` | that provider's context window | `contextWindow` |
| `max_completion_tokens` | that provider's output ceiling | `maxTokens` |
| `quantization` | `fp8`, `fp4`, `bf16`… | `quantizations` filter |
| `pricing.prompt` / `.completion` | per-token cost | — |
| `supported_parameters` | what it accepts (`seed`, `structured_outputs`…) | — |
| `uptime_last_30m` | recent reliability | — |

`scripts/probe-endpoints.py` in this repo prints that table sorted by price.

### The 404 that is not a 404

If you pin a provider **and** set `maxTokens` above that provider's ceiling,
every endpoint gets filtered out and OpenRouter answers:

```
404 "No endpoints found for vendor/model-name"
```

The message names the model, so it reads as though the model is gone. It is
not. Your own token ceiling excluded every candidate.

> **Rule: pin the provider first, then take that provider's numbers.**

### `maxTokens` applies to the WHOLE `order`, not just the first entry

`maxTokens` is a per-model setting, so it filters **every** provider in your
`order` list. If a fallback's ceiling is lower than your primary's, that
fallback is silently excluded from the candidate set — exactly when
`allow_fallbacks: true` was supposed to save you.

Measured, pinning one provider at a time on this model:

```
CoreWeave (ceiling 262144), max_tokens 300000  -> 404 No endpoints found
CoreWeave (ceiling 262144), max_tokens   1000  -> served by CoreWeave
```

For the config in this repo the chain is intact — DeepInfra, GMICloud and
BaseTen all accept `384000`, verified by pinning each alone:

```
GMICloud, max_tokens 384000  -> ok
BaseTen,  max_tokens 384000  -> ok
```

**Rule:** set `maxTokens` to the *minimum* ceiling across every provider in
`order`, not the primary's. Or omit it entirely and let each provider apply its
own — the safest choice when you list several with different limits.

One trap in the probe output: a provider that reports
`max_completion_tokens: 0` (GMICloud does) is not advertising a limit, not
declaring a zero one. Do not copy that `0` into `maxTokens`, and do not treat
it as an exclusion.


---

## 3. Provider pinning — the part that needs a patch

**OpenRouter accepts provider routing only as a `provider` object in the
request BODY.** Measured, all three routes tested live against the same model:

| Method | Result |
|---|---|
| body `{"provider":{"order":["DeepInfra"]}}` | routed to DeepInfra ✅ |
| header `X-OR-Provider-Order: DeepInfra` | routed to "Io Net" ❌ ignored |
| model suffix `…-0731:nitro` | routed to "Wafer" ❌ ignored |

Only the body works. Headers and suffixes fail **silently** — you get an
answer, from someone else, at a different price.

### Why a patch is needed

The upstream library dsh vendors already emits this field.
`@earendil-works/pi-ai/dist/api/openai-completions.js:644`:

```js
// OpenRouter provider routing preferences
if (model.compat?.openRouterRouting) {
    params.provider = model.compat.openRouterRouting;
}
```

The capability was never missing. dsh's *own* settings schema simply did not
declare `openRouterRouting` as a valid `compat` key, so schemastery stripped
it before it could reach that code. `dsh-llm-pi-ai/lib/index.js:1369`
originally read:

```js
const compatProfile = z.object({
    thinkingFormat: z.union(SUPPORTED_THINKING_FORMATS),
    supportsReasoningEffort: z.boolean()
});
```

`scripts/patch-openrouter.sh` widens that schema and threads the value through
the two functions between settings and pi-ai. Three edits, idempotent, with a
`.orig` backup. See [`03-provider-pinning.md`](03-provider-pinning.md).

**Without the patch, the entire `openRouterRouting` block is silently
ignored.** No error, no warning — your requests just route wherever OpenRouter
prefers.

### The routing block

```yaml
compat:
  openRouterRouting:
    order: [ProviderA, ProviderB, ProviderC]
    allow_fallbacks: true
    quantizations: [fp8]
```

This is passed through verbatim, so every field in
[OpenRouter's provider routing docs](https://openrouter.ai/docs/features/provider-routing)
works here: `only`, `ignore`, `sort`, `data_collection`, `require_parameters`,
`max_price`.

#### `allow_fallbacks` — choose deliberately

This is the one field whose correct value depends on what you are doing.

| Value | Use for | Because |
|---|---|---|
| `true` | interactive & agentic runs | a transient 429 reroutes instead of killing a long run |
| `false` | benchmarking, cost receipts | a silent failover changes quantization, cache and price mid-run |

Measured over one 93-exchange agentic run with fallbacks **on**: 32% of
requests were served by the second-choice provider. Those requests were *not
worse* — 94.6% vs 94.4% cache hit, faster median total latency, and marginally
cheaper per output token. All listed providers were `fp8`, so answer quality
did not vary.

In a long autonomous run, one unretried 429 ends the run. That is a much worse
outcome than some requests landing on your second choice.

#### `quantizations` — set it

Cheaper `fp4` endpoints exist for most popular models and they are measurably
worse. Listing `[fp8]` excludes them even if one would otherwise rank first.

---

## 4. Reasoning — the expensive silent failure

**If you declare no `reasoningEfforts` map, reasoning silently stays ON**, and
you are billed for it, even with `reasoning: "off"` on the route.

### The mechanism

pi-ai's OpenRouter reasoning branch is guarded on *two* conditions
(`openai-completions.js:598`):

```js
else if (compat.thinkingFormat === "openrouter" && model.reasoning) {
```

`model.reasoning` comes from dsh's `resolveModelReasoning()`
(`dsh-llm-pi-ai/lib/index.js:1063`):

```js
function resolveModelReasoning(provider, entry, base) {
    const efforts = entry.reasoningEfforts;
    if (efforts === void 0) return { reasoning: base?.reasoning ?? false };
```

So with no `reasoningEfforts`, `model.reasoning` is **false**, the branch never
runs, **no reasoning field is sent at all**, and OpenRouter falls back to the
provider's own default — which is reasoning **ON**.

Measured consequence: **5,502 reasoning tokens billed across a 16-exchange
run** that had `reasoning: "off"` configured. That was 46% of all output tokens
and 9% of the run's cost, for reasoning that had been explicitly disabled.

### The fix

Declare the map, and quote `"off"`:

```yaml
reasoningEfforts:
  "off": none        # MUST be quoted -- see below
  low: low
  medium: medium
  high: high
compat:
  thinkingFormat: openrouter
  supportsReasoningEffort: true
```

After this, verified across 93 consecutive requests: `{"reasoning":{"effort":
"none"}}` on every request, **0 reasoning tokens billed**.

### The YAML `off` trap

In YAML 1.1, a bare `off` is the **boolean false**, not the string `"off"`.
Unquoted, it fails schema validation with a confusing message — or worse,
parses as a value you did not intend. This bites in two places:

```yaml
reasoning: "off"          # route level  -- quote it
reasoningEfforts:
  "off": none             # map key      -- quote it
```

`merge_settings.py` in this repo checks for exactly this and refuses to write
a file where `off` parsed as boolean `false`.

### `thinkingFormat: openrouter`

Selects OpenRouter's nested `{"reasoning":{"effort":…}}` object over OpenAI's
flat `reasoning_effort` string. Without it, the request goes out with
`reasoning: null` and the provider bills reasoning tokens anyway (measured: 76
tokens on a request configured with reasoning off).

The full set, from `SUPPORTED_THINKING_FORMATS`
(`dsh-llm-pi-ai/lib/index.js:964`): `openai`, `deepseek`, `openrouter`,
`together`, `zai`, `qwen`, `string-thinking`, `ant-ling`. Use `openrouter`
whenever you are going through OpenRouter, regardless of who made the model.

---

## 5. Caching

Prompt caching is the dominant cost lever for agentic work, where each turn
resends the whole conversation.

```yaml
cacheRetention: long
```

Measured across a 93-exchange run: **94.5% of 3.4M prompt tokens served from
cache**. At cold rates that run would have cost ~$0.27; it cost $0.076.

Two properties worth knowing:

1. **Cache is per-provider.** If request 2 lands on a different provider than
   request 1, there is nothing to hit. This is a real cost of
   `allow_fallbacks: true`, and the reason `verify-wire.py` pins hard before
   measuring cache.
2. **It warms up.** Measured curve over one run: 0% → 74% → 86% → 98% by the
   fourth exchange, then steady in the mid-90s.

---

## 6. Timeouts

```yaml
timeoutMs: 180000            # hard ceiling on one request
streamIdleTimeoutMs: 120000  # max gap between SSE chunks
transport: sse
```

Set `timeoutMs` from the p99 you actually observe, not the median. A 15.8s
stall was measured on an otherwise-warm path; a 75.8s generation was measured
on a large file write. `streamIdleTimeoutMs` only matters when routing lands on
a slow endpoint.

---

## 7. Making it the default

Without this, new sessions and headless runs fall back to dsh's built-in
`deepseek-official` route, which needs a `DEEPSEEK_API_KEY` an OpenRouter-only
setup does not have:

```yaml
agent-default-model:
  provider: openrouter                # your route key from step 1
  model: vendor/model-name
```

The Web UI's model picker rewrites this block when you choose a model there.

---

## 8. Verify — do not assume

Every failure mode above is **silent**. A wrong pin does not error; it routes
elsewhere. A missing `reasoningEfforts` does not error; it bills you. The only
way to know is to look at the wire.

```bash
python3 scripts/verify-wire.py --model vendor/model-name --order ProviderA,ProviderB
```

Checks the pin held, reasoning tokens are zero, caching engaged, and the
completion was clean. Exit 0 iff all pass.

For continuous observation of a real session, `scripts/watch-proxy.py` records
every exchange between dsh and OpenRouter and flags silent fallbacks, stray
reasoning tokens, and wrong quantization.

---

## Checklist

```yaml
llm-pi-ai:
  providers:
    <routeKey>:
      api: openai-completions          # [ ] required for compat switches
      baseURL: https://openrouter.ai/api/v1
      apiKeyEnv: OPENROUTER_API_KEY    # [ ] env var, never the key
      reasoning: "off"                 # [ ] QUOTED
      cacheRetention: long             # [ ] the cost lever
      transport: sse
      timeoutMs: 180000
      models:
        - id: vendor/model-name        # [ ] exact OpenRouter id
          contextWindow: <from endpoints API>   # [ ] pinned provider's number
          maxTokens: <from endpoints API>       # [ ] pinned provider's number
          reasoningEfforts:            # [ ] REQUIRED or reasoning stays on
            "off": none                # [ ] QUOTED
            low: low
            medium: medium
            high: high
          compat:
            thinkingFormat: openrouter # [ ] or reasoning never reaches the wire
            supportsReasoningEffort: true
            openRouterRouting:         # [ ] REQUIRES THE PATCH
              order: [ProviderA, ProviderB]
              allow_fallbacks: true    # [ ] true=agentic, false=benchmarks
              quantizations: [fp8]     # [ ] excludes fp4
agent-default-model:                   # [ ] or headless uses the wrong route
  provider: <routeKey>
  model: vendor/model-name
```
