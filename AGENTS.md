# AGENTS.md — TradingAgents

Multi-agent LLM financial-trading framework built on LangGraph. The agent
team (analysts → bull/bear debate → trader → risk debate → portfolio
manager) is driven by configurable LLM providers. The CLI is the primary
user-facing surface; the Python API lives in `tradingagents.graph`.

## Setup

```bash
pip install -e ".[dev]"      # editable + ruff/pytest extras
pip install ".[bedrock]"     # optional: AWS Bedrock (adds langchain-aws)
```

`requirements.txt` is just `.` — the real deps are in `pyproject.toml`.
Python 3.10+ (CI matrix: 3.10, 3.11, 3.12, 3.13).

## Verify

The CI gate runs three independent jobs — local work should keep all three
green:

```bash
ruff check .                 # strict; line-length 100, ignores E501
pytest -q                    # unit + integration + smoke markers
pip install . && python -c "import tradingagents, cli.main"   # clean-install import smoke
```

The clean-install import is the gate for undeclared runtime deps (#994) —
don't add an import that isn't listed in `pyproject.toml`.

## Entry points

| Surface | Path |
| --- | --- |
| Python entrypoint | `main.py` → `TradingAgentsGraph().propagate(ticker, date)` |
| CLI command | `tradingagents` (registered via `[project.scripts]`) |
| CLI source | `cli/main.py` (Typer; one subcommand: `analyze`) |
| Graph wiring | `tradingagents/graph/trading_graph.py` |
| Default config | `tradingagents/default_config.py` (also defines `_ENV_OVERRIDES`) |
| Agents | `tradingagents/agents/{analysts,researchers,managers,risk_mgmt,trader}/` |
| Data vendors | `tradingagents/dataflows/` (yfinance, alpha_vantage, fred, polymarket, stocktwits, reddit) |
| A-share & ETF (offline) | `tradingagents/dataflows/tdx_chronos.py` (optional install `[tdx_chronos]`) |
| LLM providers | `tradingagents/llm_clients/factory.py` (`create_llm_client`) |
| Smoke script | `scripts/smoke_structured_output.py <provider>` |

CLI flag surface is tiny: `tradingagents analyze [--checkpoint|--no-checkpoint] [--clear-checkpoints]`. Everything else is interactive prompts that auto-skip when the matching `TRADINGAGENTS_*` env var is set.

## Configuration

`DEFAULT_CONFIG` (in `tradingagents/default_config.py`) is the single source of truth. Any matching `TRADINGAGENTS_*` env var overrides the matching key on import — values are coerced to the existing default's type, and **invalid values raise `ValueError` at startup** (don't silently fall back). The full list lives in `_ENV_OVERRIDES`; the ones you'll touch most:

- `TRADINGAGENTS_LLM_PROVIDER` — `openai`, `google`, `anthropic`, `xai`, `deepseek`, `qwen`, `glm`, `minimax`, `openrouter`, `ollama`, `bedrock`, `azure_openai`, `openai_compatible`. Regional providers take a `-cn` suffix (`minimax-cn`, `qwen-cn`, `glm-cn`).
- `TRADINGAGENTS_DEEP_THINK_LLM`, `TRADINGAGENTS_QUICK_THINK_LLM`
- `TRADINGAGENTS_LLM_BACKEND_URL` — required for `openai_compatible` and `ollama`
- `TRADINGAGENTS_TEMPERATURE`, `TRADINGAGENTS_LLM_MAX_RETRIES`
- `TRADINGAGENTS_MAX_DEBATE_ROUNDS`, `TRADINGAGENTS_MAX_RISK_ROUNDS`
- `TRADINGAGENTS_CHECKPOINT_ENABLED`
- `TRADINGAGENTS_OUTPUT_LANGUAGE`
- `TRADINGAGENTS_TDX_CHRONOS_DATA_DIR`, `TRADINGAGENTS_TDX_CHRONOS_AUTO_ROUTE`, `TRADINGAGENTS_DISABLE_TDX_CHRONOS_AUTO_ROUTE` — A-share offline warehouse path, auto-route config key, and runtime gate escape hatch (only the `DISABLE_*` var is read by the auto-route gate in `tradingagents/dataflows/interface.py:183`).
- Provider-specific: `TRADINGAGENTS_OPENAI_REASONING_EFFORT`, `TRADINGAGENTS_GOOGLE_THINKING_LEVEL`, `TRADINGAGENTS_ANTHROPIC_EFFORT`

API keys: `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `ANTHROPIC_API_KEY`, `XAI_API_KEY`, `DEEPSEEK_API_KEY`, `DASHSCOPE_API_KEY` / `DASHSCOPE_CN_API_KEY`, `ZHIPU_API_KEY` / `ZHIPU_CN_API_KEY`, `MINIMAX_API_KEY` / `MINIMAX_CN_API_KEY`, `OPENROUTER_API_KEY`, `ALPHA_VANTAGE_API_KEY`, `FRED_API_KEY`, `AWS_BEARER_TOKEN_BEDROCK` (or full AWS credential chain).

`cp .env.example .env` is the canonical setup; `.env.enterprise.example` is the Azure variant.

## Persistence paths

All under `~/.tradingagents/` (overridable via `TRADINGAGENTS_*` env vars above):

- `memory/trading_memory.md` — always-on decision log; reflection is fed back into the Portfolio Manager on the next run for the same ticker. Override with `TRADINGAGENTS_MEMORY_LOG_PATH`.
- `cache/checkpoints/<TICKER>.db` — per-ticker SQLite, only written when `--checkpoint` / `checkpoint_enabled=True`. Override base with `TRADINGAGENTS_CACHE_DIR`. `--clear-checkpoints` wipes them.
- `logs/<TICKER>/<DATE>/reports/` — analyst report markdown tree written by the CLI.

## Tests

`tests/` mirrors `tradingagents/agents/` and `tradingagents/dataflows/` by name. Three markers (`unit`, `integration`, `smoke`) are registered in `pyproject.toml`. The conftest autouses two fixtures — without them CI hangs:

- `_dummy_api_keys` sets every provider key env var to `"placeholder"` if missing/empty.
- `_isolate_config` resets the global `dataflows.config._config` to `DEFAULT_CONFIG` around each test, because `set_config` merges and would otherwise leak state across tests.

A `mock_llm_client` fixture is available for tests that need to bypass the provider factory. Pure-LLM scripted smoke runs are in `scripts/smoke_structured_output.py` and require real keys — they're manual, not part of `pytest`.

## Conventions

- Lint: `ruff check .` with the strict `select` (`E,W,F,I,B,UP,C4,SIM`) defined in `pyproject.toml`. `ruff format` is intentionally deferred (see comment in `pyproject.toml`) — don't run it on the open-PR backlog.
- `__init__.py` re-exports are intentional; `F401` is ignored there.
- Per-region providers (`qwen`, `minimax`, `glm`) prompt for a region in the CLI; mainland and international API keys can't share. The `-cn` provider id skips the prompt.
- `--checkpoint` / `--no-checkpoint` / env var precedence: explicit flag > env var > default. `omitting` the flag preserves whatever the env says (#976).
- The data vendor config is exact — the chain in `data_vendors` / `tool_vendors` is the only resolution path; no silent fallback to unselected vendors.

## Gotchas

- **Determinism is bounded.** LLM sampling is non-deterministic; reasoning models (GPT-5.x, etc.) ignore `temperature`. Lower `TRADINGAGENTS_TEMPERATURE` only helps on non-reasoning models. Pin the analysis date; news/social sources still drift over time. See README "Reproducibility".
- **`python main.py` runs against real APIs.** It calls `propagate("NVDA", "2024-05-10")` with the configured provider — make sure the relevant key is set or it'll fail at the first LLM call.
- **`test.py` at the repo root** is a one-off dataflow timing probe, not part of the test suite.
- **Docker compose profiles**: `tradingagents` is the default; `tradingagents-ollama` is under the `ollama` profile and brings up a sidecar `ollama` container.
- **Look-ahead**: the Alpha Vantage fundamentals payload is a JSON string; date filtering must parse before filtering (#1115).
- **Crypto tickers** use Yahoo's `<BASE>-USD` form (e.g. `BTC-USD`); StockTwits wants the base symbol as `<BASE>.X`, so the social path remaps.
- **First-priority A-share**: When tdx-chronos is installed, A-share symbols (e.g. `sh600000`, `600000.SS`) auto-route to its offline parquet store before the configured `data_vendors` chain runs. Disable via `TRADINGAGENTS_DISABLE_TDX_CHRONOS_AUTO_ROUTE=1`. ETF/LOF/REIT/可转债 fundamentals return an explicit "out of scope" marker rather than fabricating values.