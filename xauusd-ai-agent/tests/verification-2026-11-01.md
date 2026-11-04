# ผลตรวจ FastAPI + SQLite — 1 พฤศจิกายน 2026

- Python 3.11.16 บน Mac Apple Silicon
- `python -m pytest -q`: **42 passed** (1.65 วินาที)
- มี deprecation warning 1 รายการจาก Starlette TestClient ที่แนะนำย้ายจาก httpx ไป httpx2 ในอนาคต ไม่ใช่ test failure
- `python -m pip check`: No broken requirements found
- HTTP จริงผ่าน Uvicorn บน loopback: health, insert, deduplication และ authenticated read ผ่าน
- HTTP insert ของข้อมูลจำลองหนึ่งคำขอใช้เวลา 2.5 ms ในเครื่องนี้ ไม่ใช่ผล load test หรือการรับรอง latency
- Smoke test ใช้ SQLite ชั่วคราวและหยุด server หลังจบ
- ฐานข้อมูลใช้งาน `data/trading.db` initialize แล้วและมี 0 signals
- `.env` สร้างด้วย token สุ่มแยกสิทธิ์รับ/อ่าน ไม่แสดง token ในรายงาน

ตรวจครอบคลุม: BUY/SELL, restart persistence, concurrent delivery 16 ครั้งได้แถวเดียว, duplicate conflict, auth ทั้งสองช่องทาง, schema strict, invalid/non-finite JSON, stale/future timestamps, OHLC/SL/TP/RR, symbol allowlist, payload size รวม chunked, rate limit, pagination bounds และ SQLite write lock

ยังไม่ได้ทดสอบ TradingView → public HTTPS → receiver, ไม่ได้เปิด Tunnel, ไม่ได้เชื่อม AI หรือ Telegram และไม่ได้เปิดบริการค้างไว้
