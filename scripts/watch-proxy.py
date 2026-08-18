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
    print("Usage: watch-proxy.py <port> <state-dir> [api-key]\n"
          "       api-key defaults to $OPENROUTER_API_KEY", file=sys.stderr)
    raise SystemExit(2)

PORT = int(sys.argv[1])
STATE = pathlib.Path(sys.argv[2])
KEY = sys.argv[3] if len(sys.argv) > 3 else os.environ.get("OPENROUTER_API_KEY", "")
if not KEY:
    print("ERROR: no API key. Pass one as argv[3] or set OPENROUTER_API_KEY.",
          file=sys.stderr)
    raise SystemExit(2)

STATE.mkdir(parents=True, exist_ok=True)
UPSTREAM = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

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
        self._record(req_json, started, first_token, chunks, provider, usage, finish=finish)

    def _record(self, req, started, first_token, chunks, provider, usage,
                finish=None, error=None):
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
