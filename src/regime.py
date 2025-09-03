from typing import Dict, List
import math
from .indicators import atr as atr_func, adx as adx_func


def _safe_close(bars: List[dict]) -> float:
    try:
        return float(bars[-1]["close"]) if bars else float("nan")
    except Exception:
        return float("nan")


def _atr_percent(daily_bars: List[dict], period: int = 14) -> float:
    if not daily_bars:
        return float("nan")
    highs = [b["high"] for b in daily_bars]
    lows = [b["low"] for b in daily_bars]
    closes = [b["close"] for b in daily_bars]
    atr_val = atr_func(highs, lows, closes, period)
    px = closes[-1] if closes else float("nan")
    if math.isnan(atr_val) or not px or math.isnan(px):
        return float("nan")
    return float(atr_val / px)


def regime_filter(cfg: Dict, btc_1d: List[dict], btc_4h: List[dict], funding_abs: float) -> Dict:
    reg_cfg = cfg.get("regime", {})
    adx_min = float(reg_cfg.get("adx_min", 18))
    atrp_min = float(reg_cfg.get("atrp_min", 0.02))
    atrp_max = float(reg_cfg.get("atrp_max", 0.08))
    funding_abs_max = float(reg_cfg.get("funding_abs_max", 0.0015))

    # Metrics
    atrp = _atr_percent(btc_1d, period=14)
    h4 = [b["high"] for b in btc_4h]
    l4 = [b["low"] for b in btc_4h]
    c4 = [b["close"] for b in btc_4h]
    adx_period = int(cfg.get("trend", {}).get("adx", 14))
    adx4h = adx_func(h4, l4, c4, adx_period) if len(c4) >= adx_period + 1 else 0.0

    # Gates
    gates = {
        "adx": (adx4h >= adx_min),
        "atrp": (atrp_min <= atrp <= atrp_max) if not math.isnan(atrp) else False,
        "funding": (abs(float(funding_abs or 0.0)) <= funding_abs_max),
    }

    ok = all(gates.values())
    reason = None if ok else ",".join([k for k, v in gates.items() if not v])

    # Risk scalers when regime gates fail (soften veto)
    risk_scale = 1.0
    if not ok:
        if not gates.get("funding", True):
            risk_scale *= 0.5
        if not gates.get("atrp", True):
            # if too high vol, cut risk
            if not math.isnan(atrp) and atrp > 0.10:
                risk_scale *= 0.6

    return {
        "ok": bool(ok),
        "metrics": {
            "atr_percent": float(atrp),
            "adx_4h": float(adx4h),
            "funding_abs": float(abs(float(funding_abs or 0.0))),
        },
        "risk_scale": float(risk_scale),
        "reason": reason,
    }


