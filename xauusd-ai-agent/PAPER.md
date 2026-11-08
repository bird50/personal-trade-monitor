# บัญชี Paper Trading ของ XM

บัญชีนี้เป็นบัญชีจำลองภายใน Python เริ่มทุน **10,000 USD** แยกจากยอดใน XM Demo ใช้ `simulated_ounce` และมูลค่าราคาเปลี่ยนหนึ่งหน่วย = 1 USD ต่อ quantity หนึ่งหน่วยตาม risk policy เดิม ไม่ใช่ lot หรือ contract specification ของ XM

เส้นทาง: `XMPriceBridge → candidate → LM Studio → Risk Manager → Paper ledger ← XMPaperTicks`

## สิ่งที่ทำงาน

- รับเฉพาะผล AI ที่เป็น BUY/SELL ฝั่งเดิมและ risk=passed, policy ตรงปัจจุบัน และยังไม่หมดอายุ
- ข้าม approvals ที่เกิดก่อนเปิดบัญชี Paper ครั้งแรก ไม่ย้อนเปิดสถานะจากผลเก่า
- เปิดด้วย tick ใหม่หลัง AI อนุมัติและหลังบัญชีเริ่มทำงาน Quote ต้องใหม่ไม่เกิน 30 วินาที และ tick stream ต้องตามทัน
- BUY เปิด Ask + slippage; SELL เปิด Bid − slippage ปัดเข้าด้านเสียเปรียบตาม tick 0.01
- ตรึง SL/TP จากสัญญาณเดิม ปฏิเสธหาก fill อยู่นอกระดับ, ห่าง Entry เกิน 0.25 ATR, spread/ATR เกินเกณฑ์ หรือ net R/R หลังต้นทุนต่ำกว่าเกณฑ์
- BUY ติดตาม Bid เพื่อปิด; SELL ติดตาม Ask เพื่อปิด เมื่อแตะ SL/TP จะจำลอง **market-on-trigger** ที่ quote ของ tick นั้นพร้อม slippage ด้านเสียเปรียบ หาก gap ข้าม SL ใช้ราคาที่พบจริงใน tick ไม่เติมราคาสมมติว่าปิดที่ SL พอดี
- Entry/exit commission อย่างละครึ่งของ 0.10 USD ต่อ quantity ไป–กลับ; slippage จำลอง 0.05 หน่วยราคาต่อขา ต้นทุนนี้เป็นสมมติฐานวิจัย ไม่ใช่อัตราค่าธรรมเนียมโบรกเกอร์จริง
- รุ่นนี้ยังไม่คิด swap/financing หรือสภาพคล่อง/partial fill
- Spread เกิดผ่าน Bid/Ask ที่ใช้ fill/mark จึงไม่หัก spread ซ้ำใน PnL ส่วน Risk Manager ของสัญญาณยังใช้ floor 0.60 สำหรับการวางแผนตามเดิม
- เก็บ balance, equity, unrealized PnL, realized cash PnL, planned risk และ maximum equity drawdown ด้วย Decimal ไม่ใช้ float รวมยอดเงิน
- เก็บ positions, decisions, ledger และ tick cursor ใน transaction เดียว ป้องกันการเปิด/ปิดหรือหักเงินซ้ำเมื่อส่งข้อมูลซ้ำหรือ process crash
- เปิด/ปิดจำลองและการเปลี่ยนสถานะ feed แสดงเป็น `PAPER ...` ใน Terminal ของ `sh scripts/run_xm.sh` หลัง commit แล้ว

## กฎบัญชีเริ่มต้น

ไฟล์ `config/paper-xm.json` และ `config/risk-xm-paper.json`:

| กฎ | ค่า |
| --- | --- |
| ทุนตั้งต้น | 10,000 USD |
| จำนวนสถานะเปิดสูงสุด | 1 |
| ความเสี่ยงต่อไม้ | ไม่เกิน 0.5% ของ equity หลังต้นทุนเปิด |
| ความเสี่ยงรวมสถานะเปิด | ไม่เกิน 0.5% ของ equity หลังต้นทุนเปิด |
| มูลค่าสถานะรวม | ไม่เกิน 1 เท่าของ equity หลังต้นทุนเปิด |
| Daily loss limit | 2% ของ equity ตั้งต้นวัน |
| วันบัญชี | Asia/Vientiane (UTC+7) |
| อายุ quote สำหรับเปิด | สูงสุด 30 วินาที |
| การเคลื่อนจาก Entry สัญญาณ | สูงสุด 0.25 ATR |

Daily loss 2%, จำนวนสถานะ 1 และ drift 0.25 ATR เป็นค่าเริ่มต้นของซอฟต์แวร์จำลองเพื่อให้เริ่มทดสอบได้ ไม่ใช่ค่าที่ผ่านการปรับประสิทธิภาพ

ขนาดสถานะปัดลงตาม quantity step และไม่เกิน quantity ที่ Risk Manager อนุมัติ คำนวณงบจาก equity ปัจจุบัน รวมต้นทุนเปิดและงบความเสี่ยงที่ถูกใช้แล้ว จึงลดขนาดเมื่องบ/ทุนลดลง ไม่มีการเพิ่มขนาดเหนือ approval เดิมแม้ equity เพิ่ม

Equity = balance + unrealized PnL ที่ mark ด้วยราคาฝั่งปิด; entry commission ถูกหักจาก balance ทันที ส่วน exit commission หักเมื่อปิด Daily loss รวม unrealized PnL และค่าธรรมเนียม วันใหม่ใช้ equity จาก mark ก่อนหน้าเป็น baseline เพื่อให้ gap ข้ามวันนับเป็นขาดทุนด้วย หากแตะเกณฑ์จะล็อกการเปิดสถานะใหม่จนวันถัดไป ยังคงติดตาม SL/TP ของสถานะเดิม ไม่มีการบังคับปิดเพียงเพราะชน daily limit

เมื่อเปิดโหมด **ยกเว้น Risk** Paper ข้าม daily loss limit, จำนวนสถานะสูงสุด, ความเสี่ยงรวม, เพดาน notional, drift, spread และ RR สุทธิตอน fill แต่ยังคุมความเสี่ยงต่อไม้ 0.5% ของ equity และยังตรวจ tick, อายุ quote, fill ระหว่าง SL/TP และ equity ดู [RISK.md](RISK.md#โหมดยกเว้น-ai-และ-risk-manager)

เมื่อแก้ policy แล้ว hash ไม่ตรงกับบัญชีเดิม ระบบหยุดรับ entry ใหม่ แต่ยังปิดสถานะเดิมด้วยต้นทุนที่บันทึกไว้ตอนเปิด ไม่รีเซ็ตยอดทุนหรือประวัติอัตโนมัติ ต้องตรวจและอนุมัติการย้าย policy ของบัญชีอย่างชัดเจนก่อนใช้ค่าใหม่ต่อ ด้วย

```sh
.venv/bin/python scripts/adopt_paper_policy.py                                   # ดูค่าที่จะเปลี่ยน (dry run)
.venv/bin/python scripts/adopt_paper_policy.py --reason "เหตุผล" --confirm      # บันทึก
```

คำสั่งนี้ไม่ทำงานถ้ายังมีไม้เปิด ไม่แตะยอดเงิน ledger ไม้ หรือ cursor ของ tick และเก็บ policy เดิมไว้ในตาราง `paper_policy_history` ให้สำรองฐานข้อมูลด้วย SQLite backup API ก่อน ถ้าบริการ XM ยังโหลด config ชุดเก่าอยู่ ให้รีสตาร์ตหลังจากนั้น

## ติดตั้ง tick exporter ใน MT5

`XMPriceBridge` เดิมส่ง snapshot ทุก 15 วินาทีและยังต้องใช้อยู่ เพิ่ม EA **XMPaperTicks** บนอีกกราฟหนึ่งเพื่ออ่าน tick ทั้งหมดระหว่างรอบ:

1. เปิดกราฟ GOLD ใหม่อีกหนึ่งกราฟ เก็บ XMPriceBridge บนกราฟเดิม
2. Navigator → Expert Advisors → Refresh แล้วลาก `XMPaperTicks` ลงกราฟใหม่
3. ใช้ ExpectedServer=`XMGlobal-MT5 7`, GoldSymbol=`GOLD`, PollSeconds=15 แล้วกด OK
4. ใช้ `MQL5/Files/bridge-token.txt` และ WebRequest allowlist `http://127.0.0.1:8001` เดียวกับ EA เดิม
5. Experts ต้องขึ้น `Paper ticks accepted` และ `/paper/account` ควรเปลี่ยนจาก `waiting_for_tick_stream` เป็น `running` เมื่อ catch up ทัน

วาง source/binary `mt5/XMPaperTicks.mq5` และ `.ex5` ใน `MQL5/Experts` ให้แล้ว รุ่นนี้อ่านข้อมูลเท่านั้น ไม่มี OrderSend/CTrade/DLL หรือคำสั่งส่งออร์เดอร์ไป XM ไม่จำเป็นต้องเปิดสิทธิ์เทรดอัตโนมัติเพื่อให้บัญชี Python ส่งออร์เดอร์ เพราะไม่มีส่วนส่งออร์เดอร์ดังกล่าว

EA ใช้ CopyTicks พร้อม cursor แบบต่อเนื่อง ส่งสูงสุด 2,048 ticks ต่อ batch ไม่ตัดกลาง millisecond และรอ ack ก่อนเลื่อน cursor หาก ack หายหรือมี exporter อื่นเลื่อน cursor จะอ่าน cursor ปัจจุบันแล้วส่งต่อ ดึง tick history คืนหลัง EA/เครือข่ายหยุดได้ตามข้อมูลที่โบรกเกอร์มี ไม่ส่งเฉพาะ quote ล่าสุดอย่างเดียว

Receiver ตรวจลำดับ, ordinal ใน millisecond, coverage, server/symbol/demo, clock และ token เก็บ tick ไว้ 48 ชั่วโมงนับจากเวลาที่ receiver ได้รับ (ไม่ใช่เวลาของ quote) ดังนั้น tick ย้อนหลังที่ EA ส่งคืนหลัง Mac/MT5 ปิดเกิน 48 ชั่วโมงยังอ่านได้จนกว่า Paper จะ replay เมื่อ Python หยุดแล้วกลับมา จะ replay ticks เพื่อปิดสถานะเดิมตามลำดับ แต่ไม่เปิด entry ใหม่จาก quote เก่า หากประวัติที่ยังไม่ได้อ่านหายหรือถูกหมุนออก จะหยุดที่ `tick_history_gap_review_required` และคงสถานะเดิมไว้ ไม่เดาราคา fill จาก OHLC

Coverage อาศัยประวัติที่ MT5/โบรกเกอร์ส่งกลับ การจับคู่จำลองไม่ได้พิสูจน์สภาพคล่องหรือ fill ที่จะได้จริงในตลาด หาก offset ของ stream เปลี่ยน Receiver จะปฏิเสธ batch จนตรวจ timezone/coverage ก่อน ไม่แปลงประวัติข้าม DST เอง

ยืนยัน tick จริงวันที่ 3 พฤศจิกายน 2026 เวลา15:09–15:10น.: sequenceเพิ่มต่อเนื่อง, account=running และราคาประเมินสด ยอดบัญชี10000 USD/0สถานะ ดู [รายงานตรวจ](tests/paper-verification-2026-11-03.md) การเปิด entry ใหม่ยังต้องมี LM Studio ออนไลน์และ approval ที่ผ่าน/ไม่หมดอายุ

## เปิดใช้งานและดูผล

ใช้บริการ Python สองหน้าต่างเดิม:

```sh
sh scripts/run_mt5_bridge.sh
```

```sh
sh scripts/run_xm.sh
```

ดูบัญชีครั้งเดียวหรืออัปเดตทุก 5 วินาทีจากอีก Terminal:

```sh
.venv/bin/python scripts/paper_status.py
.venv/bin/python scripts/paper_status.py --watch
```

`xm_status.py` เพิ่มสรุปบัญชี Paper ให้ด้วย ผล PnL ที่อิง quote เก่าจะรายงาน `valuation_fresh=false` และระบุอายุ mark อย่าตีความว่า equity นั้นเป็นราคาปัจจุบัน

API บน port 8002 ใช้ API_TOKEN เดิม:

| Endpoint | ผล |
| --- | --- |
| GET `/paper/account` | Balance/Equity/PnL, drawdown, สถานะราคาและกฎวัน |
| GET `/paper/positions?status=open` | สถานะเปิดพร้อม mark และ planned risk |
| GET `/paper/positions?status=closed` | สถานะปิดพร้อม exit reason และ net PnL |
| GET `/paper/ledger` | ทุนตั้งต้น, entry commission, exit settlement |
| GET `/paper/decisions` | เปิด/ข้าม/ปฏิเสธ approval พร้อมเหตุผล |

Lists ใช้ pagination ตาม id สูงสุด 100 แถวต่อคำขอ เก็บข้อมูลใน `data/xm-trading.db` ตารางใหม่แบบ additive สำรองก่อนเริ่มไว้ที่ `data/xm-before-paper-2026-11-03.db` ไม่มีการเติมเทรดสมมติลงบัญชีใช้งานเพื่อให้เห็นกำไร

ข้อมูล tick ใช้ `POST /mt5/ticks` และ `GET /mt5/ticks/cursor?symbol=GOLD` บน port 8001 ด้วย bridge token เดิม ไม่ต้องเปิด Tunnel หรือใช้ TradingView

## เมื่อ MT5 กลับมาเชื่อมต่อหลังหลุดนาน

`XMPaperTicks` v1.02 รับ cursor เดิมและทยอยส่ง Tick ที่ค้างตามลำดับ สูงสุด 8 หน้าต่อรอบ (หน้าละไม่เกิน 2,048 Tick) โดยเริ่มคำขอถัดไปภายในงบเวลารอบ 2 วินาที คำขอที่ช้าอาจใช้เกินงบครั้งหนึ่งได้ หากเกิดข้อผิดพลาดจะรอรอบ Timer ถัดไป ปกติยังส่งทุก 15 วินาที ไม่มีการข้ามประวัติหรือรีเซ็ตบัญชีเพื่อทำให้ราคาดูสด

Receiver แยกงบ rate limit: snapshot 20 คำขอต่อนาที และ Tick 64 คำขอต่อนาที เพื่อให้การส่ง Tick ค้างไม่แย่งโควตาราคา M5/H1

ใน XM Control หากได้รับ batch ใหม่แต่เวลาที่ครอบคลุมยังเก่า จะแสดง **กำลังรับ Tick ย้อนหลัง** พร้อมระยะที่ตามหลัง หากไม่มี batch ใหม่เกิน 60 วินาทีจะรายงานว่า stream เก่า เมื่อ quote ตามทันและ Paper ประมวลผลครบแล้วจึงแสดง **ติดตาม Tick สด** การเปิดไม้ใหม่ยังต้องผ่านกฎราคาใหม่ไม่เกิน 30 วินาที, AI และ Risk ตามเดิม

หากวาง EA รุ่นใหม่ ให้ถอดตัวเดิมออกจากกราฟ → Navigator → Refresh → แนบ XMPaperTicks ใหม่ ใช้ค่าเดิม เก็บ XMPriceBridge ไว้บนอีกกราฟหนึ่ง และเปิด Mac/MT5 ต่อจนตามข้อมูลครบ

## การทดสอบ

```sh
.venv/bin/python -m pytest -q
```

ครอบคลุม BUY/SELL, Bid/Ask, slippage/commission, gap ที่ SL, แตะ SL แล้วกลับตัวระหว่างรอบ, ledger reconciliation, idempotency, restart/crash rollback, stale/future/pending approvals, source coverage, daily loss/overnight gap และ portfolio sizing หลังต้นทุน พร้อม HTTP integration ของ approval → tick → open → close ใช้ข้อมูลสมมติและ SQLite ชั่วคราวทั้งหมด

อ้างอิง: [CopyTicks](https://www.mql5.com/en/docs/series/copyticks), [CopyTicksRange](https://www.mql5.com/en/docs/series/copyticksrange)
