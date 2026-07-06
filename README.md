# Hermes Plugins

A few plugins we built for [Hermes Agent](https://hermes-agent.nousresearch.com/). Zero core patches, survive updates.

## Plugins

### `deep-research` ⚠️ WIP
Iterative multi-source research tool. Search-agnostic — works with SearXNG, Firecrawl, Tavily, Brave. Auto-discovers addons at runtime. Returns raw markdown, no LLM summarization truncation.

Still rough around the edges. Contributions welcome.

### `firecrawl-crawler`
Extends Hermes' web capabilities with two tools backed by Firecrawl self-hosted:
- `web_crawl` — recursive site crawling with full content extraction
- `web_map` — fast URL discovery (sitemap-aware, no content)

Requires a Firecrawl instance. Set `FIRECRAWL_API_URL` (defaults to `http://localhost:3002`).

### `ollama-usage`
`/ollama` slash command that shows your Ollama Cloud usage — session and weekly quotas, plan tier, reset times. Zero LLM calls, instant response.

Requires a session cookie from ollama.com/settings:
```bash
echo '__Secure-session=<value>' > ~/.hermes/ollama_cookie.txt
```

## Install

```bash
git clone https://github.com/3L0935/hermes-plugins.git
cp -r hermes-plugins/* ~/.hermes/plugins/
hermes plugins enable <name>
# restart gateway for slash commands
```

## License
MIT
