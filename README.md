# Hermes Plugins

A few plugins we built for [Hermes Agent](https://hermes-agent.nousresearch.com/). Zero core patches, survive updates.

## Plugins

### `deep-research`
Iterative multi-source research tool. Search-agnostic — works with SearXNG, Firecrawl, Tavily, Brave. Auto-discovers addons at runtime. Returns raw markdown, no LLM summarization truncation.

**Two modes:**
- `mode="light"` (default) — base loop: plan → search → extract → converge → synthesize
- `mode="full"` — base loop + adversarial critics + gap-fill search + persistent vault indexing

Full mode runs 3 critic profiles (counter-argument, depth, width) against the assembled report via the auxiliary LLM, identifies gaps with targeted search queries, fetches gap-filling sources, and appends a Gap Analysis section. Every full-mode run is indexed in the vault at `~/.hermes/deep-research/vault/` for cross-session reuse.

### `firecrawl-crawler`
Extends Hermes' web capabilities with two tools backed by Firecrawl self-hosted:
- `web_crawl` — recursive site crawling with full content extraction
- `web_map` — fast URL discovery (sitemap-aware, no content)

Requires a Firecrawl instance. Set `FIRECRAWL_API_URL` (defaults to `http://localhost:3002`).

### `ollama-usage`
`/ollama` slash command that shows your Ollama Cloud usage — session and weekly quotas, per-model request counts, reset times. Zero LLM calls, instant response.

Uses the official API endpoint `https://ollama.com/api/usage` (no scraping). Requires your Ollama API key:
```bash
# Either export it, or add to ~/.hermes/.env
export OLLAMA_API_KEY=your_key_here
```

Reset times are fixed-clock anchors (the API does not expose them): session resets every 5h on the hour anchored at 17:00 local, weekly resets Monday 02:00 local.

## Install

```bash
git clone https://github.com/3L0935/hermes-plugins.git
cp -r hermes-plugins/* ~/.hermes/plugins/
hermes plugins enable <name>
# restart gateway for slash commands
```

## License
MIT