# Risk Manager สำหรับสัญญาณจำลอง

> โหมดแจ้งเตือน ([ALERTS.md](ALERTS.md)) ใช้ `config/risk-xm-paper.json` เฉพาะตัวกรองระดับราคา (RR สุทธิ, ระยะ SL/ATR, spread/ATR, tick, อายุสัญญาณ และ source) ผ่าน `level_metrics` ไม่ใช้การคำนวณขนาดไม้หรือกฎบัญชี

ผู้ใช้ยืนยันทุนจำลอง 10,000 USD และเสี่ยงสูงสุด 0.5% ต่อสัญญาณ (งบ 50 USD) ค่าที่เหลือเป็นสมมติฐานเริ่มต้นสำหรับทดสอบซอฟต์แวร์ ไม่ใช่ค่าบัญชีหรือโบรกเกอร์จริง

ใช้นโยบาย `config/risk-paper.json` ผ่าน `RISK_POLICY_PATH` โหลดเมื่อเริ่มแอป หากไฟล์หายหรือกฎไม่ถูกต้อง แอปจะไม่เริ่มทำงาน ไม่มีโหมดข้าม Risk Manager เพื่อยืนยันสัญญาณ

## นโยบาย XM ปัจจุบัน (`config/risk-xm-paper.json`)

บริการ XM (:8002) ใช้ไฟล์นี้แทน `config/risk-paper.json` ค่าที่ต่างจากตารางด้านล่าง (ตั้งแต่ 6 พฤศจิกายน 2026):

| กฎ | ค่า XM |
| --- | --- |
| Symbol / ต้นทาง | `XM:XAUUSD` จาก `XMGlobal-MT5 7` / `GOLD` |
| Net Reward/Risk หลังต้นทุน | อย่างน้อย **1.2** (เดิม 1.5) ตรวจทั้งตอนประเมินและตอน fill |
| โครงสร้างตลาด (`confirmation_blockers`) | **ไม่บังคับ** — `"require_market_structure": false` |
| Spread จำลองขั้นต่ำ / tick ราคา | 0.60 / 0.01 |
| ระยะ SL / M5 ATR14 | **1–12 เท่า** (ตั้งแต่ 8 พฤศจิกายน 2026 · เดิม 1–3) เพื่อให้ stop ที่อิงกรอบราคาแบบ London breakout ผ่านได้ [ผลทดสอบ](tests/tpsl-london-breakout-2026-11-08.md) RSI(2) ใช้ SL 1.8 ATR จึงไม่ได้รับผล |

`require_market_structure` เป็นค่าเลือกได้: ถ้าไม่ใส่ (ค่าเดิมของทุก policy) Risk ยังตรวจ structural blockers เหมือนเดิมและ hash ของ policy เดิมไม่เปลี่ยน ใส่ `false` แล้วข้ามเฉพาะ blocker ของ pivot ส่วนกฎอื่นทั้งหมดยังเหมือนเดิม ถ้าเปิด AI ใช้อีกครั้ง AI ยังต้อง WAIT เมื่อมี blocker ตามกฎใน [AI.md](AI.md) ปัจจุบันบัญชี Paper ใช้ **ยกเว้น AI** (ดูหัวข้อด้านล่าง) จึงไม่เรียกโมเดล

การแก้ไฟล์นี้ทำให้ policy hash ของบัญชี Paper ไม่ตรง ต้องรีสตาร์ตบริการแล้วรับ policy ใหม่ด้วย `scripts/adopt_paper_policy.py` ตาม [PAPER.md](PAPER.md)

## กฎเริ่มต้น

| กฎ | ค่า |
| --- | --- |
| Symbol | OANDA:XAUUSD |
| ทุนจำลอง | 10,000 USD คงที่ |
| งบความเสี่ยงต่อสัญญาณ | 0.5% = 50 USD |
| ระยะ SL / ATR | 1–3 เท่า |
| Net Reward/Risk หลังต้นทุน | อย่างน้อย 1.5 |
| Spread จำลอง | 0.30 หน่วยราคา |
| Slippage จำลอง | 0.05 หน่วยราคาต่อขา |
| Commission จำลอง | 0.10 USD ต่อหน่วยขนาดสถานะ ไป–กลับ |
| Spread / ATR | ไม่เกิน 0.20 |
| มูลค่าการเปลี่ยนราคา 1 หน่วยต่อ quantity 1 | 1 USD สมมติ |
| หน่วย quantity | simulated_ounce ไม่ใช่ lot ของโบรกเกอร์ |
| Quantity step / min / max | 0.01 / 0.01 / 1,000 |
| มูลค่าสถานะสูงสุด | 1 เท่าของทุนจำลอง |
| Tick ราคาจำลอง | 0.001 |
| อายุสัญญาณสูงสุด | 300 วินาที; ยอมรับ clock skew อนาคตไม่เกิน 30 วินาที |

ตรวจ structural blockers ซ้ำด้วย รวมทั้งราคาที่ AI ส่งกลับต้องตรงกับข้อมูลต้นฉบับ ปฏิเสธราคาที่ไม่ลง tick โดยไม่ปัดหรือขยับ Entry/SL/TP ให้ผ่านกฎ

## การคำนวณ

ใช้ Decimal เพื่อปัดขนาดสถานะลงเท่านั้น หลักพื้นฐานคือหารงบเสี่ยงด้วยความเสียหายต่อหน่วยที่จุดหยุดขาดทุน ตาม [ตัวอย่าง position sizing ของ TradingView](https://www.tradingview.com/pine-script-docs/faq/strategies/) รุ่นนี้เพิ่มต้นทุนจำลองและเพดานมูลค่าสถานะ:

```text
distance_to_sl = abs(entry - sl)
distance_to_tp = abs(tp - entry)
cost_per_unit = (spread + 2 × slippage) × value_per_price_unit + commission
loss_per_unit = distance_to_sl × value_per_price_unit + cost_per_unit
reward_per_unit = distance_to_tp × value_per_price_unit - cost_per_unit
net_rr = reward_per_unit / loss_per_unit
risk_budget = equity × risk_percent / 100
notional_cap = equity × max_notional_equity_multiple
raw_quantity = min(risk_budget / loss_per_unit,
                   notional_cap / (entry × value_per_price_unit),
                   max_quantity)
quantity = floor(raw_quantity / quantity_step) × quantity_step
```

ถ้าขนาดหลังปัดต่ำกว่า minimum จะไม่เพิ่มขนาดให้ถึง minimum แต่ปฏิเสธสัญญาณ พร้อมตรวจซ้ำว่า planned loss และ notional ไม่เกินเพดาน

ตัวอย่างข้อมูลสมมติ Entry 2500, SL 2495, TP 2510, ATR 3:

- ต้นทุนไป–กลับ 0.50 USD ต่อหน่วย
- Loss/unit 5.50 USD, reward/unit 9.50 USD, net R/R ≈ 1.727
- งบความเสี่ยง 50 USD รองรับได้ประมาณ 9.09 หน่วย แต่ติด notional cap จึงเหลือ **4 หน่วยจำลอง**
- Planned loss 22 USD, planned reward 38 USD, notional 10,000 USD

Planned loss เป็นผลตามสมมติฐานต้นทุนและราคา SL เท่านั้น ไม่ใช่ขอบเขตการขาดทุนจริงที่รับประกัน กฎ notional ไม่ได้ตรวจเงินคงเหลือสำหรับ margin หรือค่าธรรมเนียมจริง

## ผล AI กับผลยืนยันสุดท้าย

Worker วิเคราะห์ AI แล้วตรวจ Risk Manager ก่อนบันทึกผล AI และรายงาน risk ใน transaction เดียว ภายใต้ lease เดียวกัน

| AI | Risk ณ เวลาที่อ่าน | effective_decision |
| --- | --- | --- |
| BUY/SELL | passed และยังไม่หมดอายุ และ policy ตรงกับปัจจุบัน | ฝั่งที่ AI ยืนยัน |
| WAIT | แม้ risk ผ่าน | WAIT |
| BUY/SELL | rejected, expired, policy_changed หรือ not_evaluated | WAIT |
| failed/pending/processing | ใด ๆ | WAIT |

`GET /signals/<id>/analysis` เพิ่ม `ai_decision`, `risk_status`, `effective_decision`, `confirmed_quantity`, `mode` ขนาดยืนยันเป็น null เมื่อผลสุดท้าย WAIT ต้องใช้ effective_decision แทนการอ่าน result.decision โดยตรง

`GET /signals/<id>/risk` ใช้ API_TOKEN เช่นเดียวกัน คืนผลตรวจ, reason_codes, การคำนวณ, policy snapshot + SHA256, evaluated_at และ expires_at
`status` ใน endpoint นี้คือผลย้อนหลัง ส่วน `current_status` และ `effective_decision` คำนวณตามอายุและ policy ปัจจุบัน ผลผ่านเดิมไม่ใช่สิทธิ์ยืนยันตลอดไป

เปลี่ยน policy แล้วต้องรีสตาร์ตเพื่อใช้นโยบายใหม่ ผลย้อนหลังยังเก็บ audit เดิม แต่หาก hash ไม่ตรงจะคืน WAIT ไม่มีการประเมินงาน completed ซ้ำหรือส่งสัญญาณเดิมใหม่โดยอัตโนมัติ

SQLite schema version 2 เพิ่มตาราง `risk_evaluations` โดยไม่ทิ้งสัญญาณหรือผล AI เดิม ผลเก่าที่ไม่มี risk report จะเป็น not_evaluated และ WAIT

## โหมดยกเว้น AI และ Risk Manager

ใช้ทดลองใน Paper เท่านั้น ค่าเริ่มต้นปิดทั้งสองโหมด เปิด/ปิดจากแถบ **ยกเว้นขั้นตอนกรอง** ใน XM Control (ต้องกดยืนยันสองครั้งตอนเปิด) หรือ `POST /dashboard/bypass` ด้วย `{"ai":true}`, `{"risk":true}` หรือทั้งสองค่า สถานะถูกบันทึกในตาราง `pipeline_bypass` ของ `data/xm-trading.db` ข้ามการเริ่มใหม่ ไม่ได้แก้ไฟล์ใน `config/` จึงไม่เปลี่ยน policy hash ของบัญชี

| สิ่งที่ตรวจ | ปกติ | ยกเว้น AI | ยกเว้น Risk |
| --- | --- | --- | --- |
| เรียก LM Studio / Codex | ✓ | ข้าม · ใช้ฝั่งของสัญญาณ (`provider=bypass`, `model=none`) | ✓ |
| โครงสร้างตลาด (`confirmation_blockers`) | ✓ | ข้าม | ข้ามใน Risk แต่ถ้ายังใช้ AI อยู่ AI ยังต้อง WAIT เมื่อมี blocker |
| Stop/ATR (XM 1–12, OANDA 1–3), spread/ATR, RR สุทธิ ≥ 1.5 (XM 1.2) | ✓ | ✓ | ข้าม |
| Drift ตอน fill, spread ตอน fill, RR สุทธิตอน fill | ✓ | ✓ | ข้าม |
| เพดานขาดทุนรายวัน, จำนวนไม้สูงสุด, ความเสี่ยงรวม, เพดาน notional | ✓ | ✓ | ข้าม |
| Server/symbol, อายุสัญญาณ/quote, ราคาตรง tick, AI ไม่แก้ราคา/ไม่กลับฝั่ง | ✓ | ✓ | ✓ ไม่ยกเว้น |
| Tick ตามลำดับและตามทัน, quote ≤ 30 วินาที, fill อยู่ระหว่าง SL/TP, equity > 0 | ✓ | ✓ | ✓ ไม่ยกเว้น |
| ขนาดไม้ไม่เกิน 0.5% ของ equity ต่อไม้ | ✓ | ✓ | ✓ ยังคุม |
| ปุ่มพักเปิดไม้ใหม่ และการติดตาม SL/TP | ✓ | ✓ | ✓ |

โหมดถูกตรึงตอน worker หยิบงาน เช่นเดียวกับการเลือก AI รายงาน risk บันทึก `bypass` และ `waived_reason_codes` ส่วนสถานะ Paper ที่เปิดบันทึก `bypass` และ `waived_reason_codes` ไว้ใน position เพื่อแยกวิเคราะห์ผลภายหลัง

- ผลที่อนุมัติแบบยกเว้นจะเปิดไม้ได้เฉพาะเมื่อโหมดนั้นยังเปิดอยู่ หากปิดโหมดก่อน fill จะถูกปฏิเสธด้วย `bypass_disabled`
- การเปิดโหมดไม่ทำให้ผลเดิมที่ถูกปฏิเสธหรือ WAIT กลับมาใช้ใหม่ และไม่ผ่อนกฎของ approval ที่ประเมินแบบปกติ
- เมื่อยกเว้น Risk ขนาดไม้คำนวณจาก 0.5% ของ equity โดยไม่มีเพดาน notional จึงอาจใช้มูลค่าสถานะเกินทุน (leverage จำลอง) และไม่มีแบบจำลอง margin
- ผลจากโหมดนี้ใช้ทดลองหรือเก็บข้อมูลเปรียบเทียบเท่านั้น ไม่ได้พิสูจน์ว่ากลยุทธ์ทำกำไรได้

## ขอบเขตของขั้นตอนนี้

เป็น Risk Manager ต่อสัญญาณภายใต้บัญชีและต้นทุนจำลอง ยังไม่มีราคา bid/ask สด การเปลี่ยนราคาหลังรอ AI สเปก lot จริง สถานะบัญชี/position จริง การรวมความเสี่ยงหลายสถานะ ขาดทุนรายวัน หรือการจองงบข้ามหลายสัญญาณ
รายงานระบุรายการเหล่านี้ใน `not_checked` อย่างชัดเจน ทุนจำลองไม่เปลี่ยนตามกำไรขาดทุนและการผ่านแต่ละสัญญาณไม่ลดงบของสัญญาณถัดไป ต้องเพิ่มบัญชีรายการและผล Paper Trading ก่อนใช้กฎระดับพอร์ต

ไม่มีคำสั่งซื้อขายหรือ Telegram และไม่แก้ quantity=1 ใน Pine baseline จึงไม่ใช่ผล Backtest ของระบบคำนวณขนาดสถานะนี้

## ทดสอบ

```sh
.venv/bin/python -m pytest -q
.venv/bin/python scripts/test_lm_studio.py --v2
```

Live integration test ใช้ข้อมูลสมมติและฐานข้อมูลชั่วคราว พร้อมเพิ่มอายุ AI/risk เป็น 600 วินาทีเฉพาะไฟล์ทดสอบชั่วคราว เพื่อไม่ให้ขอบแท่งรบกวนการทดสอบการเชื่อมต่อ นโยบายใช้งานยังเป็น 300 วินาที
