# Web search over OpenRouter: the fourth silent failure

Measured 2026-08-19 against `deepseek/deepseek-v4-flash` through
`openrouter.ai/api/v1/messages`. Short version: **dsh's stock web search cannot
work over OpenRouter, and the error it raises names the wrong cause.**

This is the same shape as the three failures in the README — it looks
configured, and it is not — with one extra turn of the screw: the symptom is a
*credential* error, so the obvious fix (supply the missing key) does not fix it.

---

## The symptom

```
TError: DeepSeek search has no API key for "DEEPSEEK_API_KEY"; store it through
the credentials service (the web Models page writes it), export it in the
launching environment, or set a literal "apiKey" in the web-search-deepseek
config
```

Every remedy that message offers is a dead end on an OpenRouter-only setup.
Supplying a DeepSeek key clears this error and lands you on the next one.

---

## Cause 1: the response shape, not the credential

dsh ships one search provider, `@deepseek-ai/dsh-web-search-deepseek`. It calls
an Anthropic-compatible Messages API with the native `web_search_20250305`
server tool, and OpenRouter proxies that endpoint faithfully — **native search
genuinely runs**. The provider still fails, on one line:

```js
const resultBlocks = blocks.filter(b => b.type === "web_search_tool_result");
if (resultBlocks.length === 0) throw new WebError("...no web_search_tool_result blocks...");
```

Anthropic returns hits in `web_search_tool_result` blocks (`url`, `title`,
`page_age`) and the matching excerpts as `citations[]` on `text` blocks, joined
by url. OpenRouter returns **only the citation half** — but its citations carry
`url`, `title` *and* `cited_text`, which is every field a source needs.

Measured, HTTP 200, `max_uses: 2`, query `"What is the capital of Denmark? Search the web."`:

```
content block types        [thinking, server_tool_use, text, text]
web_search_tool_result     0
citations (url+title+text) 5
```

`server_tool_use` is present: the search ran. The data is all there. The stock
provider looks for the urls in the one container OpenRouter does not send, and
throws before reading the one it does.

### Why the error blames the credential

The provider's `available()` is:

```js
available() {
  const options = this.resolveOptions();
  return ((options.apiKey?.length ?? 0) > 0 || options.resolveApiKey !== void 0) && ...
}
```

`resolveApiKey` is always defined, so `available()` is unconditionally true —
the provider advertises itself as usable with no credential at all. The web
seam therefore selects it, and the failure surfaces at search time as the
credential error rather than as "no usable search provider". There is no
graceful fallback because nothing ever declines the job.

So the credential error is the *first* wall, not the real one. Behind it sits
the shape mismatch, which no key can fix.

---

## Cause 2: a bare query never triggers search

Independent of the above, and it will bite **any** OpenRouter-backed search
provider you write.

If you send the user's query as the raw message content, the model is free to
answer it from memory and never invoke the tool. Measured:

| Request form | `server_tool_use` | citations |
|---|---|---|
| `content: "What is the capital of Denmark?"` | absent | 0 |
| `content: [{type:"text", text:"Perform a web search for the query: …"}]` | present | 5 |
| `content: "latest Python release version"` | absent | 0 |
| `content: [{type:"text", text:"Perform a web search for the query: …"}]` | present | 15 |

The stock provider wraps for exactly this reason
(`dsh-web-search-deepseek/lib/index.js:114`). Any replacement must wrap too, or
it will throw "the model did not invoke native web search" on queries that are
perfectly valid — specifically the ones the model thinks it already knows,
which is the worst possible failure distribution.

---

## The fix

Disable the stock provider, register one that reads `citations[]`, and point
the web seam at it. No dsh source is patched.

In your profile's `cordis.patch.yml`:

```yaml
- id: web-search-deepseek
  disabled: true

- insert:
    - id: web-search-openrouter
      name: dsh-web-search-openrouter
      config:
        apiKeyEnv: OPENROUTER_API_KEY
        model: deepseek/deepseek-v4-flash
        maxUses: 5

- id: web
  config:
    searchProvider: openrouter
```

A working provider is in
[`providers/dsh-web-search-openrouter/`](../providers/dsh-web-search-openrouter/).
It reads citations first and falls back to `web_search_tool_result` blocks when
a backend does send them, so it works against both shapes.

**Patch layers are ordered, and later entries win.** If a subsequent block in
the same file re-enables `web-search-deepseek` or sets `searchProvider: null`,
it silently overrides the above and you are back to the credential error with
no indication which block won. This is easy to do while A/B testing a provider
and then forgetting which layer is live.

### Two implementation requirements

1. **`available()` is mandatory.** The `WebSearchProvider` interface requires
   it and `dsh-web/lib/index.js` calls it before every `search()`. A provider
   without one throws `provider.available is not a function` on every query.
   It must not make network calls, so it can only check that a key is reachable
   synchronously; a key held solely by the async credentials service is
   invisible there. Stay optimistic and let `search()` raise the clear error.

2. **Do not throw on zero results when search actually ran.** A search that ran
   and genuinely found nothing is a legitimate empty answer. Gate the throw on
   `server_tool_use` being absent, which is the real "search never happened"
   signal.

---

## Caveat: search bypasses the pin proxy

An OpenRouter search provider calls `openrouter.ai/api/v1/messages` **directly**
and does not go through `scripts/watch-proxy.py`. That is deliberate — the proxy
rewrites the routing block of chat-completions request bodies, and an
Anthropic-format `/messages` call is not that shape.

Consequences, both of which matter given this repo's premise:

- Search traffic is **not** provider-pinned. The DeepInfra pin does not apply to
  it, and the capability guarantees in
  [`06-hard-pin-and-fallback-cost.md`](06-hard-pin-and-fallback-cost.md) do not
  hold for search requests.
- Search cost and cache figures do **not** appear in `watch-report.py`. Your
  recorded spend understates actual spend by whatever search cost.

Each search is also a full model turn, not a cheap API call — it is billed as
inference plus OpenRouter's search fee.

---

## Verifying

```bash
curl -sS -X POST https://openrouter.ai/api/v1/messages \
  -H "authorization: Bearer $OPENROUTER_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{
    "model": "deepseek/deepseek-v4-flash",
    "max_tokens": 1024,
    "tools": [{"type":"web_search_20250305","name":"web_search","max_uses":5}],
    "messages": [{"role":"user","content":[{"type":"text",
      "text":"Perform a web search for the query: latest Python release version"}]}]
  }' | python3 -c "
import json,sys; d=json.load(sys.stdin)
print('blocks:', [b.get('type') for b in d.get('content',[])])
c=[x for b in d.get('content',[]) if b.get('type')=='text' for x in b.get('citations') or []]
print('citations:', len(c))
"
```

Expect `server_tool_use` in the block list and a non-zero citation count. If
`server_tool_use` is absent, the model declined to search — check that the
query is wrapped (Cause 2).
