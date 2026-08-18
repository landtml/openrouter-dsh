# Reasoning: the setting that silently ignores you

`reasoning: "off"` in your settings does **not** turn reasoning off, unless the
model also declares a `reasoningEfforts` map. Without it you are billed for
reasoning tokens on every request, with no error and no warning.

Measured: **5,502 reasoning tokens across a 16-exchange run** configured with
reasoning off. 46% of all output tokens, 9% of the run's cost.

---

## Why

Two conditions guard pi-ai's OpenRouter reasoning branch
(`@earendil-works/pi-ai/dist/api/openai-completions.js:598`):

```js
else if (compat.thinkingFormat === "openrouter" && model.reasoning) {
    // OpenRouter normalizes reasoning across providers via a nested object.
    const openRouterParams = params;
    if (options?.reasoningEffort) {
        openRouterParams.reasoning = {
            effort: model.thinkingLevelMap?.[options.reasoningEffort] ?? options.reasoningEffort,
        };
    }
    else if (model.thinkingLevelMap?.off !== null) {
        openRouterParams.reasoning = { effort: model.thinkingLevelMap?.off ?? "none" };
    }
}
```

Note that the "off" path lives *inside* this branch. If the branch does not
run, **no reasoning field is emitted at all** — not `off`, not `null`, nothing.
OpenRouter then applies the provider's own default, which for most reasoning
models is **on**.

`model.reasoning` is set by dsh's `resolveModelReasoning()`
(`dsh-llm-pi-ai/lib/index.js:1063`):

```js
function resolveModelReasoning(provider, entry, base) {
    const efforts = entry.reasoningEfforts;
    if (efforts === void 0) return { reasoning: base?.reasoning ?? false };
    if (efforts === false) return { reasoning: false };
    ...
    return { reasoning: true, thinkingLevelMap: map };
}
```

Line 1065 — the `efforts === void 0` case — is the trap. **No `reasoningEfforts` → `reasoning: false` → branch
never runs → nothing sent → provider default (on).**

The setting that looks like it disables reasoning is the very thing whose
absence enables it.

---

## The fix

```yaml
models:
  - id: vendor/model-name
    reasoningEfforts:
      "off": none
      low: low
      medium: medium
      high: high
    compat:
      thinkingFormat: openrouter
      supportsReasoningEffort: true
```

Both parts are required:

- **`reasoningEfforts`** makes `model.reasoning` true so the branch runs.
- **`thinkingFormat: openrouter`** selects the nested
  `{"reasoning":{"effort":…}}` object. Without it the request goes out with
  `reasoning: null` and the provider bills reasoning tokens anyway — measured
  at 76 tokens on a request configured with reasoning off.

Verified after the fix, across 93 consecutive requests in a real agentic run:
`{"reasoning":{"effort":"none"}}` on every request, **0 reasoning tokens
billed**.

---

## The YAML `off` trap

In YAML 1.1 — which is what most parsers apply — a bare `off` is the **boolean
`false`**, alongside `no`, `n`, `y`, `yes`, and `on`. Both places this matters:

```yaml
reasoning: "off"        # route level: unquoted -> false -> schema error
reasoningEfforts:
  "off": none           # map key:     unquoted -> the key becomes False
```

Unquoted at the route level, validation fails with a message about an invalid
thinking level that does not mention quoting. Unquoted as a map key, the key
becomes the boolean `False`, `efforts["off"]` is undefined, and the off level
silently goes missing.

`scripts/merge_settings.py` refuses to write a settings file where
`reasoningEfforts.off` parsed as boolean `false`.

---

## How the map is interpreted

`resolveModelReasoning()` translates your map into pi-ai's `thinkingLevelMap`,
pinning every undeclared level to `null` (unsupported). From the source comment:

> Pinning matters because pi-ai's own defaulting is asymmetric — an absent key
> means "supported" for the five base levels but "unsupported" for
> `xhigh`/`max` — and a profile author should not need to know that.

One special case, also from the source:

> A declared `off` with no value is the one exception: it stays absent from the
> map, which pi-ai reads as "supported, send nothing" — the correct dispatch
> where not thinking is the parameter's absence — while `off` with a value
> sends that value.

So:

| You write | Wire behaviour |
|---|---|
| `"off": none` | sends `{"reasoning":{"effort":"none"}}` |
| `"off":` (empty) | sends **nothing** — provider default applies |
| no `reasoningEfforts` at all | sends **nothing** — provider default applies |

For OpenRouter, **use `"off": none`**. An explicit instruction is the only way
to override a provider default you cannot see.

Two validation rules worth knowing, both from the same function:

- Only `off` may be valueless. Any other level with an empty value is rejected:
  *"reasoningEfforts.`<level>` needs the wire value dispatch should send"*.
- The map must offer at least one level beyond `off`. If you want a
  non-reasoning model, write `reasoningEfforts: false` instead.

---

## Should reasoning be off?

For **agentic and gate-driven work: yes.**

Measured on this model: reasoning on cost ~4× the output tokens and ~5× the
latency (483–681 tok / 8.3–12.1s versus 119–231 tok / 2.0–4.2s).

The architectural reason outranks the cost. Reasoning buys a
higher-variance **better first draft**. A verify-and-repair loop does not need
a better first draft; it needs a **verified final one**. Cheap fast turns buy
more repair iterations, and iteration count is the mechanism that actually
converges. Spending 5× the latency to improve a draft that will be checked and
revised anyway is paying for the wrong thing.

For **one-shot analytical questions with no verification loop**, turn it up.
That is exactly the case where a better first draft is the whole product.

Switch per-session in the Web UI, or per-route:

```yaml
reasoning: "off"      # or: low | medium | high
```

---

## Verifying

```bash
python3 scripts/verify-wire.py
```

```
[PASS] reasoning off -- 0 reasoning tokens billed
```

Any non-zero count means the field is not reaching the wire. Check, in order:

1. Does the model declare `reasoningEfforts`?
2. Is `off` quoted in both places?
3. Is `compat.thinkingFormat` set to `openrouter`?
4. Is the route's `api` set to `openai-completions`? The compat switches are
   rejected on any other api.
