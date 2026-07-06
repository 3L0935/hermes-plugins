# Hermes Plugins

A collection of community plugins for [Hermes Agent](https://hermes-agent.nousresearch.com/) — the personal AI agent by Nous Research.

## Plugins

### 🔍 `deep-research`
Iterative multi-source deep research tool. Search-agnostic — works with any Hermes web search backend (SearXNG, Firecrawl, Tavily, Brave). Auto-discovers addons (firecrawl crawl, browser scraping) at runtime. Returns raw structured markdown — no LLM summarization that truncates content.

```
hermes> deep_research query="Latest developments in RAG architectures" max_pages=20
```

### 🕸️ `firecrawl-crawler`
Exposes Firecrawl self-hosted endpoints as `web_crawl` and `web_map` tools. Recursive site crawling with full content extraction, sitemap-aware discovery, and path filtering.

```
hermes> web_crawl url=https://docs.example.com prompt="Find API reference pages"
```

**Requires:** [Firecrawl](https://github.com/nicklason/firecrawl) self-hosted instance. Set `FIRECRAWL_API_URL` env var (defaults to `http://localhost:3002`).

### 📊 `ollama-usage`
`/ollama` slash command that shows your Ollama Cloud usage — session and weekly quotas, plan tier, and reset times. Uses `register_command()` — handled at the command-dispatch level, **zero LLM calls**, instant response.

```
/ollama
📊 Ollama Cloud — Pro
  Session: 98% remaining (2% used) • resets in 4h 53m (21:00 CEST)
  Weekly:  56% remaining (44% used) • resets in 9h 53m (02:00 CEST)
```

**Requires:** Session cookie from [ollama.com/settings](https://ollama.com/settings). Save it to `~/.hermes/ollama_cookie.txt`:
```bash
echo '__Secure-session=<your_cookie_value>' > ~/.hermes/ollama_cookie.txt
```

## Installation

Each plugin is a directory you copy into your Hermes plugins folder:

```bash
# Clone the repo
git clone https://github.com/3L0935/hermes-plugins.git ~/hermes-plugins

# Install individual plugins
cp -r ~/hermes-plugins/ollama-usage ~/.hermes/plugins/
cp -r ~/hermes-plugins/firecrawl-crawler ~/.hermes/plugins/
cp -r ~/hermes-plugins/deep-research ~/.hermes/plugins/

# Enable them
hermes plugins enable ollama-usage
hermes plugins enable firecrawl-crawler
hermes plugins enable deep-research

# Restart the gateway for slash commands to take effect
hermes gateway restart
```

## Requirements

- **Hermes Agent** — any recent version (plugins use public plugin API only, no core patches)
- **Python 3.10+**
- **Firecrawl** (for `firecrawl-crawler`) — self-hosted instance
- **Ollama Cloud account** (for `ollama-usage`) — session cookie from ollama.com/settings

## Why plugins?

Hermes is designed to be extended through plugins and skills, not by growing the core. These plugins:

- ✅ **Zero core patches** — survive Hermes updates without breaking
- ✅ **Self-contained** — no dependencies on Hermes internals
- ✅ **Public API only** — use `register_command()`, `register_tool()`, and plugin hooks
- ✅ **Portable** — copy to any Hermes installation

## License

MIT — do whatever you want with them.
