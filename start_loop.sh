#!/bin/zsh
set -euo pipefail

PROJECT_DIR="/Users/vedat.madencioglu/Desktop/MarketMakerBot"
cd "$PROJECT_DIR"

# Load env vars
set -a
if [ -f .env ]; then
  source .env
fi
set +a

# Activate venv
if [ -f venv/bin/activate ]; then
  source venv/bin/activate
fi

# Runtime flags for loop.py
export RUN_FLAGS="--execute --margin CROSSED --relaxed --fallback-on-trend relaxed --news-mode auto --debug"
export TICK_SECONDS="10"

mkdir -p logs

exec python loop.py >> logs/loop.out.log 2>> logs/loop.err.log


