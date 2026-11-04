# XM → LM Studio live integration — 3 พฤศจิกายน 2026

- GET `/v1/models`: HTTP 200 และพบ `qwen/qwen3.8-27b` ตาม configuration เดิม
- การทดสอบครั้งแรก: incomplete_response หลัง 73.5 วินาที; ระบบไม่ยืนยันสัญญาณและไม่บันทึก reasoning ดู `lmstudio-xm-live-2026-11-03-initial.json`
- Probe ขนาดเล็กด้วย `reasoning_effort=none`: HTTP 200, finish_reason=stop, reasoning_tokens=0
- เพิ่ม `AI_REASONING_EFFORT` ใน configuration และตั้งค่า none สำหรับเซิร์ฟเวอร์นี้ เปลี่ยน request version เป็น signal-filter-v4; ยังคง response_format JSON Schema, max_tokens=2048, timeout=90 วินาที, price/side/structure validation และ risk gate
- Live test ด้วย `scripts/test_lm_studio.py --xm`: **ผ่าน 10.8 วินาที** รับ webhook 4.9 ms; analysis=completed; AI=WAIT; risk=passed; effective=WAIT; ราคา/RR ไม่เปลี่ยน
- ใช้ราคาสมมติ Entry 2500/SL2495/TP2510 และฐานข้อมูลชั่วคราวเท่านั้น AI/risk age limit=600 วินาทีเพื่อแยกการเชื่อมต่อจากขอบแท่ง; ข้อจำกัด quote/snapshot 120 วินาทียังคงเดิม Production age limit ยังเป็น 300 วินาที
- ผล JSON อยู่ใน `lmstudio-xm-live-2026-11-03.json` ไม่มี token หรือ reasoning_content
- **ข้อจำกัดที่พบจริง:** เหตุผล WAIT ระบุว่า Entry2500 ไม่อยู่ระหว่าง support2497/resistance2512 ซึ่งผิดจากตัวเลข จึงไม่ใช้ผลนี้อ้างคุณภาพการคัดกรองหรือความถูกต้องของเหตุผล
- ทดสอบอัตโนมัติหลังแก้: **180 passed**, 1 Starlette/httpx deprecation warning (4.69 วินาที)
- รีสตาร์ตบริการ XM ให้ใช้ configuration ใหม่แล้ว โดยคง checkpoint เดิมและไม่ requeue สัญญาณเก่า
- ขณะตรวจ feed เก่าเกิน 2 ชั่วโมง; receiver และ engine ยังทำงานและ engine หยุดที่ feed_stale ตามเกณฑ์ Log MT5 ยืนยันว่า reconnect XM Demo สำเร็จแล้ว แต่ยังไม่มี log EA เริ่ม/ส่ง snapshot หลังเปิดใหม่ จึงขอให้ผู้ใช้แนบ EA ลงกราฟอีกครั้ง ยังไม่ยืนยันการกลับมาของ feed สด ณ จุดตรวจนี้

ตรวจต่อหลังแนบ EA ใหม่: Log ยืนยัน `XMPriceBridge (EURUSD,M5)` เริ่มทำงานเวลา 06:53:56 และส่งได้ถึง receiver แต่ได้ HTTP422 ซ้ำ ตรงกับการแนบบน EURUSD ขณะที่ GoldSymbol ว่าง จึงให้กำหนด GoldSymbol=GOLD ใน Inputs โดยคง allowlist ทองเดิมไว้ ระหว่างรอแก้ engine ยังคง feed_stale และไม่ยืนยันสัญญาณจากข้อมูลผิด symbol

ตัวรับเพิ่ม GET `/mt5/status` ที่ต้องใช้ token และยืนยัน `reason=source_not_allowed`, `broker_symbol=EURUSD`, `server_matches=true` โดยไม่เก็บ payload/token จากนั้นแก้ EA เป็น v1.01: ทั้งค่าเริ่มต้นและค่าว่างใช้ GOLD และพิมพ์ชื่อ source symbol คอมไพล์ผ่าน 0 errors/0 warnings (1,431 ms) วาง binary ใหม่แล้ว รอโหลด EA ใหม่ใน terminal ก่อนยืนยันการกลับมาของ feed

## ผลหลังโหลด EA v1.01

- **GOLD กลับมาส่งสำเร็จ**: `/mt5/status` เป็น accepted; snapshot เวลา 07:02:14 Asia/Vientiane อยู่ในสถานะ fresh อายุ 5.8 วินาที มี M5/H1 อย่างละ 300 แท่ง
- Log ระบุ `Price source symbol: GOLD` แล้ว รองรับการแนบบนกราฟอื่นโดยไม่อ่าน EURUSD เมื่อช่องสัญลักษณ์ว่าง
- Engine กลับเป็น evaluated ใช้ checkpoint เดิมต่อ มี 0 candidates / 0 analyses ณ เวลาตรวจ ไม่มีการเติมสัญญาณย้อนหลัง
- H1 ready=false ณ M5 ที่เพิ่งปิดขอบชั่วโมง เพราะต้องใช้ H1 ที่ปิดก่อนเวลาเปิด M5; H1 ล่าสุดที่เพิ่งปิดจะใช้ได้กับ M5 ถัดไป ADX=12.38 ยังไม่ถึงเกณฑ์20 จึงไม่ปล่อย candidate
- LM Studio ยังตอบ HTTP200 และบริการทั้ง market receiver กับ XM engine เปิดค้างไว้
- ทดสอบหลังเพิ่ม diagnostics: **181 passed**, 1 Starlette/httpx deprecation warning (4.98 วินาที)
