import json
import os
import time
from typing import Dict


class ShadowAIScorer:
    def __init__(self, path: str = "ai_shadow_log.jsonl"):
        self.path = path

    def score(self, features: Dict) -> float:
        # Very simple placeholder: logistic-like from weighted features
        w = {
            'L': 0.25, 'M': 0.20, 'F': 0.15, 'C': 0.15, 'RS': 0.25
        }
        s = 0.0
        for k, wk in w.items():
            s += wk * float(features.get(k, 0.5))
        # map to 0..1
        return max(0.0, min(1.0, s))

    def log(self, record: Dict):
        try:
            with open(self.path, 'a') as f:
                f.write(json.dumps(record) + "\n")
        except Exception:
            pass

    def log_realized(self, symbol: str, side: str, realized_r: float, meta: Dict):
        rec = {
            'ts': time.strftime('%Y-%m-%dT%H:%M:%S'),
            'symbol': symbol,
            'side': side,
            'realized_R': realized_r,
            'meta': meta,
        }
        self.log(rec)

