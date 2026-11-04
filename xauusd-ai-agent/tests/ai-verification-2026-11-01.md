# ผลตรวจ LM Studio integration — 1 พฤศจิกายน 2026

- ทดสอบอัตโนมัติ: **67 passed**, 2.03 วินาที
- มี Starlette TestClient deprecation warning เดิม 1 รายการเรื่อง httpx ไม่ใช่ test failure
- ตรวจการเชื่อมต่อจริงไปยัง LM Studio ที่ผู้ใช้ระบุ ผ่าน `/v1/chat/completions`
- โมเดลที่ขอใช้: `qwen/qwen3.8-27b`
- ข้อมูลทดสอบ: synthetic XAUUSD BUY, entry 2500, SL 2495, TP 2510, RR 2
- Webhook receipt: 5.0 ms; end-to-end จนอ่านผลจากฐานข้อมูล: 22.5 วินาที
- งานเป็น `completed`, attempts 1, prompt `signal-filter-v1`
- ผล `WAIT`, ราคาและ RR ตรงข้อมูลต้นฉบับ
- เหตุผลหลักจากโมเดล: market_structure เป็น null และไม่มีหลักฐานแนวรับแนวต้านที่ยืนยัน
- ไม่บันทึก reasoning_content
- Live test ใช้ SQLite ชั่วคราวและหยุด worker หลังจบ ไม่เพิ่มสัญญาณสมมติในฐานข้อมูลใช้งาน

เวลาที่รายงานมาจากหนึ่งคำขอ ไม่ใช่ load test และไม่ใช่การวัดคุณภาพกลยุทธ์

Tests ใหม่ครอบคลุม structured request/output, ราคาและ RR ถูกแก้, การกลับฝั่ง, การยืนยันเมื่อข้อมูลขาด, malformed/truncated/tool-call response, timeout/HTTP failure, response size, durable queue, retry/backoff, lease recovery, stale result, additive migration, API auth และ Webhook ไม่รอ AI

เปิด AI_ENABLED ใน .env และ migrate ฐานข้อมูลใช้งานแบบเพิ่มตารางแล้ว ยังไม่ได้เปิดบริการค้างไว้ เชื่อม Tunnel หรือ Telegram
