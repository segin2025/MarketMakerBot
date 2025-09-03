import os
import yaml
from typing import Any, Dict


def load_config(path: str = "config.yaml") -> Dict[str, Any]:
    with open(path, "r") as f:
        cfg = yaml.safe_load(f) or {}

    # Basic env overrides (optional)
    cfg.setdefault("runtime", {})
    cfg["runtime"]["base_symbol"] = os.getenv("BASE_SYMBOL", cfg["runtime"].get("base_symbol", "BTCUSDT"))
    cfg["runtime"]["universe_top_n"] = int(os.getenv("UNIVERSE_TOP_N", cfg["runtime"].get("universe_top_n", 5)))
    cfg["runtime"]["demo_equity"] = float(os.getenv("DEMO_EQUITY", cfg["runtime"].get("demo_equity", 10000)))
    return cfg

