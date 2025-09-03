import os
import sys
import math
import argparse
from typing import List, Tuple
from datetime import datetime
from src.config_loader import load_config
from src.binance_client import BinanceWrapper
from src.regime import regime_filter
from src.trend import trend_filter
from src.scorer import build_universe_scores
from src.liquidity import previous_day_levels, stop_hunt_long_signal, stop_hunt_short_signal, sl_tp_from_atr, structural_sl_long, structural_sl_short, validate_stop_distance, rr_ok, t1_t2_targets_long, relaxed_long_signal, relaxed_short_signal, pullback_long_signal, pullback_short_signal, breakout_retest_long, breakout_retest_short, smc_targets_long, smc_targets_short
from src.risk import RiskEngine
from src.execution import FuturesExecutor
from src.risk_state import DayState
from src.ai_shadow import ShadowAIScorer
from src.news import resolve_news_context


def _dynamic_leverage_cap(atr_percent: float, base_cap: float) -> float:
    if math.isnan(atr_percent):
        return base_cap
    if atr_percent <= 0.03:
        return min(base_cap, 3.0)
    if atr_percent >= 0.07:
        return min(base_cap, 2.0)
    return base_cap


def _apply_leverage_cap(entry: float, qty: float, equity: float, lev_cap: float) -> Tuple[float, float]:
    notional = entry * qty
    implied = (notional / equity) if equity > 0 else 0.0
    if implied > lev_cap and implied > 0:
        scale = lev_cap / implied
        qty = qty * scale
        notional = entry * qty
        implied = (notional / equity) if equity > 0 else 0.0
    return qty, implied


def _compute_dynamic_r_per_trade(equity: float, atr_percent: float, mode: str, news_ctx: dict, cfg: dict) -> float:
    # Base r from config (fallback to global risk.r_per_trade)
    try:
        base_r = float(cfg['modes'][mode].get('r_per_trade', cfg['risk']['r_per_trade']))
    except Exception:
        base_r = float(cfg['risk'].get('r_per_trade', 0.003))
    eq = max(1.0, float(equity))
    # Smooth scaling: r = base_r * sqrt(eq / 10k)
    scale = math.sqrt(eq / 10000.0)
    r_dyn = base_r * scale
    # Clamp between floors/ceilings
    r_floor = 0.003
    r_ceiling = 0.010
    try:
        if isinstance(atr_percent, float) and atr_percent > 0.07:
            r_ceiling = 0.008
    except Exception:
        pass
    r_dyn = max(r_floor, min(r_dyn, r_ceiling))
    # Respect news suggested absolute cap if provided (safer of the two)
    try:
        news_r = float(news_ctx.get('risk_r_per_trade')) if news_ctx and ('risk_r_per_trade' in news_ctx) else None
        if news_r is not None:
            r_dyn = min(r_dyn, news_r)
    except Exception:
        pass
    # Final clamp to global max from config
    r_max = float(cfg.get('risk', {}).get('max_r_per_trade', 0.01))
    return max(0.0, min(r_dyn, r_max))


def _count_open_positions(api: BinanceWrapper) -> int:
    try:
        pos = api.client.futures_position_information()
        cnt = 0
        for p in pos:
            try:
                amt = abs(float(p.get('positionAmt', 0)))
                symbol = p.get('symbol', '')
                if amt > 0 and symbol.endswith('USDT'):
                    cnt += 1
            except Exception:
                continue
        return cnt
    except Exception:
        return 0


def _compute_dynamic_leverage(stop_distance: float, entry: float, atr_percent: float, rr: float, mode: str) -> int:
    # Stop distance in percent
    stop_pct = (abs(stop_distance) / max(1e-9, entry))
    # Baseline min and max
    lev_min = 5
    lev_max = 20
    # Start with min
    lev = lev_min
    # Favor higher leverage for tighter stops and better RR
    if stop_pct <= 0.005 and rr >= 1.5:
        lev = 20
    elif stop_pct <= 0.008 and rr >= 1.2:
        lev = 15
    elif stop_pct <= 0.012:
        lev = 10
    else:
        lev = 5
    return int(max(lev_min, min(lev, lev_max)))

def _cleanup_stale_protection_orders(api: BinanceWrapper, debug: bool = False) -> None:
    # Aggressive cleanup: for symbols without an active position, cancel ALL closePosition protection orders
    try:
        pos = api.client.futures_position_information()
        has_pos = set()
        for p in pos:
            try:
                if abs(float(p.get('positionAmt', 0))) > 0:
                    has_pos.add(p.get('symbol', ''))
            except Exception:
                continue
        oo = api.client.futures_get_open_orders()
        protected_types = {"STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET"}
        for o in oo:
            try:
                sym = o.get('symbol', '')
                typ = o.get('type')
                close_pos = str(o.get('closePosition', 'false')).lower() == 'true'
                if typ not in protected_types or not close_pos:
                    continue
                if sym in has_pos:
                    continue
                api.client.futures_cancel_order(symbol=sym, orderId=o['orderId'])
                if debug:
                    print(f"DEBUG {sym}: canceled protection order {o['orderId']} ({typ}) (no active position)")
            except Exception:
                continue
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ignore-trend", action="store_true")
    parser.add_argument("--override-direction", choices=["long", "short", "both"], default=None)
    parser.add_argument("--min-score", type=float, default=0.65)
    parser.add_argument("--relaxed", action="store_true")
    parser.add_argument("--fallback-on-trend", choices=["none", "relaxed", "ignore"], default="relaxed")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--margin", choices=["ISOLATED", "CROSSED"], default="CROSSED")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--entry-style", choices=["stop","retest"], default="stop")
    parser.add_argument("--only-core-triggers", action="store_true")
    parser.add_argument("--news-mode", choices=["off", "auto", "force"], default="off")
    parser.add_argument("--protect", action="store_true", help="Attach SL/TP for existing positions if missing and exit")
    args = parser.parse_args()

    cfg = load_config()
    api = BinanceWrapper()

    # Always clean up protection orders for symbols without active position at startup
    _cleanup_stale_protection_orders(api, debug=args.debug)
    
    # Failsafe: if executing, ensure existing positions have SL/TP protection
    if args.execute:
        try:
            ex_boot = FuturesExecutor(api.client)
            pos_boot = api.client.futures_position_information()
        except Exception:
            pos_boot = []
        for p in pos_boot:
            try:
                amt = float(p.get('positionAmt', 0.0))
                if amt == 0.0:
                    continue
                symp = p.get('symbol')
                sidep = 'LONG' if amt > 0 else 'SHORT'
                k1h_p = api.klines(symp, interval="1h", limit=120)
                k15_p = api.klines(symp, interval="15m", limit=60)
                pdh_p, pdl_p = previous_day_levels(k1h_p)
                highs_p = [k["high"] for k in k15_p]
                lows_p = [k["low"] for k in k15_p]
                closes_p = [k["close"] for k in k15_p]
                from src.indicators import atr as atr_calc
                atr15_p = atr_calc(highs_p, lows_p, closes_p, 15) if closes_p else float('nan')
                try:
                    f = api.get_futures_symbol_filters(symp)
                    tickp = float(f.get('tickSize') or 0.01)
                except Exception:
                    tickp = 0.01
                entryp = float(p.get('entryPrice', closes_p[-1] if closes_p else 0.0))
                if sidep == 'LONG':
                    wick_low_p = min(k["low"] for k in k15_p[-5:]) if k15_p else entryp
                    base_low_p = min(wick_low_p, pdl_p) if not math.isnan(pdl_p) else wick_low_p
                    sls = structural_sl_long(entryp, base_low_p, atr15_p, tickp, cfg)
                    min_mult = float(cfg["stops"]["min_atr_mult"]) ; max_mult = float(cfg["stops"]["max_atr_mult"]) 
                    dist = entryp - sls
                    target_dist = min(max(dist, min_mult * atr15_p), max_mult * atr15_p)
                    slp = entryp - target_dist
                    t1p, t2p = smc_targets_long(k1h_p, entryp, pdh_p)
                else:
                    wick_high_p = max(k["high"] for k in k15_p[-5:]) if k15_p else entryp
                    base_high_p = max(wick_high_p, pdh_p) if not math.isnan(pdh_p) else wick_high_p
                    sls = structural_sl_short(entryp, base_high_p, atr15_p, tickp, cfg)
                    min_mult = float(cfg["stops"]["min_atr_mult"]) ; max_mult = float(cfg["stops"]["max_atr_mult"]) 
                    dist = sls - entryp
                    target_dist = min(max(dist, min_mult * atr15_p), max_mult * atr15_p)
                    slp = entryp + target_dist
                    t1p, t2p = smc_targets_short(k1h_p, entryp, pdl_p)
                ex_boot.ensure_protection(symbol=symp, side=sidep, sl=slp, tp1=t1p, tp2=t2p)
            except Exception:
                continue

    # Detect available quote (USDT/USDC) by balance
    preferred_quote = cfg["runtime"].get("quote", "USDT")
    try:
        bals = api.client.futures_account_balance()
        bal_map = {b.get('asset'): float(b.get('availableBalance', b.get('balance', 0.0))) for b in bals}
        if bal_map.get('USDC', 0.0) > 0 and bal_map.get('USDT', 0.0) == 0.0:
            preferred_quote = 'USDC'
    except Exception:
        pass

    # Update base symbol to BTC + preferred quote
    base = f"BTC{preferred_quote}"

    btc_1d = api.klines(base, interval="1d", limit=220)
    btc_4h = api.klines(base, interval="4h", limit=220)

    fr = api.funding_rate(base, limit=8)
    funding_abs = abs(float(fr[-1]["fundingRate"])) if fr else 0.0

    reg = regime_filter(cfg, btc_1d, btc_4h, funding_abs)

    # Protection-only mode: ensure SL/TP for current positions
    if args.protect:
        ex = FuturesExecutor(api.client)
        try:
            pos = api.client.futures_position_information()
        except Exception:
            pos = []
        for p in pos:
            try:
                amt = float(p.get('positionAmt', 0.0))
                if amt == 0.0:
                    continue
                sym = p.get('symbol')
                side = 'LONG' if amt > 0 else 'SHORT'
                # compute SMC targets based on current context
                k1h_c = api.klines(sym, interval="1h", limit=120)
                k15_c = api.klines(sym, interval="15m", limit=60)
                pdh, pdl = previous_day_levels(k1h_c)
                closes15 = [k["close"] for k in k15_c]
                highs15 = [k["high"] for k in k15_c]
                lows15 = [k["low"] for k in k15_c]
                from src.indicators import atr as atr_calc
                atr15 = atr_calc(highs15, lows15, closes15, 15)
                try:
                    f = api.get_futures_symbol_filters(sym)
                    tick = float(f.get('tickSize') or 0.01)
                except Exception:
                    tick = 0.01
                entry = float(p.get('entryPrice', closes15[-1])) if closes15 else float(p.get('entryPrice', 0.0))
                if side == 'LONG':
                    wick_low = min(k["low"] for k in k15_c[-5:]) if k15_c else entry
                    base_low = min(wick_low, pdl) if not math.isnan(pdl) else wick_low
                    sl_struct = structural_sl_long(entry, base_low, atr15, tick, cfg)
                    min_mult = float(cfg["stops"]["min_atr_mult"]) ; max_mult = float(cfg["stops"]["max_atr_mult"]) 
                    dist = entry - sl_struct
                    target_dist = min(max(dist, min_mult * atr15), max_mult * atr15)
                    sl = entry - target_dist
                    t1, t2 = smc_targets_long(k1h_c, entry, pdh)
                else:
                    wick_high = max(k["high"] for k in k15_c[-5:]) if k15_c else entry
                    base_high = max(wick_high, pdh) if not math.isnan(pdh) else wick_high
                    sl_struct = structural_sl_short(entry, base_high, atr15, tick, cfg)
                    min_mult = float(cfg["stops"]["min_atr_mult"]) ; max_mult = float(cfg["stops"]["max_atr_mult"]) 
                    dist = sl_struct - entry
                    target_dist = min(max(dist, min_mult * atr15), max_mult * atr15)
                    sl = entry + target_dist
                    t1, t2 = smc_targets_short(k1h_c, entry, pdl)
                ex.ensure_protection(symbol=sym, side=side, sl=sl, tp1=t1, tp2=t2)
            except Exception:
                continue
        return

    # News context
    news_ctx = resolve_news_context(mode=args.news_mode, lookback_min=45)
    if args.debug and news_ctx.get('active'):
        print("NEWS MODE:", news_ctx)
    if not reg["ok"] and not args.ignore_trend:
        # SOFT REGIME: do not veto; switch to relaxed scan and reduce risk via risk_scale
        forced_relaxed = True
        if args.debug:
            print("SOFT-REGIME: regime failed, continuing with relaxed scan and risk scaling", reg)

    k4h = btc_4h
    k1h = api.klines(base, interval="1h", limit=200)
    k15 = api.klines(base, interval="15m", limit=200)
    tr = trend_filter(cfg, k4h, k1h, k15)

    # Fallback control flags (preserve previous forced_relaxed)
    forced_relaxed = forced_relaxed if 'forced_relaxed' in locals() else False
    forced_direction = None

    if args.override_direction is not None:
        direction = args.override_direction
        tr_ok = True
    else:
        direction = tr["direction"]
        tr_ok = tr["ok"]

    if not tr_ok and not args.ignore_trend:
        if args.fallback_on_trend == "ignore":
            direction = args.override_direction or "both"
            tr_ok = True
            if args.debug:
                print("FALLBACK: ignoring trend filter, scanning direction=", direction)
        elif args.fallback_on_trend == "relaxed":
            forced_relaxed = True
            direction = args.override_direction or "both"
            tr_ok = True
            if args.debug:
                print("FALLBACK: switching to relaxed mode and scanning both directions")
        else:
            print("WAIT: trend not aligned", tr)
            return

    day = DayState()

    # Mode selection: flag overrides auto
    if args.relaxed or forced_relaxed or news_ctx.get('force_relaxed'):
        mode = 'relaxed'
    else:
        mode = 'strict'
        if (not reg['ok']) or day.state.get('net_R', 0.0) > 0.0:
            mode = 'relaxed'

    mcfg = cfg['modes'][mode]

    # Thresholds by mode (explicit for relaxed)
    if mode == 'relaxed':
        wick_min = float(mcfg['wick_min'])
        vol_sig = float(mcfg['volume_spike_sigma'])
        # boosted relaxed thresholds
        min_rr = 0.65
        min_score = 0.48
    else:
        wick_min = cfg["entry"]["wick_min_ratio"]
        vol_sig = cfg["entry"]["volume_spike_sigma"]
        min_rr = cfg["take_profits"]["t1_rr"]
        min_score = float(mcfg['min_score'])

    # Universe by preferred quote
    # Forward config min volume to scorer via ENV (keeps module decoupled)
    try:
        os.environ['CONFIG_MIN_24H_VOL_USD'] = str(cfg['runtime'].get('min_24h_volume_usd', 0))
    except Exception:
        pass
    scored, details = build_universe_scores(api, base_symbol=base, top_n=300)
    scored_quote = [(s, sc) for (s, sc) in scored if s.endswith(preferred_quote) and sc >= min_score]
    universe = [s for (s, sc) in scored_quote[: cfg["runtime"]["universe_top_n"]]]
    if not universe:
        universe = [s for (s, sc) in [(s, sc) for (s, sc) in scored if s.endswith(preferred_quote)][: cfg["runtime"]["universe_top_n"]]]

    print(f"Universe: {universe} | mode={mode}")
    if args.debug and isinstance(details, dict) and ('_meta' in details):
        try:
            print("DEBUG universe meta:", details['_meta'])
        except Exception:
            pass

    # Fetch real USDⓈ-M equity with multi-assets support (use totalAvailableBalance)
    equity = float(cfg["runtime"].get("demo_equity", 10000))
    try:
        acct = api.client.futures_account()
        src_dbg = []
        tab = acct.get('totalAvailableBalance')
        tmb = acct.get('totalMarginBalance')
        if tab is not None:
            equity = float(tab)
            src_dbg.append(f"totalAvailableBalance={tab}")
        elif tmb is not None:
            equity = float(tmb)
            src_dbg.append(f"totalMarginBalance={tmb}")
        else:
            assets = acct.get('assets', [])
            bal = sum(float(a.get('availableBalance', 0.0)) for a in assets)
            equity = bal
            src_dbg.append(f"assets_sum={bal}")
        if args.debug:
            print("DEBUG equity sources:", ", ".join(src_dbg))
    except Exception:
        pass
    equity = max(0.0, equity)
    if args.debug:
        print(f"DEBUG equity (USDⓈ-M): {equity:.2f}")

    # If executing, set per-mode risk (equity-scaled)
    if args.execute:
        cfg["risk"]["r_per_trade"] = _compute_dynamic_r_per_trade(equity, reg.get("metrics", {}).get("atr_percent", float("nan")), mode, news_ctx, cfg)

    # Apply regime risk scaler
    if 'risk_scale' in reg and isinstance(reg['risk_scale'], float) and args.execute:
        cfg["risk"]["r_per_trade"] *= max(0.2, min(1.0, reg['risk_scale']))
    risk = RiskEngine(cfg, equity=equity)
    if not risk.can_trade_today():
        print("NO-TRADE: daily loss cap reached")
        return

    atrp = reg.get("metrics", {}).get("atr_percent", float("nan"))
    lev_cap_base = float(news_ctx.get('leverage_cap', cfg["risk"]["leverage_cap"]))
    lev_cap_dyn = _dynamic_leverage_cap(atrp, lev_cap_base)

    directions_to_scan = [direction] if direction in ("long", "short") else ["long", "short"]

    ai = ShadowAIScorer()

    # Build symbol guards: open positions and open orders
    try:
        pos_info = api.client.futures_position_information()
        pos_symbols = set(s['symbol'] for s in pos_info if abs(float(s.get('positionAmt', 0))) > 0)
    except Exception:
        pos_symbols = set()
    try:
        open_orders = api.client.futures_get_open_orders()
        ord_symbols = set(o['symbol'] for o in open_orders)
    except Exception:
        ord_symbols = set()

    # Global cleanup: remove zombie SL/TP for symbols without position
    _cleanup_stale_protection_orders(api, debug=args.debug)

    plans = []

    for sym in universe:
        # Skip only if there is an active position; open orders are handled with TTL refresh later
        if sym in pos_symbols:
            if args.debug:
                print(f"DEBUG {sym}: skip due to existing position")
            continue

        # Fetch coin data
        k4h_c = api.klines(sym, interval="4h", limit=220)
        k1h_c = api.klines(sym, interval="1h", limit=60)
        k15_c = api.klines(sym, interval="15m", limit=200)
        # 5m micro-timing for relaxed mode
        k5m_c = api.klines(sym, interval="5m", limit=5) if mode == 'relaxed' else []
        if not k15_c or not k1h_c or not k4h_c:
            if args.debug:
                print(f"DEBUG {sym}: skip (insufficient bars)")
            continue

        # Coin-based trend
        ct = trend_filter(cfg, k4h_c, k1h_c, k15_c)
        ct_ok = bool(ct.get('ok'))
        ct_dir = ct.get('direction')
        rs_val = details.get(sym, {}).get('RS', 0.5)

        # Strict: hard gate on coin trend + BTC direction alignment (with RS override)
        # Relaxed: do NOT hard gate; treat trend as confluence if aligned
        eff_dirs: List[str] = []
        if args.override_direction == 'both':
            eff_dirs = ['long', 'short']
        elif direction in ("long", "short"):
            eff_dirs = [direction]
        else:
            eff_dirs = [ct_dir] if ct_dir in ("long","short") else ['long','short']

        if mode == 'strict':
            if not ct_ok:
                if args.debug: print(f"DEBUG {sym}: coin trend not ok")
                continue
            if direction in ("long","short") and ct_dir != direction:
                if rs_val >= float(cfg['modes']['strict']['ignore_trend_if_RS']):
                    if args.debug: print(f"DEBUG {sym}: RS override (RS={rs_val:.2f}) allowing coin dir {ct_dir} vs BTC {direction}")
                    eff_dirs = [ct_dir]
                else:
                    if args.debug: print(f"DEBUG {sym}: coin dir {ct_dir} != BTC {direction} and RS={rs_val:.2f} < {cfg['modes']['strict']['ignore_trend_if_RS']}")
                    continue

        pdh, pdl = previous_day_levels(k1h_c)

        highs15 = [k["high"] for k in k15_c]
        lows15 = [k["low"] for k in k15_c]
        closes15 = [k["close"] for k in k15_c]
        from src.indicators import atr as atr_calc
        atr15 = atr_calc(highs15, lows15, closes15, 15)
        if math.isnan(atr15):
            atr15 = max(1e-9, (highs15[-1] - lows15[-1]))
        # ATR% floor: skip low-vol coins entirely
        try:
            min_atr_pct = float(cfg['runtime'].get('min_coin_atr_percent', 0.0))
        except Exception:
            min_atr_pct = 0.0
        if min_atr_pct and (closes15[-1] > 0) and ((atr15 / closes15[-1]) < min_atr_pct):
            if args.debug:
                print(f"DEBUG {sym}: skip due to low ATR% {(atr15/closes15[-1])*100:.2f}% < {min_atr_pct*100:.2f}%")
            continue

        if mode == 'relaxed':
            if not day.can_signal(int(mcfg['hourly_signal_cap'])):
                if args.debug: print('DEBUG: hourly cap reached')
                continue
            if day.on_cooldown(sym):
                if args.debug: print(f'DEBUG {sym}: cooldown active')
                continue

        for d in eff_dirs:
            found = False
            # confluence minimum required points: 1 in relaxed, 2 in strict
            conf_min = 1 if mode == 'relaxed' else 2
            for i in range(40, 0, -1):
                sub = k15_c if i == 1 else k15_c[:-(i-1)]
                if len(sub) < 20:
                    continue
                price = sub[-1]["close"]
                if d == "long":
                    # Triggers
                    if args.only_core_triggers:
                        sh = False
                        rv = False
                        pb = pullback_long_signal(sub, k1h_c)
                        bo = breakout_retest_long(sub, pdh)
                    else:
                        sh = stop_hunt_long_signal(sub, pdl, wick_min_ratio=wick_min, vol_sigma=vol_sig)
                        rv = relaxed_long_signal(sub, k1h_c, wick_min_ratio=wick_min, vol_sigma=vol_sig) if mcfg['enable_vwap_trigger'] else False
                        pb = pullback_long_signal(sub, k1h_c)
                        bo = breakout_retest_long(sub, pdh)
                    ok = sh or rv or pb or bo
                    if not ok:
                        continue
                    # Determine tick size for symbol
                    try:
                        f = api.get_futures_symbol_filters(sym)
                        tick = float(f.get('tickSize') or 0.01)
                    except Exception:
                        tick = 0.01
                    # Retest-based entry: prefer retest level but never below current price
                    entry = price
                    if not math.isnan(pdl):
                        entry = max(price, pdl + tick)
                    # Use wider wick window and anchor SL below PDL when available
                    wick_low = min(k["low"] for k in sub[-5:])
                    base_low = min(wick_low, pdl) if not math.isnan(pdl) else wick_low
                    sl_struct = structural_sl_long(entry, base_low, atr15, tick, cfg)
                    # Clamp SL distance into [min_atr_mult, max_atr_mult] * ATR
                    min_mult = float(cfg["stops"]["min_atr_mult"]) ; max_mult = float(cfg["stops"]["max_atr_mult"]) 
                    dist = entry - sl_struct
                    target_dist = min(max(dist, min_mult * atr15), max_mult * atr15)
                    sl = entry - target_dist
                    from src.liquidity import validate_stop_distance_dynamic
                    if mode == 'relaxed':
                        ok_dist = validate_stop_distance_dynamic(entry, sl, atr15, cfg)
                    else:
                        ok_dist = validate_stop_distance(entry, sl, atr15, cfg)
                    if not ok_dist:
                        if args.debug:
                            print(f"DEBUG {sym} LONG: invalid stop distance (entry {entry:.4f} sl {sl:.4f} atr15 {atr15:.6f})")
                        continue
                    # SMC targets for long
                    t1, t2 = smc_targets_long(k1h_c, entry, pdh)
                    # Enforce minimum T1 distance from entry to avoid TP at entry
                    try:
                        min_t1_mul = float(cfg["take_profits"].get("min_t1_atr_mult", 0.6))
                    except Exception:
                        min_t1_mul = 0.6
                    min_delta1 = max(min_t1_mul * atr15, 3 * tick)
                    if t1 < entry + min_delta1:
                        t1 = entry + min_delta1
                    # Enforce minimum T2 distance from T1
                    try:
                        min_t2_mul = float(cfg["take_profits"].get("min_t2_atr_mult", 0.8))
                    except Exception:
                        min_t2_mul = 0.8
                    min_delta2 = max(min_t2_mul * atr15, 3 * tick)
                    if t2 < t1 + min_delta2:
                        t2 = t1 + min_delta2
                    stop_dist = max(1e-9, entry - sl)
                    rr = (t1 - entry) / stop_dist if stop_dist else 0.0
                    # Confluence points
                    conf_points = 0
                    if mode == 'strict':
                        conf_points += 1
                    else:
                        if ct_ok and ct_dir == 'long': conf_points += 1
                    if sh: conf_points += 1
                    if rv: conf_points += 1
                    if pb: conf_points += 1
                    if bo: conf_points += 1
                    if rr >= min_rr: conf_points += 1
                    if conf_points < conf_min:
                        if args.debug: print(f"DEBUG {sym} LONG: conf {conf_points} < {conf_min}")
                        continue
                    qty = risk.position_size(stop_dist)
                    if (entry <= 0) or (qty <= 0):
                        if args.debug:
                            print(f"DEBUG {sym} LONG: invalid sizing entry={entry} qty={qty}")
                        continue
                    # Compute dynamic leverage for this trade
                    lev = _compute_dynamic_leverage(stop_dist, entry, atrp, rr, mode)
                    # Cap notional by chosen leverage
                    max_notional = equity * lev
                    if entry * qty > max_notional:
                        scale = max_notional / (entry * qty)
                        qty *= scale
                    # Notional cap: allow up to dynamic leverage cap; min notional handled at execution layer
                    max_notional = equity * lev_cap_dyn
                    if entry * qty > max_notional:
                        scale = max_notional / (entry * qty)
                        qty *= scale
                    plans.append(("LONG", sym, entry, sl, t1, t2, qty, lev, rr))
                    ai.log({
                        'ts': datetime.utcnow().isoformat(),
                        'symbol': sym,
                        'side': 'LONG',
                        'entry': entry,
                        'sl': sl,
                        't1': t1,
                        'rr': rr,
                        'mode': mode,
                        'features': {k: details.get(sym, {}).get(k) for k in ('L','M','F','C','RS')},
                        'ai_prob': ai.score({k: details.get(sym, {}).get(k, 0.5) for k in ('L','M','F','C','RS')}),
                        'realized_R': None
                    })
                    found = True
                    break
                else:
                    if args.only_core_triggers:
                        shs = False
                        rvs = False
                        pbs = pullback_short_signal(sub, k1h_c)
                        bos = breakout_retest_short(sub, pdl)
                    else:
                        shs = stop_hunt_short_signal(sub, pdh, wick_min_ratio=wick_min, vol_sigma=vol_sig)
                        rvs = relaxed_short_signal(sub, k1h_c, wick_min_ratio=wick_min, vol_sigma=vol_sig) if mcfg['enable_vwap_trigger'] else False
                        pbs = pullback_short_signal(sub, k1h_c)
                        bos = breakout_retest_short(sub, pdl)
                    ok = shs or rvs or pbs or bos
                    if not ok:
                        continue
                    # Determine tick size for symbol
                    try:
                        f = api.get_futures_symbol_filters(sym)
                        tick = float(f.get('tickSize') or 0.01)
                    except Exception:
                        tick = 0.01
                    # Retest-based entry for short: prefer retest but never above current price
                    entry = price
                    if not math.isnan(pdh):
                        entry = min(price, pdh - tick)
                    wick_high = max(k["high"] for k in sub[-5:])
                    base_high = max(wick_high, pdh) if not math.isnan(pdh) else wick_high
                    sl_struct = structural_sl_short(entry, base_high, atr15, tick, cfg)
                    min_mult = float(cfg["stops"]["min_atr_mult"]) ; max_mult = float(cfg["stops"]["max_atr_mult"]) 
                    dist = sl_struct - entry
                    target_dist = min(max(dist, min_mult * atr15), max_mult * atr15)
                    sl = entry + target_dist
                    from src.liquidity import validate_stop_distance_dynamic
                    if mode == 'relaxed':
                        ok_dist = validate_stop_distance_dynamic(entry, sl, atr15, cfg)
                    else:
                        ok_dist = validate_stop_distance(entry, sl, atr15, cfg)
                    if not ok_dist:
                        if args.debug:
                            print(f"DEBUG {sym} SHORT: invalid stop distance (entry {entry:.4f} sl {sl:.4f} atr15 {atr15:.6f})")
                        continue
                    # SMC targets for short
                    t1, t2 = smc_targets_short(k1h_c, entry, pdl)
                    # Enforce minimum T1 distance from entry to avoid TP at entry
                    try:
                        min_t1_mul = float(cfg["take_profits"].get("min_t1_atr_mult", 0.6))
                    except Exception:
                        min_t1_mul = 0.6
                    min_delta1 = max(min_t1_mul * atr15, 3 * tick)
                    if t1 > entry - min_delta1:
                        t1 = entry - min_delta1
                    # Enforce minimum T2 distance from T1
                    try:
                        min_t2_mul = float(cfg["take_profits"].get("min_t2_atr_mult", 0.8))
                    except Exception:
                        min_t2_mul = 0.8
                    min_delta2 = max(min_t2_mul * atr15, 3 * tick)
                    if t2 > t1 - min_delta2:
                        t2 = t1 - min_delta2
                    stop_dist = max(1e-9, sl - entry)
                    rr = (entry - t1) / stop_dist if stop_dist else 0.0
                    conf_points = 0
                    if mode == 'strict':
                        conf_points += 1
                    else:
                        if ct_ok and ct_dir == 'short': conf_points += 1
                    if shs: conf_points += 1
                    if rvs: conf_points += 1
                    if pbs: conf_points += 1
                    if bos: conf_points += 1
                    if rr >= min_rr: conf_points += 1
                    if conf_points < conf_min:
                        if args.debug: print(f"DEBUG {sym} SHORT: conf {conf_points} < {conf_min}")
                        continue
                    qty = risk.position_size(stop_dist)
                    if (entry <= 0) or (qty <= 0):
                        if args.debug:
                            print(f"DEBUG {sym} SHORT: invalid sizing entry={entry} qty={qty}")
                        continue
                    lev = _compute_dynamic_leverage(stop_dist, entry, atrp, rr, mode)
                    max_notional = equity * lev
                    if entry * qty > max_notional:
                        scale = max_notional / (entry * qty)
                        qty *= scale
                    max_notional = equity * lev_cap_dyn
                    if entry * qty > max_notional:
                        scale = max_notional / (entry * qty)
                        qty *= scale
                    plans.append(("SHORT", sym, entry, sl, t1, t2, qty, lev, rr))
                    ai.log({
                        'ts': datetime.utcnow().isoformat(),
                        'symbol': sym,
                        'side': 'SHORT',
                        'entry': entry,
                        'sl': sl,
                        't1': t1,
                        'rr': rr,
                        'mode': mode,
                        'features': {k: details.get(sym, {}).get(k) for k in ('L','M','F','C','RS')},
                        'ai_prob': ai.score({k: details.get(sym, {}).get(k, 0.5) for k in ('L','M','F','C','RS')}),
                        'realized_R': None
                    })
                    found = True
                    break
            if not found and args.debug:
                print(f"DEBUG {sym}: no signal")

    if not plans:
        print("NO PLANS FOUND under current settings")
        return

    # Enforce max open positions per mode (existing guard is already applied before)
    open_cnt = _count_open_positions(api)
    allowed_slots = max(0, int(mcfg['max_open']) - open_cnt)
    if allowed_slots <= 0:
        print("NO-EXECUTE: max open positions reached for mode")
        return

    # Sort by RR desc and apply correlation filter to diversify
    plans.sort(key=lambda x: x[-1], reverse=True)
    diversified = []
    try:
        # prefetch recent 1h returns for correlation screening
        hist_closes = {}
        for _, sym, *_ in plans:
            try:
                k1h_corr = api.klines(sym, interval="1h", limit=100)
                hist_closes[sym] = [k["close"] for k in k1h_corr]
            except Exception:
                continue
        def corr(a, b):
            try:
                import numpy as _np
                x = _np.array(a[-80:]); y = _np.array(b[-80:])
                if len(x) != len(y) or len(x) < 10:
                    return 0.0
                xr = _np.diff(x) / x[:-1]
                yr = _np.diff(y) / y[:-1]
                if _np.std(xr)==0 or _np.std(yr)==0:
                    return 0.0
                return float(_np.corrcoef(xr, yr)[0,1])
            except Exception:
                return 0.0
        seg_count = {}
        max_per_seg = 2
        def segment_of(symbol: str) -> str:
            # simple segment heuristic by prefix
            return symbol[:3]
        for p in plans:
            _, sym, *_ = p
            ok = True
            # correlation with already picked
            for _, s2, *_ in diversified:
                if sym in hist_closes and s2 in hist_closes:
                    if abs(corr(hist_closes[sym], hist_closes[s2])) > 0.8:
                        ok = False
                        break
            if not ok:
                continue
            seg = segment_of(sym)
            if seg_count.get(seg, 0) >= max_per_seg:
                continue
            diversified.append(p)
            seg_count[seg] = seg_count.get(seg, 0) + 1
        if diversified:
            plans = diversified
    except Exception:
        pass

    if not args.execute or args.dry_run:
        for side, sym, entry, sl, t1, t2, qty, lev, rr in plans[:allowed_slots]:
            print(f"PLAN {side} {sym} | mode {mode} | entry {entry:.4f} SL {sl:.4f} T1 {t1:.4f} T2 {t2:.4f} qty {qty:.4f} lev {lev:.2f}x RR {rr:.2f}")
        return

    # Extend order TTL to 6 bars (15m bars implied)
    # Dynamic min notional: max(150, min(2% of equity, 400))
    min_notional_floor = float(cfg.get('risk', {}).get('min_notional_floor_usd', 50))
    dyn_floor = max(150.0, min(0.02 * equity, 400.0))
    ex = FuturesExecutor(api.client, cancel_after_bars=4, min_notional_floor_usd=max(min_notional_floor, dyn_floor))
    # Ensure multi-assets mode ON to use USDC for USDT-margined contracts if account supports it
    try:
        api.client.futures_change_multi_assets_margin(multiAssetsMargin='true')
    except Exception:
        pass

    for side, sym, entry, sl, t1, t2, qty, lev, rr in plans[:allowed_slots]:
        # Final guard before placing: cancel any stale open orders for symbol
        try:
            # Manage existing open orders: skip if active entry order exists; cancel near-expiry or reprice retest limit
            oo = api.client.futures_get_open_orders(symbol=sym)
            skip_due_entry = False
            for o in oo:
                try:
                    reduce_only = str(o.get('reduceOnly','false')).lower() == 'true'
                    close_pos = str(o.get('closePosition','false')).lower() == 'true'
                    typ = o.get('type')
                    if (not reduce_only) and (not close_pos) and typ in ('STOP','LIMIT'):
                        od = api.client.futures_get_order(symbol=sym, orderId=o['orderId'])
                        upd = float(od.get('updateTime', 0)) / 1000.0
                        now_s = datetime.utcnow().timestamp()
                        bars_elapsed = int(max(0, (now_s - upd) / 900.0))
                        # Retest-limit auto-refresh
                        if args.entry_style == 'retest' and typ == 'LIMIT':
                            try:
                                # Compute drift and RR guards
                                # Rebuild local context (entry, sl, t1) already computed above
                                max_drift_atr = 0.8
                                drift = abs(mark - entry)
                                drift_atr = drift / max(1e-9, atr15)
                                stop_dist = abs(entry - sl)
                                rr_cur = abs(t1 - entry) / max(1e-9, stop_dist)
                                if (drift_atr > max_drift_atr) or (rr_cur < 1.0):
                                    # too far or RR poor → cancel
                                    api.client.futures_cancel_order(symbol=sym, orderId=o['orderId'])
                                    if args.debug:
                                        print(f"DEBUG {sym}: canceled retest entry due drift_atr={drift_atr:.2f} rr={rr_cur:.2f}")
                                else:
                                    # limited reprice count
                                    rp = day.get_reprice_count(sym)
                                    if rp < 2:
                                        if side == 'LONG':
                                            new_price = min(entry, max(pdl + tick if not math.isnan(pdl) else entry - 5*tick, mark - 5*tick))
                                        else:
                                            new_price = max(entry, min(pdh - tick if not math.isnan(pdh) else entry + 5*tick, mark + 5*tick))
                                        new_price_s = f"{new_price:.{len(str(tick).split('.')[-1])}f}"
                                        api.client.futures_cancel_order(symbol=sym, orderId=o['orderId'])
                                        api.client.futures_create_order(symbol=sym, side=('BUY' if side=='LONG' else 'SELL'), type='LIMIT', timeInForce='GTC', price=new_price_s, quantity=od.get('origQty'))
                                        day.inc_reprice(sym)
                                        if args.debug:
                                            print(f"DEBUG {sym}: repriced retest entry to {new_price_s} (count={rp+1})")
                                    else:
                                        # max reprices reached → keep order until TTL or cancel if near-expiry
                                        pass
                            except Exception:
                                pass
                        # TTL cleanup
                        if bars_elapsed >= ex.cancel_after_bars - 2:
                            api.client.futures_cancel_order(symbol=sym, orderId=o['orderId'])
                            if args.debug:
                                print(f"DEBUG {sym}: canceled near-expiry entry {o['orderId']} ({typ})")
                        else:
                            skip_due_entry = True
                    # also purge any leftover protection for safety handled elsewhere
                except Exception:
                    pass
            if skip_due_entry:
                if args.debug:
                    print(f"DEBUG {sym}: skip due to existing entry order")
                continue
        except Exception:
            pass

        # Skip if position appeared in the meantime
        try:
            pi = api.client.futures_position_information(symbol=sym)
            if any(abs(float(p.get('positionAmt', 0))) > 0 for p in pi):
                if args.debug:
                    print(f"DEBUG {sym}: position opened meanwhile, skip new entry")
                continue
        except Exception:
            pass

        api.set_margin_type(sym, args.margin)
        api.set_leverage(sym, lev)
        # Use stop-limit entry to avoid immediate fills and prevent -2021 immediate trigger
        try:
            f = api.get_futures_symbol_filters(sym)
            tick = float(f.get('tickSize') or 0.01)
        except Exception:
            tick = 0.01
        try:
            mark = float(api.mark_price(sym))
        except Exception:
            mark = entry
        if args.entry_style == 'stop':
            if side == 'LONG':
                stop_px = max(entry, mark + 3 * tick)
                limit_px = stop_px + 2 * tick
            else:
                stop_px = min(entry, mark - 3 * tick)
                limit_px = stop_px - 2 * tick
            res = ex.place_stop_limit_with_sl_tp(symbol=sym, side=side, stop=stop_px, limit=limit_px, qty=qty, sl=sl, tp1=t1, tp2=t2)
        else:
            # retest limit: set price favorable vs current mark within safe bounds
            if side == 'LONG':
                # prefer slightly below mark but not below structural anchor
                base_low = min([k["low"] for k in k15_c[-5:]]) if k15_c else entry
                retest_px = min(entry, max(pdl + tick if not math.isnan(pdl) else base_low + tick, mark - 5 * tick))
            else:
                base_high = max([k["high"] for k in k15_c[-5:]]) if k15_c else entry
                retest_px = max(entry, min(pdh - tick if not math.isnan(pdh) else base_high - tick, mark + 5 * tick))
            res = ex.place_limit_with_sl_tp(symbol=sym, side=side, price=retest_px, qty=qty, sl=sl, tp1=t1, tp2=t2)
        print(f"EXECUTED {side} {sym} | entry {entry:.4f} qty {qty:.4f} -> {res} (T2 target {t2:.4f})")
        try:
            # Failsafe: ensure protections present even if fill was late
            ex.ensure_protection(symbol=sym, side=side, sl=sl, tp1=t1, tp2=t2)
        except Exception:
            pass
        try:
            # Upgrade to trailing after T1 fill (best-effort)
            ex.maybe_upgrade_tp_to_trailing(symbol=sym, side=side, atr15=atr15, entry=entry)
        except Exception:
            pass
        try:
            # If position flat after exits, purge any leftover protections immediately
            ex.cancel_protection_if_flat(symbol=sym)
        except Exception:
            pass
        try:
            # Log a post-trade skeleton; realized_R to be updated by a PnL hook (future)
            ai.log({
                'ts': datetime.utcnow().isoformat(),
                'symbol': sym,
                'side': side,
                'entry': entry,
                'sl': sl,
                't1': t1,
                't2': t2,
                'qty': qty,
                'tp1_qty': res.get('tp1_qty'),
                'tp2_qty': res.get('tp2_qty'),
                'mode': mode,
                'realized_R': None,
                'post_trade': True
            })
        except Exception:
            pass

    if mode == 'relaxed':
        day.inc_signals()
        # Set cooldown for each executed symbol
        for side, sym, entry, sl, t1, t2, qty, lev, rr in plans[:allowed_slots]:
            day.set_cooldown(sym, int(mcfg['same_coin_cooldown_min']))

    # Final cleanup pass after executions: if a position got fully closed, purge any leftover protection orders
    _cleanup_stale_protection_orders(api, debug=args.debug)


if __name__ == "__main__":
    main()
