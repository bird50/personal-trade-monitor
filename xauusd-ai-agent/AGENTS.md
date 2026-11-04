# Project instructions

Applies to this directory and its descendants. Use Thai for user-facing explanations and project documentation; preserve English code identifiers and API names. Follow the user's current request and existing session authorization without adding confirmation steps for routine, reversible work.

## Project context

This is a local Mac research system: **XM Demo → MT5 EAs → Python → LM Studio/Codex → Risk → Paper Trading → XM Control/macOS notifications**. The supported execution scope is Python paper simulation. Do not introduce broker order execution or enable real-account trading as an incidental change.

Start with [README.md](README.md). Read the relevant guide for the area being changed:

| Area | Source | Guide |
| --- | --- | --- |
| Market snapshots and ordered ticks | `app/mt5_bridge.py`, `app/tick_feed.py`, `mt5/` | [MT5.md](MT5.md), [PAPER.md](PAPER.md) |
| XM candidates and checkpoints | `app/xm_engine.py`, `app/xm_main.py`, `app/structure.py` | [XM-SIGNALS.md](XM-SIGNALS.md) |
| AI providers, validation and jobs | `app/ai_analyzer.py`, `app/codex_analyzer.py`, `app/ai_providers.py`, `app/worker.py` | [AI.md](AI.md), [CODEX.md](CODEX.md) |
| Signal validation, API and storage | `app/models.py`, `app/webhook.py`, `app/main.py`, `app/database.py` | [README.md](README.md) |
| Risk and account accounting | `app/risk_manager.py`, `app/paper_trading.py`, `config/` | [RISK.md](RISK.md), [PAPER.md](PAPER.md) |
| Mac UI, controls and notifications | `dashboard/`, `mac/XMControl.swift`, `app/control_center.py`, `scripts/control_system.py` | [MAC-CONTROL.md](MAC-CONTROL.md) |
| Optional TradingView/OANDA flow | `pine/`, `app/main.py` | [pine/README.md](pine/README.md) |

Historical reports under `tests/` describe checks at their recorded dates. They do not establish current process, feed, model or account status. Some older guides retain historical limitations; compare them with the current source and README.

## Runtime boundaries

| Service | Launch command | Loopback port | Default database |
| --- | --- | --- | --- |
| MT5 receiver | `sh scripts/run_mt5_bridge.sh` | 8001 | `data/mt5-market.db` |
| XM/AI/Risk/Paper | `sh scripts/run_xm.sh` | 8002 | `data/xm-trading.db` |
| Optional TradingView webhook | `sh scripts/run_local.sh` | 8000 | `data/trading.db` |

These are long-running commands; use separate sessions when needed. Keep loopback binding, single-worker operation and disabled access logs. Do not start the optional webhook service, open a Tunnel, publish the dashboard or connect messaging services unless relevant to the user's request.

- Use Python 3.11 and `.venv/bin/python`; install project dependencies into `.venv`.
- Resolve configured paths through `app/config.py`. XM settings are specialized by `xm_settings()`; do not merge the XM and optional OANDA databases or source policies.
- Keep blocking database, filesystem and subprocess work off FastAPI's event loop. Retain bounded timeouts, response limits and graceful cancellation.
- Edit `dashboard/` and `mac/` sources, then build. Do not patch generated files in `mac/build/` or module caches as the implementation.
- Inspect Git state when available; do not initialize a repository, commit, discard user changes or perform unrelated cleanup without a corresponding request.

## Credentials and private data

- Never print, commit, embed in frontend code, or include in reports: `.env`, `.env.mt5`, `mt5/*-token.txt`, auth files, bearer tokens or account credentials.
- Do not dump environment variables, credential stores, raw remote errors or full process command lines to diagnose a single setting. Inspect only the needed non-secret fields and redact private account/IP information from logs.
- Let existing scripts and native code read credentials locally. Preserve the separate roles of `WEBHOOK_TOKEN`, `API_TOKEN` and `MT5_BRIDGE_TOKEN`.
- Keep `.gitignore` exclusions for secrets, databases/WAL files, `.runtime/`, `.venv/` and generated Mac artifacts. Preserve restrictive permissions on secret files.
- Keep the WebKit/native bridge limited to fixed, validated actions and loopback API paths; never expose tokens or arbitrary command/URL execution to JavaScript.
- Codex uses existing ChatGPT CLI authentication in place. Do not copy auth files, forward trading tokens/API keys into its subprocess, or silently switch authentication or billing methods.

## Data, accounting and risk invariants

- Treat `data/` and `.runtime/` as existing user state. Use temporary databases for tests and demonstrations; do not insert synthetic signals, approvals, ticks, funding or trades into the operational account.
- Before a migration or state-changing repair, inspect the relevant state and make a SQLite backup. Use SQLite's backup API for a running database; do not assume copying a live WAL database file alone is sufficient.
- Prefer additive migrations. Preserve signals, original evidence, ledger, positions, funding-once behavior, deduplication and replay/checkpoint state.
- Keep account money and quantities in Decimal and API money values in their established string representation. Preserve bid/ask direction, slippage, commissions and ledger reconciliation.
- Maintain atomic account/position/ledger/cursor updates and lease-guarded AI/risk commits. Retrying, restarting or receiving duplicate data must not open a trade or charge fees twice.
- Never reset funding, delete positions, jump tick cursors or clear checkpoints merely to make a stale/error status disappear. Investigate missing history, revised bars, policy hashes and offset changes.
- Recently received batches may contain old historical ticks. Track receipt age separately from quote/coverage age, preserve ordered contiguous coverage and millisecond boundaries, and wait for catch-up before opening new positions.
- Preserve the time basis: top-level signal `bar_close_ms` is UTC; schema-3 H1/pivot context uses the declared broker wall clock. Do not reinterpret broker history with an assumed current offset.
- Keep strict JSON validation, source/server/symbol pinning, closed-bar checks, freshness/expiry rules, risk limits and account policy checks. Read limits from configuration; do not relax them to make a test or model output pass.
- Pausing blocks new entries while existing positions continue to be marked and processed for SL/TP. Stopping services must respect open-position guards. Preserve exit monitoring when policy changes block new entries.
- If a requested change affects policy or account semantics, implement and test that change explicitly; do not silently migrate or reset the operational account.

## AI provider behavior

- LM Studio and Codex share output schema and price/direction/confirmation validation. AI cannot change Entry/SL/TP/RR or reverse the proposed side; incomplete confirmation requires WAIT.
- A model response is not an entry authorization. Retain Risk evaluation, source/quote freshness, signal expiry and Paper account gates. Recheck age after inference and expose the effective decision.
- Provider selection persists and affects subsequent claims. Already-running work keeps its original analyzer; save the provider/model actually used for the result.
- Do not add automatic provider/model fallback or replay historical failed approvals as an incidental change.
- Keep Codex inference isolated, ephemeral, read-only and tool-restricted. Preserve bounded output, timeout/process-group cleanup and credential environment filtering. Do not resume a user's development chat for signal analysis.
- Login status and service health are not proof of successful inference or remaining quota. Report timeout, invalid output, authentication, model availability and quota failures accurately; do not invent a successful result.
- Default tests use mocks. Run a bounded live AI check when relevant and authorized by the task; existing authorization to implement/test an integration counts. Do not spend model quota for unrelated documentation work or repeatedly rerun live checks without a reason.

## Verification commands

Run from this directory. Select checks appropriate to the change rather than running every command below automatically.

| Change | Check |
| --- | --- |
| Python behavior | `.venv/bin/python -m pytest -q` |
| Focused module work | Run relevant `tests/test_*.py` files first; use the full suite for shared AI, storage, risk or accounting changes |
| Dashboard JavaScript | `node --check dashboard/app.js`, then inspect the actual UI when possible |
| Native app/assets | Close the UI, run `sh scripts/build_mac.sh`, then `codesign --verify --deep --strict 'mac/build/XM Control.app'` |
| Optional webhook HTTP integration | `.venv/bin/python scripts/smoke_test.py` |
| MT5 receiver HTTP integration | `.venv/bin/python scripts/smoke_mt5.py` |

Explicit live model checks use synthetic evidence and a temporary database:

```sh
.venv/bin/python scripts/test_lm_studio.py --xm --provider lm_studio
.venv/bin/python scripts/test_lm_studio.py --xm --provider codex
```

The Codex command uses account quota. The live-test script extends signal/policy age only inside its disposable test setup; never apply that extension to production settings.

For documentation-only changes, verify referenced paths, commands, config names and API routes against source; a full test run is not normally needed. Add regression coverage for meaningful behavioral changes, especially accounting, auth, retries, expiry and process lifecycle. Once appropriate checks pass, repeat them only after a relevant change or unresolved failure.

## Mac/MT5 deployment and status checks

- Use the supported computer-use tools for UI interaction when available. Preserve the native bridge and macOS notification permission flow.
- Before restarting services, inspect current positions, worker jobs and pause/provider settings. Use `scripts/control_system.py` guards; only manage verified project processes, never kill unrelated processes occupying the ports.
- Preserve intentional user settings during testing/deployment. Rebuild and reopen the app when needed to verify bundled assets; closing its window should retain menu-bar monitoring, while quitting the UI should leave Python services running.
- Compile changed MQL5 sources with MetaEditor and inspect its compiler log. A process exit code alone does not prove compilation succeeded. Verify the installed binary matches the compiled source.
- Installing an EA file does not prove MT5 loaded it. Reattach/reload and confirm the version/startup log and advancing data. If Wine UI control is unavailable, prepare the files first and request only the necessary manual MT5 steps from the user.
- Diagnose tick problems using snapshots, stream receipt/coverage age, paper status and filtered EA logs. Do not infer fresh ticks from a fresh snapshot or a successful `/health` response.
- Use `scripts/mt5_status.py GOLD`, `scripts/xm_status.py` and `scripts/paper_status.py` for existing local diagnostics. Do not stop services just to perform a read-only status check.

## Documentation and handoff

Update README and the relevant guide when changing commands, configuration, endpoints, provider behavior or UI controls. Keep detailed dated evidence under `tests/` where useful; exclude secrets and raw private logs.

Summarize what changed, the relevant verification and any remaining limitation. Distinguish synthetic model tests from real market feed checks, and tests of connectivity/schema/risk from evidence of strategy profitability. Report current runtime status only after checking it.
