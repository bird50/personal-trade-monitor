"""Decimal paper ledger driven by ordered bid/ask ticks. No broker execution."""
import hashlib
import json
import math
import time
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from . import bypass as bypass_rules
from .models import Signal
from .risk_manager import Positive, RiskManager


def D(value):
    result=Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Non-finite financial value")
    return result


class PaperPolicy(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    version:Literal['xm-paper-v1']
    initial_balance_usd:Positive
    max_positions:Annotated[int,Field(ge=1,le=10,strict=True)]
    max_open_risk_percent:Annotated[Decimal,Field(gt=0,le=100,allow_inf_nan=False)]
    daily_loss_limit_percent:Annotated[Decimal,Field(gt=0,le=100,allow_inf_nan=False)]
    max_entry_quote_age_seconds:Annotated[int,Field(ge=1,le=120,strict=True)]
    max_entry_drift_atr:Positive
    day_timezone:Literal['Asia/Vientiane']

    @classmethod
    def load(cls,path):
        return cls.model_validate_json(path.read_text())


class PaperEngine:
    def __init__(self,db,policy:PaperPolicy,risk:RiskManager,now=None):
        self.db,self.policy,self.risk=db,policy,risk
        self.server=risk.policy.source_server
        self.symbol=risk.policy.source_broker_symbol
        if not self.server or not self.symbol:
            raise ValueError('Paper requires a pinned XM risk policy')
        combined={'paper':policy.model_dump(mode='json'),'risk':risk.snapshot}
        self.policy_json=json.dumps(combined,sort_keys=True,separators=(',',':'))
        self.policy_hash=hashlib.sha256(self.policy_json.encode()).hexdigest()
        now=time.time() if now is None else now
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""CREATE TABLE IF NOT EXISTS paper_account (
                id INTEGER PRIMARY KEY CHECK(id=1),initial_balance TEXT NOT NULL,balance TEXT NOT NULL,
                activated_at REAL NOT NULL,policy_hash TEXT NOT NULL,policy_json TEXT NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS paper_positions (
                id INTEGER PRIMARY KEY,signal_record_id INTEGER NOT NULL UNIQUE REFERENCES signals(id),
                status TEXT NOT NULL CHECK(status IN ('open','closed')),opened_ms INTEGER NOT NULL,
                closed_ms INTEGER,payload_json TEXT NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS paper_ledger (
                id INTEGER PRIMARY KEY,event_key TEXT NOT NULL UNIQUE,position_id INTEGER REFERENCES paper_positions(id),
                kind TEXT NOT NULL,amount TEXT NOT NULL,balance_after TEXT NOT NULL,at_utc_ms INTEGER NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS paper_decisions (
                signal_record_id INTEGER PRIMARY KEY REFERENCES signals(id),status TEXT NOT NULL,
                reason TEXT NOT NULL,approved_at REAL NOT NULL,position_id INTEGER REFERENCES paper_positions(id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS paper_runtime (
                id INTEGER PRIMARY KEY CHECK(id=1),state_json TEXT NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS paper_days (
                day TEXT PRIMARY KEY,start_equity TEXT NOT NULL,locked INTEGER NOT NULL DEFAULT 0)""")
            bypass_rules.ensure(conn)
            initial=format(policy.initial_balance_usd,'f')
            created=conn.execute("INSERT OR IGNORE INTO paper_account VALUES(1,?,?,?,?,?)",
                                (initial,initial,now,self.policy_hash,self.policy_json)).rowcount
            if created:
                conn.execute("INSERT INTO paper_ledger(event_key,kind,amount,balance_after,at_utc_ms) VALUES('initial_funding','initial_funding',?,?,?)",
                             (initial,initial,int(now*1000)))
            conn.execute("INSERT OR IGNORE INTO paper_runtime VALUES(1,?)",(json.dumps({'last_seq':0,'status':'waiting_for_tick_stream','last_tick_utc_ms':None,'equity_peak':initial,'max_drawdown_percent':'0'}),))

    def _account(self,conn):
        return conn.execute('SELECT * FROM paper_account WHERE id=1').fetchone()

    @staticmethod
    def _open(conn):
        return [(row['id'],json.loads(row['payload_json'])) for row in conn.execute("SELECT * FROM paper_positions WHERE status='open' ORDER BY id")]

    def _equity(self,conn):
        balance=D(self._account(conn)['balance'])
        floating=Decimal(0)
        for _,p in self._open(conn):
            mark=D(p['mark_bid'] if p['side']=='BUY' else p['mark_ask'])
            floating+=(mark-D(p['entry']))*D(p['quantity'])*(1 if p['side']=='BUY' else -1)*D(p['usd_per_price_unit'])
        return balance,balance+floating,floating

    def _cash(self,conn,key,position_id,kind,amount,at_ms):
        balance=D(self._account(conn)['balance'])+amount
        conn.execute('INSERT INTO paper_ledger(event_key,position_id,kind,amount,balance_after,at_utc_ms) VALUES(?,?,?,?,?,?)',
                     (key,position_id,kind,format(amount,'f'),format(balance,'f'),at_ms))
        conn.execute('UPDATE paper_account SET balance=? WHERE id=1',(format(balance,'f'),))
        return balance

    def _day(self,conn,at_ms):
        day=datetime.fromtimestamp(at_ms/1000,ZoneInfo(self.policy.day_timezone)).date().isoformat()
        _,equity,_=self._equity(conn)
        conn.execute('INSERT OR IGNORE INTO paper_days(day,start_equity) VALUES(?,?)',(day,format(equity,'f')))
        row=conn.execute('SELECT * FROM paper_days WHERE day=?',(day,)).fetchone()
        baseline=D(row['start_equity'])
        limit=baseline*self.policy.daily_loss_limit_percent/100
        locked=bool(row['locked']) or baseline-equity>=limit
        if locked and not row['locked']:
            conn.execute('UPDATE paper_days SET locked=1 WHERE day=?',(day,))
        return day,baseline,limit,locked

    def _collect(self,conn,now):
        account=self._account(conn)
        rows=conn.execute("""SELECT a.*,s.payload_json FROM analyses a JOIN signals s ON s.id=a.signal_record_id
            LEFT JOIN paper_decisions p ON p.signal_record_id=s.id WHERE p.signal_record_id IS NULL
            AND s.symbol='XM:XAUUSD' AND a.status IN ('completed','failed','expired') ORDER BY s.id""").fetchall()
        for row in rows:
            status,reason='pending','awaiting_post_approval_tick'
            if row['updated_at']<account['activated_at']:
                status,reason='skipped','before_paper_activation'
            elif row['status']!='completed':
                status,reason='skipped','analysis_'+row['status']
            else:
                result=json.loads(row['result_json']) if row['result_json'] else {}
                if result.get('decision')=='WAIT':
                    status,reason='skipped','ai_wait'
                else:
                    risk=conn.execute('SELECT report_json FROM risk_evaluations WHERE signal_record_id=?',(row['signal_record_id'],)).fetchone()
                    report=json.loads(risk[0]) if risk else {}
                    try:
                        signal=Signal.model_validate_json(row['payload_json'])
                        valid=(result.get('decision')==signal.side and signal.source is not None
                               and all(result.get(k)==getattr(signal,k) for k in ('entry','sl','tp','risk_reward')))
                        if report.get('status')=='passed':
                            valid=valid and D(report['approved_quantity'])>0 and D(report['expires_at'])>0
                    except (ValueError,KeyError,TypeError):
                        valid=False
                    if not valid or report.get('status')!='passed':
                        status,reason='rejected','approval_not_passed'
                    elif report.get('policy_sha256')!=self.risk.policy_hash:
                        status,reason='rejected','risk_policy_changed'
                    elif now>report['expires_at']:
                        status,reason='rejected','approval_expired'
            conn.execute('INSERT INTO paper_decisions VALUES(?,?,?,?,NULL)',(row['signal_record_id'],status,reason,row['updated_at']))
        for row in conn.execute("""SELECT p.signal_record_id,r.report_json FROM paper_decisions p
            JOIN risk_evaluations r ON r.signal_record_id=p.signal_record_id WHERE p.status='pending'""").fetchall():
            if now>json.loads(row['report_json'])['expires_at']:
                conn.execute("UPDATE paper_decisions SET status='rejected',reason='approval_expired' WHERE signal_record_id=?",(row['signal_record_id'],))

    def _finish(self,conn,position_id,p,tick,reason):
        direction=1 if p['side']=='BUY' else -1
        quote=D(tick['bid'] if direction==1 else tick['ask'])
        raw=quote-direction*D(p['slippage_per_side'])
        rounding=ROUND_FLOOR if direction==1 else ROUND_CEILING
        exit_price=(raw/D(p['price_tick'])).to_integral_value(rounding=rounding)*D(p['price_tick'])
        quantity=D(p['quantity'])
        gross=(exit_price-D(p['entry']))*quantity*direction*D(p['usd_per_price_unit'])
        exit_fee=quantity*D(p['commission_per_unit_round_trip'])/2
        net=gross-D(p['entry_fee'])-exit_fee
        p.update(exit_price=format(exit_price,'f'),gross_pnl=format(gross,'f'),exit_fee=format(exit_fee,'f'),
                 net_pnl=format(net,'f'),exit_reason=reason,closed_utc_ms=tick['utc_ms'],close_tick_sequence=tick['seq'])
        balance=self._cash(conn,f'close:{position_id}',position_id,'exit_settlement',gross-exit_fee,tick['utc_ms'])
        conn.execute("UPDATE paper_positions SET status='closed',closed_ms=?,payload_json=? WHERE id=? AND status='open'",
                     (tick['utc_ms'],json.dumps(p),position_id))
        return {'event':'paper_close','position_id':position_id,'side':p['side'],'net_pnl_usd':format(net,'f'),
                'balance_usd':format(balance,'f'),'reason':reason,'at_utc_ms':tick['utc_ms']}

    def _try_entries(self,conn,tick,now,feed_ready):
        events=[]
        pending=conn.execute("""SELECT p.*,s.payload_json,r.report_json FROM paper_decisions p
            JOIN signals s ON s.id=p.signal_record_id JOIN risk_evaluations r ON r.signal_record_id=s.id
            WHERE p.status='pending' ORDER BY p.approved_at,p.signal_record_id""").fetchall()
        for decision in pending:
            report=json.loads(decision['report_json'])
            reason=None
            current=bypass_rules.read(conn)
            if now>report['expires_at']:
                reason='approval_expired'
            elif self._account(conn)['policy_hash']!=self.policy_hash or report['policy_sha256']!=self.risk.policy_hash:
                reason='policy_changed'
            elif not bypass_rules.still_allowed(report,current):
                reason='bypass_disabled'
            if reason:
                conn.execute("UPDATE paper_decisions SET status='rejected',reason=? WHERE signal_record_id=?",(reason,decision['signal_record_id']))
                continue
            if not feed_ready or tick['utc_ms']<math.ceil(decision['approved_at']*1000):
                continue
            age=now-tick['utc_ms']/1000
            if not 0<=age<=self.policy.max_entry_quote_age_seconds:
                continue
            signal=Signal.model_validate_json(decision['payload_json'])
            p=self.risk.policy
            direction=1 if signal.side=='BUY' else -1
            quote=D(tick['ask'] if direction==1 else tick['bid'])
            raw=quote+direction*p.slippage_price_per_side
            rounding=ROUND_CEILING if direction==1 else ROUND_FLOOR
            entry=(raw/p.price_tick).to_integral_value(rounding=rounding)*p.price_tick
            stop,target=D(signal.sl),D(signal.tp)
            open_positions=self._open(conn)
            _,equity,_=self._equity(conn)
            day,baseline,daily_limit,locked=self._day(conn,tick['utc_ms'])
            # Risk bypass skips account rules and fill-time strategy filters only.
            relaxed=bool((report.get('bypass') or {}).get('risk')) and current['risk']
            if locked and not relaxed:
                reason='daily_loss_limit'
            elif len(open_positions)>=self.policy.max_positions and not relaxed:
                reason='max_positions'
            elif equity<=0:
                reason='insufficient_equity'
            elif not (stop<entry<target if direction==1 else target<entry<stop):
                reason='fill_outside_sl_tp'
            elif not relaxed and abs(entry-D(signal.entry))>D(signal.m5.atr)*self.policy.max_entry_drift_atr:
                reason='entry_price_drift'
            elif not relaxed and (D(tick['ask'])-D(tick['bid']))/D(signal.m5.atr)>p.max_spread_atr:
                reason='spread_atr_too_high'
            loss=(abs(entry-stop)+p.slippage_price_per_side)*p.usd_per_price_unit_per_quantity+p.commission_usd_per_unit_round_trip
            reward=(abs(target-entry)-p.slippage_price_per_side)*p.usd_per_price_unit_per_quantity-p.commission_usd_per_unit_round_trip
            if reason is None and not relaxed and reward/loss<p.min_net_reward_risk:
                reason='fill_net_reward_risk'
            if reason is None and relaxed:
                # Keep per-trade risk on actual equity; no aggregate, daily or notional room.
                risk_fraction=p.risk_percent/100
                liquidation=D(tick['bid'] if direction==1 else tick['ask'])
                friction=(entry-liquidation)*direction*p.usd_per_price_unit_per_quantity+p.commission_usd_per_unit_round_trip/2
                raw_qty=min(D(report['approved_quantity']),p.max_quantity,equity*risk_fraction/(loss+risk_fraction*friction))
                quantity=(raw_qty/p.quantity_step).to_integral_value(rounding=ROUND_FLOOR)*p.quantity_step
                if quantity<p.min_quantity:
                    reason='risk_budget_below_minimum'
            elif reason is None:
                aggregate=sum((D(pos['planned_risk_usd']) for _,pos in open_positions),Decimal(0))
                portfolio_room=max(Decimal(0),equity*self.policy.max_open_risk_percent/100-aggregate)
                daily_room=max(Decimal(0),daily_limit-(baseline-equity)-aggregate)
                risk_fraction=p.risk_percent/100
                portfolio_fraction=self.policy.max_open_risk_percent/100
                liquidation=D(tick['bid'] if direction==1 else tick['ask'])
                friction=(entry-liquidation)*direction*p.usd_per_price_unit_per_quantity+p.commission_usd_per_unit_round_trip/2
                # Size against equity AFTER paying opening costs, not just the pre-fill balance.
                market_notional=sum((D(pos['quantity'])*(D(pos['mark_bid'])+D(pos['mark_ask']))/2*D(pos['usd_per_price_unit']) for _,pos in open_positions),Decimal(0))
                notional_room=max(Decimal(0),equity*p.max_notional_equity_multiple-market_notional)
                cap=min(D(report['approved_quantity']),p.max_quantity)
                raw_qty=min(cap,equity*risk_fraction/(loss+risk_fraction*friction),
                            portfolio_room/(loss+portfolio_fraction*friction),daily_room/(loss+friction),
                            notional_room/(entry*p.usd_per_price_unit_per_quantity+p.max_notional_equity_multiple*friction))
                quantity=(raw_qty/p.quantity_step).to_integral_value(rounding=ROUND_FLOOR)*p.quantity_step
                if quantity<p.min_quantity:
                    reason='risk_budget_below_minimum'
            if reason:
                conn.execute("UPDATE paper_decisions SET status='rejected',reason=? WHERE signal_record_id=?",(reason,decision['signal_record_id']))
                continue
            entry_fee=quantity*p.commission_usd_per_unit_round_trip/2
            position={"signal_record_id":decision['signal_record_id'],'symbol':'XM:XAUUSD','side':signal.side,
                'quantity':format(quantity,'f'),'quantity_unit':'simulated_ounce','entry':format(entry,'f'),
                'signal_entry':str(signal.entry),'sl':format(stop,'f'),'tp':format(target,'f'),
                'opened_utc_ms':tick['utc_ms'],'approved_at_utc_s':decision['approved_at'],
                'entry_bid':tick['bid'],'entry_ask':tick['ask'],'open_tick_sequence':tick['seq'],
                'opening_received_at_utc_s':tick['received_at'],
                'mark_bid':tick['bid'],'mark_ask':tick['ask'],'mark_utc_ms':tick['utc_ms'],
                'entry_fee':format(entry_fee,'f'),'planned_risk_usd':format(loss*quantity,'f'),
                'notional_usd':format(entry*quantity*p.usd_per_price_unit_per_quantity,'f'),
                'slippage_per_side':str(p.slippage_price_per_side),'commission_per_unit_round_trip':str(p.commission_usd_per_unit_round_trip),
                'usd_per_price_unit':str(p.usd_per_price_unit_per_quantity),'price_tick':str(p.price_tick),
                'paper_policy_hash':self.policy_hash,'risk_policy_hash':self.risk.policy_hash,'exit_model':'market_on_trigger',
                'bypass':{'ai':bool((report.get('bypass') or {}).get('ai')),'risk':relaxed},
                'waived_reason_codes':report.get('waived_reason_codes') or []}
            position_id=conn.execute("INSERT INTO paper_positions(signal_record_id,status,opened_ms,payload_json) VALUES(?,'open',?,?)",
                (decision['signal_record_id'],tick['utc_ms'],json.dumps(position))).lastrowid
            balance=self._cash(conn,f'open:{position_id}',position_id,'entry_commission',-entry_fee,tick['utc_ms'])
            conn.execute("UPDATE paper_decisions SET status='opened',reason='filled_post_approval_tick',position_id=? WHERE signal_record_id=?",(position_id,decision['signal_record_id']))
            self._day(conn,tick['utc_ms'])
            events.append({'event':'paper_open','position_id':position_id,'side':signal.side,'quantity':format(quantity,'f'),
                           'entry':format(entry,'f'),'balance_usd':format(balance,'f'),'at_utc_ms':tick['utc_ms']})
        return events

    def note(self,status):
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            state=json.loads(conn.execute('SELECT state_json FROM paper_runtime WHERE id=1').fetchone()[0])
            state['status']=status
            conn.execute('UPDATE paper_runtime SET state_json=? WHERE id=1',(json.dumps(state),))

    def step(self,tick_store,now=None):
        now=time.time() if now is None else now
        with self.db.connect() as conn:
            cursor=json.loads(conn.execute('SELECT state_json FROM paper_runtime WHERE id=1').fetchone()[0])['last_seq']
        feed=tick_store.read(self.server,self.symbol,cursor)
        events=[]
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            state=json.loads(conn.execute('SELECT state_json FROM paper_runtime WHERE id=1').fetchone()[0])
            self._collect(conn,now)
            stream=feed['stream']
            if stream and not state.get('tick_baselined') and state['last_seq']==0 and not conn.execute('SELECT 1 FROM paper_positions LIMIT 1').fetchone():
                # A brand-new flat account has no exposure in pre-activation history.
                state['last_seq']=max(0,(feed['min_seq'] or stream['last_seq']+1)-1)
                state['tick_baselined']=True
            new_ticks=[tick for tick in feed['ticks'] if tick['seq']>state['last_seq']]
            sequence_gap=any(tick['seq']!=state['last_seq']+i+1 for i,tick in enumerate(new_ticks))
            state['updated_at']=now
            if stream is None:
                state['status']='waiting_for_tick_stream'
            elif state.get('integrity_halted'):
                state['status']='tick_history_gap_review_required'
            elif stream['last_seq']<state['last_seq'] or sequence_gap or (stream['last_seq']>state['last_seq'] and (feed['min_seq'] is None or feed['min_seq']>state['last_seq']+1)):
                # Missing history cannot be reconstructed into invented fills.
                state.update(status='tick_history_gap_review_required',integrity_halted=True)
            else:
                through_utc=stream['through_server_ms']-stream['offset_s']*1000
                caught_up=not feed['ticks'] or feed['ticks'][-1]['seq']==stream['last_seq']
                ready=caught_up and 0<=now-through_utc/1000<=self.policy.max_entry_quote_age_seconds
                state['status']='running' if ready else 'feed_stale' if caught_up else 'catching_up'
                state['coverage_through_utc_ms']=through_utc
                for tick in feed['ticks']:
                    if tick['seq']<=state['last_seq']:
                        continue
                    if tick['utc_ms']>int(now*1000):
                        break
                    if tick['utc_ms']>=int(self._account(conn)['activated_at']*1000):
                        # Carry the previous mark into a new day so overnight gaps count as loss.
                        self._day(conn,tick['utc_ms'])
                    for position_id,position in self._open(conn):
                        position.update(mark_bid=tick['bid'],mark_ask=tick['ask'],mark_utc_ms=tick['utc_ms'])
                        conn.execute('UPDATE paper_positions SET payload_json=? WHERE id=?',(json.dumps(position),position_id))
                    if tick['utc_ms']>=int(self._account(conn)['activated_at']*1000):
                        self._day(conn,tick['utc_ms'])
                    for position_id,position in self._open(conn):
                        mark=D(tick['bid'] if position['side']=='BUY' else tick['ask'])
                        stop,target=D(position['sl']),D(position['tp'])
                        reason='SL' if (mark<=stop if position['side']=='BUY' else mark>=stop) else 'TP' if (mark>=target if position['side']=='BUY' else mark<=target) else None
                        if reason:
                            events.append(self._finish(conn,position_id,position,tick,reason))
                    if tick['utc_ms']>=int(self._account(conn)['activated_at']*1000):
                        self._day(conn,tick['utc_ms'])
                        events.extend(self._try_entries(conn,tick,now,ready and not state.get('operator_paused',False)))
                    _,equity,_=self._equity(conn)
                    peak=max(D(state['equity_peak']),equity)
                    drawdown=(peak-equity)/peak*100 if peak>0 else Decimal(0)
                    state.update(last_seq=tick['seq'],last_tick_utc_ms=tick['utc_ms'],equity_peak=format(peak,'f'),
                                 max_drawdown_percent=format(max(D(state['max_drawdown_percent']),drawdown),'f'))
            conn.execute('UPDATE paper_runtime SET state_json=? WHERE id=1',(json.dumps(state),))
        return events

    def summary(self,now=None):
        now=time.time() if now is None else now
        with self.db.connect() as conn:
            account=self._account(conn)
            state=json.loads(conn.execute('SELECT state_json FROM paper_runtime').fetchone()[0])
            balance,equity,floating=self._equity(conn)
            opened=self._open(conn)
            closed=[json.loads(row[0]) for row in conn.execute("SELECT payload_json FROM paper_positions WHERE status='closed'")]
            age=None if state['last_tick_utc_ms'] is None else now-state['last_tick_utc_ms']/1000
            fresh=age is not None and 0<=age<=self.policy.max_entry_quote_age_seconds and state['status']=='running'
            day=conn.execute('SELECT * FROM paper_days ORDER BY day DESC LIMIT 1').fetchone()
            bypass=bypass_rules.read(conn)
            day_blocks=bool(day and day['locked']) and not bypass['risk']
            return {'mode':'paper_simulation','currency':'USD','quantity_unit':'simulated_ounce',
                'initial_balance_usd':account['initial_balance'],'balance_usd':format(balance,'f'),
                'equity_usd':format(equity,'f'),'unrealized_pnl_usd':format(floating,'f'),
                'realized_cash_pnl_usd':format(balance-D(account['initial_balance']),'f'),
                'open_positions':len(opened),'closed_trades':len(closed),
                'winning_trades':sum(D(p['net_pnl'])>0 for p in closed),
                'open_planned_risk_usd':format(sum((D(p['planned_risk_usd']) for _,p in opened),Decimal(0)),'f'),
                'max_drawdown_percent':state['max_drawdown_percent'],'status':state['status'],
                'mark_age_seconds':None if age is None else round(age,1),
                'valuation_fresh':fresh,
                'policy_matches':account['policy_hash']==self.policy_hash,
                'operator_paused':bool(state.get('operator_paused',False)),
                'entries_enabled':account['policy_hash']==self.policy_hash and fresh and not state.get('operator_paused',False) and not day_blocks and equity>0,
                'bypass':{'ai':bypass['ai'],'risk':bypass['risk']},
                'day':None if day is None else dict(day),'activated_at_utc_s':account['activated_at']}

    def positions(self,status,after_id=0,limit=50):
        with self.db.connect() as conn:
            rows=conn.execute('SELECT * FROM paper_positions WHERE status=? AND id>? ORDER BY id LIMIT ?',(status,after_id,limit)).fetchall()
        return [{'id':r['id'],'status':r['status'],**json.loads(r['payload_json'])} for r in rows]

    def ledger(self,after_id=0,limit=50):
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute('SELECT * FROM paper_ledger WHERE id>? ORDER BY id LIMIT ?',(after_id,limit))]

    def decisions(self,after_signal_id=0,limit=50):
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute('SELECT * FROM paper_decisions WHERE signal_record_id>? ORDER BY signal_record_id LIMIT ?',(after_signal_id,limit))]
