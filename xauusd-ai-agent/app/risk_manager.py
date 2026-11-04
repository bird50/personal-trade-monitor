"""Deterministic paper-signal risk gate. Does not manage or execute live positions."""
import hashlib
import json
import time
from decimal import Decimal, ROUND_FLOOR, localcontext
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import bypass as bypass_rules
from .models import Signal
from .structure import confirmation_blockers

Positive = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
Nonnegative = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class RiskPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    policy_version: Literal["paper-risk-v1"]
    mode: Literal["paper_simulation"]
    currency: Literal["USD"]
    allowed_symbols: Annotated[list[str], Field(min_length=1)]
    source_server: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    source_broker_symbol: Annotated[str, Field(min_length=1, max_length=40)] | None = None
    equity_usd: Positive
    risk_percent: Annotated[Decimal, Field(gt=0, le=100, allow_inf_nan=False)]
    min_net_reward_risk: Positive
    min_stop_atr: Positive
    max_stop_atr: Positive
    spread_price: Nonnegative
    slippage_price_per_side: Nonnegative
    commission_usd_per_unit_round_trip: Nonnegative
    max_spread_atr: Nonnegative
    usd_per_price_unit_per_quantity: Positive
    quantity_unit: Literal["simulated_ounce"]
    quantity_step: Positive
    min_quantity: Positive
    max_quantity: Positive
    max_notional_equity_multiple: Annotated[Decimal, Field(gt=0, le=1, allow_inf_nan=False)]
    price_tick: Positive
    max_signal_age_seconds: Annotated[int, Field(gt=0, le=3600, strict=True)]
    max_future_seconds: Annotated[int, Field(ge=0, le=60, strict=True)]

    @model_validator(mode="after")
    def ordered_limits(self):
        if "XM:XAUUSD" in self.allowed_symbols and (self.source_server is None or self.source_broker_symbol is None):
            raise ValueError("XM policies must pin server and broker symbol")
        if self.min_stop_atr > self.max_stop_atr or self.min_quantity > self.max_quantity:
            raise ValueError("Risk ranges must be ordered")
        if self.min_quantity % self.quantity_step or self.max_quantity % self.quantity_step:
            raise ValueError("Quantity limits must be multiples of quantity_step")
        return self

    @classmethod
    def load(cls, path: Path):
        return cls.model_validate_json(path.read_text())


class RiskManager:
    def __init__(self, policy: RiskPolicy):
        self.policy = policy
        self.snapshot = policy.model_dump(mode="json", exclude_none=True)
        self.policy_hash = hashlib.sha256(json.dumps(self.snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def evaluate(self, signal: Signal, ai_result: dict, now: float | None = None, bypass: dict | None = None) -> dict:
        now = time.time() if now is None else now
        p = self.policy
        bypass = {"ai": bool((bypass or {}).get("ai")), "risk": bool((bypass or {}).get("risk"))}
        relaxed = bypass["risk"]
        reasons = confirmation_blockers(signal)
        spread = p.spread_price
        expires_at = signal.bar_close_ms / 1000 + p.max_signal_age_seconds
        if signal.source is not None:
            source = signal.source
            if source.server != p.source_server or source.broker_symbol != p.source_broker_symbol:
                reasons.append("risk_source_not_allowed")
            spread = max(spread, Decimal(str(source.ask)) - Decimal(str(source.bid)))
            quote_utc_s = source.quote_server_ms / 1000 - source.observed_utc_offset_s
            expires_at = min(expires_at, source.snapshot_utc_s + 120, quote_utc_s + 120)
            if not -30 <= now - quote_utc_s <= 120 or not -30 <= now - source.snapshot_utc_s <= 120:
                reasons.append("stale_source_quote")
            if Decimal(str(source.price_tick)) != p.price_tick:
                reasons.append("source_tick_policy_mismatch")
        if signal.symbol not in p.allowed_symbols:
            reasons.append("risk_symbol_not_allowed")
        age = Decimal(str(now)) - Decimal(signal.bar_close_ms) / 1000
        if age > p.max_signal_age_seconds:
            reasons.append("stale_signal")
        if age < -p.max_future_seconds:
            reasons.append("future_signal")
        if ai_result.get("decision") not in (signal.side, "WAIT"):
            reasons.append("invalid_ai_decision")
        for name in ("entry", "sl", "tp", "risk_reward"):
            value = ai_result.get(name)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or value != getattr(signal, name):
                reasons.append("ai_changed_prices_or_rr")
                break
        with localcontext() as ctx:
            ctx.prec = 40
            entry, stop, target, atr = (Decimal(str(value)) for value in (signal.entry, signal.sl, signal.tp, signal.m5.atr))
            stop_distance = abs(entry - stop)
            reward_distance = abs(target - entry)
            stop_atr = stop_distance / atr
            spread_atr = spread / atr
            if any(value % p.price_tick != 0 for value in (entry, stop, target)):
                reasons.append("price_not_on_tick")
            if not p.min_stop_atr <= stop_atr <= p.max_stop_atr:
                reasons.append("stop_atr_out_of_range")
            if spread_atr > p.max_spread_atr:
                reasons.append("spread_atr_too_high")
            value_per_unit = p.usd_per_price_unit_per_quantity
            costs = (spread + 2 * p.slippage_price_per_side) * value_per_unit + p.commission_usd_per_unit_round_trip
            loss_per_unit = stop_distance * value_per_unit + costs
            reward_per_unit = reward_distance * value_per_unit - costs
            net_rr = reward_per_unit / loss_per_unit
            if net_rr < p.min_net_reward_risk:
                reasons.append("net_reward_risk_too_low")
            budget = p.equity_usd * p.risk_percent / 100
            notional_cap = p.equity_usd * p.max_notional_equity_multiple
            # Risk bypass keeps per-trade risk sizing but drops the notional account cap.
            limits = [budget / loss_per_unit, p.max_quantity]
            if not relaxed:
                limits.append(notional_cap / (entry * value_per_unit))
            raw_quantity = min(limits)
            steps = (raw_quantity / p.quantity_step).to_integral_value(rounding=ROUND_FLOOR)
            quantity = steps * p.quantity_step
            planned_loss = quantity * loss_per_unit
            notional = quantity * entry * value_per_unit
            if quantity < p.min_quantity:
                reasons.append("quantity_below_minimum")
            if planned_loss > budget or (notional > notional_cap and not relaxed):
                reasons.append("sizing_limit_exceeded")
            values = {
                "entry": entry, "sl": stop, "tp": target,
                "stop_distance": stop_distance, "reward_distance": reward_distance,
                "stop_atr": stop_atr, "spread_atr": spread_atr,
                "spread_price_used": spread,
                "gross_reward_risk": reward_distance / stop_distance,
                "net_reward_risk": net_rr, "cost_usd_per_unit_round_trip": costs,
                "loss_usd_per_unit": loss_per_unit, "reward_usd_per_unit": reward_per_unit,
                "risk_budget_usd": budget, "candidate_quantity": quantity,
                "planned_loss_usd": planned_loss, "planned_reward_usd": quantity * reward_per_unit,
                "notional_usd": notional, "notional_cap_usd": notional_cap,
            }
        reasons = list(dict.fromkeys(reasons))
        waivable = bypass_rules.waivable(bypass)
        waived = [code for code in reasons if code in waivable]
        reasons = [code for code in reasons if code not in waivable]
        return {
            "status": "rejected" if reasons else "passed", "mode": p.mode,
            "reason_codes": reasons, "policy_version": p.policy_version,
            "bypass": bypass, "waived_reason_codes": waived,
            "policy_sha256": self.policy_hash, "policy": self.snapshot,
            "evaluated_at": now, "expires_at": expires_at,
            "quantity_unit": p.quantity_unit,
            "approved_quantity": None if reasons else format(quantity, "f"),
            "calculations": {key: format(value, "f") for key, value in values.items()},
            "not_checked": ["live_spread", "current_market_price", "broker_contract_specifications",
                            "actual_equity", "open_positions", "aggregate_exposure", "daily_loss", "gap_risk"],
        }
