# ผลตรวจ Market Structure v0.2 — 1 พฤศจิกายน 2026

## สำเร็จ

- Pine v0.2 คอมไพล์และรันบน TradingView OANDA:XAUUSD M5
- บันทึกอัปเดตสคริปต์เป็นชื่อ XAUUSD H1 + M5 Signal v0.2; เก็บ v0.1 ในไฟล์สำรอง
- แสดงแนวรับและแนวต้านจาก confirmed pivots บนกราฟ
- Baseline มี 31 trades, Win Rate 41.94%, PF 1.243 และ PnL +30.50 USD เท่าเดิม ไม่ใช่ผลหลัง AI กรอง
- ชุดทดสอบอัตโนมัติ **88 passed** รวมการยืนยัน BUY/SELL ด้วยผลโมเดลจำลอง, WAIT, missing/stale/ranging/opposite structure, broken levels, SL/TP ขัดกับระดับ, future/early pivots, การตรวจ state และ schema version
- ทดสอบ Webhook v2 → SQLite → analysis queue และยืนยันว่า v1/v2 timestamp เดียวกันไม่ชน signal_id
- คงรองรับ v0.1 โดยบังคับ WAIT เมื่อไม่มี structure

## การทดสอบโมเดลจริงรอบแรก: ติดต่อไม่ได้

เรียก `scripts/test_lm_studio.py --v2` ด้วยข้อมูลสมมติและ SQLite ชั่วคราว Webhook ตอบรับได้ใน 5.9 ms แต่ LM Studio ติดต่อไม่ได้ งานจบ `failed`, `error_code=model_unreachable`, `effective_decision=WAIT` หลังประมาณ 75.6 วินาที ไม่มีผลราคาหรือการยืนยัน BUY/SELL ถูกสร้างขึ้น

ตรวจซ้ำด้วย GET `http://172.17.185.101:1234/api/v1/models` แล้ว connection timeout หลังประมาณ 5 วินาที ยังระบุไม่ได้ว่าเซิร์ฟเวอร์หยุด, IP เปลี่ยน หรือเครือข่ายไม่ถึง

## ทดสอบซ้ำตามคำขอผู้ใช้: ผ่าน

รัน `scripts/test_lm_studio.py --v2` อีกครั้งและเชื่อมต่อสำเร็จ:

- โมเดล `qwen/qwen3.8-27b`, prompt `signal-filter-v2`
- Webhook ตอบรับ 36.4 ms; เวลารวมจนอ่านผลจาก SQLite 38.3 วินาที
- สถานะ `completed`, attempts 1, ไม่มี error
- ผลจำลอง `BUY`, Entry 2500, SL 2495, TP 2510, Risk/Reward 2
- ราคาและ Risk/Reward ตรงกับข้อมูลต้นฉบับ ผ่าน schema และ deterministic evidence gate
- เหตุผลอ้างอิง H1 bullish, RSI 55, +DI 28 > -DI 15, higher high 2512 > 2508 และ higher low 2497 > 2490
- Entry อยู่ระหว่าง support 2497 กับ resistance 2512; SL ต่ำกว่า support และ TP ต่ำกว่า resistance
- ใช้ข้อมูลสมมติและ SQLite ชั่วคราวเท่านั้น หยุด worker หลังจบ ไม่มีการส่งคำสั่งซื้อขาย

ผลนี้ยืนยันการเชื่อมต่อและการประมวลผลกรณีทดสอบหนึ่งกรณี ไม่ใช่สัญญาณตลาดปัจจุบันหรือผลประเมิน Win Rate ของ AI

ยังไม่ได้ทดสอบ realtime TradingView webhook, reload/replay consistency หรือ Win Rate หลัง AI กรอง ไม่มีการเปิด Tunnel หรือบริการค้างไว้

มี Starlette TestClient deprecation warning เดิม 1 รายการเรื่อง httpx ในชุดทดสอบ ไม่ใช่ test failure
