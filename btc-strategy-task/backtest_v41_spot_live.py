#!/usr/bin/env python3
"""
BTC v4.1 现货回测 — 2秒轮询+1秒精度
优化: 纯numpy, 预计算1s蜡烛边界, O(1)切片
"""
import pickle, pandas as pd, numpy as np, ta, time, sys

DATA_DIR = '/root/.openclaw/workspace/btc-strategy-task/backtest_data'
OUT = f'{DATA_DIR}/backtest_v41_spot_live.csv'
TP, SL = 0.025, 0.015

print("📦 加载...", flush=True); t0=time.time()
with open(f'{DATA_DIR}/btc_1s_1y.pkl','rb') as f: df1s = pickle.load(f)
with open(f'{DATA_DIR}/btc_data_4y.pkl','rb') as f: d4y = pickle.load(f)

t_min = max(df1s.index[0], d4y['5m'].index[20])
t_max = min(df1s.index[-1], d4y['5m'].index[-1])

# 转为numpy (极速)
ts1s = df1s.index.values
prices_1s = df1s['close'].astype(float).values
mask = (ts1s >= np.datetime64(t_min)) & (ts1s <= np.datetime64(t_max))
ts1s = ts1s[mask]; prices_1s = prices_1s[mask]

df5m = d4y['5m'][(d4y['5m'].index >= t_min) & (d4y['5m'].index <= t_max)]
df1h = d4y['1h'][d4y['1h'].index <= t_max]; df4h = d4y['4h'][d4y['4h'].index <= t_max]; df1d = d4y['1d'][d4y['1d'].index <= t_max]
print(f"  1s:{len(prices_1s):,} 5m:{len(df5m):,} {time.time()-t0:.1f}s", flush=True)

# ═══ 预计算 ═══
t0=time.time()

# 每个5m蜡烛在1s数据中的起止索引
ts5m = df5m.index.values; n5m = len(ts5m)
ts5m_ns = ts5m.astype('datetime64[ns]').astype('int64')
ts1s_ns = ts1s.astype('datetime64[ns]').astype('int64')

# 预计算每个5m蜡烛的1s起止索引
five_min_ns = 5 * 60 * 1_000_000_000
start_idx = np.searchsorted(ts1s_ns, ts5m_ns)
end_idx = np.searchsorted(ts1s_ns, ts5m_ns + five_min_ns)

# 5m close数组 + 滚动和
c5m = df5m['close'].astype(float).values

# 1h闭K ADX
c1h,h1h,l1h = df1h['close'].astype(float),df1h['high'].astype(float),df1h['low'].astype(float)
adx1h = pd.Series(ta.trend.ADXIndicator(h1h,l1h,c1h,14).adx().values, index=df1h.index).shift(1)

# 4h闭K
c4h,h4h,l4h = df4h['close'].astype(float),df4h['high'].astype(float),df4h['low'].astype(float)
clo4h = c4h.shift(1); sma4h = pd.Series(ta.trend.SMAIndicator(c4h,20).sma_indicator().values, index=df4h.index).shift(1)
adx4h = pd.Series(ta.trend.ADXIndicator(h4h,l4h,c4h,14).adx().values, index=df4h.index).shift(1)

# 1d闭K
c1d = df1d['close'].astype(float)
clo1d = c1d.shift(1); sma1d = pd.Series(ta.trend.SMAIndicator(c1d,20).sma_indicator().values, index=df1d.index).shift(1)

# 5m量比
v5 = df5m['volume'].astype(float)
vol_ratio = (v5.shift(1) / v5.rolling(20).mean().shift(1)).fillna(1.0).values.copy()

def last_before(series, ts_ns):
    pos = series.index.searchsorted(pd.Timestamp(ts_ns), side='right') - 1
    return float(series.iloc[pos]) if pos >= 0 else np.nan

# 为每个5m蜡烛预计算闭K指标
pre_adx1h = np.array([last_before(adx1h, ts5m_ns[i]) for i in range(n5m)])
pre_adx4h = np.array([last_before(adx4h, ts5m_ns[i]) for i in range(n5m)])
pre_clo4h = np.array([last_before(clo4h, ts5m_ns[i]) for i in range(n5m)])
pre_sma4h = np.array([last_before(sma4h, ts5m_ns[i]) for i in range(n5m)])
pre_clo1d = np.array([last_before(clo1d, ts5m_ns[i]) for i in range(n5m)])
pre_sma1d = np.array([last_before(sma1d, ts5m_ns[i]) for i in range(n5m)])

# 预设sum19/sum14
sum19 = np.array([c5m[max(0,i-19):i].sum() for i in range(n5m)])
sum14 = np.array([c5m[max(0,i-14):i].sum() for i in range(n5m)])

# 预筛选
ok = np.zeros(n5m, dtype=bool)
for i in range(n5m):
    vals = [pre_adx1h[i],pre_adx4h[i],pre_clo4h[i],pre_sma4h[i],pre_clo1d[i],pre_sma1d[i],vol_ratio[i]]
    if any(np.isnan(v) for v in vals): continue
    if pre_adx1h[i]<=25 or pre_adx4h[i]>=40 or vol_ratio[i]<1.0: continue
    if (pre_clo4h[i]>pre_sma4h[i]) != (pre_clo1d[i]>pre_sma1d[i]): continue
    ok[i] = True

print(f"  预计算: {n5m}根 | 预筛: {ok.sum()}/{n5m} | {time.time()-t0:.1f}s", flush=True)

# ═══ 主循环 ═══
print("🚀 回测...", flush=True)
trades = []; pos = None; last_l = last_s = False; t0 = time.time()

def rsi14(arr):
    d = np.diff(arr); g = np.clip(d,0,None).mean(); l = np.clip(-d,0,None).mean()
    return 100.0 if l<1e-9 else 100-100/(1+g/l)

for i in range(20, n5m):
    s, e = start_idx[i], end_idx[i]
    if e <= s+1: continue  # 数据不足

    px_arr = prices_1s[s:e]; npx = len(px_arr)

    # ── 出场: 逐秒 ──
    if pos:
        d, ent, slv, tpv = pos['dir'], pos['entry'], pos['sl'], pos['tp']
        for j in range(npx):
            px = float(px_arr[j])
            if d == 'LONG':
                if px <= slv or px >= tpv:
                    typ = 'SL' if px <= slv else 'TP'
                    pnl = (px/ent-1)*100
                    trades.append({'entry_ts':pos['ts'],'exit_ts':ts5m[i]+np.timedelta64(j,'s'),
                                  'dir':d,'entry':ent,'exit':px,'type':typ,'pnl':pnl})
                    pos = None; last_l = last_s = False
                    break
            else:
                if px >= slv or px <= tpv:
                    typ = 'SL' if px >= slv else 'TP'
                    pnl = (ent/px-1)*100
                    trades.append({'entry_ts':pos['ts'],'exit_ts':ts5m[i]+np.timedelta64(j,'s'),
                                  'dir':d,'entry':ent,'exit':px,'type':typ,'pnl':pnl})
                    pos = None; last_l = last_s = False
                    break
        if pos is None: continue

    # ── 入场: 2秒轮询 ──
    if pos is None and ok[i]:
        steps = np.arange(0, npx, 2)
        for j in steps:
            px = float(px_arr[j])
            sma20 = (sum19[i] + px) / 20.0 if sum19[i] > 0 else px
            if abs((px-sma20)/sma20*100) > 1.0: continue
            if i < 14: continue

            arr14 = np.append(c5m[i-13:i], px)
            rsi = rsi14(arr14)
            h4b = pre_clo4h[i] > pre_sma4h[i]
            d1b = pre_clo1d[i] > pre_sma1d[i]

            sig = None
            if h4b and d1b and rsi > 40 and not last_l: sig = 'LONG'
            elif not h4b and not d1b and rsi < 60 and not last_s: sig = 'SHORT'

            if sig == 'LONG':
                pos = {'dir':'LONG','entry':px,'sl':px*(1-SL),'tp':px*(1+TP),'ts':ts5m[i]+np.timedelta64(j,'s')}
                last_l = True; break
            elif sig == 'SHORT':
                pos = {'dir':'SHORT','entry':px,'sl':px*(1+SL),'tp':px*(1-TP),'ts':ts5m[i]+np.timedelta64(j,'s')}
                last_s = True; break
            else:
                last_l = (sig=='LONG'); last_s = (sig=='SHORT')

    if i % 2000 == 0 and i > 0:
        e = time.time()-t0
        print(f"  ⏳ {i}/{n5m} ({100*i/n5m:.0f}%) | {len(trades)}笔 | {e:.0f}s | ETA {e/i*(n5m-i):.0f}s", flush=True)

# EOD
if pos:
    lp = float(prices_1s[-1])
    d=pos['dir']; pnl=(lp/pos['entry']-1)*100 if d=='LONG' else (pos['entry']/lp-1)*100
    trades.append({'entry_ts':pos['ts'],'exit_ts':ts5m[-1],'dir':d,'entry':pos['entry'],'exit':lp,'type':'EOD','pnl':pnl})

elapsed = time.time()-t0
print(f"\n✅ 完成 {elapsed:.0f}s | {len(trades)}笔", flush=True)

if not trades: print("⚠️ 无交易"); sys.exit(0)

df = pd.DataFrame(trades)
df['month'] = pd.to_datetime(df['entry_ts']).dt.strftime('%Y-%m')
n=len(df); nw=(df['pnl']>0).sum(); wr=nw/n*100
tp_sum=df['pnl'].sum(); mdd=df['pnl'].cumsum().min()
longs=df[df['dir']=='LONG']; shorts=df[df['dir']=='SHORT']
sl_n=(df['type']=='SL').sum(); tp_n=(df['type']=='TP').sum()

print(f"""
╔════════════════════════════════════════════╗
║  BTC v4.1 现货回测 (2秒轮询+1秒精度)       ║
╠════════════════════════════════════════════╣
║  区间: {str(t_min)[:10]} ~ {str(t_max)[:10]}
║  总交易: {n}笔 | 胜率: {wr:.1f}% ({nw}W/{n-nw}L)
║  LONG: {len(longs)}笔 | SHORT: {len(shorts)}笔
║  TP: {tp_n}笔 | SL: {sl_n}笔
║  总盈亏: {tp_sum:+.2f}% | 最大回撤: {mdd:+.2f}%
╚════════════════════════════════════════════╝
""")

monthly = df.groupby('month').agg(
    笔数=('pnl','count'), 胜率=('pnl',lambda x:(x>0).sum()/len(x)*100), 盈亏=('pnl','sum')
).round(2)
print("📅 月度表现:")
print(monthly.to_string())
df.to_csv(OUT, index=False)
print(f"\n💾 {OUT}")
