/**
 * dsh-web-search-openrouter — native web search through OpenRouter.
 *
 * dsh ships one search provider, `@deepseek-ai/dsh-web-search-deepseek`. It
 * calls an Anthropic-compatible Messages API with the native
 * `web_search_20250305` tool, and OpenRouter proxies that endpoint faithfully —
 * the search runs, and the results come back. The stock provider still fails
 * against it, for two reasons in sequence.
 *
 * FIRST on the credential, because its apiKeyEnv defaults to DEEPSEEK_API_KEY:
 *
 *     TError: DeepSeek search has no API key for "DEEPSEEK_API_KEY"
 *
 * That message names the wrong cause, and every remedy it suggests is a dead
 * end on an OpenRouter-only setup. Supplying a DeepSeek key clears it and lands
 * you on the real wall:
 *
 *     const resultBlocks = blocks.filter(b => b.type === "web_search_tool_result");
 *     if (resultBlocks.length === 0) throw new WebError("...no web_search_tool_result blocks...");
 *
 * Anthropic returns search hits in `web_search_tool_result` blocks (url, title,
 * page_age) and the matching excerpts as `citations[]` on `text` blocks, joined
 * by url. OpenRouter returns ONLY the citation half — but its citations carry
 * `url`, `title` AND `cited_text`, which is every field a source needs.
 *
 * So the data is all present; the stock provider just looks for the urls in the
 * one container OpenRouter does not send. This provider reads the citations
 * directly and falls back to result blocks when a backend does send them, so it
 * works against both shapes.
 *
 * Verified against OpenRouter with deepseek/deepseek-v4-flash, 2026-08-19:
 * blocks [thinking, server_tool_use, text, text], 0 web_search_tool_result,
 * 5 citations each with url + title + cited_text.
 *
 * See docs/08-web-search.md for the full measurements, including why search
 * bypasses the pin proxy and is therefore neither provider-pinned nor recorded
 * by watch-report.py.
 *
 * ## Configuration
 *
 *   - id: web-search-openrouter
 *     name: dsh-web-search-openrouter
 *     config:
 *       apiKeyEnv: OPENROUTER_API_KEY
 *       model: deepseek/deepseek-v4-flash
 *
 * And point the web seam at it:
 *
 *   - id: web
 *     config:
 *       searchProvider: openrouter
 */

const PROVIDER_ID = "openrouter";
const DEFAULT_BASE_URL = "https://openrouter.ai/api/v1";
const DEFAULT_MODEL = "deepseek/deepseek-v4-flash";
const DEFAULT_API_VERSION = "2023-06-01";
const DEFAULT_MAX_TOKENS = 4096;
const DEFAULT_MAX_USES = 5;

export const name = "web-search-openrouter";
export const inject = ["web"];

/**
 * Collect sources from a Messages response, from either container.
 *
 * Two shapes are accepted on purpose:
 *   * `citations[]` on text blocks — what OpenRouter sends; carries the excerpt.
 *   * `web_search_tool_result` blocks — what Anthropic/DeepSeek send; carries
 *     url/title/page_age, with the excerpt joined from the citation by url.
 *
 * Deduped by url, first occurrence winning, because a `max_uses > 1` request
 * can surface the same page across several searches.
 */
export function collectSources(response) {
  const blocks = response?.content ?? [];
  const seen = new Map();

  for (const block of blocks) {
    if (block?.type !== "text") continue;
    for (const cite of block.citations ?? []) {
      const url = cite?.url;
      if (!url || seen.has(url)) continue;
      seen.set(url, {
        url,
        ...(cite.title ? { title: cite.title } : {}),
        ...(cite.cited_text ? { snippet: cite.cited_text } : {}),
      });
    }
  }

  // A backend that DOES send result blocks: fill in anything the citations
  // missed, and enrich existing entries with title/page_age.
  for (const block of blocks) {
    if (block?.type !== "web_search_tool_result") continue;
    for (const item of block.content ?? []) {
      if (item?.type !== "web_search_result" || !item.url) continue;
      const existing = seen.get(item.url);
      if (existing) {
        if (!existing.title && item.title) existing.title = item.title;
        if (item.page_age) existing.publishedAt = item.page_age;
        continue;
      }
      seen.set(item.url, {
        url: item.url,
        ...(item.title ? { title: item.title } : {}),
        ...(item.page_age ? { publishedAt: item.page_age } : {}),
      });
    }
  }

  return [...seen.values()];
}

/** Whether the model actually invoked native search, regardless of result shape. */
export function searchWasAttempted(response) {
  return (response?.content ?? []).some((b) => b?.type === "server_tool_use");
}

class OpenRouterSearchProvider {
  id = PROVIDER_ID;

  constructor(resolveOptions) {
    this.resolveOptions = resolveOptions;
  }

  /**
   * Cheap local usability check. Required by the WebSearchProvider interface
   * (dsh-web/lib/types/types.d.ts:100) and called by dsh-web/lib/index.js:124
   * BEFORE search() -- a provider without it throws
   * "provider.available is not a function" at every search, which is exactly
   * how this shipped broken the first time.
   *
   * Must not make network calls, so this only checks that a key is reachable
   * synchronously. A key held only by the async credentials service cannot be
   * seen here, so we stay optimistic and let search() raise the clear error.
   */
  available() {
    const o = this.resolveOptions();
    if (o.apiKey) return true;
    if (process.env[o.apiKeyEnv]) return true;
    return o.hasCredentialsService;
  }

  async search(request, signal) {
    const o = this.resolveOptions();
    const apiKey = await o.resolveApiKey();
    if (!apiKey) {
      throw new Error(
        `web-search-openrouter: no API key for "${o.apiKeyEnv}". Export it, or store it through the credentials service.`
      );
    }

    const res = await fetch(`${o.baseURL}/messages`, {
      method: "POST",
      signal,
      headers: {
        "content-type": "application/json",
        authorization: `Bearer ${apiKey}`,
        "anthropic-version": o.apiVersion,
      },
      body: JSON.stringify({
        model: o.model,
        max_tokens: o.maxTokens,
        tools: [
          { type: "web_search_20250305", name: "web_search", max_uses: o.maxUses },
        ],
        // The query is WRAPPED, not sent bare. A bare question the model can
        // answer from memory ("What is the capital of Denmark?") comes back as
        // [thinking, text] with no server_tool_use at all -- native search
        // never runs, and searchWasAttempted() below then throws on a query
        // that was perfectly valid. Measured 2026-08-19: bare -> 0 citations,
        // wrapped -> 5. The stock provider wraps for the same reason
        // (dsh-web-search-deepseek/lib/index.js:114).
        messages: [
          {
            role: "user",
            content: [
              { type: "text", text: `Perform a web search for the query: ${request.query}` },
            ],
          },
        ],
      }),
    });

    if (!res.ok) {
      const body = await res.text().catch(() => "");
      throw new Error(
        `web-search-openrouter: HTTP ${res.status} from ${o.baseURL}${body ? ` — ${body.slice(0, 300)}` : ""}`
      );
    }

    const response = await res.json();
    const sources = collectSources(response);

    // An empty result is only an error when search never ran. A search that ran
    // and genuinely found nothing is a legitimate empty answer, and throwing on
    // it would turn "no hits" into a broken tool.
    if (sources.length === 0 && !searchWasAttempted(response)) {
      throw new Error(
        "web-search-openrouter: the model did not invoke native web search for this query"
      );
    }

    return { sources, truncated: false };
  }
}

function resolveOptions(ctx, config) {
  const apiKeyEnv = config.apiKeyEnv ?? "OPENROUTER_API_KEY";
  return {
    apiKeyEnv,
    // Exposed synchronously so available() can answer without awaiting.
    apiKey: config.apiKey,
    hasCredentialsService: ctx.get?.("credentials") !== undefined,
    baseURL: config.baseURL ?? DEFAULT_BASE_URL,
    model: config.model ?? DEFAULT_MODEL,
    apiVersion: config.apiVersion ?? DEFAULT_API_VERSION,
    maxTokens: config.maxTokens ?? DEFAULT_MAX_TOKENS,
    maxUses: config.maxUses ?? DEFAULT_MAX_USES,
    resolveApiKey: async () => {
      if (config.apiKey) return config.apiKey;
      const credentials = ctx.get?.("credentials");
      if (credentials !== undefined) {
        const found = await credentials.resolve(apiKeyEnv);
        if (found?.value) return found.value;
      }
      return process.env[apiKeyEnv];
    },
  };
}

export function apply(ctx, config = {}) {
  ctx.web.registerSearchProvider(
    new OpenRouterSearchProvider(() => resolveOptions(ctx, config))
  );
}

export default { name, inject, apply };
