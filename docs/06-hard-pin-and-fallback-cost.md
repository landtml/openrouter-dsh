# The hard pin: why `order` is not a boundary

Measured 2026-08-18 against `deepseek/deepseek-v4-flash-0731`, on a real
55-minute agent session (299 recorded exchanges) plus targeted probes. Every
number below comes from a captured request/response pair, not a price table.

---

## The failure: a soft pin routes 30% of traffic elsewhere

The natural way to pin a provider looks like this, and it is what
`docs/03-provider-pinning.md` originally shipped:

```yaml
openRouterRouting:
  order: [DeepInfra, GMICloud, BaseTen]
  allow_fallbacks: true
  quantizations: [fp8]
```

Across 299 exchanges sent with exactly that block:

| served by | n | share | cache hit |
|---|---:|---:|---:|
| DeepInfra | 209 | 70% | 92.1% |
| **GMICloud** | **87** | **29%** | 86.1% |
| BaseTen | 1 | <1% | 0.0% |
| **StreamLake** | **1** | <1% | 0.0% |

StreamLake is the finding. **It was never in the `order` list.** With
`allow_fallbacks: true`, `order` expresses a *preference* and OpenRouter may
serve any endpoint on the board. It is not a boundary.

A second probe made the mechanism explicit. Given an unsatisfiable
`max_tokens`, the two configurations behave in opposite ways:

| config | result |
|---|---|
| `order` + `allow_fallbacks: true` | silently served by **Parasail** (4.4x the cache-read rate) |
| `only` + `allow_fallbacks: false` | **error** — no silent substitution |

---

## Why a switch costs more than the price difference

Provider switching is usually discussed as a unit-price question. On an agent
loop it is mostly a **cache** question, and secondarily a **capability**
question.

### Cache

KV caches are per-provider. Every switch lands cold. Measured over the same 299
exchanges, restricted to requests above 5k prompt tokens:

| preceding request | n | cache hit (median) | effective input price |
|---|---:|---:|---:|
| same provider | 158 | **98.0%** | $0.0264/M |
| **after a switch** | 96 | **85.5%** | **$0.0405/M** |

**+53% on effective input price.** One observed transition dropped
92.0% -> 15.3% on a 51,120-token prompt: a single switch re-billed roughly 43k
tokens at the cold rate.

That session switched provider **101 times**.

### Capability — the part that breaks a task rather than costing more

Supported parameters differ per endpoint, and the differences are not visible
until a request fails:

| provider | seed | structured_outputs | logit_bias |
|---|---|---|---|
| DeepInfra | yes | **yes** | yes |
| GMICloud | yes | **NO** | NO |
| StreamLake | **NO** | **NO** | NO |

A `json_schema` `response_format` call, same prompt, three providers:

```
DeepInfra    VALID JSON   {"answer": 42}
GMICloud     HTTP 400     Backend request failed
StreamLake   HTTP 400     invalid_parameter_value
```

Reproducible across retries; the same call *without* the schema succeeds on all
three. So a mid-run failover does not degrade gracefully — it **fails the
request outright**, at whatever step the agent happened to be on.

---

## The fix: `only`, not `order`

```yaml
openRouterRouting:
  only: [DeepInfra]          # the boundary
  order: [DeepInfra]         # redundant, kept for readability
  allow_fallbacks: false
  quantizations: [fp8]
```

`only` restricts the candidate set to one endpoint, so there is nothing to fail
over *to*. Combined with `allow_fallbacks: false`, an unavailable DeepInfra
becomes a loud 404 rather than a quiet reroute at a different price,
quantization and cache warmth.

**Verified: 8/8 requests served by DeepInfra, 0 leaks.**

The trade is deliberate. A hard pin converts a *silent capability regression*
into an *immediate, visible failure*. For a gated harness the loud failure is
strictly better: it happens at request time instead of corrupting a step.

---

## The pin's one real cost, and how to remove it

A hard pin has no failover, so a provider hiccup surfaces instead of being
routed around. Measured over a 26-exchange session pinned to DeepInfra:

| | |
|---|---|
| exchanges | 26 |
| **HTTP 429** `engine_overloaded` | **5 (19%)** |
| pin violations | 0 |
| cache hit | 90.2% |
| task outcome | **completed** |

Every 429 recovered. `dsh-llm-retry` ships mounted and `RATE_LIMIT` is already
in `DEFAULT_RETRYABLE_CODES`, so dsh retried and the agent never saw them.

**The defaults are still too tight.** Consecutive-429 burst lengths were
`[2, 2, 1]` against `maxRetries: 2` — the budget was fully consumed twice. A
third consecutive failure would have surfaced as a hard error and killed the
step.

It is not a quota on your key. Probes reproducing the same shape returned
**0/40** on rapid small calls and **0/12** on large tool-heavy calls. It is
transient shared-pool capacity, consistent with DeepInfra's ~99% published
uptime.

dsh's defaults (`@deepseek-ai/dsh-llm`):

```
maxRetries 2 · initialDelayMs 500 · maxDelayMs 10000 · jitterRatio 0.1
```

Retries fired ~1s apart — often too fast for a capacity event to clear. Raise
the ceiling and slow the backoff, at **provider level** in settings:

```yaml
retryPolicy:
  mode: normal
  maxRetries: 5
  backoff:
    initialDelayMs: 1000
    maxDelayMs: 20000
    jitterRatio: 0.3
```

Verified against dsh's own `resolveRetryPolicy()`: the values land exactly, and
`retryableCodes` keeps `RATE_LIMIT`.

With five retries and exponential backoff, a burst must persist ~15-30s to
break through; the observed bursts lasted 1-2 seconds.

**This is why the fallback argument is a category error.** "One unretried 429
ends the run" solves a *retry* problem with a *routing* change — buying
availability by giving up cache locality and capability guarantees. Fix retries
directly and you keep all three.

`mode: always` gives unbounded retry. Avoid it unless a stalled step genuinely
beats a failed one: it hangs indefinitely when a provider is down rather than
briefly overloaded.

---

## Config alone is not structural

The YAML above is only as good as the layer that emits it. Three ways it can
silently stop being applied:

1. `npm update` replaces the patched `dsh-llm-pi-ai/lib/index.js`, and the
   compat schema drops `openRouterRouting` again (see doc 03).
2. A plugin builds its own request body.
3. Someone edits the settings file.

If every request already passes through a local proxy (this repo's
`scripts/watch-proxy.py` records cost and cache from that position), that proxy
is the one place a pin **cannot** be bypassed. It should not trust the routing
block it receives — it should overwrite it:

```python
PIN = {
    "only": ["DeepInfra"],
    "order": ["DeepInfra"],
    "allow_fallbacks": False,
    "quantizations": ["fp8"],
}

def enforce_pin(req_json):
    """Overwrite the routing block. Returns (changed, previous)."""
    if not PIN_ENFORCE or not isinstance(req_json, dict):
        return False, None
    prev = req_json.get("provider")
    if prev == PIN:
        return False, None
    req_json["provider"] = dict(PIN)
    return True, prev
```

Rewrite the body **before forwarding**, not while recording, or the pin applies
only to the log and not to the request that leaves the machine.

Verified adversarially: a request explicitly demanding `Mancer 2` (7.1x cost,
no caching) was served by DeepInfra.

### Assert the result, do not assume it

The pin is an assertion. Check the response too:

```python
if PIN_ENFORCE and provider and provider not in PIN["only"]:
    print(f"[proxy] PIN VIOLATION: pinned to {PIN['only']} "
          f"but response was served by {provider!r}", file=sys.stderr, flush=True)
```

**Watch the non-streaming path.** A proxy that only parses `data:` SSE lines
never sees `provider` on a `stream: false` reply, so the violation check
silently passes — a blind spot in exactly the path a plugin using
non-streaming requests would take. Parse both.

### Keep the two copies from drifting

The pin now exists twice: in settings (what dsh sends) and in the proxy (what
is enforced). If they drift, the proxy quietly rewrites every request and the
settings file becomes a lie about what is on the wire. Compare them in the
verifier and fail loudly.

---

## Rejected: `require_parameters: true`

OpenRouter's `require_parameters` drops any endpoint that does not support
every parameter in the request. It looks like a natural companion to the pin:
if DeepInfra ever stops advertising `structured_outputs` or `tools`, the
request fails loudly instead of running with the parameter ignored.

**It was tested and reverted.** In isolation every probe passed — with tools,
without tools, with and without `quantizations`. Under real dsh traffic (25
tool definitions, streaming) it returned:

```
404 No endpoints found that can handle the requested parameters.
```

The exact interaction was not isolated. What matters is the shape of the
result: a flag that passes every synthetic probe and fails real traffic is not
a safety feature, it is an outage with good intentions. It is documented here
so the next person does not rediscover it at runtime.

If you want the guarantee, assert the capability out-of-band against
`/api/v1/models/<id>/endpoints` rather than making every request depend on it.
