#!/usr/bin/env python3
"""
BTC v4.1 现货回测 — 5m收盘判定版本
· 每个5m蜡烛收盘后才判定信号（跟你一致）
· 20根已收盘5m蜡烛计算SMA20/RSI
· 入场按信号K线收盘价，出场逐秒检查
"""
import pickle, pandas as pd, numpy as np, ta, time, sys

DATA_DIR = '/root/.openclaw/workspace/btc-strategy-task/backtest_data'
OUT = f'{DATA_DIR}/backtest_v41_spot_close.csv'
TP, SL = 0.025, 0.015

print("📦 加载...", flush=True); t0=time.time()
with open(f'{DATA_DIR}/btc_1s_1y.pkl','rb') as f: df1s = pickle.load(f)
with open(f'{DATA_DIR}/btc_spot_pure.pkl','rb') as f: d4y = pickle.load(f)

t_min = max(df1s.index[0], d4y['5m'].index[20])
t_max = min(df1s.index[-1], d4y['5m'].index[-1])

ts1s = df1s.index.values; prices_1s = df1s['close'].astype(float).values
mask = (ts1s >= np.datetime64(t_min)) & (ts1s <= np.datetime64(t_max))
ts1s = ts1s[mask]; prices_1s = prices_1s[mask]

df5m = d4y['5m'][(d4y['5m'].index >= t_min) & (d4y['5m'].index <= t_max)]
df1h = d4y['1h'][d4y['1h'].index <= t_max]; df4h = d4y['4h'][d4y['4h'].index <= t_max]; df1d = d4y['1d'][d4y['1d'].index <= t_max]

ts5m = df5m.index.values; n5m = len(ts5m)
ts5m_ns = ts5m.astype('datetime64[ns]').astype('int64')
ts1s_ns = ts1s.astype('datetime64[ns]').astype('int64')
five_min_ns = 5*60*1_000_000_000
start_idx = np.searchsorted(ts1s_ns, ts5m_ns)
end_idx = np.searchsorted(ts1s_ns, ts5m_ns + five_min_ns)
c5m = df5m['close'].astype(float).values

# 闭K指标（跟之前一致）
c1h=df1h['close'].astype(float); h1h=df1h['high'].astype(float); l1h=df1h['low'].astype(float)
adx1h = pd.Series(ta.trend.ADXIndicator(h1h,l1h,c1h,14).adx().values, index=df1h.index).shift(1)
c4h=df4h['close'].astype(float); h4h=df4h['high'].astype(float); l4h=df4h['low'].astype(float)
clo4h=c4h.shift(1); sma4h=pd.Series(ta.trend.SMAIndicator(c4h,20).sma_indicator().values, index=df4h.index).shift(1)
adx4h=pd.Series(ta.trend.ADXIndicator(h4h,l4h,c4h,14).adx().values, index=df4h.index).shift(1)
c1d=df1d['close'].astype(float); clo1d=c1d.shift(1)
sma1d=pd.Series(ta.trend.SMAIndicator(c1d,20).sma_indicator().values, index=df1d.index).shift(1)
v5=df5m['volume'].astype(float); vol_ratio=(v5.shift(1)/v5.rolling(20).mean().shift(1)).fillna(1.0).values.copy()

def last_before(series, ts_ns):
    pos = series.index.searchsorted(pd.Timestamp(ts_ns), side='right')-1
    return float(series.iloc[pos]) if pos>=0 else np.nan

pre_adx1h=np.array([last_before(adx1h, ts5m_ns[i]) for i in range(n5m)])
pre_adx4h=np.array([last_before(adx4h, ts5m_ns[i]) for i in range(n5m)])
pre_clo4h=np.array([last_before(clo4h, ts5m_ns[i]) for i in range(n5m)])
pre_sma4h=np.array([last_before(sma4h, ts5m_ns[i]) for i in range(n5m)])
pre_clo1d=np.array([last_before(clo1d, ts5m_ns[i]) for i in range(n5m)])
pre_sma1d=np.array([last_before(sma1d, ts5m_ns[i]) for i in range(n5m)])

# ═══ 收盘判定回测 ═══
print(f"🚀 收盘判定回测... {n5m}根蜡烛", flush=True)
trades = []; pos = None; t0=time.time()

def rsi14_close(arr14):
    d=np.diff(arr14); g=np.clip(d,0,None).mean(); l=np.clip(-d,0,None).mean()
    return 100.0 if l<1e-9 else 100-100/(1+g/l)

for i in range(21, n5m):  # 从21开始，确保20根已收盘
    # ── 出场: 用1s数据检查上一根到当前蜡烛之间 ──
    if pos:
        # 检查从上一根5m蜡烛初到当前蜡烛末的1s数据
        s_exit = start_idx[i-1] if i>0 else 0
        e_exit = end_idx[i]
        if e_exit > s_exit:
            d, ent, slv, tpv = pos['dir'], pos['entry'], pos['sl'], pos['tp']
            for j in range(s_exit, e_exit):
                px = float(prices_1s[j])
                hit, typ = False, None
                if d == 'LONG':
                    if px <= slv: hit, typ = True, 'SL'
                    elif px >= tpv: hit, typ = True, 'TP'
                else:
                    if px >= slv: hit, typ = True, 'SL'
                    elif px <= tpv: hit, typ = True, 'TP'
                if hit:
                    pnl = (px/ent-1)*100 if d=='LONG' else (ent/px-1)*100
                    trades.append({'entry_ts':pos['ts'],'exit_ts':ts1s[j],
                                  'dir':d,'entry':ent,'exit':px,'type':typ,'pnl':pnl})
                    pos = None; break

    # ── 入场: 当前蜡烛已收盘，用闭K指标判断 ──
    if pos is None:
        p = pre_adx1h[i], pre_adx4h[i], pre_clo4h[i], pre_sma4h[i], pre_clo1d[i], pre_sma1d[i], vol_ratio[i]
        if any(np.isnan(v) for v in p): continue
        if pre_adx1h[i]<=25 or pre_adx4h[i]>=40 or vol_ratio[i]<1.0: continue

        h4b = pre_clo4h[i] > pre_sma4h[i]
        d1b = pre_clo1d[i] > pre_sma1d[i]
        if h4b != d1b: continue

        # 用20根已收盘K线计算SMA20 (第i根已收盘)
        sma20 = c5m[max(0,i-19):i+1].mean()  # i-19到i，共20根
        px_close = c5m[i]  # 入场价=收盘价
        pct = (px_close - sma20) / sma20 * 100
        if abs(pct) > 1.0: continue

        # RSI14 = 最近14根已收盘K线
        if i < 14: continue
        rsi = rsi14_close(c5m[i-13:i+1])

        if h4b and d1b and rsi > 40:
            pos = {'dir':'LONG','entry':px_close,'sl':px_close*(1-SL),'tp':px_close*(1+TP),'ts':ts5m[i]}
            print(f"  🟢 LONG @ ${px_close:,.2f} | {ts5m[i]}", flush=True)
        elif not h4b and not d1b and rsi < 60:
            pos = {'dir':'SHORT','entry':px_close,'sl':px_close*(1+SL),'tp':px_close*(1-TP),'ts':ts5m[i]}
            print(f"  🔴 SHORT @ ${px_close:,.2f} | {ts5m[i]}", flush=True)

    if i % 2000 == 0:
        e=time.time()-t0; print(f"  ⏳ {i}/{n5m} | {len(trades)}笔 | {e:.0f}s", flush=True)

if pos:
    lp=float(prices_1s[-1]); d=pos['dir']
    pnl=(lp/pos['entry']-1)*100 if d=='LONG' else (pos['entry']/lp-1)*100
    trades.append({'entry_ts':pos['ts'],'exit_ts':ts5m[-1],'dir':d,'entry':pos['entry'],'exit':lp,'type':'EOD','pnl':pnl})

elapsed=time.time()-t0; print(f"\n✅ {elapsed:.0f}s | {len(trades)}笔", flush=True)

if not trades: print("⚠️ 无交易"); sys.exit(0)

df=pd.DataFrame(trades); df['month']=pd.to_datetime(df['entry_ts']).dt.strftime('%Y-%m')
n=len(df); nw=(df['pnl']>0).sum(); wr=nw/n*100; tpn=df['pnl'].sum(); mdd=df['pnl'].cumsum().min()
lo=df[df['dir']=='LONG']; sh=df[df['dir']=='SHORT']
sl_n=(df['type']=='SL').sum(); tp_n=(df['type']=='TP').sum()

print(f"""
╔════════════════════════════════════════════╗
║  BTC v4.1 收盘判定回测                      ║
╠════════════════════════════════════════════╣
║  {n}笔 | 胜率{wr:.1f}% ({nw}W/{n-nw}L) | LONG{len(lo)} SHORT{len(sh)}
║  TP{tp_n} SL{sl_n} | 盈亏{tpn:+.2f}% | 回撤{mdd:+.2f}%
╚════════════════════════════════════════════╝
""")

monthly = df.groupby('month').agg(笔数=('pnl','count'),胜率=('pnl',lambda x:(x>0).sum()/len(x)*100),盈亏=('pnl','sum')).round(2)
print(monthly.to_string())
df.to_csv(OUT, index=False); print(f"\n💾 {OUT}")
