# เปลี่ยนเป็น xm-0.2 และ RR 1.2 — 6 พฤศจิกายน 2026

คำขอของผู้ใช้: ลด RR ขั้นต่ำเป็น 1.2, ไม่ใช้ชั้น AI, และในกลยุทธ์ไม่ใช้ RSI และไม่ใช้โครงสร้างตลาด

## สิ่งที่เปลี่ยน
| เรื่อง | ก่อน | หลัง | ที่อยู่ |
| --- | --- | --- | --- |
| กลยุทธ์ | `xm-0.1`: H1 EMA50/200 + M5 EMA20 cross + RSI band + ADX/DI | `xm-0.2`: เหมือนเดิมแต่ไม่มี RSI band | `app/xm_engine.py` (`choose_side`, `VERSION`) |
| โครงสร้างตลาด | Risk ปฏิเสธเมื่อมี `confirmation_blockers` | ไม่บังคับ (`require_market_structure: false`) | `config/risk-xm-paper.json`, `app/risk_manager.py` |
| RR สุทธิขั้นต่ำ | 1.5 | 1.2 | `config/risk-xm-paper.json` |
| AI | LM Studio | ยกเว้น AI (`pipeline_bypass.ai = 1`) ตั้งผ่าน `POST /dashboard/bypass` | ตาราง `pipeline_bypass` |
| policy ของบัญชี Paper | hash `b175c8a3…` | hash `3412a301…` บันทึกใน `paper_policy_history` | `scripts/adopt_paper_policy.py` |

ค่า Risk/Paper อื่นไม่เปลี่ยน: ความเสี่ยง 0.5% ต่อไม้, stop 1–3 ATR, spread/ATR ≤ 0.20, notional ≤ 1× ทุน, ขาดทุนรายวัน 2%, ไม้ละ 1, quote ≤ 30 วินาที, drift ≤ 0.25 ATR

## ขั้นตอนที่ทำกับระบบจริง
1. ตรวจก่อน: ไม่มีไม้เปิด ไม่มีงาน AI ค้าง (มีเพียง analysis `failed` 1 รายการเก่า) ยกเว้นทั้งสองโหมดปิด
2. Engine ค้างที่ `missing_overlap_reset_required` อยู่แล้วก่อนแก้: checkpoint สุดท้าย 2026-11-04 12:40 เวลา server แต่บริการเปิดกลับมา 2026-11-06 18:10 (+07) ช่องว่างยาวกว่าหน้าต่าง M5 300 แท่ง
3. สำรอง `data/mt5-market.db` และ `data/xm-trading.db` ด้วย SQLite backup API → `.runtime/backups/*.20261106T193908.before-xm-0.2`
4. หยุดบริการ XM (ตรวจว่า PID เป็น `uvicorn app.xm_main:create_xm_app`), เก็บ checkpoint เดิมไว้ใน `xm_engine_resets` แล้วลบแถว `xm_engine` เพื่อเริ่ม baseline ใหม่
5. เปิดบริการ XM → `bootstrapped_no_historical_signals`, `engine_version = xm-0.2`
6. เปิดยกเว้น AI ผ่าน API (บันทึก event `bypass_changed`)
7. `scripts/adopt_paper_policy.py` dry run แสดงการเปลี่ยน 2 ค่า (`risk.min_net_reward_risk` 1.5→1.2, `risk.require_market_structure` null→false) แล้ว `--confirm`
8. ผล: Paper `running`, `policy_matches: true`, `entries_enabled: true`, ยอด 10,000 USD, ไม่มีไม้เปิด, ยกเว้น AI เปิด / ยกเว้น Risk ปิด

## Backtest กติกาใหม่ (ไม่ใช่หลักฐานว่าทำกำไรได้)
ข้อมูล `data/history/xm_gold_m1_36m.csv` 2023-11-06 ถึง 2026-11-06, ใช้ `--live-risk-gate` กับ policy ใหม่ (RR ≥ 1.2), `baseline_xm01` ตามที่ระบุ ไม่จำลอง daily loss lock

| กติกา | ไม้ | ไม้/สัปดาห์ | WR | E (R) | PF (USD) | RR จริง | Max DD |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `xm-0.1` (RSI + โครงสร้าง) | 6 | 0.06 | 0% | −1.000 | 0.00 | – | 1.55% |
| RSI อย่างเดียว ไม่มีโครงสร้าง | 591 | 4.0 | 32.0% | −0.090 | 0.83 | 1.85 | 15.5% |
| **`xm-0.2` (ไม่มี RSI ไม่มีโครงสร้าง)** | 610 | 4.2 | 32.1% | **−0.085** | **0.84** | 1.85 | **15.2%** |

ราย ปี ของ `xm-0.2`: 2023-11→2024-11 30 ไม้ E −0.173 · 2024-11→2025-11 164 ไม้ E +0.063 · 2025-11→2026-11 416 ไม้ E −0.137

- การตัดโครงสร้างคือสิ่งที่เปลี่ยนจำนวนไม้ (6 → ~600) ส่วน RSI band แทบไม่ได้กรองอะไรเพิ่ม (591 → 610)
- กติกาใหม่ในข้อมูลย้อนหลังขาดทุนประมาณ 0.085R ต่อไม้ และ drawdown เกิน 10%
- RR ที่ตั้ง (TP = 2 เท่าของ SL) ยังสูงกว่า 1.2 เสมอ การลดเกณฑ์จึงช่วยเฉพาะช่วงที่ ATR ต่ำจนต้นทุนกิน RR (ยังถูกตัด 301 สัญญาณด้วย `net_reward_risk_too_low`)

## Verification
- `.venv/bin/python -m pytest -q` → 262 passed
- ทดสอบใหม่: `choose_side` แบบ xm-0.1/xm-0.2, upgrade checkpoint xm-0.1→xm-0.2, ความสอดคล้องของ version ในสัญญาณ, `require_market_structure`, hash policy เดิมไม่เปลี่ยน, `adopt_policy` (ปฏิเสธเมื่อมีไม้เปิด, เก็บประวัติ, ไม่แตะยอดเงิน/ledger)
- `tests/test_bypass.py` ใช้ policy แบบที่ยังบังคับโครงสร้างเพื่อทดสอบการยกเว้น blocker ต่อไป

## ย้อนกลับ
กลับ `config/risk-xm-paper.json` และโค้ดเป็น commit ก่อนหน้า, ปิดยกเว้น AI, รีสตาร์ต แล้วรัน `scripts/adopt_paper_policy.py --confirm` อีกครั้ง; checkpoint `xm-0.2` จะต้องเริ่ม baseline ใหม่ (หรือกู้จาก backup ข้างบน)
