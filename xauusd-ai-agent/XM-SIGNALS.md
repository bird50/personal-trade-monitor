# สัญญาณ XM จากราคา MT5

เส้นทางปัจจุบัน: `XM Demo GOLD → EA → market receiver :8001 → Python engine + AI/risk API :8002`

Engine ใช้ชื่อ `XM:XAUUSD`, schema 3 และ strategy `xm-0.1` เก็บใน `data/xm-trading.db` ส่วนราคาต้นทางยังอยู่ `data/mt5-market.db` สัญญาณ OANDA และ policy เดิมแยกอยู่เหมือนเดิม

## กฎของ xm-0.1

ใช้ค่าจาก Pine v0.2 เป็นจุดเริ่มต้น:

| กฎ | BUY | SELL |
| --- | --- | --- |
| H1 ที่ปิดก่อน/ตรงเวลาเปิด M5 | Close > EMA50 > EMA200 | Close < EMA50 < EMA200 |
| M5 ปิดแล้ว | Close ตัดขึ้น EMA20 | Close ตัดลง EMA20 |
| RSI14 | 50 ≤ RSI < 70 | 30 < RSI ≤ 50 |
| ADX14 | ≥20 และ +DI > −DI | ≥20 และ −DI > +DI |
| SL | Entry − 1.5×ATR14 | Entry + 1.5×ATR14 |
| TP | 2 เท่าของระยะ SL | 2 เท่าของระยะ SL |

ระดับราคาปัดตาม tick 0.01 แบบ half-up; ไม่ต่ำกว่าระยะหนึ่ง tick Entry ต้องตรงกับราคาปิดของต้นทางเพื่อผ่าน schema/risk จึงไม่มีการขยับราคาเพื่อให้ผ่านเกณฑ์

EMA ตั้งต้นด้วยราคาค่าแรกและใช้ alpha 2/(n+1) ส่วน ATR/RSI/DMI/ADX ใช้ Wilder smoothing (alpha 1/n ตั้งต้นด้วยค่าเฉลี่ย n ตัวแรก) ข้อมูลแบนราบที่ gain/loss เป็นศูนย์ทั้งคู่ให้ RSI=50 และ DMI เป็นศูนย์ ไม่สร้าง crossover บนราคาแบน

เก็บ indicator state ต่อเนื่องใน SQLite จึงไม่เริ่ม EMA ใหม่จากหน้าต่างราคาที่เลื่อนทุก 15 วินาที ใช้ประวัติแรก 300 แท่งตั้งต้นโดย **ไม่ปล่อยสัญญาณย้อนหลัง** แล้วประเมินเฉพาะ M5 ใหม่ ตรวจ H1 อย่างน้อย 200 แท่ง และงด candidate หาก H1 cache ล้าหลังเวลาเปิด M5 ตั้งแต่หนึ่งชั่วโมงขึ้นไป

Pivot ใช้ซ้าย 3/ขวา 3 ยืนยันเมื่อแท่งขวาสุดปิด เก็บ high/low อย่างละสองจุดและอายุสูงสุด 120 แท่ง คงกฎ active level และ structural blockers ของระบบเดิม รุ่น XM ใช้ pivot ที่สูง/ต่ำกว่าทุกแท่งข้างเคียงอย่างเคร่งครัด หากราคาเสมอกันจะไม่ยืนยัน pivot

มี **candidate lock** เพื่อคงแนวทางเปิด candidate ครั้งละหนึ่งชุด เมื่อเกิด candidate จะล็อก SL/TP แม้ AI เลือก WAIT แล้วปลดล็อกเมื่อ OHLC ของแท่งถัดไปแตะ SL หรือ TP การปลดล็อกนี้ใช้ควบคุมชุด candidate เท่านั้น ไม่คำนวณ fill/PnL ไม่รับรองลำดับ intrabar ส่วนบัญชีและการเปิด–ปิดตาม ticks เพิ่มแยกใน [Paper Trading](PAPER.md)

## ความต่างจาก Pine ที่ต้องทราบ

ชื่อเวอร์ชัน XM แยกชัดเจน เพราะราคามาจากคนละผู้ให้บริการ ประวัติตั้งต้น 300 แท่งต่างจากประวัติทั้งหมดบน TradingView และยังไม่ได้ตรวจเทียบ indicator/สัญญาณกับ Pine บน OHLC ชุดเดียวกันทุกแท่ง การทดสอบสูตรและ causality ผ่านแล้ว แต่ยังไม่อ้างว่าได้ผล Backtest หรือ candidate stream เท่ากับ Pine เดิมทุกกรณี

Candidate lock ไม่มี margin, slippage ต่อ fill หรือ broker emulator ของ TradingView จึงใช้สำหรับงานวิจัยชุด XM ของตัวเอง ยังไม่มีข้อมูลประสิทธิภาพหรือ Win Rate หลัง AI กรอง

## เวลาและหลักฐานต้นทาง

- `bar_close_ms` ระดับบนสุดเป็น UTC ของ **event ล่าสุด** แปลงด้วย offset ที่สังเกต ณ snapshot นั้น
- สำหรับ schema 3 เวลา `h1.bar_close_ms` และเวลา pivot เป็น **broker server wall-clock milliseconds** ตาม `source.context_time_basis` ใช้เทียบลำดับกับ `source.bar_close_server_ms` ไม่ใช่ UTC
- ไม่แปลงประวัติทั้งหมดด้วย offset ปัจจุบัน จึงไม่สมมติ timezone/DST ย้อนหลัง
- หาก offset เปลี่ยนจาก checkpoint จะหยุดที่ `version_or_offset_changed_reset_required` เพื่อให้ตรวจ timezone และเริ่ม baseline ใหม่อย่างชัดเจน
- เก็บ server, broker_symbol, bid/ask, tick size, quote timestamp, snapshot timestamp, input SHA256 ไว้ในสัญญาณ และเก็บ snapshot เต็มพร้อม state ก่อนประมวลผลใน `xm_candidate_inputs` เพื่อ audit หลัง archive ของ receiver ถูกหมุนออก

## AI และ Risk Manager

บันทึก checkpoint, signal, pending analysis และหลักฐานต้นทางใน transaction เดียว ป้องกัน crash ระหว่างบันทึกและป้องกันการปล่อยสัญญาณซ้ำหลังรีสตาร์ต Worker ใช้ LM Studio URL/โมเดลที่กำหนดใน `.env` เท่านั้น ไม่เลือก endpoint หรือโมเดลสำรองอัตโนมัติ

Policy สำหรับ XM คือ `config/risk-xm-paper.json`: pin server `XMGlobal-MT5 7`, symbol `GOLD`, tick 0.01 และคงทุนจำลอง 10,000 USD/เสี่ยงสูงสุด 0.5% ต่อสัญญาณ หน่วยขนาดเป็น `simulated_ounce` เช่นเดิม ไม่ใช่ lot หรือสเปกบัญชี XM

Spread ที่ใช้คิดต้นทุนคือ **ค่าสูงกว่าระหว่าง spread จาก bid/ask ของ snapshot กับ floor จำลอง 0.60** ส่วน slippage/commission ยังเป็นสมมติฐานจำลอง หาก spread/ATR สูง, net R/R ต่ำ, หลักฐาน pivot ไม่ครบ, tick/source ผิด, AI เปลี่ยนราคา หรือข้อมูลเก่า จะไม่ยืนยัน candidate

Risk ต้องใช้ quote และ snapshot ใหม่ไม่เกิน 120 วินาที ผลผ่านหมดอายุไม่เกินเวลา quote+120 วินาทีและอายุสัญญาณ 300 วินาที การอ่าน API ภายหลังคำนวณ expiry ซ้ำ จึงไม่ยืนยันผลที่เคยผ่านตลอดไป

Risk Manager ต่อสัญญาณใช้ spread ที่สังเกตตอน snapshot และยังไม่ตรวจ lot/contract spec จริง, margin หรือ actual equity ส่วน [Paper Trading](PAPER.md) เพิ่มการตรวจราคา/ต้นทุน ณ fill, ทุนจำลอง, สถานะเปิดรวม และขาดทุนรายวันอีกชั้น ก่อนเปิดสถานะจำลอง ไม่มีการส่งคำสั่งซื้อขายไปโบรกเกอร์

## เปิดและตรวจสถานะ

เปิด MT5 กับ EA และ receiver เดิมไว้ แล้วเพิ่มอีก Terminal จากโฟลเดอร์โปรเจกต์:

```sh
sh scripts/run_xm.sh
```

ตรวจ:

```sh
.venv/bin/python scripts/xm_status.py
```

API ที่ `127.0.0.1:8002` ใช้ `API_TOKEN` จาก `.env` เพื่ออ่าน `/xm/status`, `/signals`, `/signals/<id>/analysis`, `/signals/<id>/risk` ส่วน `/health` ไม่ต้องใช้ token และยืนยัน worker ทำงานเท่านั้น **ไม่ยืนยันว่า LM Studio ออนไลน์**

สถานะ `evaluated` กับ candidate_side=null หมายถึงประเมินแท่งใหม่แล้วแต่ไม่มี candidate ไม่ได้สร้างสัญญาณ WAIT ปลอม หากมี candidate จริงจะเป็น `signal_queued` และเห็นผล AI/risk ใน endpoint ของ record นั้น

ไม่เปิด Tunnel และไม่ต้องมี TradingView Alert เพื่อใช้เส้นทางนี้

## กรณีหยุดอย่างปลอดภัย

`feed_stale`, `source_mismatch`, `tick_policy_mismatch`, `insufficient_history`, `closed_history_revised_reset_required`, `missing_overlap_reset_required` และ `out_of_order_feed` ไม่เลื่อน checkpoint หรือปล่อยสัญญาณจากข้อมูลผิด หาก history ถูกแก้หรือขาดช่วงยาวเกินหน้าต่างที่ทับกับ checkpoint ต้องหยุดตรวจต้นทางและเริ่ม baseline ใหม่ โดยเก็บฐานข้อมูลเดิมเป็นหลักฐานก่อน ห้ามล้าง checkpoint เพื่อให้ข้ามข้อผิดพลาดโดยไม่ตรวจ

## ผลตรวจ 2 พฤศจิกายน 2026

- `.venv/bin/python -m pytest -q`: **178 passed**, 1 deprecation warning ดู [รายงานตรวจ](tests/xm-verification-2026-11-02.md)
- ทดสอบค่าตั้งต้น/สูตร, ขอบเขต RSI/ADX, pivot ยืนยันหลังขวา 3 แท่ง, BUY/SELL จาก OHLC โดยไม่ mock กฎ entry
- ทดสอบ H1 ที่ขอบชั่วโมง, split/restart consistency, candidate lock, deduplication, rollback ระหว่าง dispatch/checkpoint, history revision, offset เปลี่ยน, stale feed/quote และ source/tick policy
- ทดสอบ candidate → mock LM Studio → AI validation → risk → effective_decision ทั้งกรณีผ่านและ WAIT โดยใช้ SQLite ชั่วคราว
- เปิดระบบจากราคาจริงและตรวจคำนวณแท่งใหม่หลังรีสตาร์ตได้ โดยไม่เพิ่มข้อมูลสมมติในฐานข้อมูลใช้งาน
- LM Studio ที่ตั้งไว้ `http://172.17.185.101:1234` เชื่อมต่อ timeout ในรอบนี้ จึงยังไม่ยืนยัน live inference ของ XM ต้องเปิดเซิร์ฟเวอร์/โมเดลตาม `.env` ก่อน

อัปเดต 3 พฤศจิกายน 2026: เชื่อม LM Studio จริงได้แล้วและทดสอบ schema 3 → AI → risk ด้วยข้อมูลสมมติและฐานข้อมูลชั่วคราวผ่านใน 10.8 วินาที หลังตั้ง `AI_REASONING_EFFORT=none` ผล AI=WAIT และ risk=passed; ราคา/RR คงเดิม ไม่ปรับข้อจำกัด quote freshness ในการทดสอบ และไม่เพิ่มข้อมูลทดสอบในฐานข้อมูลใช้งาน เหตุผลบางข้อของ AI ขัดกับตัวเลข จึงเป็นผลยืนยันการเชื่อมต่อ ไม่ใช่หลักฐานความแม่นยำ ดู [รายงาน](tests/lmstudio-xm-live-2026-11-03.json)

อ้างอิงกฎตั้งต้น: [Pine v0.2](pine/xauusd_strategy.pine), [structure gate](pine/structure-v0.2.md), [Pine built-ins](https://www.tradingview.com/pine-script-docs/language/built-ins/)
