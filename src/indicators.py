from typing import List, Dict
import numpy as np


def ema(values: List[float], period: int) -> float:
    if len(values) < period:
        return float("nan")
    k = 2 / (period + 1)
    ema_val = values[0]
    for v in values[1:]:
        ema_val = v * k + ema_val * (1 - k)
    return float(ema_val)


def atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return float("nan")
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        trs.append(tr)
    return float(np.mean(trs[-period:]))


def adx(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return float("nan")
    plus_dm = []
    minus_dm = []
    trs = []
    for i in range(1, len(closes)):
        up_move = highs[i] - highs[i-1]
        down_move = lows[i-1] - lows[i]
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])))
    atr_val = np.mean(trs[-period:]) if len(trs) >= period else np.mean(trs)
    plus_di = 100 * (np.mean(plus_dm[-period:]) / atr_val) if atr_val else 0.0
    minus_di = 100 * (np.mean(minus_dm[-period:]) / atr_val) if atr_val else 0.0
    dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di) if (plus_di + minus_di) != 0 else 0.0
    return float(dx)


def vwap_session(ohlcv: List[Dict[str, float]]) -> float:
    # ohlcv: list of {open, high, low, close, volume}
    pv = 0.0
    vol = 0.0
    for k in ohlcv:
        typical = (k["high"] + k["low"] + k["close"]) / 3.0
        pv += typical * k["volume"]
        vol += k["volume"]
    return float(pv / vol) if vol > 0 else float("nan")


def supertrend(highs: List[float], lows: List[float], closes: List[float], period: int = 10, multiplier: float = 3.0) -> str:
    # Lightweight ST direction: 'long' if close above basic upper band, else 'short'
    if len(closes) < period + 1:
        return "neutral"
    atr_val = atr(highs, lows, closes, period)
    hl2 = (highs[-1] + lows[-1]) / 2.0
    upper = hl2 + multiplier * atr_val
    lower = hl2 - multiplier * atr_val
    return "long" if closes[-1] > upper else ("short" if closes[-1] < lower else "neutral")

