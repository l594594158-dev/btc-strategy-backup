#!/usr/bin/env python3
"""
下载币安现货 BTC/USDT 1s K线，约1年数据
预计 31,536 批 × 1000根，约 45~90 分钟
"""
import ccxt
import pickle
import time
import os
from datetime import datetime

binance = ccxt.binance()
SYMBOL = 'BTC/USDT'
TF = '1s'
SAVE_PATH = '/root/.openclaw/workspace/btc-strategy-task/backtest_data/btc_1s_1y.pkl'

# 时间范围：一年
now_ms = int(time.time() * 1000)
YEAR_MS = 365 * 86400 * 1000
since_ms = now_ms - YEAR_MS

# 进度文件（断点续传）
PROGRESS_FILE = SAVE_PATH.replace('.pkl', '_progress.pkl')

all_data = []
total_expected = YEAR_MS // 1000  # ~31.5M

# 尝试恢复进度
if os.path.exists(PROGRESS_FILE):
    try:
        with open(PROGRESS_FILE, 'rb') as f:
            saved = pickle.load(f)
            all_data = saved.get('data', [])
            since_ms = saved.get('next_since', since_ms)
            print(f"📂 恢复进度: 已有 {len(all_data):,} 根, 从 {datetime.fromtimestamp(since_ms/1000)} 继续")
    except Exception as e:
        print(f"⚠️ 无法恢复: {e}")

print(f"🚀 开始下载 BTC/USDT 1s K线")
print(f"   范围: {datetime.fromtimestamp(since_ms/1000)} ~ {datetime.fromtimestamp(now_ms/1000)}")
print(f"   预计: ~31,536 批 (每批1000根)")
print(f"   文件: {SAVE_PATH}")
print()

batch = 0
start_time = time.time()
errors = 0
max_errors = 20

while since_ms < now_ms:
    try:
        candles = binance.fetch_ohlcv(SYMBOL, TF, since=since_ms, limit=1000)

        if not candles or len(candles) == 0:
            print(f"⚠️ 批次 {batch}: 无数据, since={datetime.fromtimestamp(since_ms/1000)}")
            # 跳过1小时
            since_ms += 3600 * 1000
            errors += 1
            if errors > max_errors:
                print("❌ 连续无数据过多，退出")
                break
            continue

        errors = 0
        all_data.extend(candles)
        batch += 1

        # 更新 since 到最后一根之后
        last_ts = candles[-1][0]
        since_ms = last_ts + 1000  # +1秒

        # 进度显示
        elapsed = time.time() - start_time
        pct = (since_ms - (now_ms - YEAR_MS)) / YEAR_MS * 100

        if batch % 500 == 0 or batch <= 5:
            rate = batch / elapsed * 60 if elapsed > 0 else 0
            eta_min = (total_expected - len(all_data)) / 1000 / (rate if rate > 0 else 60) * 60
            now_str = datetime.now().strftime('%H:%M:%S')
            last_dt = datetime.fromtimestamp(last_ts / 1000)
            print(f"[{now_str}] 批次 {batch:,} | 累计 {len(all_data):,} 根 | "
                  f"进度 {pct:.1f}% | 速率 {rate:.0f}批/分 | 到 {last_dt} | 预计剩余 {eta_min:.0f}分")

        # 每2000批保存一次进度
        if batch % 2000 == 0:
            with open(PROGRESS_FILE, 'wb') as f:
                pickle.dump({'data': all_data, 'next_since': since_ms}, f)
            print(f"💾 进度已保存 ({len(all_data):,} 根)")

        # 速率控制: ~8批/秒 (安全)
        time.sleep(0.12)

    except Exception as e:
        err_msg = str(e)[:100]
        print(f"❌ 批次 {batch} 错误: {err_msg}")
        errors += 1

        if '429' in err_msg or 'rate' in err_msg.lower():
            print("⏳ 限速等待 30 秒...")
            time.sleep(30)
        elif 'ETIMEDOUT' in err_msg or 'timeout' in err_msg.lower():
            time.sleep(5)
        else:
            time.sleep(2)

        if errors > max_errors:
            print("❌ 错误过多，退出")
            break

# 保存最终数据
print(f"\n📊 下载完成! 共 {len(all_data):,} 根K线")
print(f"   总耗时: {(time.time() - start_time) / 60:.1f} 分钟")

# 转为 DataFrame 再存（方便后续使用）
import pandas as pd
df = pd.DataFrame(all_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
df = df.set_index('timestamp')

print(f"   DataFrame: {df.shape}, 内存 {df.memory_usage(deep=True).sum()/1024/1024:.1f} MB")
df.to_pickle(SAVE_PATH)
print(f"✅ 已保存: {SAVE_PATH}")

# 删除进度文件
if os.path.exists(PROGRESS_FILE):
    os.remove(PROGRESS_FILE)
    print("🧹 进度文件已清理")
