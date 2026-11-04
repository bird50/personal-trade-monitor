# Cloudflare Tunnel — 2 พฤศจิกายน 2026

## ผ่านแล้ว

- ติดตั้ง `cloudflared` 2026.10.7 ผ่าน Homebrew
- เปิด FastAPI ด้วย `scripts/run_local.sh`: database=ok, ai_worker=running
- เปิด Quick Tunnel ไปยัง `http://127.0.0.1:8000`
- GET `/health` ผ่าน HTTPS ของ Tunnel สำเร็จ
- GET `/signals` โดยไม่มี token ได้ HTTP 401
- เปิด TradingView Desktop ซึ่งล็อกอินอยู่ และเพิ่มสคริปต์ที่บันทึกไว้ `XAUUSD H1 + M5 Signal v0.2` ลงกราฟ OANDA:XAUUSD M5
- เตรียม Alert โดยเลือกเฉพาะ `alert() function calls only`

## จุดที่ติด

เมื่อเปิด Webhook URL ทาง TradingView แสดงหน้าอัปเกรด: บัญชีปัจจุบัน Basic ไม่รองรับ Webhook และแนะนำ Essential จึงยังไม่ได้สร้าง Alert หรือส่ง Webhook จาก TradingView จริง ไม่ได้ซื้อหรือสมัครแผนใด

การทดสอบ POST ด้วย token จริงผ่าน Tunnel ถูก automatic approval review ปฏิเสธก่อนรัน เพราะเป็นการส่ง credential ใน URL ไปยัง Cloudflare จึงไม่มีผลทดสอบ authenticated POST ผ่าน Tunnel ในรอบนี้

ไม่ได้เพิ่มข้อมูลทดสอบลงฐานข้อมูลใช้งาน และไม่ได้ทดสอบ LM Studio จริงในรอบนี้ สถานะ worker running ไม่ยืนยันว่าโมเดลตอบได้

หลังตรวจเสร็จ ปิด receiver และ Tunnel แล้ว ยืนยันว่าไม่มี listener บน port 8000 หรือ process `cloudflared tunnel` ที่เปิดทดสอบค้างอยู่

## ทำต่อ

1. ใช้บัญชี TradingView ที่รองรับ Webhook และเปิด 2FA
2. เปิด receiver และ Quick Tunnel ใหม่; URL ชั่วคราวต้องใช้ค่าที่ได้ในรอบใหม่
3. ตั้ง Webhook URL เป็น `https://<hostname>/webhook/tradingview/<WEBHOOK_TOKEN>` จาก `.env` โดยไม่ใส่ token ลง Pine หรือรายงาน
4. สร้าง Alert ของ v0.2 บน OANDA:XAUUSD M5 เลือกเฉพาะ alert() calls
5. รอสัญญาณใหม่ ตรวจ Alert Log เทียบ `/signals`, analysis และ risk

เก็บ token และ URL เต็มเป็นความลับ ปิด access log ของ receiver และกรอง log ของ Tunnel ไม่ให้แสดง request path
