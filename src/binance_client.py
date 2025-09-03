import os
import time
from typing import Any, Dict, List, Optional
from binance.client import Client


class BinanceWrapper:
    def __init__(self):
        api_key = os.getenv("BINANCE_API_KEY")
        api_secret = os.getenv("BINANCE_API_SECRET")
        if not api_key or not api_secret:
            raise RuntimeError("Missing BINANCE_API_KEY/SECRET in environment")
        self.client = Client(api_key, api_secret)
        self._fut_ex_info = None
        self._fut_ex_info_ts = 0.0

    def _load_futures_exchange_info(self) -> Dict[str, Any]:
        now = time.time()
        if self._fut_ex_info and (now - self._fut_ex_info_ts) < 300:
            return self._fut_ex_info
        self._fut_ex_info = self.client.futures_exchange_info()
        self._fut_ex_info_ts = now
        return self._fut_ex_info

    def get_futures_symbol_filters(self, symbol: str) -> Dict[str, Any]:
        info = self._load_futures_exchange_info()
        sym = next((s for s in info.get("symbols", []) if s.get("symbol") == symbol), None)
        result = {"tickSize": None, "stepSize": None, "minQty": None, "minNotional": None}
        if not sym:
            return result
        for f in sym.get("filters", []):
            ft = f.get("filterType")
            if ft == "PRICE_FILTER":
                result["tickSize"] = float(f.get("tickSize", 0))
            if ft in ("LOT_SIZE", "MARKET_LOT_SIZE"):
                result["stepSize"] = float(f.get("stepSize", 0))
                result["minQty"] = float(f.get("minQty", 0))
            if ft == "MIN_NOTIONAL":
                result["minNotional"] = float(f.get("notional", 0))
        return result

    def has_symbol(self, symbol: str) -> bool:
        info = self._load_futures_exchange_info()
        for s in info.get("symbols", []):
            try:
                if s.get("symbol") == symbol and str(s.get("status", "TRADING")).upper() == "TRADING":
                    return True
            except Exception:
                continue
        return False

    def klines(self, symbol: str, interval: str, limit: int = 500) -> List[Dict[str, Any]]:
        raw = self.client.futures_klines(symbol=symbol, interval=interval, limit=limit)
        out = []
        for k in raw:
            out.append({
                "open_time": k[0],
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
                "close_time": k[6],
            })
        return out

    def ticker_24h(self, symbol: str) -> Dict[str, Any]:
        return self.client.futures_ticker(symbol=symbol)

    def mark_price(self, symbol: str) -> float:
        return float(self.client.futures_mark_price(symbol=symbol)["markPrice"]) 

    def funding_rate(self, symbol: str, limit: int = 8) -> List[Dict[str, Any]]:
        return self.client.futures_funding_rate(symbol=symbol, limit=limit)

    def top_usd_symbols(self, top_n: int = 50, quote: str = 'USDT') -> List[str]:
        tickers = self.client.futures_ticker()
        sym = [t for t in tickers if t.get("symbol", "").endswith(quote)]
        sym.sort(key=lambda x: float(x.get("quoteVolume", 0.0)), reverse=True)
        return [t["symbol"] for t in sym[:top_n]]

    # --- Execution surface ---
    def set_leverage(self, symbol: str, leverage: int):
        return self.client.futures_change_leverage(symbol=symbol, leverage=leverage)

    def set_margin_type(self, symbol: str, marginType: str = 'ISOLATED'):
        try:
            return self.client.futures_change_margin_type(symbol=symbol, marginType=marginType)
        except Exception:
            return None

    def futures_create_order(self, **kwargs):
        return self.client.futures_create_order(**kwargs)

    def futures_cancel_order(self, **kwargs):
        return self.client.futures_cancel_order(**kwargs)
