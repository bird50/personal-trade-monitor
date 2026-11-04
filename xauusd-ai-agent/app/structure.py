"""Deterministic checks of supplied pivot evidence, not a full risk manager."""
from .models import Signal


def confirmation_blockers(signal: Signal) -> list[str]:
    structure = signal.context.market_structure
    if structure is None:
        return ["missing_structure"]
    expected = "bullish" if signal.side == "BUY" else "bearish"
    if structure.derived_state() != expected:
        return ["structure_not_aligned_or_stale"]
    support, resistance = structure.recent_low, structure.recent_high
    if not support.active or not resistance.active:
        return ["broken_pivot_level"]
    if not support.price < signal.entry < resistance.price:
        return ["entry_not_between_levels"]
    if signal.side == "BUY":
        if signal.sl >= support.price or signal.tp > resistance.price:
            return ["stop_or_target_conflicts_with_levels"]
    else:
        if signal.sl <= resistance.price or signal.tp < support.price:
            return ["stop_or_target_conflicts_with_levels"]
    return []
