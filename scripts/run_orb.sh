#!/bin/bash
# ORB Bot Launcher - runs at 9:25 AM ET on trading days

cd ~/projects/orb-trader
source venv/bin/activate

# Check if already running
if pgrep -f "python.*src.main" > /dev/null; then
    echo "$(date): ORB bot already running, skipping"
    exit 0
fi

# Check if it's a weekend (skip Sat/Sun)
DOW=$(date +%u)
if [ "$DOW" -gt 5 ]; then
    echo "$(date): Weekend - skipping"
    exit 0
fi

# Log startup
echo "$(date): Starting ORB bot..."
mkdir -p logs

# Run with output to log file
python -m src.main >> logs/orb_$(date +%Y%m%d).log 2>&1 &
PID=$!
echo $PID > /tmp/orb_bot.pid

echo "$(date): ORB bot started with PID $PID"
