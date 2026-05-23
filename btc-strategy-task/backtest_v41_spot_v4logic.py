#!/usr/bin/env python3
"""
BTC v4.1 现货回测 — 严格复刻v4验证版逻辑
· ffill高周期指标(非shift)
· 无量比过滤
· 理论价出场
· 5m收盘判定（与v4一致）
"""
import pickle, pandas as pd, numpy as np, ta, time, sys

DATA_DIR = '/root/.openclaw/workspace/btc-strategy-task/backtest_data'
OUT = f'{DATA_DIR}/backtest_v41_spot_v4logic.csv'
TP, SL = 0.025, 0.015

print("📦 加载...", flush=True); t0=time.time()
with open(f'{DATA_DIR}/btc_1s_1y.pkl','rb') as f: df1s = pickle.load(f)
with open(f'{DATA_DIR}/btc_spot_pure.pkl','rb') as f: d4y = pickle.load(f)

t_min = max(df1s.index[0], d4y['5m'].index[20])
t_max = min(df1s.index[-1], d4y['5m'].index[-1])

df5m = d4y['5m'][(d4y['5m'].index >= t_min) & (d4y['5m'].index <= t_max)].copy()
df1h = d4y['1h'][d4y['1h'].index <= t_max].copy()
df4h = d4y['4h'][d4y['4h'].index <= t_max].copy()
df1d = d4y['1d'][d4y['1d'].index <= t_max].copy()

# === 严格复刻v4的指标计算（ffill, 非shift）===
close_5m = df5m['close'].astype(float)
high_5m = df5m['high'].astype(float); low_5m = df5m['low'].astype(float)
df5m['sma20_5m'] = ta.trend.SMAIndicator(close_5m, 20).sma_indicator()
df5m['rsi_5m'] = ta.momentum.RSIIndicator(close_5m, 14).rsi()
df5m['pct_sma'] = (close_5m - df5m['sma20_5m']) / df5m['sma20_5m'] * 100

c1h=df1h['close'].astype(float); h1h=df1h['high'].astype(float); l1h=df1h['low'].astype(float)
df1h['adx_1h'] = ta.trend.ADXIndicator(h1h,l1h,c1h,14).adx()

c4h=df4h['close'].astype(float); h4h=df4h['high'].astype(float); l4h=df4h['low'].astype(float)
df4h['sma20_4h'] = ta.trend.SMAIndicator(c4h,20).sma_indicator()
df4h['adx_4h'] = ta.trend.ADXIndicator(h4h,l4h,c4h,14).adx()

c1d=df1d['close'].astype(float)
df1d['sma20_1d'] = ta.trend.SMAIndicator(c1d,20).sma_indicator()

# ffill到5m（非shift!）
for col, src in [('adx_1h',df1h),('sma20_4h',df4h),('adx_4h',df4h),('sma20_1d',df1d)]:
    df5m[col] = src[col].reindex(df5m.index, method='ffill')
df5m['close_4h'] = df4h['close'].reindex(df5m.index, method='ffill')
df5m['close_1d'] = df1d['close'].reindex(df5m.index, method='ffill')
df5m.dropna(inplace=True)

# 获取有效索引后的1s数据
ts5m = df5m.index.values; n5m = len(ts5m)
c5m = close_5m.loc[ts5m].values
sma5m = df5m['sma20_5m'].values
rsi5m = df5m['rsi_5m'].values
pct5m = df5m['pct_sma'].values
adx1h = df5m['adx_1h'].values
adx4h = df5m['adx_4h'].values
sma4h = df5m['sma20_4h'].values
sma1d = df5m['sma20_1d'].values
clo4h = df5m['close_4h'].values
clo1d = df5m['close_1d'].values

print(f"  {n5m}有效5m蜡烛 | {time.time()-t0:.1f}s", flush=True)

# === 准备1s数据用于出场 ===
ts1s = df1s.index.values; prices_1s = df1s['close'].astype(float).values
mask = (ts1s >= np.datetime64(ts5m[0])) & (ts1s <= np.datetime64(ts5m[-1]))
ts1s = ts1s[mask]; prices_1s = prices_1s[mask]
ts1s_ns = ts1s.astype('datetime64[ns]').astype('int64')
ts5m_ns = ts5m.astype('datetime64[ns]').astype('int64')
five_min_ns = 5*60*1_000_000_000
start_idx = np.searchsorted(ts1s_ns, ts5m_ns)
end_idx = np.searchsorted(ts1s_ns, ts5m_ns + five_min_ns)

# === 主循环 ===
print(f"🚀 回测...", flush=True)
trades = []; long_pos = None; short_pos = None
long_sig = False; short_sig = False; t0 = time.time()

for i in range(n5m):
    cl = c5m[i]; ts = ts5m[i]

    # ── 出场（用1s市价，更精确） ──
    for pos_key, direction in [(('long_pos', long_pos), 'LONG'), (('short_pos', short_pos), 'SHORT')]:
        pos = pos_key[1]
        if pos is None: continue
        # 查上一根到当前的1s数据
        s_exit = start_idx[i-1] if i>0 else 0
        e_exit = end_idx[i]
        for j in range(s_exit, min(e_exit, len(prices_1s))):
            px = float(prices_1s[j])
            if direction == 'LONG':
                if px >= pos['entry']*(1+TP) or px <= pos['entry']*(1-SL):
                    typ = 'TP' if px >= pos['entry']*(1+TP) else 'SL'
                    pnl = (px/pos['entry']-1)*100
                    trades.append({'ts':ts,'dir':direction,'entry':pos['entry'],'exit':px,'type':typ,'pnl':pnl})
                    if direction == 'LONG': long_pos = None
                    else: short_pos = None
                    break
            else:
                if px <= pos['entry']*(1-TP) or px >= pos['entry']*(1+SL):
                    typ = 'TP' if px <= pos['entry']*(1-TP) else 'SL'
                    pnl = (pos['entry']/px-1)*100
                    trades.append({'ts':ts,'dir':direction,'entry':pos['entry'],'exit':px,'type':typ,'pnl':pnl})
                    if direction == 'LONG': long_pos = None
                    else: short_pos = None
                    break

    # ── 入场（v4逻辑: ffill指标 + 无量比过滤）──
    h4b = clo4h[i] > sma4h[i]
    d1b = clo1d[i] > sma1d[i]
    within = abs(pct5m[i]) <= 1.0
    adx1_ok = adx1h[i] > 25
    adx4_ok = adx4h[i] < 40

    long_sig_now = h4b and d1b and within and adx1_ok and adx4_ok and rsi5m[i] > 40
    short_sig_now = (not h4b) and (not d1b) and within and adx1_ok and adx4_ok and rsi5m[i] < 60

    if long_pos is None and long_sig_now and not long_sig:
        long_pos = {'entry': cl, 'ts': ts}
        print(f"  🟢 LONG @ ${cl:,.2f} | {ts}", flush=True)
    if short_pos is None and short_sig_now and not short_sig:
        short_pos = {'entry': cl, 'ts': ts}
        print(f"  🔴 SHORT @ ${cl:,.2f} | {ts}", flush=True)

    long_sig = long_sig_now; short_sig = short_sig_now

    if i % 4000 == 0:
        e=time.time()-t0; print(f"  ⏳ {i}/{n5m} | {len(trades)}笔 | {e:.0f}s", flush=True)

# EOD
for pos, direction in [(long_pos, 'LONG'), (short_pos, 'SHORT')]:
    if pos:
        lp=float(prices_1s[-1])
        pnl=(lp/pos['entry']-1)*100 if direction=='LONG' else (pos['entry']/lp-1)*100
        trades.append({'ts':ts5m[-1],'dir':direction,'entry':pos['entry'],'exit':lp,'type':'EOD','pnl':pnl})

elapsed=time.time()-t0
print(f"\n✅ {elapsed:.0f}s | {len(trades)}笔", flush=True)

if not trades: print("⚠️ 无交易"); sys.exit(0)

df=pd.DataFrame(trades); df['month']=pd.to_datetime(df['ts']).dt.strftime('%Y-%m')
n=len(df); nw=(df['pnl']>0).sum(); wr=nw/n*100; tpn=df['pnl'].sum(); mdd=df['pnl'].cumsum().min()
lo=df[df['dir']=='LONG']; sh=df[df['dir']=='SHORT']
sl_n=(df['type']=='SL').sum(); tp_n=(df['type']=='TP').sum()

print(f"""
╔════════════════════════════════════════════╗
║  BTC v4.1 ffill+无量比 现货回测             ║
╠════════════════════════════════════════════╣
║  {n}笔 | 胜率{wr:.1f}% ({nw}W/{n-nw}L) | LONG{len(lo)} SHORT{len(sh)}
║  TP{tp_n} SL{sl_n} | 盈亏{tpn:+.2f}% | 回撤{mdd:+.2f}%
╚════════════════════════════════════════════╝
""")

monthly = df.groupby('month').agg(笔数=('pnl','count'),胜率=('pnl',lambda x:(x>0).sum()/len(x)*100),盈亏=('pnl','sum')).round(2)
print(monthly.to_string())
df.to_csv(OUT, index=False); print(f"\n💾 {OUT}")
