#!/usr/bin/env python3
"""
下载币安现货 BTC/USDT 1s K线（内存安全版）
分批下载 → 每10万根写一个 parquet chunk → 最后合并
"""
import ccxt
import pandas as pd
import time
import os
from datetime import datetime

binance = ccxt.binance()
SYMBOL = 'BTC/USDT'
TF = '1s'
OUT_DIR = '/root/.openclaw/workspace/btc-strategy-task/backtest_data/1s_chunks'
FINAL_FILE = '/root/.openclaw/workspace/btc-strategy-task/backtest_data/btc_1s_1y.pkl'
CHUNK_SIZE = 100_000  # 每10万根写一个chunk

os.makedirs(OUT_DIR, exist_ok=True)

now_ms = int(time.time() * 1000)
YEAR_MS = 365 * 86400 * 1000
since_ms = now_ms - YEAR_MS

# 检查已有chunks
existing = sorted([f for f in os.listdir(OUT_DIR) if f.startswith('chunk_')])
if existing:
    # 从最后一个chunk恢复
    last = existing[-1]
    df_last = pd.read_parquet(os.path.join(OUT_DIR, last))
    last_ts = int(df_last.index[-1].timestamp() * 1000)
    since_ms = last_ts + 1000
    chunk_num = int(last.replace('chunk_', '').replace('.parquet', '')) + 1
    total_rows = chunk_num * CHUNK_SIZE
    print(f'📂 恢复: {len(existing)} 个chunk, {total_rows:,} 根, 从 {datetime.fromtimestamp(since_ms/1000)} 继续')
else:
    chunk_num = 0
    total_rows = 0
    print(f'🚀 全新下载')

print(f'范围: {datetime.fromtimestamp(since_ms/1000)} ~ {datetime.fromtimestamp(now_ms/1000)}')
print()

start_time = time.time()
batch = []
batch_count = 0
errors = 0

while since_ms < now_ms:
    try:
        candles = binance.fetch_ohlcv(SYMBOL, TF, since=since_ms, limit=1000)
        if not candles:
            errors += 1
            since_ms += 3600_000
            if errors > 50: break
            continue
        errors = 0
        batch.extend(candles)
        batch_count += 1
        last_ts = candles[-1][0]
        since_ms = last_ts + 1000

        # 每10万根写一个parquet chunk
        if len(batch) >= CHUNK_SIZE:
            df = pd.DataFrame(batch[:CHUNK_SIZE], columns=['ts','o','h','l','c','v'])
            df['ts'] = pd.to_datetime(df['ts'], unit='ms')
            df = df.set_index('ts')
            df.columns = ['open','high','low','close','volume']
            chunk_path = os.path.join(OUT_DIR, f'chunk_{chunk_num:06d}.parquet')
            df.to_parquet(chunk_path)
            chunk_num += 1
            total_rows += CHUNK_SIZE
            batch = batch[CHUNK_SIZE:]  # keep overflow

            pct = total_rows / (365 * 86400) * 100
            elapsed = (time.time() - start_time) / 60
            rate = batch_count / elapsed if elapsed > 0 else 0
            print(f'[{datetime.now().strftime("%H:%M:%S")}] chunk {chunk_num} | '
                  f'{total_rows:,} 根 | {pct:.1f}% | {rate:.0f}批/分 | '
                  f'到 {datetime.fromtimestamp(last_ts/1000).strftime("%m-%d %H:%M")}')

        time.sleep(0.12)

    except Exception as e:
        err = str(e)[:80]
        print(f'❌ 错误: {err}')
        errors += 1
        if '429' in err: time.sleep(30)
        elif 'timeout' in err.lower(): time.sleep(5)
        else: time.sleep(2)
        if errors > 50: break

# 写最后一个chunk（剩余数据）
if batch:
    df = pd.DataFrame(batch, columns=['ts','o','h','l','c','v'])
    df['ts'] = pd.to_datetime(df['ts'], unit='ms')
    df = df.set_index('ts')
    df.columns = ['open','high','low','close','volume']
    chunk_path = os.path.join(OUT_DIR, f'chunk_{chunk_num:06d}.parquet')
    df.to_parquet(chunk_path)
    chunk_num += 1
    total_rows += len(batch)
    print(f'📦 最后chunk: {len(batch):,} 根')

elapsed = (time.time() - start_time) / 60
print(f'\n✅ 下载完成: {total_rows:,} 根, {elapsed:.0f}分')

# 合并所有chunks
print('🔄 合并chunks...')
chunks = sorted([f for f in os.listdir(OUT_DIR) if f.startswith('chunk_')])
dfs = []
for i, f in enumerate(chunks):
    df = pd.read_parquet(os.path.join(OUT_DIR, f))
    dfs.append(df)
    if (i+1) % 50 == 0:
        print(f'  读取 {i+1}/{len(chunks)}')

df_all = pd.concat(dfs)
df_all = df_all.sort_index()
print(f'📊 最终: {df_all.shape}, {df_all.memory_usage(deep=True).sum()/1024/1024:.0f}MB')

df_all.to_pickle(FINAL_FILE)
print(f'💾 保存: {FINAL_FILE}')

# 清理chunks
import shutil
shutil.rmtree(OUT_DIR)
print('🧹 Chunks已清理')
