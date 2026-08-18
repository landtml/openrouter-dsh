# Sampling parameters on DeepInfra fp8: what actually moves

Measured 2026-08-18 against `deepseek/deepseek-v4-flash-0731`, DeepInfra fp8,
pinned. Short version: **the defaults are already right, and the one parameter
most people reach for does not do what its name promises.**

---

## The endpoint

From `/api/v1/models/deepseek/deepseek-v4-flash-0731/endpoints`:

```
tag                       deepinfra/fp8
quantization              fp8
context_length            1,048,576
max_completion_tokens     384,000
uptime_last_30m           99.19%
latency_last_30m          p50 779ms · p90 2913ms · p99 18114ms
throughput_last_30m       p50 29 tok/s
pricing                   in $0.08/M · out $0.18/M · cache_read $0.016/M
supports_implicit_caching false
```

Take `context_length` and `max_completion_tokens` from **the pinned endpoint**,
not from the model-level catalogue, which reports the best provider on the
board. With `only` + `allow_fallbacks:false`, a `max_tokens` above the pinned
provider's ceiling filters every endpoint away and OpenRouter answers
`404 No endpoints found`.

---

## Accuracy is saturated — no sampling knob moved it

Two tasks, DeepInfra pinned, reasoning off.

Tool-call argument accuracy (exact match on all three arguments, 5 trials):

| setting | exact args |
|---|---|
| defaults | 5/5 |
| `temperature=0` | 5/5 |
| `temperature=0` + `seed=42` | 5/5 |
| `temperature=0.2` | 5/5 |
| `top_p=0.95` | 5/5 |

A harder multi-constraint ordering task (8 trials): **8/8 correct at every
setting, including `temperature=1.0`.**

Temperature *is* honoured — on a creative prompt it grades cleanly (temp=0 ->
4/5 distinct, temp=1.5 -> 5/5 wildly varied). It simply does not change
correctness on tasks the model already handles.

---

## `seed` is accepted and does not give reproducibility

This is the one worth knowing, and the first measurement of it was **wrong**.

**A flawed first test.** Running `temperature=0 + seed=42` with a small
`max_tokens` while reasoning was enabled, the whole budget was consumed by
reasoning tokens and every reply came back an **empty string**. Six identical
empty strings look exactly like perfect determinism.

**The corrected method** fixed three things:

1. Dumped the exact JSON body to confirm `temperature` and `seed` sit at the
   top level (standard OpenAI shape).
2. Added `require_parameters: true` *to the probe only*, so OpenRouter itself
   drops providers lacking the parameters. DeepInfra still served — OpenRouter
   confirming it accepts both, rather than trusting a capability list.
3. Set `max_tokens: 200` with reasoning off, and asserted
   `reasoning_tokens == 0` on every call.

Result:

| config | distinct outputs |
|---|---|
| `temp=0` + `seed=42` | **6/8** |
| `temp=0`, no seed | **6/8** |

Identical. On a low-entropy prompt, `seed=42` and `seed=999` produced the *same*
dominant completion 3/5 times each — if the seed drove sampling, different
seeds would diverge systematically.

**Conclusion:** the parameter is accepted and enforced as "supported" by
OpenRouter, and the provider returns non-deterministic output anyway. This is
normal for batched GPU inference: request batching and kernel non-determinism
break bit-exactness even at `temperature=0`, and a seed cannot recover it.

Do not build replay, caching, or A/B logic that assumes identical output from
identical input here.

---

## Caching needs no help

Despite `supports_implicit_caching: false` in the endpoint metadata, plain
string content cached automatically:

```
call 1: prompt=21,009  cached=0       (0.0%)  $0.001681
call 2: prompt=21,009  cached=20,736 (98.7%)  $0.000354
call 3: prompt=21,009  cached=20,736 (98.7%)  $0.000354
```

No `cache_control` markers, no configuration. Adding cache directives would
risk disturbing the prefix for no measured gain.

---

## Parallel tool calls already work

The model emits multiple tool calls per turn by default (2/turn on a two-part
request). `parallel_tool_calls` is **not** in DeepInfra's supported-parameter
list, so sending it is a no-op.

---

## Penalties: supported, not recommended

`frequency_penalty` and `repetition_penalty` are supported and code still
parsed in a spot check. But penalising repeated tokens in code output is
actively risky — indentation, `self.`, and closing brackets are legitimately
repetitive. No upside was observed; there is no reason to carry the risk.

---

## Structured outputs: real, and provider-exclusive

`structured_outputs` works on DeepInfra and returns valid JSON under a strict
`json_schema`. It is worth enabling when you call a model **programmatically
and parse the reply**.

It is the wrong tool when the agent works through **tools and files**: tool
calls already carry enforced schemas (measured 5/5 exact-argument accuracy),
and constrained decoding masks tokens during generation, which is a constraint
on prose quality with no offsetting benefit.

Note the coupling cost. Among the providers tested, structured outputs is
DeepInfra-only — GMICloud and StreamLake both return HTTP 400. Depending on it
turns the pin from an optimisation into a hard requirement.

---

## Summary

| parameter | verdict |
|---|---|
| `temperature` | honoured; leave at default — no accuracy effect |
| `seed` | accepted, **does not** give reproducibility |
| `top_p` / `min_p` / `top_k` | no measured effect on agent tasks |
| `frequency_penalty` / `repetition_penalty` | risky for code, no upside |
| `parallel_tool_calls` | unsupported here; already works by default |
| cache directives | unnecessary; 98.7% automatic |
| `structured_outputs` | real, but couples you to one provider |
| `reasoning: {effort: none}` | **keep** — ~4x output tokens and ~5x latency when on |

The leverage was never in the sampling knobs. It was in the pin.
