"""Recording proxy between dsh and OpenRouter.

Streams SSE through unbuffered -- a proxy that accumulates the response before
forwarding would make the UI feel broken and would also destroy the
time-to-first-token measurement, which is the number that actually tells you
whether routing landed somewhere slow.

Writes one exchange-NNN.json per request holding the request body, the usage
block, the serving provider, and the timings.
"""
from __future__ import annotations
import http.server, json, pathlib, sys, threading, time, urllib.error, urllib.request

import os

if len(sys.argv) < 3:
    print(__doc__)
    print("Usage: OPENROUTER_API_KEY=sk-or-... watch-proxy.py <port> <state-dir>\n"
          "       (deprecated: an api-key may be passed as argv[3] — see below)",
          file=sys.stderr)
    raise SystemExit(2)

PORT = int(sys.argv[1])
STATE = pathlib.Path(sys.argv[2])

# Prefer the environment. A key passed as argv[3] is visible to EVERY process on
# the machine -- `ps aux`, `pgrep -af`, /proc/<pid>/cmdline -- for as long as
# this proxy runs, which is the whole session. It is retained only for backward
# compatibility; pass the key in the environment instead.
KEY = os.environ.get("OPENROUTER_API_KEY", "")
if not KEY and len(sys.argv) > 3:
    KEY = sys.argv[3]
    print("WARNING: the API key was passed on the command line, where every "
          "local process can read it (ps/pgrep//proc). Set OPENROUTER_API_KEY "
          "in the environment instead, and rotate this key if the machine is "
          "shared.", file=sys.stderr)
if not KEY:
    print("ERROR: no API key. Set OPENROUTER_API_KEY in the environment.",
          file=sys.stderr)
    raise SystemExit(2)

STATE.mkdir(parents=True, exist_ok=True)
UPSTREAM = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

# --- optional structural provider pin ----------------------------------------
# Off by default: this script's primary job is to OBSERVE. Set
# DSH_PIN_PROVIDER to turn it into an enforcer as well.
#
#     DSH_PIN_PROVIDER=DeepInfra python3 scripts/watch-proxy.py 8799 ./watch
#
# Why enforce here rather than in settings.yaml alone: if dsh's baseURL points
# at this proxy, every request passes through it, so it is the one place a pin
# cannot be bypassed by an `npm update` reverting the schema patch, a plugin
# building its own body, or an edited config file. The proxy does not TRUST the
# routing block it receives -- it overwrites it.
#
# `only` is the boundary, not `order`: measured over 299 exchanges,
# `order` + allow_fallbacks:true leaked 30% of traffic to other providers,
# including one not in the list. See docs/06-hard-pin-and-fallback-cost.md.
PIN_PROVIDER = os.environ.get("DSH_PIN_PROVIDER", "").strip()
PIN_QUANT = [q for q in os.environ.get("DSH_PIN_QUANT", "fp8").split(",") if q]
PIN = {
    "only": [PIN_PROVIDER],
    "order": [PIN_PROVIDER],
    "allow_fallbacks": False,
    **({"quantizations": PIN_QUANT} if PIN_QUANT else {}),
} if PIN_PROVIDER else None


def enforce_pin(req_json):
    """Overwrite the routing block in place. Returns (changed, previous)."""
    if PIN is None or not isinstance(req_json, dict):
        return False, None
    prev = req_json.get("provider")
    if prev == PIN:
        return False, None
    req_json["provider"] = dict(PIN)
    return True, prev

print(f"watch-proxy listening on http://127.0.0.1:{PORT}/v1 -> {UPSTREAM}\n"
      f"recording to {STATE}\n\n"
      f"Point dsh at it by setting, in ~/.dsh/settings.yaml:\n"
      f"    baseURL: http://127.0.0.1:{PORT}/v1\n",
      file=sys.stderr, flush=True)


_SEQ_LOCK = threading.Lock()
_SEQ = None


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        try:
            req_json = json.loads(body)
        except json.JSONDecodeError:
            req_json = {}

        # Rewrite BEFORE forwarding, not while recording -- otherwise the pin
        # applies only to the log and not to the request that leaves the host.
        pin_rewritten, pin_prev = enforce_pin(req_json)
        if pin_rewritten:
            body = json.dumps(req_json).encode()

        started = time.time()
        first_token = None
        chunks, provider, usage, finish = [], None, None, None

        upstream = urllib.request.Request(
            UPSTREAM + self.path.split("/v1", 1)[-1],
            data=body, method="POST",
            headers={"Authorization": f"Bearer {KEY}",
                     "Content-Type": "application/json",
                     "Accept": self.headers.get("accept", "text/event-stream")})
        try:
            resp = urllib.request.urlopen(upstream, timeout=300)
        except urllib.error.HTTPError as exc:
            detail = exc.read()
            self.send_response(exc.code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(detail)))
            self.end_headers()
            self.wfile.write(detail)
            self._record(req_json, started, None, [], None, None,
                         error={"code": exc.code, "body": detail.decode(errors="replace")[:2000]})
            return
        except Exception as exc:                                  # noqa: BLE001
            self.send_response(502); self.end_headers()
            self._record(req_json, started, None, [], None, None, error={"exception": repr(exc)})
            return

        self.send_response(200)
        ctype = resp.headers.get("content-type", "application/json")
        self.send_header("content-type", ctype)
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.end_headers()

        for line in resp:
            if first_token is None and line.startswith(b"data:") and b"[DONE]" not in line:
                first_token = time.time()
            try:
                self.wfile.write(line)
                self.wfile.flush()          # never buffer: the UI streams
            except (BrokenPipeError, ConnectionResetError):
                break
            if line.startswith(b"data:"):
                payload = line[5:].strip()
                if payload and payload != b"[DONE]":
                    try:
                        obj = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    provider = obj.get("provider") or provider
                    usage = obj.get("usage") or usage
                    for ch in obj.get("choices") or []:
                        finish = ch.get("finish_reason") or finish
                        delta = (ch.get("delta") or {}).get("content")
                        if delta:
                            chunks.append(delta)
            else:
                # Non-streaming reply: the whole body is one JSON object, so no
                # `data:` line ever appears. Without this branch `provider`
                # stays None -- the recording loses served_by AND the violation
                # check below silently passes, a blind spot in exactly the path
                # a `stream: false` client would take.
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    obj = None
                if isinstance(obj, dict):
                    provider = obj.get("provider") or provider
                    usage = obj.get("usage") or usage
                    for ch in obj.get("choices") or []:
                        finish = ch.get("finish_reason") or finish

        # LOUD FAIL: the pin is an assertion, not a hope. Substitution is the
        # exact failure it exists to prevent, so say so where a log will catch
        # it rather than letting it show up in a bill.
        violation = None
        if PIN is not None and provider and provider not in PIN["only"]:
            violation = provider
            print(f"[watch-proxy] PIN VIOLATION: pinned to {PIN['only']} "
                  f"but response was served by {provider!r}",
                  file=sys.stderr, flush=True)

        self._record(req_json, started, first_token, chunks, provider, usage,
                     finish=finish, pin_rewritten=pin_rewritten,
                     pin_prev=pin_prev, violation=violation)

    def _record(self, req, started, first_token, chunks, provider, usage,
                finish=None, error=None, pin_rewritten=False, pin_prev=None,
                violation=None):
        # This is a ThreadingHTTPServer, so two in-flight requests could
        # otherwise glob the same count and one would overwrite the other --
        # silently losing an exchange from a record whose whole purpose is to
        # be complete. Serialize the numbering.
        with _SEQ_LOCK:
            global _SEQ
            if _SEQ is None:
                _SEQ = len(list(STATE.glob("exchange-*.json")))
            _SEQ += 1
            n = _SEQ
        (STATE / f"exchange-{n:03d}.json").write_text(json.dumps({
            "n": n,
            "at": time.strftime("%H:%M:%S", time.localtime(started)),
            "request": {
                "model": req.get("model"),
                "provider_pref": req.get("provider"),
                "reasoning": req.get("reasoning"),
                "max_tokens": req.get("max_tokens"),
                "n_messages": len(req.get("messages") or []),
                "stream": req.get("stream"),
                "tools": len(req.get("tools") or []),
            },
            "served_by": provider,
            # Pin audit trail: what the client asked for, whether the proxy
            # had to overwrite it, and whether the answer honoured it.
            "pin": {
                "enforced": PIN is not None,
                "rewritten": pin_rewritten,
                "requested_by_client": pin_prev,
                "violation": violation,
            },
            "usage": usage,
            "finish_reason": finish,
            "timing": {
                "ttft_s": round(first_token - started, 3) if first_token else None,
                "total_s": round(time.time() - started, 3),
            },
            "reply_chars": sum(len(c) for c in chunks),
            "error": error,
        }, indent=2))


http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
