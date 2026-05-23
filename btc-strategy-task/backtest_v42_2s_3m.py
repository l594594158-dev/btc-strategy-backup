#!/usr/bin/env python3
"""
BTC v4.2 2秒轮询回测 — 优化版
预计算+向量化, 模拟实盘
"""
import pickle, pandas as pd, numpy as np, ta, time, sys
from datetime import datetime

DATA_DIR = '/root/.openclaw/workspace/btc-strategy-task/backtest_data'
OUT = f'{DATA_DIR}/backtest_v42_2s_3m.csv'
TP, SL = 0.025, 0.015

t0 = time.time(); print("📦 加载...", flush=True)
with open(f'{DATA_DIR}/btc_1s_1y.pkl','rb') as f: df1s = pickle.load(f)
with open(f'{DATA_DIR}/btc_spot_pure.pkl','rb') as f: d4y = pickle.load(f)

t_s = pd.Timestamp('2026-03-01'); t_e = pd.Timestamp('2026-05-22 12:30:00')

df5m_raw = d4y['5m'].loc[d4y['5m'].index >= t_s - pd.Timedelta('2D')].copy()
df1h = d4y['1h'].copy(); df4h = d4y['4h'].copy(); df1d = d4y['1d'].copy()

# 预计算指标（不用shift，直接用最新闭K值，ffill到5m）
c1h=df1h['close'].astype(float); h1h=df1h['high'].astype(float); l1h=df1h['low'].astype(float)
df1h['adx_c'] = ta.trend.ADXIndicator(h1h,l1h,c1h,14).adx()

c4h=df4h['close'].astype(float); h4h=df4h['high'].astype(float); l4h=df4h['low'].astype(float)
df4h['sma_c'] = ta.trend.SMAIndicator(c4h,20).sma_indicator()
df4h['adx_c'] = ta.trend.ADXIndicator(h4h,l4h,c4h,14).adx()
df4h['cl_c'] = c4h

c1d=df1d['close'].astype(float)
df1d['sma_c'] = ta.trend.SMAIndicator(c1d,20).sma_indicator()
df1d['cl_c'] = c1d

for col, src, sc in [('adx_1h',df1h,'adx_c'),('close_4h',df4h,'cl_c'),
    ('sma_4h',df4h,'sma_c'),('adx_4h',df4h,'adx_c'),
    ('close_1d',df1d,'cl_c'),('sma_1d',df1d,'sma_c')]:
    df5m_raw[col] = src[sc].reindex(df5m_raw.index, method='ffill')
df5m_raw.dropna(inplace=True)

mask = (df5m_raw.index >= t_s) & (df5m_raw.index <= t_e)
ts5m = df5m_raw.index[mask]; n5m = len(ts5m)

# 预计算5m close (numpy, 按ts5m顺序)
c5m_all = df5m_raw['close'].astype(float)
v5m_all = df5m_raw['volume'].astype(float)
# 找到ts5m在df5m_raw中的位置
idx_in_raw = np.array([df5m_raw.index.get_loc(t) for t in ts5m])
c5m = c5m_all.iloc[idx_in_raw].values
v5m = v5m_all.iloc[idx_in_raw].values

# 预计算vol_ratio (闭K: candle i-1的vol / avg candle[i-20:i-1]的vol)
vol_ratio = np.ones(n5m)
for i in range(20, n5m):
    gi = idx_in_raw[i]
    if gi >= 1:
        pv = v5m_all.iloc[gi-1]
        av = v5m_all.iloc[max(0,gi-20):gi].mean()
        if av > 0: vol_ratio[i] = pv/av

# 预计算前19根闭K之和 (每个5m蜡烛对应的, 不含当前蜡烛)
sum19 = np.zeros(n5m)
for i in range(19, n5m):
    sum19[i] = c5m[i-19:i].sum()

# 预计算前14根闭K的diffs (用于RSI)
# 对于蜡烛i, 需要[close[i-13]-close[i-14], ..., close[i-1]-close[i-2]]
# 即c5m[i-13:i] - c5m[i-14:i-1]
diff14_gains = np.zeros(n5m); diff14_losses = np.zeros(n5m)
for i in range(15, n5m):
    diffs = np.diff(c5m[i-14:i])
    gains = np.maximum(diffs, 0); losses = np.maximum(-diffs, 0)
    diff14_gains[i] = gains.sum(); diff14_losses[i] = losses.sum()

# 冻结指标
adx1h_arr = df5m_raw.iloc[idx_in_raw]['adx_1h'].values
adx4h_arr = df5m_raw.iloc[idx_in_raw]['adx_4h'].values
clo4h_arr = df5m_raw.iloc[idx_in_raw]['close_4h'].values
sma4h_arr = df5m_raw.iloc[idx_in_raw]['sma_4h'].values
clo1d_arr = df5m_raw.iloc[idx_in_raw]['close_1d'].values
sma1d_arr = df5m_raw.iloc[idx_in_raw]['sma_1d'].values

# 1s数据裁剪
ts1s_r = df1s.index.values; px_r = df1s['close'].astype(float).values
m1 = (ts1s_r >= np.datetime64(t_s - pd.Timedelta('1h'))) & (ts1s_r <= np.datetime64(t_e + pd.Timedelta('1h')))
ts1s = ts1s_r[m1]; prices = px_r[m1]
ts1s_ns = ts1s.astype('datetime64[ns]').astype('int64')

# 5m→1s边界
FIVE_MIN = 5*60*1_000_000_000
ts5m_ns = ts5m.astype('datetime64[ns]').astype('int64')
c_s = np.searchsorted(ts1s_ns, ts5m_ns)
c_e = np.searchsorted(ts1s_ns, ts5m_ns + FIVE_MIN)

print(f"  {n5m}蜡烛 {len(ts1s):,}点 | {time.time()-t0:.1f}s", flush=True)

trades = []; long_pos = None; short_pos = None
last_lc = -99; last_sc = -99; d5m = 5*60

for i in range(19, n5m):
    # 冻结检查
    if np.isnan(adx1h_arr[i]) or np.isnan(adx4h_arr[i]): continue
    if adx1h_arr[i] <= 25: continue
    if adx4h_arr[i] >= 40: continue
    if vol_ratio[i] < 1.0: continue
    
    h4b = clo4h_arr[i] > sma4h_arr[i]
    d1b = clo1d_arr[i] > sma1d_arr[i]
    long_ok = h4b and d1b; short_ok = (not h4b) and (not d1b)
    if not long_ok and not short_ok: continue

    s1 = c_s[i]; e1 = c_e[i]
    if s1 >= e1 or e1 > len(prices): continue

    sum19_i = sum19[i]; g14 = diff14_gains[i]; l14 = diff14_losses[i]
    prev_close = c5m[i-1]  # 上一根闭K

    for j in range(s1, e1, 2):
        px = float(prices[j])

        # 出场
        if long_pos is not None:
            pnl = (px/long_pos['entry']-1)*100
            if pnl <= -1.5:
                trades.append({'ts':ts1s[j],'dir':'LONG','entry':long_pos['entry'],'exit':px,'type':'SL','pnl':pnl,'candle':i})
                long_pos = None; break
            elif pnl >= 2.5:
                trades.append({'ts':ts1s[j],'dir':'LONG','entry':long_pos['entry'],'exit':px,'type':'TP','pnl':pnl,'candle':i})
                long_pos = None; break
        if short_pos is not None:
            pnl = (short_pos['entry']/px-1)*100
            if pnl <= -1.5:
                trades.append({'ts':ts1s[j],'dir':'SHORT','entry':short_pos['entry'],'exit':px,'type':'SL','pnl':pnl,'candle':i})
                short_pos = None; break
            elif pnl >= 2.5:
                trades.append({'ts':ts1s[j],'dir':'SHORT','entry':short_pos['entry'],'exit':px,'type':'TP','pnl':pnl,'candle':i})
                short_pos = None; break

        # 动态SMA20: (sum19 + px) / 20
        sma20 = (sum19_i + px) / 20
        pct = (px/sma20 - 1)*100
        if abs(pct) > 1.0: continue

        # 动态RSI: 14个diff[13个闭K + 1个(px-prev_close)]
        cur_diff = px - prev_close
        if cur_diff > 0: g = g14 + cur_diff; l = l14
        else: g = g14; l = l14 - cur_diff
        avg_g = g/14; avg_l = l/14
        if avg_l > 0:
            rsi = 100 - 100/(1+avg_g/avg_l)
        else:
            rsi = 100

        # 入场
        if long_pos is None and long_ok and rsi > 40 and (i - last_lc > 0):
            long_pos = {'entry': px, 'ts': ts1s[j], 'candle': i}; last_lc = i
            print(f"  🟢 LONG @ ${px:,.2f} | {ts1s[j]}", flush=True)
        if short_pos is None and short_ok and rsi < 60 and (i - last_sc > 0):
            short_pos = {'entry': px, 'ts': ts1s[j], 'candle': i}; last_sc = i
            print(f"  🔴 SHORT @ ${px:,.2f} | {ts1s[j]}", flush=True)

    if i % 4000 == 0:
        print(f"  ⏳ {i}/{n5m} | {len(trades)}笔 | {time.time()-t0:.0f}s", flush=True)

for pos, direction in [(long_pos,'LONG'),(short_pos,'SHORT')]:
    if pos:
        lp=float(prices[-1])
        pnl=(lp/pos['entry']-1)*100 if direction=='LONG' else (pos['entry']/lp-1)*100
        trades.append({'ts':ts1s[-1],'dir':direction,'entry':pos['entry'],'exit':lp,'type':'EOD','pnl':pnl,'candle':n5m-1})

elapsed=time.time()-t0; df=pd.DataFrame(trades)
if len(df)==0: print("⚠️ 无交易"); sys.exit(0)
n=len(df); nw=int((df['pnl']>0).sum()); wr=nw/n*100; tpnl=df['pnl'].sum()
sl_n=int((df['type']=='SL').sum()); tp_n=int((df['type']=='TP').sum())
lo=df[df['dir']=='LONG']; sh=df[df['dir']=='SHORT']

cap=1000; eq=[cap]
for p in df['pnl']: cap*=1+(p/100-0.0008); eq.append(cap)
eq=pd.Series(eq); mdd=((eq-eq.expanding().max())/eq.expanding().max()*100).min()
st=0;mx=0;mxp=0;cp=0
for p in df['pnl']:
    if p<=0: st+=1; cp+=p; mx=max(mx,st); mxp=min(mxp,cp)
    else: st=0; cp=0

print(f"""
╔═══════════════════════════════════════════╗
║  BTC v4.2 2秒轮询回测 (3-5月)             ║
╠═══════════════════════════════════════════╣
║  {n}笔 | WR{wr:.1f}% ({nw}W/{n-nw}L) | L{len(lo)} S{len(sh)}
║  TP{tp_n} SL{sl_n} | PnL{tpnl:+.2f}% | DD{mdd:.2f}% | 连亏{mx}笔{mxp:+.2f}%
║  耗时{elapsed:.0f}s
╚═══════════════════════════════════════════╝
""")
df['month']=pd.to_datetime(df['ts']).dt.strftime('%m')
mly=df.groupby('month').agg(笔数=('pnl','count'),胜率=('pnl',lambda x:(x>0).sum()/len(x)*100),盈亏=('pnl','sum')).round(2)
print(mly.to_string())
df.to_csv(OUT,index=False); print(f"\n💾 {OUT}")
