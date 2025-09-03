from typing import Dict, List, Tuple
import os
import math
import numpy as np
from .binance_client import BinanceWrapper


WEIGHTS = {
    "L": 0.25,
    "M": 0.20,
    "F": 0.15,
    "B": 0.10,
    "C": 0.15,  # 1 - |corr|
    "RS": 0.15,
}


def _norm01(x: float, lo: float, hi: float) -> float:
    if math.isnan(x):
        return 0.0
    if hi == lo:
        return 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def _momentum_score(closes: List[float], bars_3d: int = 72, bars_7d: int = 168) -> float:
    if len(closes) < max(bars_3d, bars_7d) + 1:
        return 0.5
    r3 = (closes[-1] / closes[-bars_3d] - 1)
    r7 = (closes[-1] / closes[-bars_7d] - 1)
    s3 = 0.5 * (math.tanh(r3) + 1)
    s7 = 0.5 * (math.tanh(r7) + 1)
    return float(0.5 * (s3 + s7))


def _funding_pain(frates: List[dict]) -> float:
    if not frates:
        return 0.5
    vals = [float(x.get("fundingRate", 0.0)) for x in frates]
    m = np.mean(vals)
    return float(max(0.0, min(1.0, abs(m) / 0.003)))


def _basis_spread_placeholder() -> float:
    return 0.5


def _corr_abs1minus(btc_returns: List[float], coin_returns: List[float]) -> float:
    n = min(len(btc_returns), len(coin_returns))
    if n < 10:
        return 0.5
    x = np.array(btc_returns[-n:])
    y = np.array(coin_returns[-n:])
    if np.std(x) == 0 or np.std(y) == 0:
        return 0.5
    rho = float(np.corrcoef(x, y)[0, 1])
    return float(1 - abs(rho))


def _relative_strength(coin_closes: List[float], btc_closes: List[float], window: int = 72) -> float:
    if len(coin_closes) < window + 1 or len(btc_closes) < window + 1:
        return 0.5
    ratio = np.array(coin_closes[-window:]) / np.array(btc_closes[-window:])
    x = np.arange(len(ratio))
    slope = np.polyfit(x, ratio, 1)[0]
    return float(0.5 + (2 / math.pi) * math.atan(slope))


def build_universe_scores(api: BinanceWrapper, base_symbol: str, top_n: int = 150) -> Tuple[List[Tuple[str, float]], Dict[str, dict]]:
    quote = 'USDC' if base_symbol.endswith('USDC') else 'USDT'
    symbols = api.top_usd_symbols(top_n, quote=quote)
    if base_symbol in symbols:
        symbols.remove(base_symbol)

    # Filter only symbols that exist
    symbols = [s for s in symbols if api.has_symbol(s)]

    # Stage 1: liquidity ranking by tickers only
    try:
        tickers_list = api.client.futures_ticker()
        tickerd = {t["symbol"]: t for t in tickers_list}
    except Exception:
        tickerd = {}
    liq_pairs = []
    for s in symbols:
        t = tickerd.get(s, {})
        try:
            qv = float(t.get("quoteVolume", 0.0))
        except Exception:
            qv = 0.0
        liq_pairs.append((s, qv))
    liq_pairs.sort(key=lambda x: x[1], reverse=True)
    # widen deep scoring universe beyond 30 to avoid over-filtering
    try:
        deep_n = int(os.getenv('DEEP_LIQ_TOP_N', '120'))
    except Exception:
        deep_n = 120
    top_liq = [s for s, _ in liq_pairs[:max(30, deep_n)]]

    # Preload BTC 1h closes for correlation/RS
    btc_1h = api.klines(base_symbol, interval="1h", limit=200)
    btc_closes_1h = [k["close"] for k in btc_1h]
    btc_rets_1h = np.diff(btc_closes_1h) / btc_closes_1h[:-1] if len(btc_closes_1h) > 1 else []

    details: Dict[str, dict] = {}
    scored: List[Tuple[str, float]] = []

    vols = [qv for _, qv in liq_pairs]
    v_lo, v_hi = (min(vols), max(vols)) if vols else (0.0, 1.0)

    meta = {"total": len(symbols), "deep": 0, "skipped": 0, "errors": 0}

    # Use config runtime min volume if available via env override; prefer config via env var forward (keeps module decoupled)
    min_vol_usd = 0.0
    try:
        min_vol_usd = float(os.getenv('MIN_24H_VOL_USD', os.getenv('CONFIG_MIN_24H_VOL_USD', '0')))
    except Exception:
        min_vol_usd = 0.0

    for s in symbols:
        try:
            L = _norm01(dict(liq_pairs).get(s, 0.0), v_lo, v_hi)

            if s not in top_liq:
                S = WEIGHTS["L"] * L
                details[s] = {"L": L, "light": True, "score": S}
                scored.append((s, float(S)))
                meta["skipped"] += 1
                continue

            meta["deep"] += 1
            k1h = api.klines(s, interval="1h", limit=200)
            closes_1h = [k["close"] for k in k1h]
            rets_1h = np.diff(closes_1h) / np.array(closes_1h[:-1]) if len(closes_1h) > 1 else []
            M = _momentum_score(closes_1h, 72, 168)

            fr = api.funding_rate(s, limit=8)
            F = _funding_pain(fr)

            B = _basis_spread_placeholder()

            C = _corr_abs1minus(list(btc_rets_1h), list(rets_1h))

            RS = _relative_strength(closes_1h, btc_closes_1h, 72)

            S = (
                WEIGHTS["L"] * L
                + WEIGHTS["M"] * M
                + WEIGHTS["F"] * F
                + WEIGHTS["B"] * B
                + WEIGHTS["C"] * C
                + WEIGHTS["RS"] * RS
            )

            # Volume filter: approximate using 24h quoteVolume if available
            try:
                t = tickerd.get(s, {})
                qv = float(t.get("quoteVolume", 0.0))
                if min_vol_usd and qv < min_vol_usd:
                    meta["skipped"] += 1
                    continue
            except Exception:
                pass

            details[s] = {"L": L, "M": M, "F": F, "B": B, "C": C, "RS": RS, "score": S, "light": False}
            scored.append((s, float(S)))
        except Exception:
            meta["errors"] += 1
            continue

    scored.sort(key=lambda x: x[1], reverse=True)
    details["_meta"] = meta
    return scored, details
