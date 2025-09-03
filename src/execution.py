from typing import Dict, Optional
import math
import time


def _round_step(value: float, step: float, mode: str = 'down') -> float:
    if step is None or step == 0:
        return value
    q = value / step
    if mode == 'down':
        q = math.floor(q)
    else:
        q = math.ceil(q)
    return q * step


def _decimals_from_step(step: float) -> int:
    s = f"{step:.12f}".rstrip('0')
    if '.' in s:
        return len(s.split('.')[1])
    return 0


def _format_by_step(value: float, step: float, mode: str = 'down') -> str:
    v = _round_step(value, step, mode)
    d = _decimals_from_step(step)
    return f"{v:.{d}f}"


class FuturesExecutor:
    def __init__(self, client, cancel_after_bars: int = 3, min_notional_floor_usd: float = 50.0):
        self.client = client
        self.cancel_after_bars = cancel_after_bars
        self.min_notional_floor_usd = float(min_notional_floor_usd)

    def _get_symbol_filters(self, symbol: str):
        ex_info = self.client.futures_exchange_info()
        sym = next((s for s in ex_info.get('symbols', []) if s.get('symbol') == symbol), None)
        tick = 0.01
        step = 0.001
        min_qty = 0.0
        min_notional = 20.0
        if sym:
            for f in sym.get('filters', []):
                ft = f.get('filterType')
                if ft == 'PRICE_FILTER':
                    tick = float(f.get('tickSize', tick))
                if ft in ('LOT_SIZE', 'MARKET_LOT_SIZE'):
                    step = float(f.get('stepSize', step))
                    min_qty = float(f.get('minQty', 0.0))
                if ft == 'MIN_NOTIONAL':
                    try:
                        min_notional = float(f.get('notional', min_notional))
                    except Exception:
                        pass
        return tick, step, min_qty, min_notional

    def cancel_all_open_orders(self, symbol: str) -> None:
        try:
            oo = self.client.futures_get_open_orders(symbol=symbol)
            for o in oo:
                try:
                    self.client.futures_cancel_order(symbol=symbol, orderId=o['orderId'])
                except Exception:
                    pass
        except Exception:
            pass

    def place_stop_limit_with_sl_tp(self, symbol: str, side: str, stop: float, limit: float, qty: float, sl: float, tp1: float, tp2: float, wait_fill_seconds: int = 120) -> Dict:
        tick, step, min_qty, min_notional = self._get_symbol_filters(symbol)

        stop_s = _format_by_step(stop, tick, mode='down' if side=='LONG' else 'up')
        limit_s = _format_by_step(limit, tick, mode='down' if side=='LONG' else 'up')
        qty_r = max(_round_step(qty, step, mode='down'), min_qty)

        notional = float(limit) * qty_r
        floor = max(min_notional, self.min_notional_floor_usd)
        if floor and notional < floor:
            target_qty = _round_step(floor / float(limit), step, mode='up')
            qty_r = max(qty_r, target_qty)
        qty_s = _format_by_step(qty_r, step, mode='down')

        order_side = 'BUY' if side == 'LONG' else 'SELL'

        try:
            order = self.client.futures_create_order(
                symbol=symbol,
                side=order_side,
                type='STOP',
                timeInForce='GTC',
                quantity=qty_s,
                price=limit_s,
                stopPrice=stop_s,
                workingType='MARK_PRICE'
            )
        except Exception as e:
            return {'error': f'entry_order_failed:{e}'}
        order_id = order.get('orderId')

        # Staged: wait for fill (poll), then attach SL/TP with safe buffers
        filled = False
        deadline = time.time() + wait_fill_seconds
        while time.time() < deadline:
            try:
                od = self.client.futures_get_order(symbol=symbol, orderId=order_id)
                status = od.get('status')
                if status == 'FILLED':
                    filled = True
                    break
            except Exception:
                pass
            # Symbol-specific position check
            try:
                pi = self.client.futures_position_information(symbol=symbol)
                # Ensure position reflects our qty (within 10%)
                for p in pi:
                    amt = abs(float(p.get('positionAmt', 0)))
                    # attach SL/TP even on partial fill
                    if amt > 0.0:
                        filled = True
                        break
                if filled:
                    break
            except Exception:
                pass
            time.sleep(2)

        res = {'entry_order_id': order_id}
        if filled:
            # Attach SL (closePosition) and two TPs (reduceOnly LIMIT)
            # 1) SL closePosition
            mark = float(self.client.futures_mark_price(symbol=symbol)["markPrice"])
            # Stronger immediate-trigger buffer: max(5*tick, 0.05%)
            buf = max(tick * 5, mark * 0.0005)
            if side == 'LONG':
                sl_price = min(sl, mark - buf)
                sl_s = _format_by_step(sl_price, tick, mode='down')
                sl_side = 'SELL'
            else:
                sl_price = max(sl, mark + buf)
                sl_s = _format_by_step(sl_price, tick, mode='up')
                sl_side = 'BUY'
            try:
                sl_order = self.client.futures_create_order(
                    symbol=symbol,
                    side=sl_side,
                    type='STOP_MARKET',
                    stopPrice=sl_s,
                    closePosition=True,
                    workingType='MARK_PRICE',
                    priceProtect=True
                )
            except Exception as e:
                sl_order = {'orderId': None, 'error': f'sl_failed:{e}'}

            # Current position qty to split TPs safely against minQty
            pos_qty = 0.0
            try:
                pi = self.client.futures_position_information(symbol=symbol)
                for p in pi:
                    amt = float(p.get('positionAmt', 0.0))
                    if (side == 'LONG' and amt > 0) or (side == 'SHORT' and amt < 0):
                        pos_qty = abs(amt)
                        break
            except Exception:
                pos_qty = max(qty, 0.0)

            tp_side = 'SELL' if side == 'LONG' else 'BUY'
            # Enforce minimum TP distances at execution layer (post-rounding safeguard)
            try:
                min_t1_mul = 0.6
                min_t2_mul = 0.8
            except Exception:
                min_t1_mul, min_t2_mul = 0.6, 0.8
            # ATR proxy: use percent of mark if ATR not known at this layer
            atr_abs = max(mark * 0.003, tick * 3)
            min_d1 = max(min_t1_mul * atr_abs, tick * 3)
            min_d2 = max(min_t2_mul * atr_abs, tick * 3)
            # Use latest mark as price anchor if explicit price context not present
            anchor = mark
            if side == 'LONG':
                tp1 = max(tp1, float(anchor) + min_d1)
                tp2 = max(tp2, tp1 + min_d2)
            else:
                tp1 = min(tp1, float(anchor) - min_d1)
                tp2 = min(tp2, tp1 - min_d2)
            tp1_s = _format_by_step(tp1, tick, mode='up' if side=='LONG' else 'down')
            tp2_s = _format_by_step(tp2, tick, mode='up' if side=='LONG' else 'down')

            tp1_order = {'orderId': None}
            tp2_order = {'orderId': None}
            q1 = 0.0
            q2 = 0.0

            if pos_qty >= (2 * min_qty):
                # Dual TP possible
                q1 = _round_step(pos_qty * 0.5, step, mode='down')
                q2 = _round_step(pos_qty - q1, step, mode='down')
                # If q2 falls below min, collapse to single TP with full qty
                if q2 < min_qty:
                    q1 = _round_step(pos_qty, step, mode='down')
                    q2 = 0.0
                if q1 < min_qty:
                    # Fallback: single TP with all if even q1 is too small
                    q1 = _round_step(pos_qty, step, mode='down')
                    q2 = 0.0
            elif pos_qty >= min_qty:
                # Single TP only
                q1 = _round_step(pos_qty, step, mode='down')
                q2 = 0.0
            else:
                # Position smaller than minQty → use TAKE_PROFIT_MARKET closePosition
                # Apply safe buffer to avoid immediate trigger
                try:
                    mark_now = float(self.client.futures_mark_price(symbol=symbol)["markPrice"])
                except Exception:
                    mark_now = mark
                buf_tp = max(tick * 5, mark_now * 0.0005)
                if side == 'LONG':
                    tp_mkt_price = max(tp1, mark_now + buf_tp)
                    tp_mkt_s = _format_by_step(tp_mkt_price, tick, mode='up')
                else:
                    tp_mkt_price = min(tp1, mark_now - buf_tp)
                    tp_mkt_s = _format_by_step(tp_mkt_price, tick, mode='down')
                try:
                    tp1_order = self.client.futures_create_order(
                        symbol=symbol,
                        side=tp_side,
                        type='TAKE_PROFIT_MARKET',
                        stopPrice=tp_mkt_s,
                        closePosition=True,
                        workingType='MARK_PRICE',
                        priceProtect=True
                    )
                except Exception as e:
                    tp1_order = {'orderId': None, 'error': f'tp_market_failed:{e}'}

            # Place LIMIT reduceOnly TPs if quantities are valid
            if q1 >= min_qty:
                try:
                    tp1_order = self.client.futures_create_order(
                        symbol=symbol,
                        side=tp_side,
                        type='LIMIT',
                        timeInForce='GTC',
                        price=tp1_s,
                        quantity=_format_by_step(q1, step, mode='down'),
                        reduceOnly=True
                    )
                except Exception as e:
                    tp1_order = {'orderId': None, 'error': f'tp1_failed:{e}'}
            if q2 >= min_qty:
                try:
                    tp2_order = self.client.futures_create_order(
                        symbol=symbol,
                        side=tp_side,
                        type='LIMIT',
                        timeInForce='GTC',
                        price=tp2_s,
                        quantity=_format_by_step(q2, step, mode='down'),
                        reduceOnly=True
                    )
                except Exception as e:
                    tp2_order = {'orderId': None, 'error': f'tp2_failed:{e}'}

            res.update({
                'sl_order_id': sl_order.get('orderId'),
                'tp1_order_id': tp1_order.get('orderId'),
                'tp2_order_id': tp2_order.get('orderId'),
                'tp1_qty': q1,
                'tp2_qty': q2,
            })
        return res

    def place_limit_with_sl_tp(self, symbol: str, side: str, price: float, qty: float, sl: float, tp1: float, tp2: float, wait_fill_seconds: int = 120) -> Dict:
        tick, step, min_qty, min_notional = self._get_symbol_filters(symbol)

        price_s = _format_by_step(price, tick, mode='down' if side=='LONG' else 'up')
        qty_r = max(_round_step(qty, step, mode='down'), min_qty)

        notional = float(price) * qty_r
        floor = max(min_notional, self.min_notional_floor_usd)
        if floor and notional < floor:
            target_qty = _round_step(floor / float(price), step, mode='up')
            qty_r = max(qty_r, target_qty)
        qty_s = _format_by_step(qty_r, step, mode='down')

        order_side = 'BUY' if side == 'LONG' else 'SELL'

        try:
            order = self.client.futures_create_order(
                symbol=symbol,
                side=order_side,
                type='LIMIT',
                timeInForce='GTC',
                quantity=qty_s,
                price=price_s
            )
        except Exception as e:
            return {'error': f'entry_order_failed:{e}'}
        order_id = order.get('orderId')

        filled = False
        deadline = time.time() + wait_fill_seconds
        while time.time() < deadline:
            try:
                od = self.client.futures_get_order(symbol=symbol, orderId=order_id)
                status = od.get('status')
                if status == 'FILLED':
                    filled = True
                    break
            except Exception:
                pass
            try:
                pi = self.client.futures_position_information(symbol=symbol)
                for p in pi:
                    amt = abs(float(p.get('positionAmt', 0)))
                    if amt > 0.0:
                        filled = True
                        break
                if filled:
                    break
            except Exception:
                pass
            time.sleep(2)

        res = {'entry_order_id': order_id}
        if filled:
            # Attach SL (closePosition) and TP(s)
            mark = float(self.client.futures_mark_price(symbol=symbol)["markPrice"])
            buf = max(tick * 5, mark * 0.0005)
            if side == 'LONG':
                sl_price = min(sl, mark - buf)
                sl_s = _format_by_step(sl_price, tick, mode='down')
                sl_side = 'SELL'
            else:
                sl_price = max(sl, mark + buf)
                sl_s = _format_by_step(sl_price, tick, mode='up')
                sl_side = 'BUY'
            try:
                sl_order = self.client.futures_create_order(
                    symbol=symbol,
                    side=sl_side,
                    type='STOP_MARKET',
                    stopPrice=sl_s,
                    closePosition=True,
                    workingType='MARK_PRICE',
                    priceProtect=True
                )
            except Exception as e:
                sl_order = {'orderId': None, 'error': f'sl_failed:{e}'}

            # Determine position quantity to split TP safely
            pos_qty = 0.0
            try:
                pi = self.client.futures_position_information(symbol=symbol)
                for p in pi:
                    amt = float(p.get('positionAmt', 0.0))
                    if (side == 'LONG' and amt > 0) or (side == 'SHORT' and amt < 0):
                        pos_qty = abs(amt)
                        break
            except Exception:
                pos_qty = max(qty, 0.0)

            tp_side = 'SELL' if side == 'LONG' else 'BUY'
            # Enforce TP minimum distances at execution layer (anchor=limit price)
            try:
                min_t1_mul = 0.6
                min_t2_mul = 0.8
            except Exception:
                min_t1_mul, min_t2_mul = 0.6, 0.8
            atr_abs = max(mark * 0.003, tick * 3)
            d1 = max(min_t1_mul * atr_abs, tick * 3)
            d2 = max(min_t2_mul * atr_abs, tick * 3)
            anchor = float(price)
            if side == 'LONG':
                tp1 = max(tp1, anchor + d1)
                tp2 = max(tp2, tp1 + d2)
            else:
                tp1 = min(tp1, anchor - d1)
                tp2 = min(tp2, tp1 - d2)
            tp1_s = _format_by_step(tp1, tick, mode='up' if side=='LONG' else 'down')
            tp2_s = _format_by_step(tp2, tick, mode='up' if side=='LONG' else 'down')

            tp1_order = {'orderId': None}
            tp2_order = {'orderId': None}
            q1 = 0.0
            q2 = 0.0

            if pos_qty >= (2 * min_qty):
                q1 = _round_step(pos_qty * 0.5, step, mode='down')
                q2 = _round_step(pos_qty - q1, step, mode='down')
                if q2 < min_qty:
                    q1 = _round_step(pos_qty, step, mode='down')
                    q2 = 0.0
                if q1 < min_qty:
                    q1 = _round_step(pos_qty, step, mode='down')
                    q2 = 0.0
            elif pos_qty >= min_qty:
                q1 = _round_step(pos_qty, step, mode='down')
                q2 = 0.0
            else:
                # Too small → use market TP closePosition
                try:
                    mark_now = float(self.client.futures_mark_price(symbol=symbol)["markPrice"])
                except Exception:
                    mark_now = mark
                buf_tp = max(tick * 5, mark_now * 0.0005)
                if side == 'LONG':
                    tp_mkt_price = max(tp1, mark_now + buf_tp)
                    tp_mkt_s = _format_by_step(tp_mkt_price, tick, mode='up')
                else:
                    tp_mkt_price = min(tp1, mark_now - buf_tp)
                    tp_mkt_s = _format_by_step(tp_mkt_price, tick, mode='down')
                try:
                    tp1_order = self.client.futures_create_order(
                        symbol=symbol,
                        side=tp_side,
                        type='TAKE_PROFIT_MARKET',
                        stopPrice=tp_mkt_s,
                        closePosition=True,
                        workingType='MARK_PRICE',
                        priceProtect=True
                    )
                except Exception as e:
                    tp1_order = {'orderId': None, 'error': f'tp_market_failed:{e}'}

            if q1 >= min_qty:
                try:
                    tp1_order = self.client.futures_create_order(
                        symbol=symbol,
                        side=tp_side,
                        type='LIMIT',
                        timeInForce='GTC',
                        price=tp1_s,
                        quantity=_format_by_step(q1, step, mode='down'),
                        reduceOnly=True
                    )
                except Exception as e:
                    tp1_order = {'orderId': None, 'error': f'tp1_failed:{e}'}
            if q2 >= min_qty:
                try:
                    tp2_order = self.client.futures_create_order(
                        symbol=symbol,
                        side=tp_side,
                        type='LIMIT',
                        timeInForce='GTC',
                        price=tp2_s,
                        quantity=_format_by_step(q2, step, mode='down'),
                        reduceOnly=True
                    )
                except Exception as e:
                    tp2_order = {'orderId': None, 'error': f'tp2_failed:{e}'}

            res.update({
                'sl_order_id': sl_order.get('orderId'),
                'tp1_order_id': tp1_order.get('orderId'),
                'tp2_order_id': tp2_order.get('orderId'),
                'tp1_qty': q1,
                'tp2_qty': q2,
            })
        return res

    def cancel_protection_if_flat(self, symbol: str) -> Dict:
        """If position is flat, cancel any closePosition protection orders (SL/TP_MARKET)."""
        try:
            pi = self.client.futures_position_information(symbol=symbol)
            if any(abs(float(p.get('positionAmt', 0))) != 0 for p in pi):
                return {'ok': True, 'skipped': True}
        except Exception:
            return {'ok': False}
        try:
            oo = self.client.futures_get_open_orders(symbol=symbol)
        except Exception:
            oo = []
        protected_types = {"STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET", "TRAILING_STOP_MARKET"}
        canceled = 0
        for o in oo:
            try:
                typ = o.get('type')
                close_pos = str(o.get('closePosition','false')).lower() == 'true'
                reduce_only = str(o.get('reduceOnly','false')).lower() == 'true'
                if typ in protected_types and (close_pos or reduce_only):
                    self.client.futures_cancel_order(symbol=symbol, orderId=o['orderId'])
                    canceled += 1
            except Exception:
                continue
        return {'ok': True, 'canceled': canceled}

    def ensure_protection(self, symbol: str, side: str, sl: float, tp1: float, tp2: float) -> Dict:
        """Attach SL/TP if missing for an existing position."""
        tick, step, min_qty, _ = self._get_symbol_filters(symbol)
        try:
            orders = self.client.futures_get_open_orders(symbol=symbol)
        except Exception:
            orders = []
        has_sl = any(o.get('type') in ('STOP','STOP_MARKET') and str(o.get('closePosition','false')).lower()=='true' for o in orders)
        has_tp = any(
            (o.get('type') == 'LIMIT' and str(o.get('reduceOnly','false')).lower()=='true') or
            (o.get('type') in ('TAKE_PROFIT','TAKE_PROFIT_MARKET') and str(o.get('closePosition','false')).lower()=='true')
            for o in orders
        )
        if has_sl and has_tp:
            return {'ok': True, 'skipped': True}
        # fetch current position qty
        pos_qty = 0.0
        try:
            pi = self.client.futures_position_information(symbol=symbol)
            for p in pi:
                amt = float(p.get('positionAmt', 0.0))
                if amt != 0.0:
                    pos_qty = abs(amt)
                    break
        except Exception:
            pass
        if pos_qty <= 0:
            return {'ok': False, 'reason': 'no_position'}
        q1 = max(min_qty, _round_step(pos_qty * 0.5, step, mode='down'))
        q2 = max(min_qty, _round_step(pos_qty - q1, step, mode='down'))
        tp_side = 'SELL' if side == 'LONG' else 'BUY'
        # SL
        sl_ok = True
        if not has_sl:
            mark = float(self.client.futures_mark_price(symbol=symbol)["markPrice"])
            buf = max(tick * 5, mark * 0.0005)
            if side == 'LONG':
                sl_price = min(sl, mark - buf)
                sl_s = _format_by_step(sl_price, tick, mode='down')
                sl_side = 'SELL'
            else:
                sl_price = max(sl, mark + buf)
                sl_s = _format_by_step(sl_price, tick, mode='up')
                sl_side = 'BUY'
            try:
                self.client.futures_create_order(symbol=symbol, side=sl_side, type='STOP_MARKET', stopPrice=sl_s, closePosition=True, workingType='MARK_PRICE', priceProtect=True)
            except Exception as e:
                sl_ok = False
        # TPs
        tp_ok = True
        if not has_tp:
            tp1_s = _format_by_step(tp1, tick, mode='up' if side=='LONG' else 'down')
            tp2_s = _format_by_step(tp2, tick, mode='up' if side=='LONG' else 'down')
            # Small position handling
            if pos_qty < 2 * min_qty:
                # If below minQty, use TP market close; else single LIMIT TP
                if pos_qty < min_qty:
                    # market TP with buffer
                    mark = float(self.client.futures_mark_price(symbol=symbol)["markPrice"])
                    buf = max(tick * 5, mark * 0.0005)
                    if side == 'LONG':
                        tp_mkt_price = max(tp1, mark + buf)
                        tp_mkt_s = _format_by_step(tp_mkt_price, tick, mode='up')
                    else:
                        tp_mkt_price = min(tp1, mark - buf)
                        tp_mkt_s = _format_by_step(tp_mkt_price, tick, mode='down')
                    try:
                        self.client.futures_create_order(symbol=symbol, side=tp_side, type='TAKE_PROFIT_MARKET', stopPrice=tp_mkt_s, closePosition=True, workingType='MARK_PRICE', priceProtect=True)
                    except Exception:
                        tp_ok = False
                else:
                    q_all = _round_step(pos_qty, step, mode='down')
                    try:
                        self.client.futures_create_order(symbol=symbol, side=tp_side, type='LIMIT', timeInForce='GTC', price=tp1_s, quantity=_format_by_step(q_all, step, mode='down'), reduceOnly=True)
                    except Exception:
                        tp_ok = False
            else:
                # Dual LIMIT TPs
                q1 = _round_step(pos_qty * 0.5, step, mode='down')
                q2 = _round_step(pos_qty - q1, step, mode='down')
                if q2 < min_qty:
                    q1 = _round_step(pos_qty, step, mode='down')
                    q2 = 0.0
                try:
                    self.client.futures_create_order(symbol=symbol, side=tp_side, type='LIMIT', timeInForce='GTC', price=tp1_s, quantity=_format_by_step(q1, step, mode='down'), reduceOnly=True)
                    if q2 >= min_qty:
                        self.client.futures_create_order(symbol=symbol, side=tp_side, type='LIMIT', timeInForce='GTC', price=tp2_s, quantity=_format_by_step(q2, step, mode='down'), reduceOnly=True)
                except Exception:
                    tp_ok = False
        return {'ok': sl_ok and tp_ok}

    def maybe_upgrade_tp_to_trailing(self, symbol: str, side: str, atr15: float, entry: float) -> Dict:
        """If T1 likely filled (one TP left), cancel remaining TP and attach trailing stop-market closePosition.
        callbackRate ~ 0.6 * ATR% clamped to [0.4%, 1.2%], activation ~ entry +/- 0.5*ATR.
        """
        try:
            orders = self.client.futures_get_open_orders(symbol=symbol)
        except Exception:
            orders = []
        # Count reduceOnly LIMIT TP orders and existing SL closePosition
        tp_limits = [o for o in orders if o.get('type')=='LIMIT' and str(o.get('reduceOnly','false')).lower()=='true']
        sl_orders = [o for o in orders if o.get('type') in ('STOP','STOP_MARKET') and str(o.get('closePosition','false')).lower()=='true']
        if len(tp_limits) != 1:
            return {'skipped': True, 'reason': 'tp_count!=1'}
        # Check if a reduceOnly LIMIT was recently filled → signal T1 filled
        t1_filled = False
        try:
            hist = self.client.futures_get_all_orders(symbol=symbol, limit=30)
            for h in reversed(hist or []):
                if h.get('type')=='LIMIT' and str(h.get('reduceOnly','false')).lower()=='true' and h.get('status')=='FILLED':
                    t1_filled = True
                    break
        except Exception:
            # best-effort: assume filled to avoid being stuck
            t1_filled = True
        if not t1_filled:
            return {'skipped': True, 'reason': 'no_recent_filled_tp'}

        # Cancel remaining TP LIMIT ve varsa SL'leri de kaldır (pozisyon yarı kaldığında trailing devreye girecek)
        try:
            self.client.futures_cancel_order(symbol=symbol, orderId=tp_limits[0]['orderId'])
        except Exception:
            pass
        for so in sl_orders:
            try:
                self.client.futures_cancel_order(symbol=symbol, orderId=so['orderId'])
            except Exception:
                pass

        # Compute trailing parameters
        try:
            mark = float(self.client.futures_mark_price(symbol=symbol)["markPrice"])
        except Exception:
            mark = entry
        atrp = (atr15 / entry) if entry > 0 else 0.01
        callback = max(0.4, min(1.2, 0.6 * atrp * 100.0))  # percent
        if side == 'LONG':
            activation = entry + 0.5 * atr15
            trail_side = 'SELL'
        else:
            activation = entry - 0.5 * atr15
            trail_side = 'BUY'
        # place trailing stop-market closePosition
        try:
            trail = self.client.futures_create_order(
                symbol=symbol,
                side=trail_side,
                type='TRAILING_STOP_MARKET',
                callbackRate=f"{callback:.2f}",
                activationPrice=f"{activation:.8f}",
                reduceOnly=False,
                closePosition=True,
                workingType='MARK_PRICE',
                priceProtect=True
            )
        except Exception as e:
            return {'ok': False, 'error': f'trail_failed:{e}'}
        return {'ok': True, 'trail_order_id': trail.get('orderId'), 'callbackRate': callback, 'activationPrice': activation}

    def attach_sl_tp(self, symbol: str, side: str, sl: float, tp: float, tick_buffer: int = 5) -> Dict:
        tick, step, _, _ = self._get_symbol_filters(symbol)
        mark = float(self.client.futures_mark_price(symbol=symbol)["markPrice"])
        # dynamic absolute buffer: max(ticks, 5 bps)
        buf = max(tick * max(1, tick_buffer), mark * 0.0005)

        if side == 'LONG':
            sl_price = min(sl, mark - buf)
            sl_s = _format_by_step(sl_price, tick, mode='down')
            tp_price = max(tp, mark + buf)
            tp_s = _format_by_step(tp_price, tick, mode='up')
            sl_side = 'SELL'
            tp_side = 'SELL'
        else:
            sl_price = max(sl, mark + buf)
            sl_s = _format_by_step(sl_price, tick, mode='up')
            tp_price = min(tp, mark - buf)
            tp_s = _format_by_step(tp_price, tick, mode='down')
            sl_side = 'BUY'
            tp_side = 'BUY'

        sl_order = self.client.futures_create_order(
            symbol=symbol,
            side=sl_side,
            type='STOP_MARKET',
            stopPrice=sl_s,
            closePosition=True,
            workingType='MARK_PRICE',
            priceProtect=True
        )

        # Multi-TP: T1 (50%) as TAKE_PROFIT_MARKET, T2 (50%) via limit reduce-only
        tp_order = self.client.futures_create_order(
            symbol=symbol,
            side=tp_side,
            type='TAKE_PROFIT_MARKET',
            stopPrice=tp_s,
            closePosition=True,
            workingType='MARK_PRICE',
            priceProtect=True
        )

        return {
            'sl_order_id': sl_order.get('orderId'),
            'tp_order_id': tp_order.get('orderId')
        }

    def cancel_if_not_filled(self, symbol: str, entry_order_id: int, bars_elapsed: int) -> Optional[Dict]:
        if bars_elapsed < self.cancel_after_bars:
            return None
        try:
            self.client.futures_cancel_order(symbol=symbol, orderId=entry_order_id)
            return {'canceled': True}
        except Exception:
            return {'canceled': False}
