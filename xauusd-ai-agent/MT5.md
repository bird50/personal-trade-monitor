# XM Demo → MT5 บน Mac → Python

เตรียม bridge รุ่นแรกสำหรับ **รับราคาเท่านั้น**: EA ส่ง bid/ask และแท่งปิด M5/H1 อย่างละ 300 แท่งทุก 15 วินาที เข้า FastAPI ที่ `127.0.0.1:8001` และเก็บใน `data/mt5-market.db` แยกจากฐานข้อมูลสัญญาณเดิม

ตัวรับราคา port 8001 ทำหน้าที่รับราคาเท่านั้น เพิ่ม [ตัวสร้างสัญญาณ XM และการเชื่อม AI/Risk](XM-SIGNALS.md) เป็นบริการแยกที่ port 8002 แล้ว ใช้ชื่อ XM และ policy ของตัวเอง ยังไม่มี Telegram หรือบัญชีสถานะ Paper Trading และยังต้องตรวจเทียบกับ Pine บนข้อมูลชุดเดียวกันก่อนอ้างผลเท่ากัน

## 1. ติดตั้งและล็อกอิน MT5

ใช้ตัวติดตั้งจาก [คู่มือ MetaQuotes ทางการ](https://www.metatrader5.com/en/terminal/help/start_advanced/install_mac) ซึ่งจัดเตรียม Wine บน Mac ผู้ใช้เลือกติดตั้งด้วยตนเอง

ล็อกอินบัญชี **Demo** บนเซิร์ฟเวอร์ `XMGlobal-MT5 7` ด้วยข้อมูลของคุณในหน้าต่าง MT5 โดยตรง ไม่ต้องส่งรหัสผ่านให้ Python หรือใส่ในแชต โค้ด bridge ไม่เก็บหรือส่งเลขบัญชี รหัสผ่าน ยอดเงิน หรือประวัติการซื้อขาย

เปิด Market Watch แล้วหาสัญลักษณ์ทองที่บัญชีมีจริง เช่น `GOLD`, `GOLD#`, `GOLDmicro` หรือ `XAUUSD` ชื่อข้างต้นเป็น allowlist เริ่มต้น ไม่ใช่การยืนยันว่าสัญลักษณ์ทุกชื่อมีบนบัญชีนี้ หากชื่อจริงต่างออกไป ให้แก้ `MT5_SYMBOLS` ใน `.env.mt5` แล้วรีสตาร์ต receiver

## 2. เปิด Python receiver

จากโฟลเดอร์โปรเจกต์:

```sh
.venv/bin/python scripts/init_mt5.py
sh scripts/run_mt5_bridge.sh
```

`init_mt5.py` สร้าง `.env.mt5` และ `mt5/bridge-token.txt` สิทธิ์ 0600 โดยไม่แสดง token และไม่เขียนทับไฟล์เดิม ทั้งสองไฟล์ถูกละเว้นใน Git แยก token จากระบบ TradingView เดิม

เปิด `http://127.0.0.1:8001/health` ต้องได้ `status: ok` และ `mode: market_data_only` ผลนี้ยืนยันเฉพาะ receiver พร้อม ไม่ได้ยืนยันว่ารับราคาจาก MT5 แล้ว

## 3. ใส่ EA และ token ใน MT5

ใน MT5 เลือก **File → Open Data Folder** เพื่อใช้ data folder ของ terminal ที่เปิดจริง จากนั้น:

1. คัดลอก `mt5/XMPriceBridge.mq5` ไปยัง `MQL5/Experts/`
2. คัดลอก `mt5/bridge-token.txt` ไปยัง `MQL5/Files/` เก็บไฟล์นี้เป็นความลับ
3. เปิดไฟล์ EA ด้วย MetaEditor และกด Compile ตรวจว่ามี 0 errors
4. ที่ **Tools → Options → Expert Advisors** อนุญาต WebRequest ไปยัง `http://127.0.0.1:8001`
5. เปิดกราฟทองชื่อจริงของ XM บน M5 แล้วลาก `XMPriceBridge` จาก Navigator ลงกราฟ
6. ใช้ `ExpectedServer=XMGlobal-MT5 7`, `HistoryBars=1000` (ค่าเริ่มต้นของ EA คือ 300 ซึ่งต้องรออุ่นเครื่องหลายวัน ดู [ALERTS.md](ALERTS.md#การอุ่นเครื่อง-indicator)), `PollSeconds=15`, `GoldSymbol=GOLD`

EA **v1.01** กำหนด `GoldSymbol=GOLD` เป็นค่าเริ่มต้นและเมื่อช่องว่าง โดยไม่ขึ้นกับ symbol ของกราฟ ส่วน v1.00 เดิมเมื่อช่องว่างจะอ่าน symbol ของกราฟ หากแนบอยู่บน EURUSD ตัวรับจะปฏิเสธ HTTP422 ตาม symbol allowlist จึงแก้ใน v1.01 และเพิ่ม log `Price source symbol` เพื่อระบุแหล่งราคาจริง หลังเปลี่ยนไฟล์ `.ex5` ให้นำ EA เดิมออกแล้วแนบใหม่เพื่อโหลดโค้ดใหม่ ไม่ใช่เพียง Refresh รายการ Navigator

EA ตรวจบัญชี Demo และเซิร์ฟเวอร์ทั้งตอนเริ่มและก่อนส่งทุกครั้ง หากเปลี่ยนไปบัญชีเงินจริงจะหยุดส่งข้อมูล EA ไม่มีฟังก์ชันส่งคำสั่งเทรด ไม่มี DLL และปลายทาง HTTP ถูกกำหนดตายตัวเป็น loopback

รอ MT5 ดาวน์โหลดประวัติ M5 และ H1 ครบ 300 แท่งต่อกรอบเวลา หากยังไม่ครบ Experts จะแจ้ง `Waiting for complete M5/H1 history.`

## 4. ตรวจข้อมูลเข้า

เปิด Terminal อีกหน้าต่าง จากโฟลเดอร์โปรเจกต์:

```sh
.venv/bin/python scripts/mt5_status.py GOLD
```

เปลี่ยน `GOLD` ให้ตรงกับชื่อจริง คำสั่งอ่าน token จาก `.env.mt5` โดยไม่แสดงออกมา ผลที่ต้องเห็นคือ `fresh`, จำนวน M5/H1 และ bid/ask ส่วน Experts ใน MT5 ต้องขึ้น `Price snapshot accepted` หากไม่มีข้อมูลจะแจ้ง `Receiver ready; no MT5 data received yet.`

Mac ต้องเปิด MT5 กับ receiver ค้าง มีอินเทอร์เน็ตและไม่พักเครื่อง มือถือใช้ดูบัญชีต่อได้โดยไม่ต้องเปิดค้างไว้

## ข้อตกลงข้อมูล

- ใช้ `CopyRates(..., 1, ...)` เพื่อข้ามแท่งปัจจุบัน และส่งแท่งเรียงเก่าไปใหม่
- จำกัด payload 512 KiB, รับ JSON แบบ strict, ไม่รับ key ซ้ำหรือ NaN/Infinity และจำกัดคำขอ 20 ครั้ง/นาที
- ตรวจ bearer token, บัญชี Demo ที่ EA รายงาน, server และ symbol allowlist, OHLC, ลำดับเวลา, แท่งปิดแล้ว, อายุ snapshot และ quote
- การรายงาน `account_mode` ไม่ใช่การรับรองจากโบรกเกอร์ ผู้ที่รู้ token สามารถสร้างข้อมูลปลอมได้ จึงจำกัดบริการไว้ที่ loopback และเก็บ token เป็นความลับ
- quote ต้องใหม่ไม่เกิน 120 วินาที; snapshot ต้องใหม่ไม่เกิน 120 วินาทีและเวลาอนาคตไม่เกิน 30 วินาที; ล่าสุด M5 ต้องปิดมาไม่เกิน 600 วินาที
- เก็บเวลาแท่งเป็น `open_server_s` และ tick เป็น `time_server_ms` ตามเวลาเซิร์ฟเวอร์ MT5 พร้อม `captured_at_utc_s` และ offset ที่สังเกต ณ เวลาส่ง **ไม่แปลงแท่งย้อนหลังทั้งหมดด้วย offset ปัจจุบัน** เพราะ DST อาจเปลี่ยนในช่วงประวัติ
- ตรวจ offset กับนาฬิกา ณ เวลาส่ง ไม่ใช่การยืนยัน timezone จากโบรกเกอร์ ต้องตรวจ UTC/DST อีกครั้งก่อนใช้สร้างสัญญาณ
- หากขาดการส่งเกิน 120 วินาที `/mt5/latest` รายงาน `stale`; ตลาดปิดหรือ terminal หลุดจะไม่มีราคาสด และ EA จะข้ามการส่ง quote เก่า
- เก็บ snapshot ล่าสุดไม่เกิน 240 ชุดต่อ server/symbol (ประมาณหนึ่งชั่วโมงที่ทุก 15 วินาที) การส่งซ้ำ timestamp เดิม+ข้อมูลเดิมได้ 200; ข้อมูลต่างได้ 409; ข้อมูลใหม่ได้ 201
- HTTP 401 = token ผิด, 422 = ข้อมูล/เวลา/server/symbol ไม่ผ่าน, 429 = ส่งถี่เกิน, 503 = ฐานข้อมูลไม่พร้อม
- GET `/mt5/status` ใช้ bearer bridge token เช่นเดียวกับ `/mt5/latest` เพื่อดูผลรับ/ปฏิเสธล่าสุดและ error code แบบไม่มี payload/token ข้อมูลวินิจฉัยอยู่ในหน่วยความจำและเริ่มใหม่เมื่อรีสตาร์ต receiver

## ผลตรวจ 2 พฤศจิกายน 2026

- `.venv/bin/python -m pytest -q`: **146 passed** (รวมระบบเดิม) พร้อม deprecation warning จาก Starlette/httpx 1 รายการ
- `.venv/bin/python scripts/smoke_mt5.py`: HTTP จริงผ่าน loopback ผ่าน auth, storage M5/H1, deduplication, readback และปฏิเสธ account_mode=real โดยใช้ราคาสมมติกับ SQLite ชั่วคราว
- เตรียม token ภายในเครื่องแล้วโดยไม่แสดงในรายงาน
- หลังผู้ใช้ติดตั้ง MT5 และแจ้งว่าล็อกอิน XM Demo แล้ว: คอมไพล์ EA ด้วย MetaEditor CLI ผ่าน **0 errors, 0 warnings** (1,457 ms) ดู `tests/mt5-compile-2026-11-02.txt`
- วาง `.mq5`, `.ex5` ใน terminal `MQL5/Experts` และ token ใน `MQL5/Files` แล้ว ตรวจ hash ตรงกับโปรเจกต์โดยไม่แสดง token
- ผู้ใช้เพิ่ม WebRequest allowlist และแนบ EA ลงกราฟ `GOLD,M5` แล้ว
- **รับราคาจริงสำเร็จ** จาก `XMGlobal-MT5 7`, account_mode=demo, broker_symbol=`GOLD`: M5/H1 อย่างละ 300 แท่ง พร้อม bid/ask, digits=2, tick_size=0.01
- ตรวจเวลา 23:02 น. (Asia/Vientiane): snapshot ล่าสุด 23:02:44 อยู่ในสถานะ fresh อายุประมาณ 10 วินาที; 5 snapshot ล่าสุดมีช่วงห่าง 15 วินาทีทุกคู่ ยืนยันการส่งต่อเนื่อง
- ณ snapshot ที่ตรวจ แท่งปิดล่าสุดของ M5/H1 ผ่านมา 164 วินาที; offset ปัจจุบัน +7200 วินาที คงเวลาแท่งย้อนหลังเป็นเวลาเซิร์ฟเวอร์ตามเดิม
- Log EA ยืนยัน `Price snapshot accepted: M5=300, H1=300` ไม่มีข้อมูลบัญชีหรือ token ในผลตรวจ
- เปิด receiver `127.0.0.1:8001` ค้างไว้ให้ EA ส่งข้อมูลต่อ ต้องเปิด MT5 และ Mac ต่อเนื่องด้วย

อัปเดต 3 พฤศจิกายน 2026: แก้ EA เป็น v1.01 ให้ default/fallback อ่าน GOLD และคอมไพล์ผ่าน 0 errors/0 warnings หลังผู้ใช้โหลดใหม่รับข้อมูลสดได้อีกครั้งเวลา 07:02 น. พร้อม M5/H1 อย่างละ 300 แท่ง Engine ทำงานต่อจาก checkpoint และ LM Studio ออนไลน์ ดู [ผลตรวจ](tests/xm-ai-verification-2026-11-03.md)

อ้างอิง: [CopyRates](https://www.mql5.com/en/docs/series/copyrates), [WebRequest](https://www.mql5.com/en/docs/network/webrequest), [TimeGMT](https://www.mql5.com/en/docs/dateandtime/timegmt)
