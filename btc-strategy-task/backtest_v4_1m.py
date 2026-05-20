#!/usr/bin/env python3
"""
BTC v4.0 回测 — 1分钟精确版
- 下载4年1m数据，重采样5m算指标
- 入场: 5m收盘价判断+入场
- 出场: 每1m收盘价检查，触发即平仓
- 输出: 修正后的交易明细文件
"""
import ccxt, pandas as pd, numpy as np, ta, time, pickle, os, sys
from datetime import datetime

SYMBOL = 'BTC/USDT:USDT'
DATA_DIR = '/root/.openclaw/workspace/btc-strategy-task/backtest_data'
START_DATE = '2022-06-01'
TP_PCT, SL_PCT = 2.5 / 100, 1.5 / 100
FEE = 0.04 / 100

os.makedirs(DATA_DIR, exist_ok=True)

# ============ 下载 ============
def download_1m():
    cache = os.path.join(DATA_DIR, 'btc_1m_4y.pkl')
    if os.path.exists(cache):
        print(f"📦 1m缓存已存在: {cache}")
        with open(cache, 'rb') as f:
            return pickle.load(f)
    
    print("📥 下载4年1m K线...")
    ex = ccxt.binance({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})
    since = ex.parse8601(f'{START_DATE}T00:00:00Z')
    end_ms = int(datetime.now().timestamp() * 1000)
    tf_ms = 60000
    all_k = []
    
    while since < end_ms:
        try:
            k = ex.fetch_ohlcv(SYMBOL, '1m', since=since, limit=1000)
            if not k: break
            all_k.extend(k)
            last_ts = k[-1][0]
            if len(all_k) % 50000 == 0:
                print(f"  已下载 {len(all_k):,} 根, 最新 {datetime.fromtimestamp(last_ts/1000).strftime('%Y-%m-%d %H:%M')}")
            if len(k) < 1000 or last_ts >= end_ms: break
            since = last_ts + tf_ms
            time.sleep(0.2)
        except Exception as e:
            print(f"  ⚠️ {e}, 重试..."); time.sleep(5)
    
    df = pd.DataFrame(all_k, columns=['ts','open','high','low','close','volume'])
    df['ts'] = pd.to_datetime(df['ts'], unit='ms')
    df = df.set_index('ts').drop_duplicates()
    print(f"  ✅ 1m: {len(df):,} 根, {df.index[0]} ~ {df.index[-1]}")
    
    with open(cache, 'wb') as f: pickle.dump(df, f)
    return df

def download_tf(ex, tf, since_ms):
    all_k, current = [], since_ms
    tf_ms = ex.parse_timeframe(tf) * 1000
    end_ms = int(datetime.now().timestamp() * 1000)
    while current < end_ms:
        try:
            k = ex.fetch_ohlcv(SYMBOL, tf, since=current, limit=1000)
            if not k: break
            all_k.extend(k)
            last = k[-1][0]
            if len(k) < 1000 or last >= end_ms: break
            current = last + tf_ms; time.sleep(0.3)
        except: time.sleep(5)
    df = pd.DataFrame(all_k, columns=['ts','open','high','low','close','volume'])
    df['ts'] = pd.to_datetime(df['ts'], unit='ms')
    return df.set_index('ts').drop_duplicates()

# ============ 主流程 ============
print("=" * 60)
print("BTC v4.0 1分钟精确回测")
print("=" * 60)

# 1. 下载/加载数据
df_1m = download_1m()

# 加载或下载高周期数据
cache_hf = os.path.join(DATA_DIR, 'btc_data_4y.pkl')
if os.path.exists(cache_hf):
    with open(cache_hf, 'rb') as f:
        hf = pickle.load(f)
    df_1h, df_4h, df_1d = hf['1h'], hf['4h'], hf['1d']
else:
    ex = ccxt.binance({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})
    since = ex.parse8601(f'{START_DATE}T00:00:00Z')
    for tf in ['1h','4h','1d']:
        print(f"下载{tf}...")
        df_t = download_tf(ex, tf, since)
        if tf == '1h': df_1h = df_t
        elif tf == '4h': df_4h = df_t
        else: df_1d = df_t

# 2. 重采样1m→5m，计算指标
print("\n📊 计算指标...")
df_5m = df_1m.resample('5min').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna()

close_5m = df_5m['close'].astype(float)
df_5m['sma20_5m'] = ta.trend.SMAIndicator(close_5m, 20).sma_indicator()
df_5m['rsi_5m'] = ta.momentum.RSIIndicator(close_5m, 14).rsi()
df_5m['pct_sma'] = (close_5m - df_5m['sma20_5m']) / df_5m['sma20_5m'] * 100

# 高周期指标
for tf_name, src_df in [('1h', df_1h), ('4h', df_4h), ('1d', df_1d)]:
    c = src_df['close'].astype(float); h = src_df['high'].astype(float); l = src_df['low'].astype(float)
    if tf_name == '1h':
        src_df['adx_1h'] = ta.trend.ADXIndicator(h, l, c, 14).adx()
    elif tf_name == '4h':
        src_df['sma20_4h'] = ta.trend.SMAIndicator(c, 20).sma_indicator()
        src_df['adx_4h'] = ta.trend.ADXIndicator(h, l, c, 14).adx()
        src_df['close_4h'] = c
    elif tf_name == '1d':
        src_df['sma20_1d'] = ta.trend.SMAIndicator(c, 20).sma_indicator()
        src_df['close_1d'] = c

# 映射到5m
for col, src in [('adx_1h', df_1h), ('sma20_4h', df_4h), ('adx_4h', df_4h), ('close_4h', df_4h), ('sma20_1d', df_1d), ('close_1d', df_1d)]:
    df_5m[col] = src[col].reindex(df_5m.index, method='ffill')
df_5m.dropna(inplace=True)
print(f"  有效5m行: {len(df_5m)}")

# 3. 运行回测
print("\n🔬 回测中(入场5m/出场1m)...")
trades = []
long_pos = short_pos = None
long_last = short_last = False
total = len(df_5m)

for i_5m in range(total):
    row_5m = df_5m.iloc[i_5m]
    ts_5m = df_5m.index[i_5m]
    
    # --- 出场检查：用1m逐根扫描 ---
    # 获取这个5m区间内的所有1m K线
    ts_next = df_5m.index[i_5m + 1] if i_5m + 1 < total else df_1m.index[-1] + pd.Timedelta('1min')
    df_1m_slice = df_1m.loc[ts_5m:ts_next - pd.Timedelta('1ms')]
    
    for ts_1m, row_1m in df_1m_slice.iterrows():
        cl = row_1m['close']
        
        if long_pos is not None:
            pnl = (cl - long_pos['entry_price']) / long_pos['entry_price'] * 100
            if pnl >= 2.5:
                trades.append({'direction':'LONG','entry_time':long_pos['entry_time'],
                    'exit_time':ts_1m,'entry_price':long_pos['entry_price'],
                    'exit_price':long_pos['entry_price']*1.025,'pnl_pct':2.50,'exit_type':'TP'})
                long_pos = None; continue
            elif pnl <= -1.5:
                trades.append({'direction':'LONG','entry_time':long_pos['entry_time'],
                    'exit_time':ts_1m,'entry_price':long_pos['entry_price'],
                    'exit_price':long_pos['entry_price']*0.985,'pnl_pct':-1.50,'exit_type':'SL'})
                long_pos = None; continue
        
        if short_pos is not None:
            pnl = (short_pos['entry_price'] - cl) / short_pos['entry_price'] * 100
            if pnl >= 2.5:
                trades.append({'direction':'SHORT','entry_time':short_pos['entry_time'],
                    'exit_time':ts_1m,'entry_price':short_pos['entry_price'],
                    'exit_price':short_pos['entry_price']*0.975,'pnl_pct':2.50,'exit_type':'TP'})
                short_pos = None; continue
            elif pnl <= -1.5:
                trades.append({'direction':'SHORT','entry_time':short_pos['entry_time'],
                    'exit_time':ts_1m,'entry_price':short_pos['entry_price'],
                    'exit_price':short_pos['entry_price']*1.015,'pnl_pct':-1.50,'exit_type':'SL'})
                short_pos = None; continue
    
    # --- 入场判断（用5m收盘价）---
    cl5 = row_5m['close']
    within = abs(row_5m['pct_sma']) <= 1.0
    adx1h = row_5m['adx_1h'] > 25
    adx4h = row_5m['adx_4h'] < 40
    
    long_sig = (row_5m['close_4h'] > row_5m['sma20_4h'] and 
                row_5m['close_1d'] > row_5m['sma20_1d'] and
                within and adx1h and adx4h and row_5m['rsi_5m'] > 40)
    short_sig = (row_5m['close_4h'] < row_5m['sma20_4h'] and 
                 row_5m['close_1d'] < row_5m['sma20_1d'] and
                 within and adx1h and adx4h and row_5m['rsi_5m'] < 60)
    
    long_new = long_sig and not long_last
    short_new = short_sig and not short_last
    
    if long_pos is None and long_new:
        long_pos = {'entry_price': cl5, 'entry_time': ts_5m}
    if short_pos is None and short_new:
        short_pos = {'entry_price': cl5, 'entry_time': ts_5m}
    
    long_last, short_last = long_sig, short_sig
    
    if (i_5m + 1) % 50000 == 0:
        print(f"  进度: {(i_5m+1)*100//total}%  |  已交易 {len(trades)} 笔")

# 末位强平
last_cl = df_1m.iloc[-1]['close']
for pos, d in [(long_pos,'LONG'),(short_pos,'SHORT')]:
    if pos:
        xp = last_cl
        pnl = ((xp-pos['entry_price'])/pos['entry_price']*100) if d=='LONG' else ((pos['entry_price']-xp)/pos['entry_price']*100)
        trades.append({'direction':d,'entry_time':pos['entry_time'],'exit_time':df_1m.index[-1],
            'entry_price':pos['entry_price'],'exit_price':xp,'pnl_pct':pnl,'exit_type':'FORCE'})

print(f"\n✅ 回测完成: {len(trades)} 笔")

# 4. 统计
df_t = pd.DataFrame(trades)
df_t['entry_time'] = pd.to_datetime(df_t['entry_time'])
df_t['exit_time'] = pd.to_datetime(df_t['exit_time'])
df_t = df_t.sort_values('entry_time').reset_index(drop=True)

wins = df_t[df_t['pnl_pct'] > 0]
losses = df_t[df_t['pnl_pct'] <= 0]
n, nw, nl = len(df_t), len(wins), len(losses)
wr = nw/n*100

tp_t = df_t[df_t['exit_type']=='TP']
sl_t = df_t[df_t['exit_type']=='SL']
avg_w = wins['pnl_pct'].mean()
avg_l = losses['pnl_pct'].mean()

# 回撤
eq = 1000
curve = [eq]
for p in df_t['pnl_pct']:
    eq *= (1 + (p/100 - FEE*2))
    curve.append(eq)
curve_s = pd.Series(curve)
dd = (curve_s / curve_s.expanding().max() - 1) * 100

# 连亏
streak = cur = 0; mx_streak = 0; mx_loss_sum = 0; cur_loss_sum = 0
for p in df_t['pnl_pct']:
    if p <= 0:
        streak += 1; cur_loss_sum += p
        mx_streak = max(mx_streak, streak)
        mx_loss_sum = min(mx_loss_sum, cur_loss_sum)
    else:
        streak = 0; cur_loss_sum = 0

print("\n" + "="*50)
print("📊 1分钟精确回测结果")
print("="*50)
print(f"  笔数: {n}  |  胜率: {wr:.1f}%  ({nw}胜/{nl}负)")
print(f"  总盈亏: {df_t['pnl_pct'].sum():+.1f}%")
print(f"  复利收益: {curve[-1]/1000*100-100:+.1f}%")
print(f"  最大回撤: {dd.min():.1f}%")
print(f"  最大连亏: {mx_streak}次 ({mx_loss_sum:.1f}%)")
print(f"  TP平均: {tp_t['pnl_pct'].mean():+.2f}%  SL平均: {sl_t['pnl_pct'].mean():+.2f}%")
print(f"  LONG: {len(df_t[df_t['direction']=='LONG'])}笔  SHORT: {len(df_t[df_t['direction']=='SHORT'])}笔")

# 5. 生成文件
print("\n📝 生成交易明细...")
lines = []
for i, row in df_t.iterrows():
    d = row['direction']; ep = row['entry_price']; xp = row['exit_price']
    pnl = row['pnl_pct']; ext = row['exit_type']
    result = '止盈' if ext == 'TP' else ('止损' if ext == 'SL' else '强平')
    tp = ep * (1.025 if d == 'LONG' else 0.975)
    sl = ep * (0.985 if d == 'LONG' else 1.015)
    lines.append(f'| 序号 | {i+1} |')
    lines.append(f'| 方向 | {d} |')
    lines.append('| 开仓时间 | ' + row['entry_time'].strftime('%Y-%m-%d %H:%M') + ' |')
    lines.append(f'| 开仓价 | ${ep:,.1f} |')
    lines.append('| 平仓时间 | ' + row['exit_time'].strftime('%Y-%m-%d %H:%M') + ' |')
    lines.append(f'| 平仓价 | \${xp:,.1f} |')
    lines.append(f'| 结果 | {result} |')
    lines.append(f'| 盈亏% | {pnl:+.2f}% |')
    lines.append(f'| 止盈价 | \${tp:,.1f} |')
    lines.append(f'| 止损价 | \${sl:,.1f} |')
    lines.append('─' * 50)

out = os.path.join(DATA_DIR, 'v4_trades_1m_precise.txt')
with open(out, 'w') as f: f.write('\n'.join(lines))

df_t.to_csv(os.path.join(DATA_DIR, 'backtest_trades_1m.csv'), index=False)
print(f"💾 已保存: {out}")
print(f"💾 CSV: {DATA_DIR}/backtest_trades_1m.csv")
print("\n✅ 全部完成")
