# เลือก Codex เป็นผู้วิเคราะห์สัญญาณ

> **โหมดเดิม:** ตั้งแต่ 8 พฤศจิกายน 2026 บริการที่ XM Control เปิดคือโหมดแจ้งเตือนอย่างเดียว ([ALERTS.md](ALERTS.md)) ซึ่งไม่ใช้AI เนื้อหาด้านล่างใช้กับบริการเดิม `sh scripts/run_xm_paper.sh`

ใน XM Control มีปุ่ม **LM Studio** และ **Codex · ChatGPT** เลือกได้ในแถบ **ผู้วิเคราะห์ AI** ตัวเลือกถูกเก็บใน SQLite และคงอยู่เมื่อเริ่มบริการใหม่ ค่าเริ่มต้นของบัญชีที่ยังไม่เคยเลือกคือ LM Studio

การเปลี่ยนมีผลกับงานวิเคราะห์ถัดไป งานที่เริ่มไปแล้วใช้ AI/โมเดลเดิมจนจบ และผลแต่ละงานบันทึก `provider` กับ `model` ที่ใช้จริง ผลวิเคราะห์เก่าแสดงชื่อผู้วิเคราะห์และวันที่ของงานนั้น

เส้นทางของ Codex: `XM → Python → Codex CLI → ตรวจ JSON/ราคา/ฝั่ง → Risk → Paper → UI/แจ้งเตือน` ใช้บัญชี ChatGPT ที่ล็อกอินใน CLI อยู่ โดยเป็นงานใหม่แยกจากแชตที่ใช้พัฒนาระบบนี้ ใช้อินเทอร์เน็ตและโควตา Codex ตามบัญชี สัญญาณที่ส่งไปวิเคราะห์ประมวลผลผ่าน OpenAI

## การตั้งค่า

เครื่องนี้พบ Codex CLI 0.164.0 และทดสอบกับ `gpt-6-astra` ซึ่งเป็นโมเดลใน Codex config เดิมแล้ว หากไม่มีการล็อกอิน ให้เปิด Terminal และทำ `codex login` ให้เสร็จด้วยตัวเอง ไม่ต้องส่งรหัสผ่านหรือ token ในแชต

ตัวเลือกใน `.env` ที่เพิ่มได้:

```dotenv
AI_PROVIDER=lm_studio
CODEX_MODEL=gpt-6-astra
# ใส่เมื่อจำเป็นต้องระบุตำแหน่ง CLI เอง
# CODEX_CLI_PATH=/absolute/path/to/bin/codex
```

`AI_PROVIDER` ใช้ตั้งค่าครั้งแรก เมื่อเลือกผ่าน UI แล้วค่าที่บันทึกในฐานข้อมูลมีผลต่อการรันต่อไป `CODEX_MODEL` เป็นโมเดลของตัวเลือก Codex ส่วน LM Studio ยังใช้ `LM_STUDIO_MODEL` และ `LM_STUDIO_URL` เดิม ปรับ env แล้วเริ่มบริการใหม่

ตัวควบคุมหา CLI จาก PATH และจาก nvm ในเครื่อง เพื่อรองรับบริการที่เปิดผ่านแอป Mac ซึ่งอาจไม่ได้รับ PATH ของ shell ไม่อ่านหรือคัดลอกไฟล์ auth ไม่ส่ง token ของ receiver/MT5 หรือ Platform API keys ไปยังโปรเซส Codex และบังคับการล็อกอินแบบ ChatGPT เพื่อไม่เปลี่ยนไปใช้ API key โดยอัตโนมัติ

## ขอบเขตการวิเคราะห์

เรียก `codex exec` แบบ ephemeral ในโฟลเดอร์ชั่วคราว ใช้ JSON Schema, sandbox read-only, reasoning `low`, ปิด shell, apps, plugins, browser, image, memories และ multi-agent สำหรับงานวิเคราะห์นี้ ไม่โหลด user config ของ CLI ไม่ส่งประวัติแชต หรือ `.env` ให้โมเดล งานใช้ข้อมูลสัญญาณที่ Python เตรียมไว้เท่านั้น

ผลต้องผ่านกฎชุดเดียวกับ LM Studio: ห้ามแก้ entry/SL/TP/RR, ห้ามกลับฝั่ง, หากหลักฐานยืนยันไม่ครบต้อง WAIT และต้องผ่าน Risk กับอายุสัญญาณก่อน Paper เปิดไม้ เมื่อ Codex เรียกไม่ได้ ตอบผิดรูปแบบ หมดโควตา หรือหมดเวลา ระบบไม่อนุมัติสถานะจากผลนั้น ไม่มีการสลับผู้ให้บริการอัตโนมัติ

ใช้ timeout เดิม `AI_TIMEOUT_SECONDS` และอายุสัญญาณเดิม `AI_MAX_SIGNAL_AGE_SECONDS` ตรวจอายุซ้ำเมื่อ AI ตอบกลับ เกินเวลาจะเป็น expired/WAIT แม้โมเดลตอบ BUY/SELL การ timeout/ปิด worker จะหยุดเฉพาะ process group ที่ระบบสร้างให้ Codex เพื่อไม่ทิ้งงานค้าง

การ์ด Codex ระบุ **ล็อกอินพร้อมเรียกวิเคราะห์** จากการตรวจติดตั้งและ auth ภายในเครื่อง ไม่ได้ยิงงานโมเดลซ้ำทุก 15 วินาที สถานะนี้ไม่ได้รับประกันเครือข่ายหรือโควตาคงเหลือ ข้อผิดพลาดจากงานจริงแสดงในผลวิเคราะห์; quota/auth/model unavailable จะปรากฏเป็นคำเตือนด้วย

## ทดสอบแบบแยกบัญชีใช้งาน

```sh
.venv/bin/python scripts/test_lm_studio.py --xm --provider codex --report tests/codex-xm-live.json
```

คำสั่งนี้ใช้โควตา Codex จริง แต่ข้อมูลสัญญาณและฐานข้อมูลเป็นชุดทดสอบชั่วคราว ไม่มีการสร้างเทรดทดสอบในบัญชี Paper ที่ใช้งาน การทดสอบขยายอายุสัญญาณ/นโยบายเป็น 600 วินาทีเฉพาะฐานข้อมูลชั่วคราวเพื่อข้ามเส้นแท่ง M5 ได้ ไม่เปลี่ยนกฎของบัญชีจริงในโปรแกรม

วันที่ 4 พฤศจิกายน 2026 ทดสอบจริงสำเร็จใน **11.3 วินาที**: JSON ถูกต้อง, provider=codex, model=gpt-6-astra, AI=BUY และ risk=passed ดู [ผลทดสอบ](tests/codex-xm-live-2026-11-04.json) ผลนี้ยืนยันการเชื่อมและการตรวจ schema/risk ของข้อมูลทดสอบ ยังไม่ใช่หลักฐานผลกำไรของกลยุทธ์

เอกสาร OpenAI: [Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode), [Authentication](https://learn.chatgpt.com/docs/auth), [Usage limits](https://learn.chatgpt.com/docs/pricing)
