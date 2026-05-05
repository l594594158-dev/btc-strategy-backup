#!/bin/bash
pkill -f "auto_trade.py"
sleep 2
cd /root/.openclaw/workspace/btc-strategy-task
nohup python3 -u auto_trade.py >> logs/auto_trade.log 2>&1 &
echo "PID: $!"
sleep 5
ps aux | grep "auto_trade.py" | grep -v grep
echo "---STATE---"
cat databases/state.json
echo "---LOG---"
tail -6 logs/auto_trade.log
