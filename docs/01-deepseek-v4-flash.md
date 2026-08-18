# DeepSeek V4 Flash 0731 on dsh, end to end

The complete worked example. Every number here was measured, not quoted from a
price table, and the tools to re-derive them are in this repo.

**Model:** `deepseek/deepseek-v4-flash-0731` (July 2026)
**Verified against:** dsh 0.1.0-rc.7

---

## Quick path

```bash
export OPENROUTER_API_KEY=sk-or-v1-...
./scripts/install.sh
```

Then:

```bash
dsh --profile web --host 127.0.0.1 --port 3080     # Web UI
dsh --profile headless "your prompt"               # one-shot
```

The rest of this document explains what that did and why.

---

## Step 1 — Choosing the provider

Thirteen providers serve this model at fp8. They are not interchangeable:

```console
$ python3 scripts/probe-endpoints.py deepseek/deepseek-v4-flash-0731 --quant fp8

provider                     ctx   max_out   quant   $/Mtok in  $/Mtok out   uptime
-----------------------------------------------------------------------------------
StreamLake               1024000    384000     fp8      0.0786      0.1572    94.4%
DeepInfra                1048576    384000     fp8      0.0800      0.1800    99.0%
GMICloud                 1048575         0     fp8      0.0840      0.1680    99.7%
BaseTen                  1048576   1048576     fp8      0.1300      0.2600    98.9%
CoreWeave                 262144    262144     fp8      0.1300      0.2800    99.6%
AkashML                   131072    131072     fp8      0.1400      0.2800    92.5%
Novita                   1048576    393216     fp8      0.1400      0.2800    99.3%
Parasail                 1048576   1048576     fp8      0.1400      0.2800    96.9%
SiliconFlow              1048576    393216     fp8      0.1400      0.2800    99.0%
Baidu                    1048576    131072     fp8      0.1400      0.2800    99.9%
Mancer 2                 1048576   1048576     fp8      0.1400      0.4500    96.4%
Io Net                    262100     65536     fp8      0.1490      0.3200    97.9%
DeepSeek                 1048576    384000     fp8      0.2200      0.6600   100.0%

  note: StreamLake lacks seed, structured_outputs
  note: GMICloud lacks structured_outputs
  note: BaseTen lacks seed, structured_outputs
```

Three things this table tells you that the model page does not:

1. **Price spans 2.8×** for identical quantization — $0.0786 to $0.2200 per
   Mtok in. Going through the model's own first-party endpoint (`DeepSeek`,
   bottom row) is the most expensive option here.
2. **Context and output ceilings differ per provider.** `AkashML` offers 131k
   context where `DeepInfra` offers 1048k. A config written for one is wrong
   for the other.
3. **Capability differs.** `StreamLake` is cheapest but lacks `seed` and
   `structured_outputs` — it cannot do deterministic or schema-bound work at
   any price.

**DeepInfra is the pick**: second-cheapest, 99.0% uptime, full parameter
support, and the largest context on offer. `probe-endpoints.py --yaml`
independently arrives at the same conclusion by ranking reliability and
capability above price.

### The numbers that go in the config

```yaml
contextWindow: 1048576   # DeepInfra's context_length
maxTokens: 384000        # DeepInfra's max_completion_tokens
```

**Not** the model-level catalogue figures (1310720 / 393216), which describe
the best endpoint on the board. With a provider pin, a `maxTokens` above your
pinned provider's ceiling filters every endpoint away and OpenRouter answers
`404 "No endpoints found for deepseek/deepseek-v4-flash-0731"` — which reads
as though the model is gone. Measured.

> **Pin the provider first, then take that provider's numbers.**

---

## Step 2 — The configuration

The full annotated file is [`config/deepseek-v4-flash.yaml`](../config/deepseek-v4-flash.yaml).
`install.sh` merges it into `~/.dsh/settings.yaml` without disturbing your
other settings. Condensed:

```yaml
llm-pi-ai:
  providers:
    openrouter:
      displayName: OpenRouter (DeepSeek V4 Flash)
      api: openai-completions          # required for the compat switches
      baseURL: https://openrouter.ai/api/v1
      apiKeyEnv: OPENROUTER_API_KEY
      reasoning: "off"                 # QUOTED -- bare off is YAML boolean false
      timeoutMs: 180000
      streamIdleTimeoutMs: 120000
      transport: sse
      cacheRetention: long             # the dominant cost lever
      defaultInput: [ text ]
      models:
        - id: deepseek/deepseek-v4-flash-0731
          name: DeepSeek V4 Flash 0731
          contextWindow: 1048576       # DeepInfra's own limits
          maxTokens: 384000
          input: [ text ]
          reasoningEfforts:            # REQUIRED or reasoning stays ON
            "off": none                # QUOTED
            low: low
            medium: medium
            high: high
          compat:
            thinkingFormat: openrouter
            supportsReasoningEffort: true
            openRouterRouting:         # REQUIRES THE PATCH
              order: [DeepInfra, GMICloud, BaseTen]
              allow_fallbacks: true
              quantizations: [fp8]

agent-default-model:
  provider: openrouter
  model: deepseek/deepseek-v4-flash-0731
```

### Why each non-obvious value

| Setting | Why |
|---|---|
| `reasoning: "off"` | measured ~4× output tokens and ~5× latency when on; see below |
| `reasoningEfforts` | without it `model.reasoning` is false and reasoning silently stays **on** |
| `"off"` quoted (×2) | YAML 1.1 parses bare `off` as boolean `false` |
| `thinkingFormat: openrouter` | selects the nested reasoning object; without it, `reasoning: null` on the wire and 76 reasoning tokens billed anyway |
| `cacheRetention: long` | 94.5% of 3.4M prompt tokens served from cache in a real run |
| `allow_fallbacks: true` | one unretried 429 ends a 93-exchange run; see below |
| `quantizations: [fp8]` | excludes cheaper, worse fp4 endpoints |
| `timeoutMs: 180000` | a 15.8s stall was measured on a warm path; a 75.8s generation on a large file write |
| `agent-default-model` | without it, headless falls back to `deepseek-official`, which needs a key you do not have |

### On `allow_fallbacks: true`

The gate-harness config for the same model uses `false`. That difference is
deliberate.

- **`false`** for benchmarking and cost receipts: a silent failover changes
  quantization, cache behaviour and price mid-run, making the numbers
  meaningless.
- **`true`** for interactive and agentic use: a transient 429 reroutes instead
  of killing a long run.

Measured over one 93-exchange run with fallbacks on, 32% of requests were
served by GMICloud rather than DeepInfra. Those requests were **not worse**:

| | DeepInfra (63) | GMICloud (30) |
|---|---|---|
| TTFT median | 1.43s | 3.57s |
| **Total** median | 10.10s | **6.01s** |
| Cache hit | 94.4% | **94.6%** |
| $/1k output | $0.00181 | **$0.00171** |

Slower to first token, faster to finish, marginally cheaper, same fp8
quantization. The fallback chain is doing its job.

### On reasoning off

Measured on this model: reasoning on cost ~4× the output tokens and ~5× the
latency (483–681 tok / 8.3–12.1s versus 119–231 tok / 2.0–4.2s).

The architectural reason outranks the cost. Reasoning buys a higher-variance
**better first draft**. An agentic loop that verifies and repairs does not need
a better first draft — it needs a **verified final one**. Cheap fast turns buy
more repair iterations, and iteration count is what actually converges.

Turn it up (`low`/`medium`/`high`) for one-shot analytical questions with no
verification loop. That is the case where a better first draft *is* the product.

---

## Step 3 — The patch

Without it, the entire `openRouterRouting` block above is silently stripped by
dsh's settings schema and your requests route wherever OpenRouter prefers.

```bash
./scripts/patch-openrouter.sh
```

Three idempotent edits with a `.orig` backup. Full explanation and porting
notes in [`03-provider-pinning.md`](03-provider-pinning.md).

**Re-run after every `npm update -g @deepseek-ai/dsh`**, or use
`./scripts/dsh-update.sh`, which does both.

---

## Step 4 — Verify

```console
$ python3 scripts/verify-wire.py
[PASS] provider pin -- served by DeepInfra (in pinned order [...])
[PASS] reasoning off -- 0 reasoning tokens billed
[PASS] prompt caching -- 256/377 tokens cached (67.9%) on the 2nd request
[PASS] clean completion -- finish_reason=stop
[PASS] model responded -- 'OK'
5/5 checks passed
```

To watch a whole real session:

```bash
# terminal 1
python3 scripts/watch-proxy.py 8799 ~/.local/share/dsh/watch

# then point baseURL at http://127.0.0.1:8799/v1 and use dsh normally

# terminal 2, afterwards
python3 scripts/watch-report.py ~/.local/share/dsh/watch
```

---

## Using it in the Web UI

```bash
dsh --profile web --host 127.0.0.1 --port 3080
```

"DeepSeek V4 Flash 0731" appears under "OpenRouter (DeepSeek V4 Flash)" in the
model picker. Selecting it there rewrites the `agent-default-model` block.

---

## What it costs in practice

A 93-exchange autonomous run that built a complete Next.js site — scaffolding,
17 source files, headless-Chrome verification at three breakpoints, a
production build, and a running dev server:

| | |
|---|---|
| Wall clock | ~19 minutes |
| Exchanges | 93 |
| **Cost** | **$0.0758** |
| Prompt tokens | 3,436,401 (**94.5% cached**) |
| Output tokens | 42,682 |
| Reasoning tokens | 0 |
| Errors | 0 |

At cold rates those prompt tokens alone would have cost roughly $0.27. The
94.5% cache hit is not incidental — it is `cacheRetention: long` plus a stable
prompt prefix doing the work.

Cache warms fast: measured 0% → 74% → 86% → 98% across the first four
exchanges, then steady in the mid-90s.

---

## Troubleshooting

See [`05-troubleshooting.md`](05-troubleshooting.md). The three most common:

| Symptom | Cause |
|---|---|
| `404 No endpoints found` | `maxTokens` above the pinned provider's ceiling |
| reasoning tokens billed with reasoning off | missing `reasoningEfforts`, or unquoted `off` |
| requests served by the wrong provider | patch not applied, or reverted by `npm update` |
