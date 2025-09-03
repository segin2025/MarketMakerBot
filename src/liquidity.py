from typing import Dict, List, Tuple
import numpy as np
from .indicators import vwap_session, atr as atr_func, supertrend as st_func


def previous_day_levels(k1h: List[dict]) -> Tuple[float, float]:
    # Assuming 24 bars of 1h make previous day
    if len(k1h) < 48:
        return float("nan"), float("nan")
    prev = k1h[-48:-24]
    pdh = max(k["high"] for k in prev)
    pdl = min(k["low"] for k in prev)
    return float(pdh), float(pdl)


def equal_levels(series: List[float], tol: float = 0.001) -> List[float]:
    # find cluster peaks within tolerance
    if len(series) < 5:
        return []
    levels = []
    s = np.array(series)
    for i in range(2, len(s)-2):
        if s[i] >= s[i-2:i+3].max():
            levels.append(float(s[i]))
    # cluster by proximity
    levels.sort()
    clusters = []
    for x in levels:
        if not clusters or abs(x - clusters[-1][-1]) / x > tol:
            clusters.append([x])
        else:
            clusters[-1].append(x)
    return [float(np.mean(c)) for c in clusters if len(c) >= 2]


def vwap_bands(k1h: List[dict], std_k1: float = 1.0, std_k2: float = 2.0) -> Dict[str, float]:
    vw = vwap_session(k1h)
    closes = np.array([k["close"] for k in k1h])
    std = float(np.std(closes - vw)) if not np.isnan(vw) else float("nan")
    return {"vwap": vw, "vwap_k1_up": vw + std_k1 * std, "vwap_k1_dn": vw - std_k1 * std, "vwap_k2_up": vw + std_k2 * std, "vwap_k2_dn": vw - std_k2 * std}


def stop_hunt_long_signal(k15: List[dict], pdl: float, wick_min_ratio: float = 0.35, vol_sigma: float = 1.6) -> bool:
    if not k15 or np.isnan(pdl):
        return False
    o = k15[-1]["open"]; h = k15[-1]["high"]; l = k15[-1]["low"]; c = k15[-1]["close"]; v = k15[-1]["volume"]
    range_ = max(1e-9, h - l)
    lower_wick_ratio = (min(o, c) - l) / range_
    # volume spike
    vols = np.array([k["volume"] for k in k15[-50:]])
    spike = (v > (np.mean(vols) + vol_sigma * np.std(vols))) if len(vols) >= 10 else True
    # wick and sweep
    swept = (l < pdl and c > pdl)
    return swept and spike and (lower_wick_ratio >= wick_min_ratio)


def stop_hunt_short_signal(k15: List[dict], pdh: float, wick_min_ratio: float = 0.35, vol_sigma: float = 1.6) -> bool:
    if not k15 or np.isnan(pdh):
        return False
    o = k15[-1]["open"]; h = k15[-1]["high"]; l = k15[-1]["low"]; c = k15[-1]["close"]; v = k15[-1]["volume"]
    range_ = max(1e-9, h - l)
    upper_wick_ratio = (h - max(o, c)) / range_
    vols = np.array([k["volume"] for k in k15[-50:]])
    spike = (v > (np.mean(vols) + vol_sigma * np.std(vols))) if len(vols) >= 10 else True
    swept = (h > pdh and c < pdh)
    return swept and spike and (upper_wick_ratio >= wick_min_ratio)


def relaxed_long_signal(k15: List[dict], k1h: List[dict], wick_min_ratio: float = 0.35, vol_sigma: float = 1.0) -> bool:
    if not k15 or not k1h:
        return False
    bands = vwap_bands(k1h)
    vw = bands["vwap"]; k1up = bands["vwap_k1_up"]; k1dn = bands["vwap_k1_dn"]
    if np.isnan(vw) or np.isnan(k1dn):
        return False
    o = k15[-1]["open"]; h = k15[-1]["high"]; l = k15[-1]["low"]; c = k15[-1]["close"]; v = k15[-1]["volume"]
    range_ = max(1e-9, h - l)
    lower_wick_ratio = (min(o, c) - l) / range_
    vols = np.array([k["volume"] for k in k15[-50:]])
    spike = (v > (np.mean(vols) + vol_sigma * np.std(vols))) if len(vols) >= 10 else True
    return (c > vw) and (l <= k1dn) and spike and (lower_wick_ratio >= wick_min_ratio)


def relaxed_short_signal(k15: List[dict], k1h: List[dict], wick_min_ratio: float = 0.35, vol_sigma: float = 1.0) -> bool:
    if not k15 or not k1h:
        return False
    bands = vwap_bands(k1h)
    vw = bands["vwap"]; k1up = bands["vwap_k1_up"]; k1dn = bands["vwap_k1_dn"]
    if np.isnan(vw) or np.isnan(k1up):
        return False
    o = k15[-1]["open"]; h = k15[-1]["high"]; l = k15[-1]["low"]; c = k15[-1]["close"]; v = k15[-1]["volume"]
    range_ = max(1e-9, h - l)
    upper_wick_ratio = (h - max(o, c)) / range_
    vols = np.array([k["volume"] for k in k15[-50:]])
    spike = (v > (np.mean(vols) + vol_sigma * np.std(vols))) if len(vols) >= 10 else True
    return (c < vw) and (h >= k1up) and spike and (upper_wick_ratio >= wick_min_ratio)

# --- New: Pullback signals ---

def pullback_long_signal(k15: List[dict], k1h: List[dict]) -> bool:
    if len(k1h) < 30 or len(k15) < 5:
        return False
    st = st_func([k["high"] for k in k1h], [k["low"] for k in k1h], [k["close"] for k in k1h], period=10, multiplier=3.0)
    if st != "long":
        return False
    bands = vwap_bands(k1h)
    vw = bands["vwap"]; k1dn = bands["vwap_k1_dn"]
    if np.isnan(vw) or np.isnan(k1dn):
        return False
    # pullback: low touches/breaches k1dn and closes back above vw or k1dn
    l = k15[-1]["low"]; c = k15[-1]["close"]
    return (l <= k1dn) and (c >= k1dn)


def pullback_short_signal(k15: List[dict], k1h: List[dict]) -> bool:
    if len(k1h) < 30 or len(k15) < 5:
        return False
    st = st_func([k["high"] for k in k1h], [k["low"] for k in k1h], [k["close"] for k in k1h], period=10, multiplier=3.0)
    if st != "short":
        return False
    bands = vwap_bands(k1h)
    vw = bands["vwap"]; k1up = bands["vwap_k1_up"]
    if np.isnan(vw) or np.isnan(k1up):
        return False
    h = k15[-1]["high"]; c = k15[-1]["close"]
    return (h >= k1up) and (c <= k1up)

# --- New: Breakout + retest ---

def breakout_retest_long(k15: List[dict], pdh: float, lookback: int = 40) -> bool:
    if np.isnan(pdh) or len(k15) < lookback:
        return False
    closes = [k["close"] for k in k15[-lookback:]]
    lows = [k["low"] for k in k15[-lookback:]]
    # breakout occurred
    if max(closes[:-5]) <= pdh:
        return False
    # retest near level in last 5 bars and close above
    for i in range(5, 0, -1):
        if lows[-i] <= pdh * 1.0005 and closes[-i] >= pdh:
            return True
    return False


def breakout_retest_short(k15: List[dict], pdl: float, lookback: int = 40) -> bool:
    if np.isnan(pdl) or len(k15) < lookback:
        return False
    closes = [k["close"] for k in k15[-lookback:]]
    highs = [k["high"] for k in k15[-lookback:]]
    if min(closes[:-5]) >= pdl:
        return False
    for i in range(5, 0, -1):
        if highs[-i] >= pdl * 0.9995 and closes[-i] <= pdl:
            return True
    return False


def structural_sl_long(entry: float, wick_low: float, atr15: float, tick_size: float, cfg: Dict) -> float:
    buffer = max(cfg["stops"]["buffer_atr_mult"] * atr15, 10 * tick_size)
    return float(wick_low - buffer)


def structural_sl_short(entry: float, wick_high: float, atr15: float, tick_size: float, cfg: Dict) -> float:
    buffer = max(cfg["stops"]["buffer_atr_mult"] * atr15, 10 * tick_size)
    return float(wick_high + buffer)


def validate_stop_distance(entry: float, sl: float, atr15: float, cfg: Dict) -> bool:
    dist = abs(entry - sl)
    # Slightly relax lower bound to reduce false rejections
    min_mult = float(cfg["stops"]["min_atr_mult"]) * 0.9
    max_mult = float(cfg["stops"]["max_atr_mult"]) * 1.1
    return (dist >= min_mult * atr15) and (dist <= max_mult * atr15)


def validate_stop_distance_dynamic(entry: float, sl: float, atr15: float, cfg: Dict) -> bool:
    """Dynamic bounds: widen for micro-priced or ultra-low ATR%% coins."""
    dist = abs(entry - sl)
    if entry <= 0 or np.isnan(atr15):
        return False
    atrp = atr15 / entry
    min_mult = float(cfg["stops"]["min_atr_mult"])
    max_mult = float(cfg["stops"]["max_atr_mult"])
    # If price < $1 or ATR% < 1%, widen bounds
    if entry < 1.0 or atrp < 0.01:
        min_mult = 0.4
        max_mult = 3.0
    return (dist >= min_mult * atr15) and (dist <= max_mult * atr15)


def rr_ok(entry: float, sl: float, t1: float, min_rr: float) -> bool:
    stop = abs(entry - sl)
    return (t1 - entry) / stop >= min_rr if stop > 0 else False


def t1_t2_targets_long(k1h: List[dict], entry: float, pdh: float, cfg: Dict) -> Tuple[float, float]:
    bands = vwap_bands(k1h)
    vw1 = bands["vwap_k1_up"] if not np.isnan(bands["vwap_k1_up"]) else entry
    t1 = max(entry, vw1)
    t2 = max(t1, pdh) if not np.isnan(pdh) else t1
    return float(t1), float(t2)


def smc_targets_long(k1h: List[dict], entry: float, pdh: float, lookback: int = 120) -> Tuple[float, float]:
    highs = [k["high"] for k in k1h[-lookback:]] if k1h else []
    bands = vwap_bands(k1h)
    vwk1 = bands.get("vwap_k1_up", float("nan"))
    eqh = equal_levels(highs)
    internal_eqh = [x for x in eqh if x > entry and (np.isnan(pdh) or x < pdh)]
    t1_candidates = [entry]
    if not np.isnan(vwk1):
        t1_candidates.append(vwk1)
    if internal_eqh:
        t1_candidates.append(max(internal_eqh))
    t1 = max(t1_candidates)
    t2_candidates = [t1]
    if not np.isnan(pdh):
        t2_candidates.append(pdh)
    # If there are external liquidity pools above PDH, prefer the nearest
    external_eqh = [x for x in eqh if x >= t1]
    if external_eqh:
        t2_candidates.append(min(external_eqh))
    t2 = max(t2_candidates)
    return float(t1), float(t2)


def smc_targets_short(k1h: List[dict], entry: float, pdl: float, lookback: int = 120) -> Tuple[float, float]:
    lows = [k["low"] for k in k1h[-lookback:]] if k1h else []
    bands = vwap_bands(k1h)
    vwk1 = bands.get("vwap_k1_dn", float("nan"))
    # reuse equal_levels by inverting lows to find clusters of lows
    inv = [(-x) for x in lows]
    eql_inv = equal_levels(inv)
    eql = [(-x) for x in eql_inv]
    internal_eql = [x for x in eql if x < entry and (np.isnan(pdl) or x > pdl)]
    t1_candidates = [entry]
    if not np.isnan(vwk1):
        t1_candidates.append(vwk1)
    if internal_eql:
        t1_candidates.append(min(internal_eql))
    t1 = min(t1_candidates)
    t2_candidates = [t1]
    if not np.isnan(pdl):
        t2_candidates.append(pdl)
    external_eql = [x for x in eql if x <= t1]
    if external_eql:
        t2_candidates.append(max(external_eql))
    t2 = min(t2_candidates)
    return float(t1), float(t2)


def sl_tp_from_atr(k15: List[dict], entry: float, long: bool, atr_period: int = 15, atr_mult: float = 1.0) -> Tuple[float, float]:
    highs = [k["high"] for k in k15]
    lows = [k["low"] for k in k15]
    closes = [k["close"] for k in k15]
    a = atr_func(highs, lows, closes, atr_period)
    if np.isnan(a):
        a = 0.003 * entry
    if long:
        sl = entry - atr_mult * a
        tp = entry + 2 * (entry - sl)
    else:
        sl = entry + atr_mult * a
        tp = entry - 2 * (sl - entry)
    return float(sl), float(tp)
