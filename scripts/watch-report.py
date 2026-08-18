#!/usr/bin/env python3
"""Report what actually happened on the wire during a watched dsh session.

Every line is read from a captured exchange. The checks at the end are the
things that fail SILENTLY -- a fallback that served the request without
complaint, reasoning quietly re-enabled, a cache that never warmed. Those cost
money or fidelity without ever showing up as an error in the UI.
"""
from __future__ import annotations
import json, pathlib, sys

DEFAULT_STATE = pathlib.Path.home() / ".local/share/dsh/watch"
STATE = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_STATE

files = sorted(STATE.glob("exchange-*.json"))
if not files:
    print(f"No exchanges captured in {STATE}.\n"
          f"Is watch-proxy.py running, is dsh's baseURL pointed at it, and "
          f"did you send a prompt?\n\n"
          f"Usage: watch-report.py [state-dir]")
    sys.exit(1)

rows = [json.loads(f.read_text()) for f in files]

# Distinct provider-pin orders across the session. More than one means the
# config changed mid-session, which makes cost and cache numbers incomparable.
orders_seen = {tuple(((r.get("request") or {}).get("provider_pref") or {})
                     .get("order") or [])
               for r in rows}
orders_seen.discard(())

# dsh fires side calls (session titling) that the client may abandon mid-stream.
# They carry no usage and no provider, and reporting them as rows implies a
# failure that did not happen.
skipped = [r for r in rows if not (r.get("usage") or r.get("served_by") or r.get("error"))]
rows = [r for r in rows if r not in skipped]
if not rows:
    print("No completed exchanges captured yet.")
    sys.exit(1)
note = f"  ({len(skipped)} incomplete side-call(s) hidden)" if skipped else ""
print(f"{len(rows)} exchange(s) captured{note}\n")

hdr = f"{'#':>2} {'time':8s} {'served by':13s} {'ttft':>6s} {'total':>7s} " \
      f"{'prompt':>7s} {'cached':>7s} {'hit%':>5s} {'out':>5s} {'cost $':>10s}"
print(hdr); print("-" * len(hdr))

tot_cost = tot_prompt = tot_cached = tot_out = 0.0
providers, errors, warns = set(), [], []

for r in rows:
    u = r.get("usage") or {}
    det = u.get("prompt_tokens_details") or {}
    p = int(u.get("prompt_tokens") or 0)
    c = int(det.get("cached_tokens") or 0)
    o = int(u.get("completion_tokens") or 0)
    cost = float(u.get("cost") or 0.0)
    served = r.get("served_by") or "-"
    providers.add(served)
    tot_cost += cost; tot_prompt += p; tot_cached += c; tot_out += o
    t = r.get("timing") or {}
    hit = f"{c / p * 100:.0f}" if p else "-"
    print(f"{r['n']:>2} {r['at']:8s} {served:13s} "
          f"{(str(t.get('ttft_s')) + 's') if t.get('ttft_s') else '-':>6s} "
          f"{str(t.get('total_s')) + 's':>7s} {p:>7d} {c:>7d} {hit:>5s} {o:>5d} {cost:>10.7f}")

    if r.get("error"):
        errors.append((r["n"], r["error"]))
    # The expected provider is whatever the REQUEST asked for first, not a
    # name hardcoded here -- this tool is used with many models.
    pref = ((r.get("request") or {}).get("provider_pref") or {})
    want = (pref.get("order") or [None])[0]
    if want and served not in (want, "-"):
        warns.append(f"exchange {r['n']}: served by {served}, not first choice "
                     f"{want} (fallback engaged; cache is per-provider, so a "
                     f"split lowers the hit rate)")
    req = r.get("request") or {}
    pref = req.get("provider_pref")
    if not pref:
        warns.append(f"exchange {r['n']}: NO provider pin in the request body "
                     f"-- the patch is not in effect")
    # NOTE: do NOT check the order against a hardcoded provider name. The
    # expected first choice is whatever THIS request asked for; a mismatch
    # between requests is the thing worth flagging, and that is handled by the
    # served-vs-first-choice check above.
    elif len(orders_seen) > 1:
        warns.append(f"exchange {r['n']}: pin order is {pref.get('order')}, "
                     f"which differs from other requests in this session")
    if pref and not pref.get("quantizations"):
        warns.append(f"exchange {r['n']}: no quantizations filter -- a cheaper, "
                     f"lower-precision endpoint (fp4) may serve this request")
    reasoning = req.get("reasoning")
    if reasoning and reasoning.get("effort") not in (None, "none", "off"):
        warns.append(f"exchange {r['n']}: reasoning is ON ({reasoning}) -- "
                     f"~4x tokens and ~5x latency vs measured baseline")
    # An absent `reasoning` field is correct when reasoning is off: pi-ai omits
    # it rather than sending an explicit off. Billed reasoning tokens are the
    # only proof it actually ran, so that check below is the load-bearing one.
    if (u.get("completion_tokens_details") or {}).get("reasoning_tokens"):
        warns.append(f"exchange {r['n']}: billed for "
                     f"{u['completion_tokens_details']['reasoning_tokens']} reasoning tokens")
    if r.get("finish_reason") == "length":
        warns.append(f"exchange {r['n']}: finish_reason=length -- the reply was TRUNCATED")

print("-" * len(hdr))
overall = f"{tot_cached / tot_prompt * 100:.1f}%" if tot_prompt else "n/a"
print(f"totals: prompt {int(tot_prompt)}  cached {int(tot_cached)} ({overall})  "
      f"out {int(tot_out)}  cost ${tot_cost:.6f}")
print(f"providers seen: {', '.join(sorted(providers))}")

print()
if errors:
    print("ERRORS")
    for n, e in errors:
        print(f"  exchange {n}: {json.dumps(e)[:300]}")
if warns:
    print("WARNINGS")
    for w in dict.fromkeys(warns):
        print(f"  ! {w}")
if not errors and not warns:
    print("CLEAN: every request carried its provider pin and quantization "
          "filter, reasoning was off, nothing truncated, no silent fallback.")

if len(rows) == 1 and tot_cached == 0:
    print("\nNote: turn 1 is always a cold cache miss on every provider "
          "(measured). Cache warms from turn 2-3; a single exchange cannot "
          "show a hit rate.")
