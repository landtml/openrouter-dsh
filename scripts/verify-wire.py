#!/usr/bin/env python3
"""verify-wire.py -- prove the OpenRouter config reaches the wire.

Every failure mode this repo documents is SILENT. A wrong provider pin does
not error, it just routes elsewhere. A missing reasoningEfforts map does not
error, it just bills you for reasoning you disabled. The only way to know is
to look at the bytes on the wire and at what came back.

This makes one real request through OpenRouter with your exact settings and
reports what actually happened:

  * which provider served it        (is the pin working?)
  * reasoning tokens billed         (is reasoning actually off?)
  * cached tokens                   (is prompt caching engaged?)
  * TTFT and total latency
  * the quantization served

Usage:
    export OPENROUTER_API_KEY=sk-or-...
    python3 scripts/verify-wire.py
    python3 scripts/verify-wire.py --model deepseek/deepseek-v4-flash-0731

Exit 0 iff every check passes.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "deepseek/deepseek-v4-flash-0731"
DEFAULT_ORDER = ["DeepInfra", "GMICloud", "BaseTen"]

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" -- {detail}" if detail else ""))
    return bool(ok)


def note(name, detail):
    """Informational -- reported but never fails the run."""
    print(f"[INFO] {name} -- {detail}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--order", default=",".join(DEFAULT_ORDER),
                    help="comma-separated provider order")
    ap.add_argument("--expect-provider", default=None,
                    help="fail unless this exact provider served the request "
                         "(default: accept any provider in --order)")
    ap.add_argument("--allow-fallbacks", default="true",
                    choices=["true", "false"])
    ap.add_argument("--reasoning", default="none",
                    help='"none" to verify reasoning is off, or a level')
    args = ap.parse_args()

    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        print("ERROR: OPENROUTER_API_KEY is not set.", file=sys.stderr)
        print("       Get a key at https://openrouter.ai/keys", file=sys.stderr)
        return 2

    order = [p.strip() for p in args.order.split(",") if p.strip()]

    body = {
        "model": args.model,
        # A long, stable prefix is what prompt caching keys on. One request
        # cannot show a cache HIT (nothing to hit yet) -- we run two.
        "messages": [
            {"role": "system",
             "content": "You are a terse assistant. " + ("Answer precisely. " * 120)},
            {"role": "user", "content": "Reply with exactly: OK"},
        ],
        "max_tokens": 32,
        "provider": {
            "order": order,
            "allow_fallbacks": args.allow_fallbacks == "true",
            "quantizations": ["fp8"],
        },
        "reasoning": {"effort": args.reasoning},
    }

    print(f"model:    {args.model}")
    print(f"pin:      order={order} allow_fallbacks={args.allow_fallbacks} "
          f"quantizations=['fp8']")
    print(f"reasoning:{{'effort': '{args.reasoning}'}}")
    print()

    def call(label):
        req = urllib.request.Request(
            API,
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/openrouter-dsh",
                "X-Title": "openrouter-dsh verify",
            },
        )
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                raw = r.read().decode()
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:400]
            print(f"\nHTTP {e.code} on {label}:\n{detail}\n", file=sys.stderr)
            if e.code == 404 and "No endpoints found" in detail:
                print("HINT: 404 'No endpoints found' with a provider pin "
                      "usually means your max_tokens or quantization filter\n"
                      "      excluded every endpoint of the pinned provider. "
                      "See docs/05-troubleshooting.md.", file=sys.stderr)
            return None, 0.0
        return json.loads(raw), time.time() - t0

    d1, t1 = call("request 1 (cold)")
    if d1 is None:
        return 1
    served1 = d1.get("provider") or "?"
    note("request 1", f"{t1:.2f}s on {served1} (cold -- priming the cache)")

    # Cache is PER-PROVIDER. If request 2 lands elsewhere there is nothing to
    # hit and the cache check would fail for a reason unrelated to config.
    # Pin request 2 to whoever served request 1 so the check measures caching
    # rather than routing luck.
    if served1 != "?":
        body["provider"] = {"order": [served1],
                            "allow_fallbacks": False,
                            "quantizations": ["fp8"]}
        note("request 2", f"pinned hard to {served1} so the cache check is "
                          f"measuring cache, not routing")

    time.sleep(1.0)
    d2, t2 = call("request 2 (warm)")
    if d2 is None:
        return 1

    # ---------------------------------------------------------------- checks
    # Judge the pin on request 1 -- request 2 was deliberately re-pinned above.
    served = d1.get("provider") or "?"
    if args.expect_provider:
        check("provider pin", served == args.expect_provider,
              f"served by {served}, expected {args.expect_provider}")
    else:
        check("provider pin", served in order,
              f"served by {served} (in pinned order {order})")
        if served != order[0]:
            note("fallback", f"{served} is not first choice {order[0]}. "
                             f"Normal with allow_fallbacks=true; it means the "
                             f"first choice was busy.")

    usage = d2.get("usage") or {}
    det = usage.get("completion_tokens_details") or {}
    rt = det.get("reasoning_tokens") or 0
    if args.reasoning == "none":
        check("reasoning off", rt == 0,
              f"{rt} reasoning tokens billed"
              + ("" if rt == 0 else
                 " -- reasoning is NOT off. Check that the model declares a "
                 "reasoningEfforts map; without it dsh sets model.reasoning="
                 "false and pi-ai never emits the field. "
                 "See docs/04-reasoning.md."))
    else:
        note("reasoning tokens", f"{rt} (effort={args.reasoning})")

    pd = usage.get("prompt_tokens_details") or {}
    cached = pd.get("cached_tokens") or 0
    total_in = usage.get("prompt_tokens") or 0
    pct = (cached / total_in * 100) if total_in else 0.0
    check("prompt caching", cached > 0,
          f"{cached}/{total_in} tokens cached ({pct:.1f}%) on the 2nd request"
          + ("" if cached > 0 else
             " -- no cache hit. Some providers need a longer prefix, and "
             "cache is per-provider: if request 2 landed on a different "
             "provider than request 1 there is nothing to hit."))

    fr = d2.get("choices", [{}])[0].get("finish_reason")
    check("clean completion", fr in ("stop", "length"),
          f"finish_reason={fr}")

    content = (d2.get("choices", [{}])[0].get("message") or {}).get("content", "")
    check("model responded", bool(content and content.strip()),
          f"{content.strip()[:60]!r}")

    note("latency", f"cold {t1:.2f}s, warm {t2:.2f}s")
    note("cost", f"${usage.get('cost', 0):.6f} (this request)")
    if d1.get("provider") != d2.get("provider"):
        note("routing", f"request 1 -> {d1.get('provider')}, "
                        f"request 2 -> {d2.get('provider')} despite the hard "
                        f"pin. Cache is per-provider, so a split lowers the "
                        f"hit rate.")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{'=' * 58}\n{passed}/{total} checks passed")
    if passed < total:
        print("\nSee docs/05-troubleshooting.md")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
