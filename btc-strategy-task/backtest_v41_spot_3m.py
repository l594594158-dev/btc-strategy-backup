#!/usr/bin/env python3
"""v4.1 现货全K线 近3月回测"""
import pandas as pd, numpy as np, ta, time, sys

t0 = time.time()
TP = 0.025; SL = 0.015; FEE = 0.0008

print('加载现货1s -> 构建全量K线...')
df = pd.read_pickle('backtest_data/btc_1s_1y.pkl')
cut = df.index[-1] - pd.Timedelta(days=180)

for tf,rule in [('1m','1min'),('5m','5min'),('1h','1h'),('4h','4h'),('1d','D')]:
    g = df.resample(rule,label='right',closed='right')
    dft = g.agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna()
    globals()[f'c_{tf}'] = dft['close'].astype(float).values
    globals()[f'h_{tf}'] = dft['high'].astype(float).values
    globals()[f'l_{tf}'] = dft['low'].astype(float).values
    globals()[f'ts_{tf}'] = dft.index
    if tf == '5m':
        v5m = dft['volume'].astype(float).values
        ts5 = dft.index
    print(f'  {tf}: {len(dft)}')

n1m = len(c_1m); n5 = len(c_5m)

print('预计算指标...')
def build_tf(cv, hv, lv):
    n = len(cv)
    cc = np.full(n, np.nan); sc = np.full(n, np.nan); ac = np.full(n, np.nan)
    sf = ta.trend.SMAIndicator(pd.Series(cv), 20).sma_indicator().values
    try:
        af = ta.trend.ADXIndicator(pd.Series(hv), pd.Series(lv), pd.Series(cv), 14).adx().values
    except:
        af = np.full(n, 25.0)
    for i in range(20, n):
        cc[i] = cv[i]
        sc[i] = sf[i] if not np.isnan(sf[i]) else np.nan
        ac[i] = af[i] if not np.isnan(af[i]) else 25.0
    return cc, sc, ac

c4hc, s4hc, a4hc = build_tf(c_4h, h_4h, l_4h)
c1dc, s1dc, _ = build_tf(c_1d, h_1d, l_1d)
_, _, a1hc = build_tf(c_1h, h_1h, l_1h)

def map_to_5m(st, vv):
    r = np.full(n5, np.nan)
    for i in range(1, n5):
        t = ts5[i]
        p = np.searchsorted(st, t, side='right') - 1
        if 0 <= p < len(vv):
            r[i] = vv[p]
    return r

c4h5 = map_to_5m(ts_4h, c4hc); s4h5 = map_to_5m(ts_4h, s4hc); a4h5 = map_to_5m(ts_4h, a4hc)
c1d5 = map_to_5m(ts_1d, c1dc); s1d5 = map_to_5m(ts_1d, s1dc)
a1h5 = map_to_5m(ts_1h, a1hc)

ag5 = np.full(n5, np.nan); al5 = np.full(n5, np.nan)
for i in range(15, n5):
    d = c_5m[i-1] - c_5m[i-2]
    if i == 15:
        ag5[i] = np.mean([max(c_5m[j]-c_5m[j-1],0) for j in range(1,15)])
        al5[i] = np.mean([max(c_5m[j-1]-c_5m[j],0) for j in range(1,15)])
    else:
        ag5[i] = (ag5[i-1]*13 + max(d,0)) / 14
        al5[i] = (al5[i-1]*13 + max(-d,0)) / 14

vr5 = np.full(n5, 1.0)
for i in range(21, n5):
    a = v5m[i-20:i].mean()
    vr5[i] = v5m[i-1] / a if a > 0 else 1.0

I5S = max(30, np.searchsorted(ts5, cut))
print(f'回测 {n5-I5S} 根5m bar...')

lp = None; sp = None; lls = False; lss = False
trades = []
step = 20000

for i5 in range(I5S, n5):
    if i5 % step == 0:
        print(f'  [{i5}/{n5}] {len(trades)}笔', flush=True)
    if np.isnan(c4h5[i5]) or np.isnan(s4h5[i5]) or np.isnan(a4h5[i5]):
        continue
    if np.isnan(c1d5[i5]) or np.isnan(s1d5[i5]) or np.isnan(a1h5[i5]):
        continue
    hb = c4h5[i5] > s4h5[i5]
    db = c1d5[i5] > s1d5[i5]
    a1v = a1h5[i5]; a4v = a4h5[i5]; vr = vr5[i5]
    te = ts5[i5]; ts2 = te - pd.Timedelta(minutes=5)
    s0 = np.searchsorted(ts_1m, ts2)
    se = np.searchsorted(ts_1m, te)
    if se > s0: se -= 1

    for js in range(s0, min(se+1, n1m)):
        px = c_1m[js]; pxt = ts_1m[js]
        rsi = 50.0  # default

        # Exit LONG
        if lp is not None:
            pnl_pct = (px - lp['ep']) / lp['ep'] * 100
            if pnl_pct <= -SL * 100:
                trades.append({'d':'LONG','ets':str(lp['ts']),'ep':lp['ep'],
                    'xts':str(pxt),'xp':px,'xt':'SL','pnl':pnl_pct-FEE*2*100})
                lp = None; lls = False
            elif pnl_pct >= TP * 100:
                trades.append({'d':'LONG','ets':str(lp['ts']),'ep':lp['ep'],
                    'xts':str(pxt),'xp':px,'xt':'TP','pnl':pnl_pct-FEE*2*100})
                lp = None; lls = False

        # Exit SHORT
        if sp is not None:
            pnl_pct = (sp['ep'] - px) / sp['ep'] * 100
            if pnl_pct <= -SL * 100:
                trades.append({'d':'SHORT','ets':str(sp['ts']),'ep':sp['ep'],
                    'xts':str(pxt),'xp':px,'xt':'SL','pnl':pnl_pct-FEE*2*100})
                sp = None; lss = False
            elif pnl_pct >= TP * 100:
                trades.append({'d':'SHORT','ets':str(sp['ts']),'ep':sp['ep'],
                    'xts':str(pxt),'xp':px,'xt':'TP','pnl':pnl_pct-FEE*2*100})
                sp = None; lss = False

        if js == s0: continue
        if a1v <= 25: continue
        if a4v >= 40: continue
        s19 = c_5m[max(0,i5-19):i5].sum()
        sd = (s19 + px) / 20
        if abs((px - sd) / sd * 100) > 1.0: continue
        if vr < 1.0: continue
        if np.isnan(ag5[i5]) or np.isnan(al5[i5]): continue
        diff = px - c_5m[i5-1]
        cg = (ag5[i5] * 13 + max(diff, 0)) / 14
        cl = (al5[i5] * 13 + max(-diff, 0)) / 14
        if cl <= 0: continue
        rsi = 100 - 100 / (1 + cg / cl)

        # LONG signal
        ln = hb and db and rsi > 40
        if ln and not lls and lp is None:
            lp = {'ep': px, 'ts': pxt}
        lls = ln

        # SHORT signal
        sn = (not hb) and (not db) and rsi < 60
        if sn and not lss and sp is None:
            sp = {'ep': px, 'ts': pxt}
        lss = sn

df_t = pd.DataFrame(trades)
print(f'\n全部交易: {len(df_t)}笔')

if len(df_t) == 0:
    print('没有交易生成!')
    sys.exit(1)

# 近3月筛选
df_t['ets_dt'] = pd.to_datetime(df_t['ets'])
cut_dt = pd.Timestamp('2026-02-23')
R = df_t[df_t['ets_dt'] >= cut_dt]

wins = (R['pnl'] > 0).sum()
wr = wins / len(R) * 100 if len(R) > 0 else 0
tp_c = (R['xt'] == 'TP').sum()
sl_c = (R['xt'] == 'SL').sum()
nL = len(R[R['d'] == 'LONG']); nS = len(R[R['d'] == 'SHORT'])
pnl_sum = R['pnl'].sum()

print(f'\n=============== 现货K线 近3月回测 ===============')
print(f'区间: 2026-02-23 ~ 2026-05-22')
print(f'总笔数: {len(R)} | 胜率: {wr:.1f}% | 总盈亏: {pnl_sum:+.1f}%')
print(f'LONG: {nL}笔 | SHORT: {nS}笔 | TP: {tp_c} | SL: {sl_c}')
if nL > 0:
    ldf = R[R['d'] == 'LONG']
    lwr = (ldf['pnl'] > 0).sum() / nL * 100
    lpnl = ldf['pnl'].sum()
    print(f'LONG: WR={lwr:.0f}% PnL={lpnl:+.1f}%')
if nS > 0:
    sdf = R[R['d'] == 'SHORT']
    swr = (sdf['pnl'] > 0).sum() / nS * 100
    spnl = sdf['pnl'].sum()
    print(f'SHORT: WR={swr:.0f}% PnL={spnl:+.1f}%')

# 按月分组
R['ym'] = R['ets_dt'].dt.strftime('%Y-%m')
R = R.sort_values('ets_dt')
for ym in sorted(R['ym'].unique()):
    sub = R[R['ym'] == ym]
    w = (sub['pnl'] > 0).sum()
    p = sub['pnl'].sum()
    print(f'\n--- {ym} --- {len(sub)}笔 WR={w/len(sub)*100:.0f}% PnL={p:+.1f}%')
    for i, (_, t) in enumerate(sub.iterrows(), 1):
        e = t['ets'][5:16]; x = t['xts'][5:16]
        print(f'  {i:2d}. {t["d"]:>5} 入:{e} ${t["ep"]:,.0f}  出:{x} ${t["xp"]:,.0f}  {t["xt"]} {t["pnl"]:+.2f}%')

print(f'\n耗时: {(time.time()-t0)/60:.1f}分')
