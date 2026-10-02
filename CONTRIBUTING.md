# Contributing

Thanks for helping. Short version:

1. Read [AGENTS.md](AGENTS.md) (it applies to humans too) and [docs/LEGAL.md](docs/LEGAL.md).
2. `uv sync`, make your change, then `uv run ruff format . && uv run ruff check . && uv run pytest --cov`.
3. New retailer? Follow [docs/RETAILERS.md § Adding a retailer](docs/RETAILERS.md#adding-a-retailer). The contract suite must pass.
4. Open a PR describing what changed and how you verified it.

Contributions are accepted under the project's AGPL-3.0 licence. PRs that add
bot-protection evasion (proxy rotation, fingerprint spoofing, challenge solving)
will be declined.
