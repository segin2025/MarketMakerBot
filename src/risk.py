from typing import Dict


class RiskEngine:
    def __init__(self, cfg: Dict, equity: float):
        self.cfg = cfg
        self.equity = equity
        self.day_r_pnl = 0.0  # accumulated in R units
        self.losing_streak = 0

    def can_trade_today(self) -> bool:
        return self.day_r_pnl > -self.cfg["risk"]["max_daily_loss_r"]

    def position_size(self, stop_distance: float) -> float:
        r = float(self.cfg["risk"]["r_per_trade"])  # fraction of equity
        if self.losing_streak >= 3:
            r *= 0.5
        if stop_distance <= 0:
            return 0.0
        usd_risk = self.equity * r
        qty = usd_risk / stop_distance
        return max(0.0, qty)

    def leverage_cap(self, implied_leverage: float) -> float:
        return min(implied_leverage, float(self.cfg["risk"]["leverage_cap"]))

    def on_trade_result(self, r_result: float):
        self.day_r_pnl += r_result
        if r_result < 0:
            self.losing_streak += 1
        else:
            self.losing_streak = 0




