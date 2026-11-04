# รายงานทดสอบกลยุทธ์ย้อนหลัง XM GOLD (M1/M5) — 2026-11-04

> รายงานนี้เป็นผลจำลองจากข้อมูลย้อนหลังเท่านั้น ยังใช้เป็นหลักฐานว่ากลยุทธ์จะทำกำไรได้จริงไม่ได้ ระหว่างทำงานไม่ได้แก้ `app/`, `config/`, `data/`, harness หรือไฟล์ทดสอบของผู้อื่น ไม่ได้ commit และไม่ได้เปลี่ยน branch (`dev`)

## 1. สรุปสั้น

- **ไม่มีกลยุทธ์ใดผ่านเกณฑ์คัดเลือกครบทุกข้อ** มี 3 ตัวที่ผ่านเกณฑ์ใน in-sample รอบที่ 2 แต่ทั้ง 3 ตัวไม่ผ่านใน out-of-sample (expectancy ติดลบ และ PF ต่ำกว่า 1.2)
- ตัวที่ใกล้เป้าหมาย "planned net RR > 1:1 และ win rate สูง" ที่สุดคือ **`bb_squeeze_breakout_r2`** (รัน `r2-bb_squeeze_breakout_r2-2`)
  - in-sample: WR 72.0%, planned net RR 1.40, E +0.120R, PF 1.29
  - out-of-sample: WR 60.0%, E −0.050R, PF 0.95
  - ถือว่าไม่ผ่าน และยังไม่ควร port เป็น xm-0.2
- win rate ที่สูงส่วนใหญ่มาจากการออกที่ breakeven ซึ่ง lock กำไรไว้ +0.1R สัดส่วนที่ถึง TP จริงมีเพียง 23–37% realized RR จึงอยู่ที่ 0.56–0.71 แม้ planned net RR จะเกิน 1.0
- ทุกเทรดของทุกกลยุทธ์จะถูก Risk ของ Paper ปฏิเสธ เพราะ `min_net_reward_risk` = 1.5 ใน `config/risk-xm-paper.json` สูงกว่า rr ที่ใช้ (1.05–1.4) การทดสอบนี้จึงไม่เปิด `--live-risk-gate`

## 2. วัตถุประสงค์และเกณฑ์

- **เป้าหมายจากผู้ใช้:** planned net RR > 1:1 และ win rate สูงที่สุดเท่าที่ทำได้
- **เกณฑ์คัดเลือก:**
  1. ต้องมี `planned_net_rr` ≥ 1.0
  2. จากนั้นเลือกชุดที่ `win_rate` สูงสุด โดยต้องมี `expectancy_r` > 0, `profit_factor` ≥ 1.2, `max_drawdown_pct` ≤ 10
  3. ทั้งปีต้องมีอย่างน้อย 100 เทรด และ out-of-sample อย่างน้อย 30 เทรด ระหว่าง tune ประมาณจาก in-sample ว่าต้องมีอย่างน้อย ~67 เทรด
- **กติกา:** tune บน in-sample เท่านั้น แต่ละกลยุทธ์ที่เข้ารอบรัน out-of-sample ครั้งเดียวด้วยพารามิเตอร์ที่ล็อกไว้ และไม่ปรับอะไรอีกหลังเห็นผล

## 3. ข้อมูลและสมมติฐานต้นทุน

### ข้อมูล

| รายการ | ค่า |
| --- | --- |
| ไฟล์ | `data/history/xm_gold_m1.csv` (sha256 `9a228073…13265`) |
| แหล่ง | XM MT5 demo, server `XMGlobal-MT5 7`, symbol `GOLD`, M1 ราคา bid พร้อม spread |
| ขนาด | 352,984 แท่ง M1; ราคา 3,870–5,598; median spread 48 points (0.48 USD) |
| เวลา | broker server wall clock (ตอน export เป็น UTC+2; XM เลื่อนตาม DST) |
| In-sample | 2025-11-04 12:49 ถึง 2026-07-04 00:00 server (34.5 สัปดาห์, 8 เดือนแรก) |
| Out-of-sample | 2026-07-04 00:00 ถึง 2026-11-04 12:47 server (17.6 สัปดาห์) |
| Session (ชั่วโมง server) | asia [0,10), london [10,15), new_york [15,24) |

### ต้นทุน การ fill และขนาดสัญญา

ทุกค่าตรงกับ Paper policy (`config/risk-xm-paper.json`, `config/paper-xm.json`) และไม่ได้แก้ไข:

- **บัญชีและความเสี่ยง:** equity 10,000 USD, ความเสี่ยง 0.5% ต่อเทรด, เปิดได้ครั้งละ 1 position, notional cap 1× equity
  - cap นี้ทำให้ขนาดจริงอยู่ราว 2.1–2.2 oz และความเสี่ยงจริงราว 25–30 USD ต่อเทรด ไม่ใช่ 50 USD
- **ต้นทุน:** slippage 0.05 ต่อข้าง, commission 0.10 ต่อหน่วยต่อรอบ, spread ใช้ค่าจริงของแท่ง
  - live Risk ใช้ max(0.60, spread จริง)
- **การ fill:**
  - สัญญาณเกิดเมื่อแท่ง M5 ปิด แล้ว fill ที่ open ของ M1 แท่งถัดไป: BUY ที่ ask + slippage, SELL ที่ bid − slippage
  - สัญญาณที่อายุเกิน 300 วินาทีถือว่าหมดอายุ
  - SL/TP ตรวจทุกแท่ง M1: BUY ดูที่ bid, SELL ดูที่ ask
  - ถ้าแตะทั้ง SL และ TP ในแท่งเดียว ถือว่าโดน SL ก่อน (SL-first)
  - ถ้าราคา gap ข้ามระดับ จะ fill ที่ open
- **Net-RR helper (ทุกกลยุทธ์):** ตั้ง TP ให้ `planned_net_rr` = (|tp − entry| − 0.15) / (|entry − sl| + 0.15) ใกล้ค่า rr ที่กำหนด
- **สิ่งที่ไม่ได้ model:** daily loss lock, AI review, quote-age gate, margin

## 4. กลยุทธ์ที่ทดสอบ

| กลยุทธ์ | Indicators | กติกาหลัก (entry / exit) | ผลการตัดสิน |
| --- | --- | --- | --- |
| **baseline xm-0.1** (`baseline_xm01`) | H1 EMA50/200, M5 EMA20, RSI14, ADX14, ±DI14, ATR14, pivot structure | **Entry:** BUY เมื่อ H1 close > EMA50 > EMA200 + M5 close ตัด EMA20 ขึ้น + RSI ใน [50,70) + ADX ≥ 20 + +DI > −DI (SELL กลับด้าน); มีตัวเลือก structure filter (`confirmation_blockers`)<br>**Exit:** SL 1.5×ATR, TP 2× ระยะ SL | ใช้อ้างอิง |
| **keltner_er_pullback** | H1 EMA50/200, H1 Efficiency Ratio(10), M5 Keltner(20,10,1.5), M5 EMA50, ATR14, Lowest/Highest(6) | **Entry:** H1 ต้องเรียงเทรนด์และ ER ≥ er_min; ใน 6 แท่งล่าสุดราคาต้องย่อลงถึง KC middle − depth×ATR; trigger เมื่อ close กลับเหนือ middle เป็นแท่งเขียว และ close > high ของแท่งก่อนหน้า<br>**Exit:** SL ที่ Lowest(6) − 0.2 ATR, clamp ระยะใน [1, 2.5] ATR; time stop 48 แท่ง | drop |
| **rsi2_trend_pullback** (r1 → `_r2`) | H1 EMA50/200, M5 RSI(2), M5 EMA200, ATR14 | **Entry:** H1 และ M5 EMA200 ต้องเป็นขาขึ้น; RSI2 ≤ rsi_low และเมื่อ confirm=true ต้อง close > high ของแท่งก่อนหน้า; re-arm เมื่อ RSI2 ข้าม 50<br>**Exit:** SL = sl_atr×ATR; BE 0.6R lock 0.1R; time stop 24 แท่ง<br>**r2 เพิ่ม:** `cost_r_max`, `blackout_hours` | refine → ผ่าน IS, ไม่ผ่าน OOS |
| **bb_squeeze_breakout** (r1 → `_r2`) | M5 Bollinger(20,2), Keltner(20,20,kc_mult), ATR14, H1 EMA50 | **Entry:** BB ต้องอยู่ใน KC (squeeze) ต่อเนื่อง ≥ min_squeeze แท่ง; หลัง release มี window 3 แท่ง; LONG เมื่อ close > BB upper เป็นแท่งเขียว และ H1 close > EMA50 (SHORT กลับด้าน)<br>**Exit:** SL ที่ BB middle ∓ 0.3 ATR, clamp ใน [1, 2.5] ATR; time stop 36 แท่ง<br>**r2 เพิ่ม:** `window_bars`, `blackout_hours`, BE ปรับได้ | refine → ผ่าน IS, ไม่ผ่าน OOS |
| **asian_range_london** | SessionRange 02–10 server, RelativeVolume(20 วัน), M5 ATR14, H1 ATR14 | **Entry:** ใช้วันที่ width ของกรอบ Asia อยู่ใน [0.5, width_max]×H1 ATR; trade window 10:00–13:00 server; 1 เทรดต่อวัน; โหมด breakout (ทะลุกรอบพร้อม RVOL) หรือ fade (sweep แล้ว reclaim)<br>**Exit:** BE 0.7R lock 0.1R; time stop 60 แท่ง | drop |
| **vwap_reclaim** (r1 → `_r2`) | SessionVWAP(anchor), H1 EMA50/200, ATR14, RVOL(20), Lowest/Highest(6) | **Entry:** H1 ต้องเรียงเทรนด์และ VWAP ต้องชันตามทิศ; ราคา dip ใต้ VWAP ≥ dip_atr×ATR; trigger เมื่อ close > VWAP + 0.1 ATR เป็นแท่งเขียว และ close > high ของแท่งก่อนหน้า, RVOL ≥ rvol_min; window 10–21 server<br>**Exit:** SL ที่ Lowest(6) − 0.2 ATR, clamp; BE 0.7R; time stop 48 แท่ง<br>**r2 เพิ่ม:** `setup_bars`, `trend_filter`, `blackout_hours` | refine → ผ่าน IS, ไม่ผ่าน OOS |

ตัวกรองที่ใช้ร่วมกันทุกกลยุทธ์:
- spread ≤ 0.20×ATR14
- ห้ามเข้าวันศุกร์หลัง 20:00 server
- H1 indicators อัปเดตจากแท่งที่ปิดแล้วใน `ctx.new['H1']` เท่านั้น

## 5. ผล in-sample รอบที่ 1 เทียบรอบที่ 2

### Baseline xm-0.1 (in-sample)

| รัน | Trades | WR | Planned net RR | E (R) | PF | Max DD % |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `baseline-in_sample` (structure filter) | 3 | 0.0% | 1.90 | −1.000 | 0.00 | 0.86 |
| `baseline-nostruct-in_sample` | 249 | 29.3% | 1.87 | −0.183 | 0.77 | 11.40 |
| `baseline-out_of_sample` (structure filter) | 0 | — | — | — | — | — |

### รอบที่ 1 (16 combos ต่อกลยุทธ์; แสดงชุดที่ดีที่สุดของแต่ละตัว)

| กลยุทธ์ / รัน | Trades | WR | Planned net RR | Realized RR | E (R) | PF | Max DD % | ผ่าน IS? |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| keltner_er_pullback / r1-…-8 | 216 | 41.7% | 1.30 | 1.21 | −0.076 | 0.96 | 3.73 | ไม่ผ่าน (ติดลบทั้ง 16 ชุด) |
| rsi2_trend_pullback / r1-…-7 | 120 | 62.5% | 1.05 | 0.67 | +0.041 | 1.11 | 1.55 | ไม่ผ่าน (PF < 1.2) |
| bb_squeeze_breakout / r1-…-11 | 69 | 65.2% | 1.40 | 0.79 | +0.167 | 1.44 | 1.59 | ผ่าน แต่จำนวนเทรดก้ำกึ่ง |
| asian_range_london / r1-…-12 | 22 | 54.5% | 1.40 | 0.85 | +0.010 | 1.09 | 0.91 | ไม่ผ่าน (เทรดน้อยเชิงโครงสร้าง) |
| vwap_reclaim / r1-…-6 | 58 | 58.6% | 1.30 | 0.77 | +0.032 | 1.10 | 1.93 | ไม่ผ่าน (PF, จำนวนเทรด) |

**เหตุผลที่ drop สองตัว**
- **keltner_er_pullback:** ไม่มีแกนพารามิเตอร์ใดพาไปบริเวณที่เป็นบวก ฝั่ง BUY เสีย −20.6R แม้ทองจะขึ้นทั้งปี
- **asian_range_london:** width filter เหลือวันที่ใช้ได้เพียง 64 วันใน IS ต่อให้เทรดทุกวันก็ได้ราว 96 เทรดต่อปี และ edge ใกล้ศูนย์

### รอบที่ 2 (12 combos ต่อกลยุทธ์; ชุดที่เลือก)

| กลยุทธ์ / รัน | Trades | WR | Planned net RR | Realized RR | E (R) | PF | Max DD % | Exits (BE/TP/SL/TIME) | TP hit | ผ่าน IS? |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: | --- |
| bb_squeeze_breakout_r2 / r2-…-2 | 93 | 72.0% | 1.40 | 0.56 | +0.120 | 1.29 | 3.22 | 45/23/24/1 | 24.7% | ผ่าน |
| rsi2_trend_pullback_r2 / r2-…-6 | 92 | 67.4% | 1.10 | 0.71 | +0.141 | 1.35 | 1.08 | 23/34/27/8 | 37.0% | ผ่าน |
| vwap_reclaim_r2 / r2-vwap_reclaim-11 | 90 | 64.4% | 1.30 | 0.69 | +0.087 | 1.26 | 1.99 | 26/27/28/9 | 30.0% | ผ่าน |

**พารามิเตอร์ที่เลือก**
- **bb_squeeze_breakout_r2:** min_squeeze 9, kc_mult 1.5, rr 1.4, h1_filter true, window_bars 3, blackout_hours [13,15), BE 0.5R lock 0.1R, time stop 36
- **rsi2_trend_pullback_r2:** rsi_low 5, confirm true, sl_atr 1.8, rr 1.1, cost_r_max 0.08, ไม่มี blackout, BE 0.6R lock 0.1R, time stop 24
- **vwap_reclaim_r2:** anchor 0, dip_atr 0.8, rvol_min 0.8, rr 1.3, setup_bars 12, trend_filter strict, blackout_hours [13,15), BE 0.7R lock 0.1R, time stop 48

**ข้อสังเกตจาก grid IS รอบที่ 2**
- Control run ของทั้ง 3 ตัวให้ผลตรงหรือใกล้ r1 จึงยืนยันได้ว่าการ port ถูกต้อง
- bb_squeeze:
  - BE 0.5R ดัน WR ขึ้น 3–4 จุด แต่ลด realized RR
  - blackout 13–15 คือตัวที่ทำให้ min_squeeze 9 ผ่าน PF
  - window 5 แท่งทำให้แย่ลงทุกครั้ง
- rsi2:
  - BE 0.45R และ sl_atr 2.4 ทำให้ E/PF ลดลงทุกคู่
  - cost filter ให้ผลดีที่สุด แต่เพื่อนบ้านไม่ผ่าน PF ความเสถียรจึงอ่อน
- vwap:
  - setup_bars 12 + strict + blackout เป็นทิศที่สอดคล้องกัน (เพื่อนบ้าน PF 1.15–1.19)
  - ตัวกรอง ema50 ทำให้แย่ลงทุกครั้ง
  - กำไร IS ~7.8R ในจำนวนนี้ 6.3R มาจากเดือน 2026-04 เดือนเดียว

## 6. ผล out-of-sample (รันครั้งเดียวด้วยพารามิเตอร์ที่ล็อกไว้)

ตัวเลขยืนยันจาก `summary.json` ใน `.runtime/backtest/<run>/`

| กลยุทธ์ / รัน | Trades | WR | Planned net RR | Realized RR | E (R) | Total R | PF | Max DD % | Exits (BE/TP/SL/TIME) | ผ่าน OOS? |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| bb_squeeze_breakout_r2 / r2-…-oos | 35 | 60.0% | 1.40 | 0.58 | −0.050 | −1.73 | 0.95 | 0.98 | 13/8/14/0 | ไม่ผ่าน |
| rsi2_trend_pullback_r2 / r2-…-oos | 39 | 59.0% | 1.11 | 0.64 | −0.029 | −1.14 | 0.80 | 1.90 | 10/11/15/3 | ไม่ผ่าน |
| vwap_reclaim_r2 / r2-vwap_reclaim-oos | 51 | 47.1% | 1.30 | 0.71 | −0.189 | −9.63 | 0.63 | 3.80 | 11/12/26/2 | ไม่ผ่าน |

### ภาพรวมทั้งปี (IS + OOS)

ตารางนี้นำรายการเทรดจาก `trades.json` ของรัน IS และ OOS มาต่อกัน ไม่ใช่การรัน `--segment all` ขนาดสัญญาจึงคิดจาก equity แยกกันในแต่ละช่วง และไม่ได้คิด DD รวม

| กลยุทธ์ | Trades ทั้งปี | WR | E (R) | Total R | PF (USD) | ผ่านเกณฑ์ทั้งปี? |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| bb_squeeze_breakout_r2 | 128 | 68.8% | +0.074 | +9.42 | 1.19 | ไม่ผ่าน (PF < 1.2 และ OOS E < 0) |
| rsi2_trend_pullback_r2 | 131 | 64.9% | +0.090 | +11.84 | 1.16 | ไม่ผ่าน (PF < 1.2 และ OOS E < 0) |
| vwap_reclaim_r2 | 141 | 58.2% | −0.013 | −1.78 | 1.02 | ไม่ผ่าน |

### การเสื่อมจาก in-sample ไป out-of-sample

| กลยุทธ์ | WR (IS → OOS) | E (R) | PF | TP hit |
| --- | --- | --- | --- | --- |
| bb_squeeze_breakout_r2 | 72.0% → 60.0% (−12.0 จุด) | +0.120 → −0.050 | 1.29 → 0.95 | 24.7% → 22.9% |
| rsi2_trend_pullback_r2 | 67.4% → 59.0% (−8.4 จุด) | +0.141 → −0.029 | 1.35 → 0.80 | 37.0% → 28.2% |
| vwap_reclaim_r2 | 64.4% → 47.1% (−17.3 จุด) | +0.087 → −0.189 | 1.26 → 0.63 | 30.0% → 23.5% |

**สิ่งที่พบใน OOS**
- **bb_squeeze_r2:** ใน IS ฝั่ง SELL ดีกว่าชัด (+0.262R) แต่ใน OOS ฝั่ง SELL 28 เทรด ได้ −0.137R ขณะที่ BUY 7 เทรด ได้ +0.30R ทิศทางกลับกัน; asia −0.119R, london −0.127R, new_york +0.073R
- **rsi2_r2:** asia −0.071R, london +0.191R (6 เทรด), new_york −0.067R
- **vwap_r2:**
  - london พังหนัก: 17 เทรด WR 29%, E −0.61R โดยชั่วโมง 10 อย่างเดียวเสีย −5.5R
  - รายเดือน: ก.ค. +4.3R แล้วติดลบต่อเนื่อง (ส.ค. −1.7R, ก.ย. −2.2R, ต.ค. −9.1R)
  - edge ใน IS น่าจะเป็น noise หรือเฉพาะ regime
- ไม่มีเทรดใน OOS ที่ fill นอก SL/TP หรือมี position ซ้อนกัน

## 7. คำตอบต่อเป้าหมายของผู้ใช้

- **ตัวที่ใกล้เป้าหมายที่สุด: `bb_squeeze_breakout_r2`**
  - planned net RR 1.40 (ผ่าน ≥ 1.0)
  - WR สูงที่สุดทั้งใน IS (72.0%) และ OOS (60.0%)
  - PF ใน OOS 0.95 ใกล้ 1.0 ที่สุด และ DD ต่ำ
  - จำนวนเทรดถึงเกณฑ์ (128 ทั้งปี, 35 OOS)
- **แต่ไม่ผ่านเกณฑ์ครบ:** ใน OOS ได้ E −0.050R และ PF 0.95 ส่วนทั้งปี PF 1.19 ต่ำกว่า 1.2
- **ไม่มีกลยุทธ์ใดผ่านเกณฑ์ทุกข้อ** ทั้งใน OOS และเมื่อรวมทั้งปี
- **"RR > 1:1" ในงานนี้หมายถึง planned net RR เท่านั้น** realized RR (ชนะเฉลี่ย ÷ แพ้เฉลี่ย) ของทุกตัวที่เข้ารอบอยู่ที่ 0.56–0.71 เพราะเทรดที่นับเป็นชนะส่วนใหญ่ออกที่ BE +0.1R ถ้าผู้ใช้ต้องการให้กำไรเฉลี่ยมากกว่าขาดทุนเฉลี่ยจริง กลยุทธ์เหล่านี้ยังไม่ตอบโจทย์
- **ต้องตัดสินใจเรื่อง policy ก่อนใช้กับ Paper:** ทุกเทรดถูก flag `net_reward_risk_too_low` / `fill_net_reward_risk` เพราะ `min_net_reward_risk` = 1.5 ผู้ใช้ต้องตัดสินใจเองว่าจะคง policy ไว้ หรือให้กลยุทธ์ใช้ rr ≥ 1.5 (ซึ่งจะต้องทดสอบใหม่) รอบนี้ไม่ได้แก้ config

## 8. คำสั่งสำหรับทำซ้ำ

รันจาก `/Users/siratiswarawat/bird_dev/golife_proj/xauusd-ai-agent`

```sh
# bb_squeeze_breakout_r2 (in-sample / out-of-sample)
.venv/bin/python scripts/run_backtest.py --strategy bb_squeeze_breakout_r2 --data data/history/xm_gold_m1.csv --segment in_sample --params '{"kc_mult": 1.5, "rr": 1.4, "h1_filter": true, "min_squeeze": 9, "exits": {"breakeven_r": 0.5, "breakeven_lock_r": 0.1, "time_stop_bars": 36}, "window_bars": 3, "blackout_hours": [13, 15]}' --run r2-bb_squeeze_breakout_r2-2
.venv/bin/python scripts/run_backtest.py --strategy bb_squeeze_breakout_r2 --data data/history/xm_gold_m1.csv --segment out_of_sample --params '{"kc_mult": 1.5, "rr": 1.4, "h1_filter": true, "min_squeeze": 9, "exits": {"breakeven_r": 0.5, "breakeven_lock_r": 0.1, "time_stop_bars": 36}, "window_bars": 3, "blackout_hours": [13, 15]}' --run r2-bb_squeeze_breakout_r2-oos

# rsi2_trend_pullback_r2
.venv/bin/python scripts/run_backtest.py --strategy rsi2_trend_pullback_r2 --data data/history/xm_gold_m1.csv --segment in_sample --params '{"rsi_low": 5, "confirm": true, "rr": 1.1, "sl_atr": 1.8, "exits": {"breakeven_r": 0.6, "breakeven_lock_r": 0.1, "time_stop_bars": 24}, "cost_r_max": 0.08, "blackout_hours": []}' --run r2-rsi2_trend_pullback_r2-6
.venv/bin/python scripts/run_backtest.py --strategy rsi2_trend_pullback_r2 --data data/history/xm_gold_m1.csv --segment out_of_sample --params '{"rsi_low": 5, "confirm": true, "rr": 1.1, "sl_atr": 1.8, "exits": {"breakeven_r": 0.6, "breakeven_lock_r": 0.1, "time_stop_bars": 24}, "cost_r_max": 0.08, "blackout_hours": []}' --run r2-rsi2_trend_pullback_r2-oos

# vwap_reclaim_r2 (ค่าที่ไม่ได้ระบุใช้ default ซึ่งตรงกับ params ใน summary.json)
.venv/bin/python scripts/run_backtest.py --strategy vwap_reclaim_r2 --data data/history/xm_gold_m1.csv --segment in_sample --params '{"dip_atr":0.8,"setup_bars":12,"trend_filter":"strict","blackout_hours":[13,15]}' --run r2-vwap_reclaim-11
.venv/bin/python scripts/run_backtest.py --strategy vwap_reclaim_r2 --data data/history/xm_gold_m1.csv --segment out_of_sample --params '{"dip_atr":0.8,"setup_bars":12,"trend_filter":"strict","blackout_hours":[13,15]}' --run r2-vwap_reclaim-oos

# ชุดที่ดีที่สุดของรอบที่ 1
.venv/bin/python scripts/run_backtest.py --strategy bb_squeeze_breakout --data data/history/xm_gold_m1.csv --segment in_sample --params '{"min_squeeze": 12, "kc_mult": 1.5, "rr": 1.4, "h1_filter": true}' --run r1-bb_squeeze_breakout-11
.venv/bin/python scripts/run_backtest.py --strategy rsi2_trend_pullback --data data/history/xm_gold_m1.csv --segment in_sample --params '{"rsi_low": 5, "confirm": true, "sl_atr": 1.8, "rr": 1.05}' --run r1-rsi2_trend_pullback-7
.venv/bin/python scripts/run_backtest.py --strategy vwap_reclaim --data data/history/xm_gold_m1.csv --segment in_sample --params '{"anchor_hour":0,"dip_atr":0.8,"rvol_min":0.8,"rr":1.3}' --run r1-vwap_reclaim-6
.venv/bin/python scripts/run_backtest.py --strategy keltner_er_pullback --data data/history/xm_gold_m1.csv --segment in_sample --params '{"er_min": 0.25, "depth": 1, "rr": 1.3, "exits": {"breakeven_r": null, "breakeven_lock_r": null, "time_stop_bars": 48}}' --run r1-keltner_er_pullback-8
.venv/bin/python scripts/run_backtest.py --strategy asian_range_london --data data/history/xm_gold_m1.csv --segment in_sample --params '{"mode":"fade","rvol_min":1.2,"width_max":2.5,"rr":1.4}' --run r1-asian_range_london-12
```

**ไฟล์ strategy ที่สร้างใหม่** (ใน `backtest/strategies/`): `keltner_er_pullback.py`, `rsi2_trend_pullback.py`, `bb_squeeze_breakout.py`, `asian_range_london.py`, `vwap_reclaim.py`, `bb_squeeze_breakout_r2.py`, `rsi2_trend_pullback_r2.py`, `vwap_reclaim_r2.py`

**ผลรัน:** อยู่ใน `.runtime/backtest/r1-*` และ `.runtime/backtest/r2-*`
- รอบที่ 1: 5 × 16 combos + smoke run
- รอบที่ 2: 3 × 12 combos + smoke run + OOS ตัวละ 1 ครั้ง

## 9. ข้อจำกัด

1. **ไม่มีชั้น AI:** ไม่ได้จำลอง AI review, daily loss lock หรือ quote-age gate ของระบบจริง ผลจริงใน Paper อาจต่างออกไป
2. **Tick volume ไม่ใช่ volume จริง:** RelativeVolume และ VWAP ใช้ tick_volume ของ broker ซึ่งเป็นแค่ตัวแทนของ volume จริง RVOL(20 วัน) ยังต้อง warm-up ราว 1 เดือน จึงไม่มีเทรดของ vwap/asian ใน พ.ย. 2025
3. **ข้อมูลเพียงปีเดียว:** เป็นช่วงที่ทองขึ้นแรง (3,870 → 5,598) OOS มีแค่ 35–51 เทรดต่อกลยุทธ์ ช่วงความเชื่อมั่นของ WR/E จึงกว้าง
4. **ไม่มี margin model:** sizing ใช้สูตร Paper กับ notional cap 1× equity โดยไม่ model margin, swap หรือ financing
5. **สมมติฐาน SL-first และการ fill:**
   - ถ้าแตะ SL และ TP ใน M1 แท่งเดียวกันถือว่าโดน SL ก่อน การย้าย stop มีผลตั้งแต่ M1 แท่งถัดไป
   - time stop นับจากเวลาสัญญาณ และไปทำงานที่แท่งแรกหลังช่องว่างตลาด
     - ใน keltner มีเทรดที่ถือนานถึง 52 แท่ง ทั้งที่ time stop ตั้งไว้ 48
     - ใน vwap IS มีเทรด SELL วันที่ 2025-12-31 เสีย −3.24R เพราะราคา gap หลังวันหยุดปีใหม่
6. **DST และ session เป็นค่าประมาณ:**
   - ทุก window ใช้ server clock โดยไม่ปรับ offset
   - ช่วงที่ DST ของ US/EU ไม่ตรงกัน (มี.ค. และปลาย ต.ค.–ต้น พ.ย.) London จะเปิด 09:00 server แทน 10:00
   - การแบ่ง asia/london/new_york ใน report เป็นช่วงชั่วโมง server แบบคงที่
7. **Selection bias เล็กน้อย:**
   - รอบที่ 2 ออกแบบหลังเห็นผลรอบที่ 1 ใน IS (เช่น blackout 13–15 และ BE ที่ต่ำลง) ผลในรอบที่ 2 จึงมี bias ในทางบวก
   - การดู OOS ของ 3 ตัวพร้อมกันแล้วเลือกตัวที่ "ใกล้ที่สุด" ก็ถือเป็น selection อีกชั้นหนึ่ง แม้จะไม่ได้ re-tune
8. **นิยามตัวชี้วัด:**
   - WR นับเทรดที่ `net_pnl_usd > 0` จึงรวม BE +0.1R ด้วย
   - PF คิดเป็น USD ส่วน E คิดเป็น R ทั้งสองค่าอาจไม่สอดคล้องกันเพราะขนาดสัญญาเปลี่ยนตาม equity
9. **Spread ในตัวกรองต่างจาก live Risk:** กลยุทธ์ใช้ spread จริงของแท่ง ส่วน live Risk ใช้ max(0.60, spread จริง) ทำให้มีเทรดส่วนหนึ่งถูก flag `spread_atr_too_high`

## 10. ขั้นตอนถัดไปที่แนะนำ

1. **ยังไม่ port กลยุทธ์ใดเป็น xm-0.2** จนกว่าจะมีตัวที่ผ่าน OOS/walk-forward และผ่านการ review โค้ดกับผลลัพธ์แล้ว
2. **ทำ walk-forward:** เช่น train 4–6 เดือนแล้ว test 1–2 เดือนแบบเลื่อนหน้าต่าง กับ `bb_squeeze_breakout_r2` และ `rsi2_trend_pullback_r2` เพื่อดูว่าผลกลับด้านใน OOS เป็นเรื่องของ regime หรือ edge หายไปจริง
3. **หาข้อมูลที่ยาวขึ้น:** export XM GOLD M1 ย้อนหลัง 2–3 ปี (ผ่าน `mt5/XMExportHistory.mq5`) เพื่อให้ครอบคลุมหลาย regime และได้ OOS มากกว่า 100 เทรด
4. **วัดผลแยกจาก BE:** รายงาน TP hit rate และ realized RR คู่กับ WR เสมอ ถ้าเป้าหมายจริงคือกำไรเฉลี่ยมากกว่าขาดทุนเฉลี่ย ควรทดสอบชุดที่ไม่มี BE หรือ BE ที่สูงกว่านี้
5. **ตัดสินใจเรื่อง policy RR:** ผู้ใช้ต้องเลือกว่าจะทดสอบกลยุทธ์ใหม่ที่ rr ≥ 1.5 ให้สอดคล้องกับ `min_net_reward_risk` เดิม หรือจะทบทวน policy (การแก้ config เป็นการตัดสินใจของผู้ใช้)
6. **ทำ regression test ให้ harness:** เพิ่มการทดสอบกรณี time stop ข้าม gap วันหยุด และพิจารณารายงานตัวชี้วัดแบบ `--segment all` เพื่อให้ได้ PF และ DD ทั้งปีจากรันเดียว