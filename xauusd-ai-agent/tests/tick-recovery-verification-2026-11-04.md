# Tick recovery — 4 November 2026 (Asia/Vientiane)

At 08:44 the receiver and paper services were running, market snapshots were fresh (8.9 s), and tick batches had arrived 8.6 s earlier. Tick coverage itself was 47,470.7 s old (about 13 h). MT5 logs showed an overnight disconnect and a successful reconnection around 08:38. The v1.01 exporter was sending one 2,047-tick historical page per 15-second timer. The account correctly stayed `feed_stale`, with no new entries, balance 10,000 USD and zero open positions.

Changes:

- XMPaperTicks v1.02 sends up to eight consecutive acknowledged pages per timer when historical data remains, with a two-second soft scheduling budget. It stops the burst on any error or when caught up. Existing cursor, millisecond boundary handling, retry behavior, Demo/server restriction, 2,048-tick batch cap and history order are retained.
- Receiver isolates snapshot (20/min) and tick (64/min) rate budgets, so recovery bursts do not starve M5/H1 snapshots.
- Dashboard adds tick transport age and coverage age, distinguishing `catching_up`, `stale`, `live` and missing streams. The UI and alert explain historical catch-up rather than implying the EA stopped. Paper quote-age and approval/risk gates are unchanged.

Validation:

- Python suite: **218 passed**, one existing Starlette/httpx deprecation warning. New tests reproduce recently received historical ticks without treating them as fresh or enabling entries, and verify bounded catch-up traffic remains isolated from snapshots.
- MetaEditor CLI: **0 errors, 0 warnings**, 1,474 ms, log `mt5-ticks-compile-2026-11-04.txt` (UTF-16). Installed source and compiled file verified against workspace copies.
- User reattached EA; terminal confirms reload at 08:51:01 and Experts log confirms `Paper tick exporter v1.02 started`. Real batches show eight acknowledgements within about 1.5 seconds. No exporter errors after the v1.02 startup at the audit time.
- Real tick audit at 222,437 rows: sequence contiguous, stream cursor matches last stored sequence, **zero coverage gaps**.
- Backlog decreased from 26,896.6 s at 08:52:14 to 16,064.4 s at 08:52:44 while AI/model and all local workers remained ready. Account remained 10,000 USD with zero open positions and entries disabled during catch-up.
- Actual Mac app opened and verified using accessibility: displays historical catch-up and its current delay. No synthetic production trade, history reset or cursor jump was used. Trading DB backed up under `.runtime/backups/` and original pause setting restored after the guarded restart.

Final live recovery result is recorded in `.runtime/tick-recovery-live.json`.

Recovery confirmed at **08:53:44**: tick status `live`, Paper `running`, `valuation_fresh=true`, `entries_enabled=true`, quote age 11.2 s; all three workers running and LM Studio/model ready. Balance 10000 USD, 0 open positions. Mac screenshot at 08:53:51 visibly shows **ระบบพร้อม · รอสัญญาณเข้าเงื่อนไข** and **ติดตาม Tick สด**, with the stale-tick warning removed.
