import json
import os
from datetime import datetime, timedelta, timezone
from typing import Dict


class DayState:
    def __init__(self, path: str = "day_state.json"):
        self.path = path
        self.state = {
            "date": self._today(),
            "net_R": 0.0,
            "relaxed_enabled": True,
            "signals_this_hour": 0,
            "hour": datetime.now(timezone.utc).hour,
            "cooldown_until": {},  # symbol -> iso time
            "reprice_count": {},   # symbol -> int
        }
        self._load()

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r") as f:
                    data = json.load(f)
                if data.get("date") == self._today():
                    self.state.update(data)
            except Exception:
                pass

    def save(self):
        try:
            with open(self.path, "w") as f:
                json.dump(self.state, f)
        except Exception:
            pass

    def add_R(self, r: float):
        self.state["net_R"] += r
        self.save()

    def reset_hour(self):
        h = datetime.now(timezone.utc).hour
        if h != self.state.get("hour"):
            self.state["hour"] = h
            self.state["signals_this_hour"] = 0
            self.save()

    def inc_signals(self):
        self.state["signals_this_hour"] += 1
        self.save()

    def can_signal(self, hourly_cap: int) -> bool:
        self.reset_hour()
        return self.state.get("signals_this_hour", 0) < hourly_cap

    def set_cooldown(self, symbol: str, minutes: int):
        until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        self.state["cooldown_until"][symbol] = until.isoformat()
        self.save()

    def on_cooldown(self, symbol: str) -> bool:
        ts = self.state["cooldown_until"].get(symbol)
        if not ts:
            return False
        try:
            until = datetime.fromisoformat(ts)
            now = datetime.now(timezone.utc)
            # normalize naive timestamps to UTC when comparing
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            return now < until
        except Exception:
            return False

    # --- Reprice bookkeeping ---
    def get_reprice_count(self, symbol: str) -> int:
        return int(self.state.get("reprice_count", {}).get(symbol, 0))

    def inc_reprice(self, symbol: str) -> int:
        cur = self.get_reprice_count(symbol) + 1
        self.state.setdefault("reprice_count", {})[symbol] = cur
        self.save()
        return cur

    def reset_reprice(self, symbol: str):
        if symbol in self.state.get("reprice_count", {}):
            try:
                del self.state["reprice_count"][symbol]
            except Exception:
                pass
            self.save()



