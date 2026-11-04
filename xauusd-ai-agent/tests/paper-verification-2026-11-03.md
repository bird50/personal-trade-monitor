# Paper Trading verification — 3 พฤศจิกายน 2026

- `python -m pytest -q`: **210 passed**, 1 Starlette/httpx deprecation warning (7.50 วินาที)
- Tests เพิ่มครอบคลุม funding ครั้งเดียว, BUY/SELL Bid/Ask, adverse slippage, split commission, unrealized/realized PnL, SL gap, intrabatch price path, tick sequence integrity, stale/expired/pre-activation approvals, restart และ atomic rollback
- ตรวจ daily-loss lock รวม overnight gap, baseline วันถัดไป, จำนวนสถานะสูงสุด, aggregate risk และ notional หลังหัก opening friction, policy เปลี่ยนหยุด entry ใหม่แต่ปิดสถานะเดิมด้วยต้นทุนเดิม
- HTTP integration ใช้ SQLite ชั่วคราวและ mock AI: POST signal → AI/risk → POST ticks → open → TP close → ledger reconciliation พร้อม auth และ duplicate requests
- ตัวอย่าง BUY สมมติ Entry fill2500.25, quantity3.99, TP market fill2509.95: net PnL38.304 USD และ final balance10038.304 USD ตรงกับผลรวม ledger ไม่ใช้ผลนี้เป็นผลตลาดจริง
- XMPaperTicks v1.01 คอมไพล์ใน MetaEditor ผ่าน **0 errors, 0 warnings**, 1,308 ms; มีทั้ง source และ binary ใน terminal MQL5/Experts ไม่มี OrderSend/CTrade/DLL
- สำรองฐานข้อมูลก่อนเริ่มไว้ `data/xm-before-paper-2026-11-03.db`
- เปิดใช้งาน account และ ledger ใน `data/xm-trading.db` แล้ว ตรวจหลังรีสตาร์ต: balance/equity10000 USD, 0 positions, 0 closed trades, ledger1แถวเป็น initial funding ไม่เติมทุนซ้ำ
- Receiver port8001 และ XM/Paper service port8002 เปิดอยู่; สถานะล่าสุด `waiting_for_tick_stream`, valuation_fresh=false, entries_enabled=false
- **ยังไม่ได้ตรวจ tick จริงจาก XMPaperTicks** ณ จุดนี้: รอผู้ใช้แนบ EA บนกราฟเพิ่มหนึ่งกราฟ โดยเก็บ XMPriceBridge เดิมไว้ ระบบจะไม่เปิด entry จาก sampled snapshot แทน ticks และไม่มีการสร้างเทรดสาธิตในบัญชีใช้งาน
- ขอบเขต: simulated_ounce, ต้นทุนตามสมมติฐาน, ไม่มี swap/financing, partial fills หรือ broker lot/margin จริง และไม่ส่งคำสั่งซื้อขายไป XM

## Live tick activation

หลังผู้ใช้แนบ XMPaperTicks: ตรวจเวลา 15:09–15:10 น. Asia/Vientiane รับ tick GOLD จริงสำเร็จและต่อเนื่อง sequence เพิ่มจาก218เป็น496 บัญชีเปลี่ยนเป็น running, valuation_fresh=true, entries_enabled=true และ mark ล่าสุดอายุ7.1วินาที

Balance/Equity10000 USD, PnL0, สถานะเปิด0, ปิด0; ledgerยังมีทุนตั้งต้นเพียง1รายการ ไม่มีการเติมทุนหรือเทรดสาธิตเพิ่ม การอนุมัติเดิมก่อนเปิดบัญชีถูก skipped/before_paper_activation ตามกฎ

Engine และ paper_worker ทำงานอยู่ Snapshot GOLD สด M5/H1อย่างละ300แท่ง ณ จุดตรวจแรก ส่วน LM Studio ที่กำหนดตอบ ConnectTimeout ในรอบนี้ จึงต้องเปิด/แก้การเชื่อมต่อเซิร์ฟเวอร์เพื่อรับ approval ใหม่จาก AI ก่อนเปิด Paper entry ใหม่ การติดตาม/ปิดสถานะจำลองที่มีอยู่ใช้ tick feed โดยไม่ต้องเรียกโมเดล

ตรวจซ้ำหลังผู้ใช้เชื่อม LM Studio เวลา15:14:37น.: `/v1/models` HTTP200 และพบโมเดลที่กำหนด, ai_worker/engine_worker/paper_worker=running, H1 ready=true, account=running, valuation_fresh=true (mark7.9วินาที), policy_matches=true, entries_enabled=true ยอด10000 USD/0สถานะ ยังไม่มี candidate ใหม่ ณ จุดตรวจ คงผล failed/model_unreachable เดิมไว้ในประวัติ ไม่ requeue approval เก่าหรือเปิดย้อนหลัง
