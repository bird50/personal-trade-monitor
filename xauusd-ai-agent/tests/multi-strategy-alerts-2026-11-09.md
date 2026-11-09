# เลือกและผสมกลยุทธ์ในโหมดแจ้งเตือน — 9 พฤศจิกายน 2026

คำขอของผู้ใช้: ให้ engine ใช้กลยุทธ์ TPSL London breakout, `ent_cci_wr_hook` และ `ent_heikin_ashi_flip` ได้ และเลือกได้ว่าจะใช้ตัวไหน หรือใช้หลายตัวผสมกัน

## สิ่งที่เปลี่ยน

| เรื่อง | ก่อน | หลัง |
| --- | --- | --- |
| Policy | `xm-alert-v1` กลยุทธ์เดียว (RSI(2)) | `xm-alert-v2` มี `strategies` 4 ตัว และ `conflict` (`warn`/`skip`) |
| Engine | `AlertCore` ตัวเดียว | `app/alert_strategies.py`: `MarketCore` (RSI(2), CCI, HA) และ `TPSLCore` (คำสั่งรอ) ทุกตัวคำนวณทุกแท่ง แต่ละตัวถือไม้สมมติแยกกัน |
| การเลือก | — | runtime ใน `control_meta` (`alert_strategies`), สวิตช์ในแอป, `GET/POST /strategies` |
| Checkpoint | `STATE_VERSION` 1 | 2 (`cores` แยกตาม `id`) เริ่มบริการครั้งแรกจะเริ่มคำนวณใหม่หนึ่งครั้ง (`state_version_changed`) |
| เหตุการณ์ | `trade_alert` | เพิ่ม `trade_update` (TPSL: คำสั่งถูกชน, เลื่อน SL เป็นทุน, 21:00) และ `strategies_changed` |
| Telegram | — | ส่ง `trade_update` ด้วย · `/status` แสดงทุกกลยุทธ์ที่เลือก · `/signal` รองรับ Buy/Sell Stop |

ค่าที่ใช้คือค่าที่ล็อกไว้ก่อนหน้า ไม่ได้ปรับใหม่:

- `cci`: config 10 (`cci_level` 150, `rr` 2.5, ไม่มี breakeven)
- `ha`: config 0 (`er_min` 0.30, `rr` 2.0)
- `tpsl`: กติกาต้นฉบับใน `scripts/tpsl_london_breakout.py`
- ทั้ง `cci` และ `ha` ไม่มี time stop เหมือนตอน backtest

ค่าเริ่มต้นคือเปิดเฉพาะ `rsi2` เท่ากับพฤติกรรมเดิม

## ความตรงกับ backtest

ข้อมูล `data/history/xm_gold_m1_36m.csv` (XM demo GOLD M1, 2023-11-06 ถึง 2026-11-06) ใช้ Risk ปัจจุบัน (SL 1–12 ATR):

```
.venv/bin/python scripts/check_alert_parity.py
```

| กลยุทธ์ | backtest | engine | ตรงกัน | หมายเหตุ |
| --- | ---: | ---: | ---: | --- |
| `rsi2` | 191 | 191 | 191 | — |
| `cci` | 250 | 251 | 250 | backtest ตัด 1 ไม้ตอน fill เพราะราคาเคลื่อนเกิน drift (`live_risk:entry_price_drift`) ซึ่งระบบแจ้งเตือนตรวจไม่ได้ |
| `ha` | 156 | 156 | 156 | — |
| `tpsl` | 213 (M1 simulator, ต้นทุน `xm_paper`, ผ่าน gate) | 213 fill จาก 219 วันที่ตั้งคำสั่ง | 212 | 1 วันราคาชนทั้งสองฝั่งในแท่ง M5 เดียว จึงเลือกฝั่งต่างจาก M1 |

## Test

- `tests/test_alert_strategies.py` (ใหม่, 9 ข้อ):
  - checkpoint ของทั้ง 4 กลยุทธ์โหลดกลับแล้วคำนวณต่อได้เหมือนเดิม
  - กติกาวันของ TPSL: ตั้งคำสั่ง 08:00, ไม่เทรดวันกรอบแคบ, ไม่เทรดวันเสาร์, ยกเลิกเมื่อถึง 21:00
  - engine กับ TPSL: บันทึกแถว Buy/Sell Stop และแจ้งตั้งคำสั่งหนึ่งครั้ง แจ้งเมื่อคำสั่งถูกชนและเมื่อเลื่อน SL เป็นทุน และไม้สมมติปิดที่ TP
  - เปลี่ยนตัวที่เลือกแล้วไม่ทำให้เริ่มคำนวณใหม่
  - เลือกหลายตัวแล้วสัญญาณของ `rsi2` เท่ากับตอนเลือกตัวเดียว และ `conflict: "skip"` ข้ามสัญญาณที่ซ้อนจริง
- `tests/test_alert_engine.py` ปรับเป็น policy v2 และเพิ่มการตรวจ `/strategies`
- `tests/test_telegram_commands.py`: `/status` หลายกลยุทธ์ และ `/signal` แบบ Buy Stop
- ชุดทดสอบทั้งหมด: **294 passed**

ข้อมูลใน test เป็นข้อมูลสังเคราะห์ ไม่ใช่ข้อมูลตลาด

## ข้อจำกัด

- ผลย้อนหลังของทุกตัวยังไม่ใช่หลักฐานว่ามี edge:
  - ไม่มีตัวไหนผ่าน PF ≥ 1.2 แบบ out-of-sample
  - ผลของ TPSL มาจากชุดที่ผู้เผยแพร่คัดมาเอง
- TPSL ตรวจการชนคำสั่งจากแท่ง M5 ที่ปิดแล้ว จึงแจ้งช้ากว่าจริงได้ถึง 5 นาที และไม่รู้ราคาที่ผู้ใช้ได้จริง
- ระบบไม่ส่งคำสั่งซื้อขาย ผู้ใช้ต้องตั้ง Buy/Sell Stop, SL และ TP ใน MT5 เอง
