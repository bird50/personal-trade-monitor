# เปลี่ยนเป็นโหมดแจ้งเตือนอย่างเดียว — 8 พฤศจิกายน 2026

คำขอของผู้ใช้:
- ปรับ policy และ App ให้ไม่ใช้ AI และไม่ใช้บัญชี Paper
- แจ้งอย่างเดียวว่าเข้า trade ได้ตอนไหน
- ใช้กลยุทธ์ที่ดีที่สุดจากการวิเคราะห์ล่าสุด

## สิ่งที่เปลี่ยน
| เรื่อง | ก่อน | หลัง |
| --- | --- | --- |
| บริการที่ :8002 | `app.xm_main:create_xm_app` (xm-0.2 → AI → Risk → Paper) | `app.alert_main:create_alert_app` (RSI(2) → ตัวกรอง Risk → แจ้งเตือน) |
| กลยุทธ์ | `xm-0.2` (EMA20 cross + ADX) | `rsi2_trend_pullback_r2` rr 2.5, ไม่มี breakeven, time stop 48 (`config/alert-xm.json`) |
| AI | ยกเว้น AI แต่ worker ยังเปิด | ไม่มี AI worker |
| บัญชี Paper | เปิด (10,000 USD, ไม่มีไม้) | ไม่เปิด ฐานข้อมูล `data/xm-trading.db` ไม่ถูกแก้ |
| ฐานข้อมูล | `data/xm-trading.db` | `data/xm-alerts.db` (ใหม่) |
| Risk | `RiskManager.evaluate` | ใช้ `level_metrics` ที่แยกออกมาจาก `evaluate` (สูตรเดียวกัน) |
| แอป Mac / dashboard | AI, Paper, ยกเว้น AI/Risk | สัญญาณล่าสุด, ประวัติสัญญาณ, สภาพตลาดตามกลยุทธ์, ปิด/เปิดเสียง |

เลือกกลยุทธ์จาก [rsi2-live-gate-walkforward-2026-11-08.md](rsi2-live-gate-walkforward-2026-11-08.md):
- ชุด rr 2.5 ไม่มี BE เป็นชุดที่ walk-forward เลือกบ่อยที่สุด
- time stop 48 คือ config 17 ซึ่งได้ผลรวม 3 ปีดีที่สุดใน grid: 191 ไม้, E +0.196R, PF 1.22 รวมช่วงที่ใช้เลือก

ตัวกรอง Risk ยังใช้ `config/risk-xm-paper.json` ชุดเดียวกับที่ใช้ใน backtest `--live-risk-gate` ไฟล์นี้ไม่ได้แก้ hash ของบัญชี Paper เดิมจึงยังตรง

## Verification
| การตรวจ | ผล |
| --- | --- |
| `.venv/bin/python -m pytest -q` | **272 passed** (เพิ่ม `tests/test_alert_engine.py` 10 tests) |
| `scripts/check_alert_parity.py` บน `data/history/xm_gold_m1_36m.csv` (1,061,903 แท่ง M1) | backtest 191 ไม้ · alert 191 สัญญาณ · **ตรงกัน 191** · ต่างกัน 0 · backtest ไม่มีไม้ที่ถูกตัดตอน fill |
| `node --check dashboard/app.js` | ผ่าน |
| ตรวจหน้าจอ dashboard ด้วยข้อมูลจาก engine บนข้อมูลสังเคราะห์ (browser, 1280×860) | แสดงสถานะพร้อม, สัญญาณล่าสุด, กติกา, ประวัติ และเหตุการณ์ถูกต้อง |
| `sh scripts/build_mac.sh` + `codesign --verify --deep --strict` | ผ่าน bundle มี `alerts.css`, ไม่มี `provider.css` |
| เปิด `app.alert_main` ด้วย config จริงที่พอร์ตทดสอบ 8012 และฐานข้อมูลชั่วคราว | `/health` ok, worker ทำงาน, engine = `feed_stale` เพราะ snapshot ล่าสุดใน `data/mt5-market.db` อายุประมาณ 37 ชั่วโมง |

ยังไม่ได้ทดสอบกับราคาสดจาก MT5 และยังไม่ได้เปิดบริการจริงหรือแอปให้ผู้ใช้ ณ เวลาตรวจ:
- snapshot ล่าสุดของ EA มี M5/H1 อย่างละ 300 แท่ง
- ถ้าไม่เปลี่ยน `HistoryBars` เป็น 1000 จะต้องอุ่นเครื่องอีกราว 100 แท่ง H1 ก่อนเริ่มแจ้งเตือน

## ย้อนกลับ
- **รันบริการเดิมเอง:** หยุดบริการแจ้งเตือนก่อน แล้วรัน `sh scripts/run_xm_paper.sh`
- **กลับเป็นแอปและ dashboard เดิม:** checkout branch `dev` แล้ว build แอปใหม่
- ฐานข้อมูลโหมดเดิมไม่ได้ถูกแก้ จึงย้อนกลับได้โดยไม่ต้องกู้ backup
