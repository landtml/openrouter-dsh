# openrouter-dsh

**Run any OpenRouter model on [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) — with the provider actually pinned, reasoning actually off, and proof on the wire.**

dsh can talk to OpenRouter out of the box. What it cannot do out of the box is
*control* OpenRouter: pin which provider serves you, and reliably turn reasoning
off. Both settings appear to work. Neither does. They fail **silently** — no
error, no warning, just a different provider at a different price, and a bill
for reasoning tokens you disabled.

This repo documents exactly why, fixes both with a 3-line patch, and gives you
tools that prove the fix on the wire instead of asking you to trust it.

```bash
git clone https://github.com/landtml/openrouter-dsh
cd openrouter-dsh
export OPENROUTER_API_KEY=sk-or-v1-...
./scripts/install.sh
```

That installs dsh if missing, merges the config, applies the patch, and makes a
real API call to verify the result. Re-runnable and idempotent.

The merge replaces only the `openrouter` provider: your other providers,
settings and top-level keys are left byte-for-byte alone, and `install.sh`
takes a timestamped backup first either way.

---

## The three silent failures

### 1. Provider pinning is discarded

OpenRouter serves most models from a dozen providers at different prices,
quantizations and latencies. Measured, all three documented ways to pin:

| Method | Served by | |
|---|---|---|
| request body `{"provider":{"order":["DeepInfra"]}}` | **DeepInfra** | works |
| header `X-OR-Provider-Order: DeepInfra` | whoever OpenRouter picks | ignored |
| model suffix `…:nitro` | whoever OpenRouter picks | ignored |

Only the body works — and dsh's settings schema strips the key that produces
it before it reaches the code that would send it. The upstream library dsh
vendors *already emits the field*; dsh simply never declared it as valid
config. Hence a three-line fix rather than a feature.

→ [`docs/03-provider-pinning.md`](docs/03-provider-pinning.md)

### 2. `reasoning: "off"` does not turn reasoning off

Unless the model also declares a `reasoningEfforts` map, `model.reasoning`
defaults to `false`, pi-ai's reasoning branch never runs, **no reasoning field
is sent at all**, and the provider applies its own default — which is *on*.

Measured: **5,502 reasoning tokens billed across one 16-exchange run**
configured with reasoning off. 46% of all output tokens; 9% of the run's cost.

After the fix, verified across 93 consecutive requests: **0 reasoning tokens.**

→ [`docs/04-reasoning.md`](docs/04-reasoning.md)

### 3. `order` does not pin — it only *prefers*

Even with the patch applied and `order: [DeepInfra, ...]` on the wire, a soft
pin routes elsewhere. Measured over 299 recorded exchanges from one agent
session:

| served by | share |
|---|---:|
| DeepInfra | 70% |
| GMICloud | **29%** |
| StreamLake | <1% — **not in the `order` list at all** |

`allow_fallbacks: true` makes `order` a preference, not a boundary. The cost is
not the price difference: KV caches are per-provider, so every switch lands
cold (98.0% -> 85.5% median cache, **+53% effective input price**), and
supported parameters differ, so a failover can fail the request outright rather
than degrade.

Use `only: [PROVIDER]` with `allow_fallbacks: false`.

→ [`docs/06-hard-pin-and-fallback-cost.md`](docs/06-hard-pin-and-fallback-cost.md)

---

## What's here

| Path | What it does |
|---|---|
| `scripts/install.sh` | one command: install → merge → patch → verify |
| `scripts/patch-openrouter.sh` | the 3-line schema patch, idempotent, with backup |
| `scripts/verify-wire.py` | makes a real call; proves pin, reasoning, caching |
| `scripts/probe-endpoints.py` | per-provider limits and prices; emits a config block |
| `scripts/watch-proxy.py` | records every request dsh makes, for a whole session |
| `scripts/watch-report.py` | flags silent fallbacks, stray reasoning tokens |
| `scripts/dsh-update.sh` | upgrade dsh **and re-apply the patch** (npm reverts it) |
| `config/deepseek-v4-flash.yaml` | the complete worked example, heavily commented |
| `docs/` | the full explanation, with source citations |

### Docs

- [01 — DeepSeek V4 Flash 0731, end to end](docs/01-deepseek-v4-flash.md) — the concrete example
- [02 — Attaching any OpenRouter model](docs/02-general-openrouter-model.md) — the general procedure
- [03 — Provider pinning and the patch](docs/03-provider-pinning.md)
- [04 — Reasoning](docs/04-reasoning.md)
- [05 — Troubleshooting](docs/05-troubleshooting.md)
- [06 — The hard pin, and what a fallback really costs](docs/06-hard-pin-and-fallback-cost.md) — why `order` is not a boundary
- [07 — Sampling parameters, measured](docs/07-parameters-measured.md) — including why `seed` does not give reproducibility

---

## Verifying, not trusting

Every failure this repo describes is invisible from the UI. So every claim here
is checkable, and the tools report measurements rather than assurances:

```console
$ python3 scripts/verify-wire.py
model:    deepseek/deepseek-v4-flash-0731
pin:      order=['DeepInfra', 'GMICloud', 'BaseTen'] allow_fallbacks=true quantizations=['fp8']
reasoning:{'effort': 'none'}

[INFO] request 1 -- 1.07s on DeepInfra (cold -- priming the cache)
[INFO] request 2 -- pinned hard to DeepInfra so the cache check is measuring cache, not routing
[PASS] provider pin -- served by DeepInfra (in pinned order [...])
[PASS] reasoning off -- 0 reasoning tokens billed
[PASS] prompt caching -- 256/377 tokens cached (67.9%) on the 2nd request
[PASS] clean completion -- finish_reason=stop
[PASS] model responded -- 'OK'

==========================================================
5/5 checks passed
```

For a whole session, `watch-proxy.py` sits between dsh and OpenRouter and
records every exchange. It is how both bugs above were found.

---

## Measured results

From a 93-exchange autonomous coding run (building a Next.js site) on this
configuration:

| | |
|---|---|
| Cost | **$0.0758** |
| Prompt tokens | 3,436,401 (**94.5% served from cache**) |
| Output tokens | 42,682 |
| Reasoning tokens | **0** |
| Transport errors | 0 |
| Requests with the pin on the wire | 93 / 93 |

At cold rates those prompt tokens would have cost roughly $0.27. Caching is the
dominant cost lever for agentic work, and it is worth configuring deliberately.

---

## Requirements

- Node.js 20+
- Python 3.8+ (stdlib only; PyYAML optional, used for extra validation)
- An [OpenRouter API key](https://openrouter.ai/keys)

Verified against **dsh 0.1.0-rc.7**. If a future dsh declares
`openRouterRouting` in its own schema, the patch becomes unnecessary — the
script detects this and tells you.

---

## Important: npm reverts the patch

`npm update -g @deepseek-ai/dsh` replaces the patched file, silently disabling
provider pinning again. Use the wrapper:

```bash
./scripts/dsh-update.sh
```

It backs up `~/.dsh`, upgrades, re-applies the patch, checks your settings still
parse, and re-verifies on the wire.

---

## License

MIT — see [LICENSE](LICENSE).

Not affiliated with DeepSeek or OpenRouter. "DeepSeek Harness" and "OpenRouter"
belong to their respective owners.
