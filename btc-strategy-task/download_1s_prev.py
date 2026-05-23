#!/usr/bin/env python3
"""
下载币安现货 BTC/USDT 1s K线 — 前一年 (2024-05 ~ 2025-05)
断点续传, 自动限速, 约31,500次请求约1小时
"""
import ccxt, pandas as pd, time, os, pickle
from datetime import datetime, timezone

SYMBOL = 'BTC/USDT'; TF = '1s'
OUT = '/root/.openclaw/workspace/btc-strategy-task/backtest_data/btc_1s_1y_prev.pkl'
CHECKPOINT = '/root/.openclaw/workspace/btc-strategy-task/backtest_data/btc_1s_prev_checkpoint.pkl'

LIMIT = 1000  # 每请求1000根
EXCHANGE = ccxt.binance({'enableRateLimit': True})

# 加载已有进度
if os.path.exists(CHECKPOINT):
    with open(CHECKPOINT, 'rb') as f:
        batches, last_ms = pickle.load(f)
    print(f"🔄 恢复: {len(batches)}批, 起始{batches[0].index[0] if batches else '?'}")
else:
    # 从2024-05-22开始
    start = datetime(2024, 5, 22, 0, 0, 0, tzinfo=timezone.utc)
    last_ms = int(start.timestamp() * 1000)
    batches = []

end_ms = int(datetime(2025, 5, 22, 0, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
total_needed = (end_ms - last_ms) // (LIMIT * 1000) + 1
print(f"🎯 目标: {total_needed}批 (2024-05-22 ~ 2025-05-22)")
t0 = time.time()

while last_ms < end_ms:
    try:
        klines = EXCHANGE.fetch_ohlcv(SYMBOL, TF, since=last_ms, limit=LIMIT)
    except Exception as e:
        print(f"⏸️ 请求失败({e}), 5秒后重试...")
        time.sleep(5)
        continue

    if not klines or len(klines) <= 1:
        print(f"⚠️ 空返回 at {datetime.fromtimestamp(last_ms/1000, tz=timezone.utc)}, 跳过")
        last_ms += LIMIT * 1000
        continue

    df = pd.DataFrame(klines, columns=['t','o','h','l','c','v'])
    df['t'] = pd.to_datetime(df['t'], unit='ms', utc=True)
    df.set_index('t', inplace=True)
    batches.append(df)

    last_ms = int(klines[-1][0] + 1000)  # 下一根
    done = len(batches)
    elapsed = time.time() - t0
    eta = (total_needed - done) * elapsed / done if done > 0 else 0
    
    if done % 500 == 0:
        print(f"  📥 {done}/{total_needed}批 ({done*100//total_needed}%) | {elapsed:.0f}s | ETA {eta:.0f}s | 最新{df.index[-1]}", flush=True)
        # 每500批保存检查点
        with open(CHECKPOINT, 'wb') as f:
            pickle.dump((batches, last_ms), f)

# 合并
print(f"\n🔧 合并{len(batches)}批...", flush=True)
full = pd.concat(batches)
full = full[~full.index.duplicated(keep='last')]
full.sort_index(inplace=True)
print(f"  {len(full):,}根 1s K线 | {full.index[0]} ~ {full.index[-1]}")

# 保存
with open(OUT, 'wb') as f:
    pickle.dump(full, f)
print(f"💾 {OUT} ({os.path.getsize(OUT)/1024/1024:.0f}MB)")
os.remove(CHECKPOINT)
print(f"✅ 完成 | 耗时{(time.time()-t0):.0f}s")
