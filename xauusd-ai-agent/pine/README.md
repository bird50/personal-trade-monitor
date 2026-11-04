# Pine Script — ขั้นตอนที่ 2

สถานะ: รุ่นปัจจุบัน v0.2 เพิ่ม Market Structure และแนวรับแนวต้านจาก confirmed pivots คอมไพล์ผ่านบน TradingView เมื่อ 1 พฤศจิกายน 2026 บน OANDA:XAUUSD M5 ดูรายละเอียดใน `structure-v0.2.md` และผล baseline เดิมใน `backtest-2026-11-01.md`
ค่าด้านล่างเป็นสมมติฐานเริ่มต้นเพื่อทดสอบ ไม่ใช่ค่าที่ผ่านการปรับหรือรับรองผลตอบแทน

## กฎสัญญาณ

ใช้ XAUUSD แบบแท่งราคาปกติบน M5 เท่านั้น ประเมินเมื่อแท่งปิด และเปิดสถานะจำลองได้ครั้งละหนึ่งสถานะ ไม่มีการเพิ่มไม้หรือกลับด้านระหว่างมีสถานะ

| เงื่อนไข | BUY | SELL |
| --- | --- | --- |
| H1 แท่งที่ปิดแล้ว | Close > EMA 50 > EMA 200 | Close < EMA 50 < EMA 200 |
| M5 | Close ตัดขึ้น EMA 20 | Close ตัดลง EMA 20 |
| RSI 14 | 50 ≤ RSI < 70 | 30 < RSI ≤ 50 |
| ADX (DI 14, smoothing 14) | ≥ 20 และ +DI > −DI | ≥ 20 และ −DI > +DI |
| Entry อ้างอิง | ราคาปิดแท่งสัญญาณ | ราคาปิดแท่งสัญญาณ |
| SL | Entry − 1.5 × ATR 14 | Entry + 1.5 × ATR 14 |
| TP | Entry + 2 × ระยะ SL | Entry − 2 × ระยะ SL |

ระยะ SL/TP ปัดตาม tick size และอย่างน้อยหนึ่ง tick ค่า Risk/Reward ใน JSON คำนวณจากราคาหลังปัดจริง ระดับถูกตรึงเมื่อเกิดสัญญาณ ไม่เลื่อนตาม ATR ภายหลัง รอข้อมูล H1 อย่างน้อย 200 แท่งตามค่าเริ่มต้นก่อนเริ่มส่งสัญญาณ

ข้อมูล H1 ใช้ `[1]` ร่วมกับ `barmerge.lookahead_on` ทุกค่า ไม่ใช้ H1 ที่ยังเปิดอยู่ จึงไม่มี future leak จาก lookahead และไม่เปลี่ยนตาม H1 ระหว่างแท่ง การแก้ข้อมูลจากผู้ให้บริการหรือเปลี่ยนการตั้งค่ายังทำให้ผลย้อนหลังเปลี่ยนได้

## ตรวจคอมไพล์และติดตั้งบนกราฟ (ขั้นตอนถัดไปหลังได้รับอนุญาต)

1. เปิดกราฟ XAUUSD จากผู้ให้บริการที่ต้องการทดสอบ เลือก 5 นาทีและแท่งปกติ
2. เปิด Pine Editor วางเนื้อหา `xauusd_strategy.pine` แล้ว Save / Add to chart
3. ตรวจข้อผิดพลาดของ compiler ก่อนเปิด Strategy Tester
4. รักษา Recalculate: On every tick / After order is filled เป็นปิด และ Fill orders: On bar close เป็นเปิดตามซอร์ส
5. ยืนยัน commission, slippage, ขนาดสัญญาและทุนตามโบรกเกอร์ก่อนประเมินผล

ค่า Properties เริ่มต้นคือทุน 10,000, ขนาด 1 หน่วยตามนิยาม symbol, commission 0.01% ต่อการ fill และ slippage 2 ticks เป็นค่า placeholder ไม่ใช่ต้นทุนจริงของโบรกเกอร์ และยังไม่ได้จำลอง spread แยกต่างหาก ขนาด 1 หน่วยไม่ใช่การเสี่ยง 1% ของทุน

คำสั่งจำลองประมวลผลที่ราคาปิด รวม slippage ตาม Properties ดังนั้นราคา fill และ Risk/Reward ที่เกิดขึ้นจริงอาจต่างจาก entry อ้างอิงใน Alert โดยเฉพาะเมื่อมี gap ไม่ควรตีความ Alert ว่าเป็นการยืนยันราคา fill ในตลาดจริง หาก SL และ TP ถูกแตะในแท่งเดียวกัน ผลขึ้นกับ broker emulator และข้อมูลภายในแท่ง ควรตรวจ Bar Magnifier ในขั้น Backtest

## Alert สำหรับเชื่อมระบบในขั้นตอนหลัง

เลือกกลยุทธ์นี้ใน Condition และเลือก **alert() function calls only** เพื่อรับเฉพาะสัญญาณตอนแท่งปิด ไม่รวม order-fill alerts ซึ่งซอร์สปิดไว้ด้วย ไม่ต้องสร้าง Alert หรือเชื่อม Webhook ในขั้นตอนนี้

JSON มี `schema_version`, `strategy_version`, `signal_id`, `symbol`, `timeframe`, เวลา Unix milliseconds, `side`, Entry/SL/TP, Risk/Reward, ข้อมูล H1/M5 และกรอบ high/low ของ M5 ก่อนหน้า 20 แท่ง

- `signal_id` ของ v0.2 มาจาก symbol + timeframe + เวลาปิดแท่ง + side + `:0.2` สำหรับให้ receiver ป้องกันข้อมูลซ้ำและแยกจาก v0.1
- `context.prior_range_low/high` เป็นเพียงกรอบราคา ไม่ใช่แนวรับแนวต้านที่ผ่านการยืนยัน ส่วน `market_structure` ของ v0.2 มี pivot evidence ตาม `structure-v0.2.md`
- กลยุทธ์นี้กรองตามสถานะจำลองของ Pine แม้ AI ปฏิเสธสัญญาณ Pine ยังถือสถานะจำลองต่อจน SL/TP จึงต้องใช้ candidate stream เดียวกันเมื่อเปรียบเทียบ AI กับ baseline ในขั้นประเมิน
- มี FastAPI receiver พร้อมตรวจ token และ schema แล้ว แต่ยังไม่เชื่อม public Tunnel หรือ Alert จริง
- หลังแก้ซอร์สหรือ Inputs ต้องสร้าง Alert ใหม่ เพราะ TradingView ใช้ snapshot ตอนสร้าง Alert

## แผนตรวจในขั้น Backtest (ยังไม่ดำเนินการ)

- ยืนยันว่าคอมไพล์ได้บน Pine v6 และรันบน XAUUSD M5
- ตรวจ BUY/SELL, ความคงที่ของ SL/TP, การไม่เพิ่มไม้ และการไม่ส่งก่อนแท่งปิด
- ตรวจ H1 เทียบกราฟ H1 และเทียบสัญญาณหลัง reload
- ตรวจ JSON จาก realtime alert ว่า parse ได้ ราคาตรงกราฟ และไม่มีข้อความซ้ำ
- ทดสอบช่วงปรับกลยุทธ์และช่วงนอกตัวอย่างแยกกัน พร้อมต้นทุนตามโบรกเกอร์
- บันทึก Win Rate, Profit Factor, Maximum Drawdown, จำนวน BUY/SELL และผลตอบแทนเฉลี่ย โดยยังไม่รวม AI

## เอกสารอ้างอิง

- [TradingView: Higher timeframe data](https://www.tradingview.com/pine-script-docs/concepts/other-timeframes-and-data/)
- [TradingView: Strategies](https://www.tradingview.com/pine-script-docs/concepts/strategies/)
- [TradingView: Alerts](https://www.tradingview.com/pine-script-docs/concepts/alerts/)
