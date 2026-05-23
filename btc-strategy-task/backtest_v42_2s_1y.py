#!/usr/bin/env python3
"""
BTC v4.2 全量2秒轮询回测 (2025-05 ~ 2026-05, 现货指标)
· 完全对齐auto_trade.py逻辑
· 每根5m蜡烛2秒步进
· 4h/1d/1h闭K冻结(无shift)
· 同向单仓, 同烛不重复
"""
import pickle, pandas as pd, numpy as np, ta, time, sys

DATA_DIR = '/root/.openclaw/workspace/btc-strategy-task/backtest_data'
OUT = f'{DATA_DIR}/backtest_v42_2s_1y.csv'
TP, SL = 0.025, 0.015

t0 = time.time(); print("📦 加载...", flush=True)
with open(f'{DATA_DIR}/btc_1s_1y.pkl','rb') as f: df1s = pickle.load(f)
with open(f'{DATA_DIR}/btc_spot_pure.pkl','rb') as f: d4y = pickle.load(f)

t_s = d4y['5m'].index[20]; t_e = min(df1s.index[-1], d4y['5m'].index[-1])
print(f"  区间: {t_s} ~ {t_e}")

df5m_raw = d4y['5m'].loc[(d4y['5m'].index >= t_s) & (d4y['5m'].index <= t_e)].copy()
df1h = d4y['1h'].copy(); df4h = d4y['4h'].copy(); df1d = d4y['1d'].copy()

# 预计算指标 (无shift, ffill)
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

ts5m = df5m_raw.index; n5m = len(ts5m)
idx_in_raw = np.arange(len(ts5m))
c5m_all = df5m_raw['close'].astype(float)
v5m_all = df5m_raw['volume'].astype(float)
c5m = c5m_all.values; v5m = v5m_all.values

vol_ratio = np.ones(n5m)
for i in range(20, n5m):
    if i >= 1:
        pv = v5m[i-1]; av = v5m[max(0,i-20):i].mean()
        if av > 0: vol_ratio[i] = pv/av

sum19 = np.zeros(n5m)
for i in range(19, n5m): sum19[i] = c5m[i-19:i].sum()

diff14_g = np.zeros(n5m); diff14_l = np.zeros(n5m)
for i in range(15, n5m):
    diffs = np.diff(c5m[i-14:i])
    diff14_g[i] = np.maximum(diffs,0).sum(); diff14_l[i] = np.maximum(-diffs,0).sum()

adx1h_a = df5m_raw['adx_1h'].values; adx4h_a = df5m_raw['adx_4h'].values
clo4h_a = df5m_raw['close_4h'].values; sma4h_a = df5m_raw['sma_4h'].values
clo1d_a = df5m_raw['close_1d'].values; sma1d_a = df5m_raw['sma_1d'].values

# 1s数据
ts1s_r = df1s.index.values; px_r = df1s['close'].astype(float).values
m1 = (ts1s_r >= np.datetime64(ts5m[0])) & (ts1s_r <= np.datetime64(ts5m[-1])+np.timedelta64(5,'m'))
ts1s = ts1s_r[m1]; prices = px_r[m1]
ts_ns = ts1s.astype('datetime64[ns]').astype('int64')
FIVE = 5*60*1_000_000_000
ts5m_ns = ts5m.astype('datetime64[ns]').astype('int64')
c_s = np.searchsorted(ts_ns, ts5m_ns)
c_e = np.searchsorted(ts_ns, ts5m_ns+FIVE)

print(f"  {n5m}蜡烛 {len(ts1s):,}点 | {time.time()-t0:.1f}s", flush=True)

trades = []; lp = None; sp = None; llc = -99; lsc = -99

for i in range(19, n5m):
    if np.isnan(adx1h_a[i]) or np.isnan(adx4h_a[i]): continue
    if adx1h_a[i] <= 25 or adx4h_a[i] >= 40: continue
    if vol_ratio[i] < 1.0: continue
    
    h4b = clo4h_a[i] > sma4h_a[i]; l_ok = h4b and (clo1d_a[i] > sma1d_a[i])
    s_ok = (not h4b) and (clo1d_a[i] < sma1d_a[i])
    if not l_ok and not s_ok: continue

    s1 = c_s[i]; e1 = c_e[i]
    if s1 >= e1 or e1 > len(prices): continue

    s19 = sum19[i]; g14 = diff14_g[i]; l14 = diff14_l[i]; prev = c5m[i-1]

    for j in range(s1, e1, 2):
        px = float(prices[j])
        # exit
        if lp is not None:
            pnl = (px/lp['entry']-1)*100
            if pnl <= -1.5 or pnl >= 2.5:
                trades.append({'ts':ts1s[j],'dir':'LONG','entry':lp['entry'],'exit':px,'type':'TP' if pnl>0 else 'SL','pnl':pnl,'ci':i})
                lp = None; break
        if sp is not None:
            pnl = (sp['entry']/px-1)*100
            if pnl <= -1.5 or pnl >= 2.5:
                trades.append({'ts':ts1s[j],'dir':'SHORT','entry':sp['entry'],'exit':px,'type':'TP' if pnl>0 else 'SL','pnl':pnl,'ci':i})
                sp = None; break

        sma20 = (s19+px)/20; pct = (px/sma20-1)*100
        if abs(pct) > 1.0: continue

        cur_d = px-prev
        g = g14+cur_d if cur_d>0 else g14; l = l14 if cur_d>0 else l14-cur_d
        ag = g/14; al = l/14
        rsi = 100 if al==0 else 100-100/(1+ag/al)

        if lp is None and l_ok and rsi>40 and (i-llc>0):
            lp = {'entry':px,'ts':ts1s[j],'ci':i}; llc = i
            print(f"  🟢 LONG @ ${px:,.2f} | {ts1s[j]}", flush=True)
        if sp is None and s_ok and rsi<60 and (i-lsc>0):
            sp = {'entry':px,'ts':ts1s[j],'ci':i}; lsc = i
            print(f"  🔴 SHORT @ ${px:,.2f} | {ts1s[j]}", flush=True)

    if i%8000==0: print(f"  ⏳ {i}/{n5m} | {len(trades)}笔 | {time.time()-t0:.0f}s", flush=True)

for p,d in [(lp,'LONG'),(sp,'SHORT')]:
    if p:
        lp2=float(prices[-1])
        pnl=(lp2/p['entry']-1)*100 if d=='LONG' else (p['entry']/lp2-1)*100
        trades.append({'ts':ts1s[-1],'dir':d,'entry':p['entry'],'exit':lp2,'type':'EOD','pnl':pnl,'ci':n5m-1})

el=time.time()-t0; df=pd.DataFrame(trades)
if len(df)==0: print("⚠️ 无交易"); sys.exit(0)
n=len(df); nw=int((df['pnl']>0).sum()); wr=nw/n*100; tp=df['pnl'].sum()
sl_n=int((df['type']=='SL').sum()); tp_n=int((df['type']=='TP').sum())
lo=df[df['dir']=='LONG']; sh=df[df['dir']=='SHORT']

cap=1000; eq=[cap]
for p in df['pnl']: cap*=1+p/100-0.0008; eq.append(cap)
eq=pd.Series(eq); mdd=((eq-eq.expanding().max())/eq.expanding().max()*100).min()
st=mx=mp=cp=0
for p in df['pnl']:
    if p<=0: st+=1; cp+=p; mx=max(mx,st); mp=min(mp,cp)
    else: st=cp=0

print(f"""
╔═══════════════════════════════════════════╗
║  BTC v4.2 2秒轮询回测 (1年, 现货指标)      ║
╠═══════════════════════════════════════════╣
║  {n}笔 | WR{wr:.1f}% ({nw}W/{n-nw}L) | L{len(lo)} S{len(sh)}
║  TP{tp_n} SL{sl_n} | PnL{tp:+.2f}% | DD{mdd:.2f}% | 连亏{mx}笔{mp:+.2f}%
║  耗时{el:.0f}s
╚═══════════════════════════════════════════╝
""")

df['month']=pd.to_datetime(df['ts']).dt.strftime('%Y-%m')
ml=df.groupby('month').agg(笔数=('pnl','count'),胜率=('pnl',lambda x:(x>0).sum()/len(x)*100),盈亏=('pnl','sum')).round(2)
print(ml.to_string())
df.to_csv(OUT,index=False); print(f"\n💾 {OUT}")
