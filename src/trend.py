from typing import Dict, List
from .indicators import ema, adx as adx_func, vwap_session, supertrend


def trend_filter(cfg: Dict, k4h: List[dict], k1h: List[dict], k15: List[dict]) -> Dict:
    # 4H primary
    c4h = [k["close"] for k in k4h]
    if len(c4h) < 200:
        return {"ok": False, "direction": "flat", "reason": "insufficient_4h"}
    ema200_4h = ema(c4h[-200:], 200)
    ema50_4h = ema(c4h[-50:], 50)
    adx_4h = adx_func([k["high"] for k in k4h], [k["low"] for k in k4h], c4h, cfg["trend"]["adx"]) if len(c4h) >= cfg["trend"]["adx"] + 1 else 0

    dir4h = "long" if (c4h[-1] > ema200_4h and ema50_4h > ema200_4h and adx_4h >= cfg["trend"]["adx"]) else (
        "short" if (c4h[-1] < ema200_4h and ema50_4h < ema200_4h and adx_4h >= cfg["trend"]["adx"]) else "flat"
    )

    # 1H confirmation
    vw = vwap_session(k1h)
    st = supertrend([k["high"] for k in k1h], [k["low"] for k in k1h], [k["close"] for k in k1h], period=10, multiplier=3.0)
    price_1h = k1h[-1]["close"]

    if dir4h == "long":
        ok = (price_1h > vw) and (st == "long")
    elif dir4h == "short":
        ok = (price_1h < vw) and (st == "short")
    else:
        ok = False

    return {
        "ok": ok,
        "direction": dir4h if ok else "flat",
        "metrics": {"ema200_4h": ema200_4h, "ema50_4h": ema50_4h, "adx_4h": adx_4h, "vwap_1h": vw, "st_1h": st},
        "reason": None if ok else "failed_filters"
    }




