# XM signal engine — 2 พฤศจิกายน 2026

- `python -m pytest -q`: **178 passed**, 1 Starlette/httpx deprecation warning (5.09 วินาที)
- ทดสอบใหม่ 32 รายการครอบคลุม indicator seeds/reference values, entry boundaries, BUY/SELL จาก OHLC โดยไม่ mock กฎ, pivot causality, H1 [1] alignment, state/restart consistency, candidate lock, atomic queue/checkpoint, bad feed/source/tick/time, AI/risk mock integration และ service/API auth
- Policy hash และ payload hash ของ OANDA เดิมคงความเข้ากันได้
- เปิด XM service ที่ loopback `127.0.0.1:8002` พร้อม engine/AI worker; ใช้ฐานข้อมูล `data/xm-trading.db` และ XM policy แยก
- ตรวจราคาจริงเวลา 23:26 น. Asia/Vientiane: feed fresh ประมาณ 15 วินาที, engine=evaluated, engine_worker=running, H1 ready=true
- ประเมิน M5 ใหม่จริงแล้ว 2 แท่งหลัง bootstrap; มี 0 candidates / 0 analyses ณ จุดตรวจ ไม่เพิ่มสัญญาณสมมติในฐานข้อมูลใช้งาน
- รีสตาร์ต service ระหว่างตรวจและใช้ checkpoint เดิมต่อได้ ไม่มี replay ของสัญญาณย้อนหลัง
- ทดสอบ LM Studio ที่กำหนด `http://172.17.185.101:1234/v1/models` สองครั้ง ได้ ConnectTimeout จึง **ยังไม่ได้ทดสอบ live AI inference ของ XM** ไม่เปลี่ยน IP/โมเดลและไม่เลือก provider สำรอง
- ขอบเขต: candidate analysis เท่านั้น ยังไม่มี Telegram, fills/PnL ledger, lot จริง, aggregate risk หรือ daily-loss control
- ไม่ได้ยืนยันความเท่ากันทุกแท่งกับ Pine หรืออ้าง Win Rate; ใช้ strategy version `xm-0.1` แยกจาก Pine v0.2 ตาม [คู่มือ](../XM-SIGNALS.md)
