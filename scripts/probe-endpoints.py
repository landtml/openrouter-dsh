#!/usr/bin/env python3
"""probe-endpoints.py -- list every provider serving a model, with ITS limits.

The numbers on OpenRouter's model page describe the BEST endpoint on the
board. If you pin a provider, those numbers may not apply to you, and a
maxTokens above your pinned provider's real ceiling produces a misleading
404 "No endpoints found".

This prints the per-provider table you actually need to fill in
`contextWindow`, `maxTokens`, and `quantizations`.

Usage:
    export OPENROUTER_API_KEY=sk-or-...
    python3 scripts/probe-endpoints.py deepseek/deepseek-v4-flash-0731
    python3 scripts/probe-endpoints.py <model> --quant fp8
    python3 scripts/probe-endpoints.py <model> --yaml
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def fetch(model, key):
    url = f"https://openrouter.ai/api/v1/models/{model}/endpoints"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()[:300]}", file=sys.stderr)
        if e.code == 404:
            print(f"\nNo such model id: {model!r}\n"
                  f"Check the exact id at https://openrouter.ai/models",
                  file=sys.stderr)
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("model", help="e.g. deepseek/deepseek-v4-flash-0731")
    ap.add_argument("--quant", default=None,
                    help="only show endpoints at this quantization (e.g. fp8)")
    ap.add_argument("--yaml", action="store_true",
                    help="emit a ready-to-paste settings.yaml model block")
    args = ap.parse_args()

    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        print("ERROR: OPENROUTER_API_KEY is not set.", file=sys.stderr)
        return 2

    data = fetch(args.model, key)
    if data is None:
        return 1

    d = data.get("data") or {}
    eps = d.get("endpoints") or []
    if not eps:
        print(f"No endpoints listed for {args.model}", file=sys.stderr)
        return 1

    if args.quant:
        eps = [e for e in eps if (e.get("quantization") or "").lower()
               == args.quant.lower()]
        if not eps:
            print(f"No {args.quant} endpoints for {args.model}", file=sys.stderr)
            return 1

    def price(e):
        try:
            return float((e.get("pricing") or {}).get("prompt") or 0)
        except (TypeError, ValueError):
            return 0.0

    def uptime(e):
        u = e.get("uptime_last_30m")
        return float(u) if isinstance(u, (int, float)) else 0.0

    def recommend_key(e):
        """Ranking for the --yaml suggestion.

        Price alone is the wrong objective: the cheapest endpoint on a popular
        model is routinely the least reliable one, and an endpoint that lacks
        `seed`/`structured_outputs` cannot do deterministic or schema-bound
        work at any price. Rank on reliability and capability first, and let
        price break the tie.
        """
        sp = set(e.get("supported_parameters") or [])
        return (
            0 if uptime(e) >= 98.0 else 1,                     # reliable first
            0 if {"seed", "structured_outputs"} <= sp else 1,   # then capable
            0 if e.get("max_completion_tokens") else 1,         # then advertised
            price(e),                                           # then cheap
        )

    eps.sort(key=price)          # the printed table stays price-sorted

    print(f"\nmodel: {args.model}")
    mc = d.get("context_length")
    print(f"model-level context: {mc if mc is not None else '(not reported)'} "
          f"-- the BEST endpoint's number, not necessarily yours\n")

    hdr = (f"{'provider':<22}{'ctx':>10}{'max_out':>10}{'quant':>8}"
           f"{'$/Mtok in':>12}{'$/Mtok out':>12}{'uptime':>9}")
    print(hdr)
    print("-" * len(hdr))
    for e in eps:
        p = e.get("pricing") or {}
        try:
            pin = float(p.get("prompt") or 0) * 1_000_000
            pout = float(p.get("completion") or 0) * 1_000_000
        except (TypeError, ValueError):
            pin = pout = 0.0
        up = e.get("uptime_last_30m")
        print(f"{(e.get('provider_name') or '?')[:21]:<22}"
              f"{e.get('context_length') or 0:>10}"
              f"{e.get('max_completion_tokens') or 0:>10}"
              f"{(e.get('quantization') or '?'):>8}"
              f"{pin:>12.4f}{pout:>12.4f}"
              f"{(f'{up:.1f}%' if isinstance(up, (int, float)) else '-'):>9}")

    # Flag capability differences that matter for structured work.
    print()
    for e in eps:
        sp = set(e.get("supported_parameters") or [])
        missing = [x for x in ("seed", "structured_outputs", "tools")
                   if x not in sp]
        if missing:
            print(f"  note: {e.get('provider_name')} lacks {', '.join(missing)}")
        if not e.get("max_completion_tokens"):
            print(f"  note: {e.get('provider_name')} does not advertise "
                  f"max_completion_tokens (shown as 0). Do not copy that 0 "
                  f"into maxTokens -- omit the field or use another "
                  f"provider's figure.")

    if args.yaml:
        ranked = sorted(eps, key=recommend_key)
        top = ranked[0]
        if top is not eps[0]:
            print(f"\n# NOTE: {top.get('provider_name')} is recommended over the "
                  f"cheapest ({eps[0].get('provider_name')}) on uptime and")
            print(f"#       supported parameters. Re-rank by price if that is "
                  f"what you want.")
        print("\n# --- paste into llm-pi-ai.providers.<route>.models ---")
        print(f"        - id: {args.model}")
        nm = str(d.get("name") or args.model)
        # Quote it: OpenRouter names routinely contain ": " which YAML reads
        # as a mapping. Escape any embedded double quotes first.
        print(f'          name: "{nm.replace(chr(92), chr(92)*2).replace(chr(34), chr(92) + chr(34))}"')
        print(f"          # {top.get('provider_name')}'s own limits")
        print(f"          contextWindow: {top.get('context_length')}")
        mt = top.get("max_completion_tokens")
        if mt:
            print(f"          maxTokens: {mt}")
        else:
            print(f"          # {top.get('provider_name')} does not advertise "
                  f"max_completion_tokens; omitting maxTokens lets the")
            print(f"          # provider decide. Setting it too high causes a "
                  f"404 'No endpoints found'.")
        print(f"          input: [ text ]")
        print(f"          reasoningEfforts:")
        print(f'            "off": none')
        print(f"            low: low")
        print(f"            medium: medium")
        print(f"            high: high")
        print(f"          compat:")
        print(f"            thinkingFormat: openrouter")
        print(f"            supportsReasoningEffort: true")
        print(f"            openRouterRouting:")
        order = [e.get("provider_name") for e in ranked[:3] if e.get("provider_name")]
        print(f"              order: [{', '.join(order)}]")
        print(f"              allow_fallbacks: true")
        q = (top.get("quantization") or "fp8").lower()
        print(f"              quantizations: [{q}]")

    return 0


if __name__ == "__main__":
    sys.exit(main())
