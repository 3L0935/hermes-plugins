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
`/ollama` slash command that shows your Ollama Cloud quota — 5h session and weekly remaining %, reset countdown, purchased balance. Zero LLM calls, instant response.

Uses the official balance endpoint `https://ollama.com/api/balance` (no scraping). Requires your Ollama API key:
```bash
# Either export it, or add to ~/.hermes/.env
export OLLAMA_API_KEY=your_key_here
```

`resets_at` comes from the API, so the countdown is real — no fixed-clock guesses. Both plan shapes are handled: a legacy plan returns `included.session` / `included.weekly` (remaining percentage per window), a credits plan returns `included.balance_usd` / `allowance_usd` / `period`, printed as a credit line.

> `/api/usage` is **not** used any more: since Nov 2026 it only returns request counts and cost per bucket (`range=24h|7d|30d`, optional `group_by=model`), with no quota block. Reading `limits.session` from it is what made the command print "unavailable".

## Install

```bash
git clone https://github.com/3L0935/hermes-plugins.git
cd hermes-plugins
./install.sh                 # interactive: pick what to link into your Hermes home
```

The installer **symlinks** each plugin into the Hermes home (no copy, so `git pull` updates the
install), runs `hermes plugins enable`, and enables the toolset the plugin exposes. Nothing is
hardcoded: a plugin is any directory with a `plugin.yaml`, and its toolset is discovered from
`hermes tools list` after enabling.

```bash
./install.sh --list                    # plugins in the repo + link state
./install.sh --status                  # link, enabled, toolsets, missing env vars
./install.sh ollama-usage            # install by name
./install.sh --all --yes               # everything, no prompt
./install.sh --uninstall ollama-usage  # disable + remove the link
./install.sh <plugin> --dry-run        # show the plan, change nothing
./install.sh --profile work            # target ~/.hermes/profiles/work
./install.sh --help                    # all options
```

Each plugin also ships its own `install.sh` that delegates to the root one
(`./ollama-usage/install.sh` is `./install.sh ollama-usage`).

Safety rules the installer follows:

- a **real directory** at the target path is never silently replaced: it is moved to
  `<name>.bak-<timestamp>` (or the plugin is skipped), because a stale copy would shadow the repo;
- a symlink pointing somewhere else is reported and only replaced after confirmation;
- `--uninstall` never deletes a real directory, only the symlink it created;
- missing `requires_env` variables from the manifest are reported, not guessed.

After installing, restart the gateway to load hooks, then open a **new session** for the
plugin's tools to appear in the model's tool list (a running conversation keeps the tools it
started with).

## License
MIT