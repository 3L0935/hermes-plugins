# Contributing

PRs and issues are welcome. These plugins are built for real use — if something's broken, missing, or could be done better, speak up.

## Guidelines

- **Keep it self-contained.** No core Hermes patches. Plugins live in `~/.hermes/plugins/` and use the public plugin API (`register_command`, `register_tool`, hooks).
- **No hardcoded paths.** Use `Path.home()`, `os.getenv()`, or config params. Don't assume the user's setup looks like mine.
- **No secrets in code.** API keys, tokens, cookies go in env vars or external files.
- **Python 3.10+** — stdlib where possible, document external deps.

## Plugins status

| Plugin | Status |
|---|---|
| `ollama-usage` | Stable |
| `firecrawl-crawler` | Stable |
| `deep-research` | ⚠️ WIP — rough edges, contributions especially welcome |

## PR flow

1. Fork the repo
2. Make your changes in a branch
3. Open a PR with a clear description of what and why
4. Keep it focused — one change per PR if possible

That's it.
