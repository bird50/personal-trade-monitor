# เชื่อม LM Studio

ระบบรองรับตัวเลือก **Codex ผ่านบัญชี ChatGPT** เพิ่มเติมแล้ว เปลี่ยนจากแถบผู้วิเคราะห์ AI ใน XM Control ดู [CODEX.md](CODEX.md) รายละเอียดด้านล่างเป็นการตั้งค่า LM Studio

เชื่อมผ่าน `http://172.17.185.101:1234/v1/chat/completions` ใช้โมเดล `qwen/qwen3.8-27b` ตามเซิร์ฟเวอร์ที่ผู้ใช้ระบุ ไม่ใช้ AI API แบบคิดเงิน และไม่ส่งข้อมูลไปยังผู้ให้บริการอื่น

## กระบวนการ

1. Webhook ตรวจ payload และบันทึก `signals` + `analyses(status=pending)` ใน transaction เดียว
2. ตอบรับทันทีหลัง commit ไม่รอ LLM
3. Worker อ่านคิวครั้งละหนึ่งงานและขอ lease ด้วย SQLite transaction
4. ส่งเฉพาะสัญญาณที่ผ่าน validation พร้อม prompt และ JSON Schema ไปยัง LM Studio
5. ตรวจ final content ของโมเดล แล้วตรวจ [Risk Manager](RISK.md) และเก็บผล JSON, model, prompt version, จำนวนครั้งและรายงานความเสี่ยงใน SQLite พร้อมกัน

ใช้ `prompts/trading_analysis.txt` และ request version `signal-filter-v4` กับ structured output ตาม [LM Studio documentation](https://beta.lmstudio.ai/docs/developer/openai-compat/structured-output) คงคำอธิบายแหล่งข้อมูล XM และเวลา context แบบ broker server wall clock จาก v3 รุ่น v4 กำหนด `reasoning_effort=none` ตาม configuration สำหรับเซิร์ฟเวอร์ที่ทดสอบแล้ว ดู [ระบบสัญญาณ XM](XM-SIGNALS.md)

ผลต้องมี `decision`, `entry`, `sl`, `tp`, `risk_reward` และ `reasons` โมเดลห้ามกลับฝั่งสัญญาณ ห้ามเปลี่ยนราคา/RR ห้ามส่ง tool calls หรือข้อมูลที่ขาดโครงสร้าง ไม่ใช้ `reasoning_content` เป็นผลลัพธ์และไม่เก็บข้อความ reasoning

Pine v0.1 ยังรับได้และต้อง WAIT เพราะ market_structure เป็น null ส่วน **v0.2 ส่ง confirmed pivot evidence** ให้ Python ตรวจทิศทาง อายุ ระดับที่ถูกทะลุ ตำแหน่ง Entry และความสัมพันธ์ SL/TP กับแนวรับแนวต้านตาม [กฎ v0.2](pine/structure-v0.2.md) ก่อนส่งรายการ `confirmation_blockers` ให้โมเดล ถ้ามี blocker แต่โมเดลยังยืนยัน BUY/SELL ระบบจะบันทึก `failed / unsupported_confirmation` หากไม่มี blocker โมเดลอาจยืนยันฝั่งเดิมหรือ WAIT ได้ กฎนี้ยังไม่ใช่ Risk Manager ครบชุด

## Configuration

| ตัวแปร | ค่าเริ่มต้น |
| --- | --- |
| `AI_ENABLED` | true ใน .env ที่เตรียมไว้; false หากไม่มีตัวแปร |
| `LM_STUDIO_URL` | http://172.17.185.101:1234 |
| `LM_STUDIO_MODEL` | qwen/qwen3.8-27b |
| `AI_TIMEOUT_SECONDS` | 90 (เวลารวมสูงสุดต่อคำขอ) |
| `AI_MAX_ATTEMPTS` | 3 |
| `AI_MAX_SIGNAL_AGE_SECONDS` | 300 |
| `AI_REASONING_EFFORT` | none เมื่อโหลดจาก .env; ใช้ server_default เพื่อละ field ตามค่าเซิร์ฟเวอร์ |

รัน `sh scripts/run_local.sh` ตามเดิม Worker เริ่มพร้อม FastAPI และหยุดพร้อมกัน `AI_ENABLED=false` หยุดเฉพาะการวิเคราะห์ โดยยังรับสัญญาณและเก็บคิวได้ เมื่อเปิดใหม่งานที่เก่าเกินอายุจะเป็น expired

HTTP client ไม่ใช้ proxy จาก environment และไม่ตาม redirect ไม่มีการค้นหา endpoint หรือเลือกโมเดลสำรองอัตโนมัติ หากเซิร์ฟเวอร์เปลี่ยน IP/โมเดลต้องแก้ .env แล้วรีสตาร์ต

## สถานะและการกู้คืน

| สถานะ | ความหมาย |
| --- | --- |
| pending | รอเริ่มหรือรอลองใหม่ |
| processing | worker ถือ lease และกำลังประมวลผล |
| completed | final JSON ผ่าน schema และกฎตรวจสอบ |
| failed | output ผิด, server ปฏิเสธ หรือครบจำนวน retry |
| expired | สัญญาณเก่าเกินอายุ ก่อนเริ่มหรือหลัง AI ตอบ |

Timeout/connection error/HTTP 408, 429, 5xx ลองใหม่สูงสุด 3 ครั้งรวมครั้งแรก หน่วง 5 และ 10 วินาที ส่วน output ผิดและ HTTP 4xx อื่นไม่ลองใหม่ เมื่อ error ให้ `effective_decision=WAIT` และไม่มีผลราคาใหม่

ถ้า process หยุดระหว่างวิเคราะห์ งานอยู่ในฐานข้อมูลและเริ่มใหม่หลัง lease หมดอายุ (timeout + 15 วินาที) token ของ lease ป้องกัน worker เก่าเขียนทับงานใหม่ การเรียกโมเดลอาจเกิดซ้ำหลัง process crash แต่มีผลสถานะสุดท้ายเพียงหนึ่งแถวต่อสัญญาณ ไม่ใช่ exactly-once inference

ตรวจ freshness ทั้งก่อนและหลังเรียกโมเดล ไม่ยืนยันสัญญาณที่หมดอายุระหว่างรอ จำกัด response 128 KiB ไม่บันทึก body ของ error, token หรือรายละเอียด exception จาก remote server

สถานะ `signals.status=received` ยังคงเดิมเพื่อแยกการรับข้อมูลจากผล AI ผลทั้งหมดอ่านผ่าน API_TOKEN เท่านั้น:

```text
GET /signals/<id>/analysis
Authorization: Bearer <API_TOKEN>
```

`ai_decision` เป็นผลโมเดล ส่วน `effective_decision` ต้องผ่าน Risk Manager และยังไม่หมดอายุด้วย หากไม่ผ่านจะเป็น WAIT ผลนี้เป็นผลคัดกรองในโหมด paper_simulation ไม่ใช่คำสั่งซื้อขาย และไม่ใช่การรับรองราคาที่ fill ได้ ไม่มีการส่งคำสั่งหรือแจ้ง Telegram ในขั้นนี้

## ตรวจสอบ

```sh
# ทดสอบอัตโนมัติทั้งหมด ใช้โมเดลจำลอง ไม่เรียกเซิร์ฟเวอร์จริง
.venv/bin/python -m pytest -q

# ทดสอบกับ LM Studio จริงอย่างชัดเจน ใช้ข้อมูลสมมติและ SQLite ชั่วคราว
.venv/bin/python scripts/test_lm_studio.py

# ข้อมูลสมมติรุ่น v0.2 พร้อมโครงสร้าง bullish และระดับที่สอดคล้องกัน
.venv/bin/python scripts/test_lm_studio.py --v2

# ทดสอบ schema 3 ของ XM ด้วยข้อมูลสมมติและฐานข้อมูลชั่วคราว
.venv/bin/python scripts/test_lm_studio.py --xm
```

Live test ใช้ AI และ risk age limit 600 วินาทีใน configuration ชั่วคราวเพื่อแยกการตรวจการเชื่อมต่อจากอายุสัญญาณ ณ ขอบแท่ง แต่ configuration ใช้งานยังเป็น 300 วินาที และ live test ลองเพียงครั้งเดียว ผลรอจน completed/failed/expired แล้วหยุด worker ไม่เพิ่มข้อมูลสมมติลงฐานข้อมูลใช้งาน

ยังไม่ประเมินคุณภาพการคัดกรองหรือ Win Rate การยืนยันผลดังกล่าวต้องมีหลักฐานตลาดครบและเปรียบเทียบกับ baseline บน candidate stream เดียวกันในขั้น AI Validation/Paper Trading

ทดสอบ XM กับ LM Studio จริงวันที่ 3 พฤศจิกายน 2026 ผ่านภายใน 10.8 วินาที: JSON ครบ, ราคา/RR คงเดิม, AI=WAIT, risk=passed, effective=WAIT ก่อนกำหนด reasoning_effort ครั้งแรกจบที่ incomplete_response (73.5 วินาที) ไม่เก็บหรือใช้ reasoning_content การตั้ง reasoning_effort=none ตรวจด้วย probe แล้วได้ reasoning_tokens=0 และใช้ได้กับเซิร์ฟเวอร์นี้ โดยไม่ลด schema/risk gate หรือเพิ่มเวลาใช้งานจริง

ผลนี้ยืนยันการเชื่อมต่อเท่านั้น **เหตุผล WAIT บางข้อขัดกับตัวเลขที่ส่งให้** เช่น Entry 2500 อยู่ระหว่าง support 2497 และ resistance 2512 แต่โมเดลระบุว่าไม่อยู่ระหว่าง จึงยังไม่ยืนยันคุณภาพเหตุผล/การคัดกรอง และไม่ใช้ข้อความเหตุผลแทน deterministic gate ดู [ผลทดสอบจริง](tests/lmstudio-xm-live-2026-11-03.json)
