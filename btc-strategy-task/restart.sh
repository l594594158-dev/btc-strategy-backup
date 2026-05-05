#!/bin/bash
cd /root/.openclaw/workspace/btc-strategy-task
pkill -f "auto_trade.py" 2>/dev/null
sleep 2
nohup python3 -u auto_trade.py >> logs/auto_trade.log 2>&1 &
echo "Started PID: $!"
