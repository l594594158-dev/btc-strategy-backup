#!/usr/bin/env python3
"""v4.1 现货 1s 精度回测 (全量现货K线, 每5m bar内逐秒检查)"""
import pandas as pd, numpy as np, ta, time, sys

t0 = time.time()
TP = 0.025; SL = 0.015; FEE = 0.0008
STRIDE = 2  # 2秒轮询模拟

print('=== 加载现货1s数据 ===')
df_sp = pd.read_pickle('backtest_data/btc_1s_1y.pkl')
close_1s = df_sp['close'].astype(float).values
ts_1s_all = df_sp.index
n1s = len(close_1s)
print(f'{n1s:,} 行 | {ts_1s_all[0]} ~ {ts_1s_all[-1]}')

# 截止: 最近3个月
cut_3m = pd.Timestamp('2026-02-23')

# 构建多周期K线
print('\n=== 构建现货K线 (5m/1h/4h/1d) ===')
g5 = df_sp.resample('5min', label='right', closed='right')
df5 = g5.agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna()
g1h = df_sp.resample('1h', label='right', closed='right')
df1h = g1h.agg({'open':'first','high':'max','low':'min','close':'last'}).dropna()
g4h = df_sp.resample('4h', label='right', closed='right')
df4h = g4h.agg({'open':'first','high':'max','low':'min','close':'last'}).dropna()
g1d = df_sp.resample('D', label='right', closed='right')
df1d = g1d.agg({'open':'first','high':'max','low':'min','close':'last'}).dropna()

c5 = df5['close'].astype(float).values; v5 = df5['volume'].astype(float).values; ts5 = df5.index; n5 = len(c5)
o5 = df5['open'].astype(float).values; h5 = df5['high'].astype(float).values; l5 = df5['low'].astype(float).values
c1h = df1h['close'].astype(float).values; h1h = df1h['high'].astype(float).values; l1h = df1h['low'].astype(float).values; ts1h = df1h.index
c4h = df4h['close'].astype(float).values; h4h = df4h['high'].astype(float).values; l4h = df4h['low'].astype(float).values; ts4h = df4h.index
c1d = df1d['close'].astype(float).values; h1d = df1d['high'].astype(float).values; l1d = df1d['low'].astype(float).values; ts1d = df1d.index
print(f'5m:{n5} 1h:{len(c1h)} 4h:{len(c4h)} 1d:{len(c1d)}')

print('\n=== 预计算大周期指标 (全部用现货K线) ===')
def calc_tf(cv, hv, lv, name):
    n = len(cv)
    cc = np.full(n, np.nan); sc = np.full(n, np.nan); ac = np.full(n, np.nan)
    sf = ta.trend.SMAIndicator(pd.Series(cv), 20).sma_indicator().values
    try:
        af = ta.trend.ADXIndicator(pd.Series(hv), pd.Series(lv), pd.Series(cv), 14).adx().values
    except:
        af = np.full(n, 25.0)
    valid = 0
    for i in range(20, n):
        cc[i] = cv[i]
        sc[i] = sf[i] if not np.isnan(sf[i]) else np.nan
        ac[i] = af[i] if not np.isnan(af[i]) else 25.0
        if not np.isnan(sc[i]) and not np.isnan(ac[i]):
            valid += 1
    print(f'  {name}: {valid}/{n} 根有效')
    return cc, sc, ac

c4hc, s4hc, a4hc = calc_tf(c4h, h4h, l4h, '4h')
c1dc, s1dc, _ = calc_tf(c1d, h1d, l1d, '1d')
_, _, a1hc = calc_tf(c1h, h1h, l1h, '1h')

# 映射大周期到5m
def map_to_5m(st, vv):
    r = np.full(n5, np.nan)
    for i in range(1, n5):
        p = np.searchsorted(st, ts5[i], side='right') - 1
        if 0 <= p < len(vv):
            r[i] = vv[p]
    return r

c4h5 = map_to_5m(ts4h, c4hc)
s4h5 = map_to_5m(ts4h, s4hc)
a4h5 = map_to_5m(ts4h, a4hc)
c1d5 = map_to_5m(ts1d, c1dc)
s1d5 = map_to_5m(ts1d, s1dc)
a1h5 = map_to_5m(ts1h, a1hc)

# 5m Wilder RSI 基准
ag5 = np.full(n5, np.nan)
al5 = np.full(n5, np.nan)
for i in range(15, n5):
    d = c5[i-1] - c5[i-2]
    if i == 15:
        ag5[i] = np.mean([max(c5[j]-c5[j-1],0) for j in range(1,15)])
        al5[i] = np.mean([max(c5[j-1]-c5[j],0) for j in range(1,15)])
    else:
        ag5[i] = (ag5[i-1]*13 + max(d,0)) / 14
        al5[i] = (al5[i-1]*13 + max(-d,0)) / 14

# 成交量比率
vr5 = np.full(n5, 1.0)
for i in range(21, n5):
    a = v5[i-20:i].mean()
    vr5[i] = v5[i-1] / a if a > 0 else 1.0

# 确定回测起始
I5S = max(30, np.searchsorted(ts5, cut_3m))
print(f'\n=== 1s精度回测 (STRIDE={STRIDE}s, 区间 {cut_3m.date()}~{ts5[-1].date()}) ===')

lp = None; sp = None
lls = False; lss = False
trades = []

# 1s数据中每根5m bar的起止位置
# 预计算每个5m bar在1s数组中的范围
bar_1s_starts = np.full(n5, -1, dtype=int)
bar_1s_ends = np.full(n5, -1, dtype=int)
p5 = 0
for i in range(n1s):
    while p5 < n5 - 1 and ts5[p5 + 1] <= ts_1s_all[i]:
        p5 += 1
    if bar_1s_starts[p5] < 0:
        bar_1s_starts[p5] = i
    bar_1s_ends[p5] = i

# 回测主循环
last_report = 0
for i5 in range(I5S, n5):
    if i5 - I5S > last_report + 5000:
        last_report = i5 - I5S
        elapsed = time.time() - t0
        print(f'  [{i5-I5S}/{n5-I5S}] {len(trades)}笔 {elapsed:.0f}s', flush=True)

    # 检查大周期指标有效性
    if np.isnan(c4h5[i5]) or np.isnan(s4h5[i5]) or np.isnan(a4h5[i5]):
        continue
    if np.isnan(c1d5[i5]) or np.isnan(s1d5[i5]) or np.isnan(a1h5[i5]):
        continue

    hb = c4h5[i5] > s4h5[i5]   # 4h多头
    db = c1d5[i5] > s1d5[i5]   # 1d多头
    a1v = a1h5[i5]             # 1h ADX
    a4v = a4h5[i5]             # 4h ADX
    vr = vr5[i5]               # 成交量比率

    # 获取该5m bar内的1s ticks
    s_start = bar_1s_starts[i5]
    s_end = bar_1s_ends[i5]
    if s_start < 0 or s_end < 0:
        continue

    # 跳过首秒 (模拟第0秒不做信号判定, 对应STRIDE跳过首tick)
    first_tick_idx = s_start  # 该bar第一个tick，用于跳过
    first_tick_done = False

    # 对bar内每个tick (按STRIDE步进)
    for t_idx in range(s_start, s_end + 1, STRIDE):
        px = close_1s[t_idx]
        pxt = ts_1s_all[t_idx]

        # === 出场检查 ===
        if lp is not None:
            pnl = (px - lp['ep']) / lp['ep'] * 100
            if pnl <= -SL * 100:
                trades.append({'d':'LONG','ets':str(lp['ts']),'ep':lp['ep'],
                    'xts':str(pxt),'xp':px,'xt':'SL','pnl':pnl-FEE*2*100})
                lp = None; lls = False
            elif pnl >= TP * 100:
                trades.append({'d':'LONG','ets':str(lp['ts']),'ep':lp['ep'],
                    'xts':str(pxt),'xp':px,'xt':'TP','pnl':pnl-FEE*2*100})
                lp = None; lls = False

        if sp is not None:
            pnl = (sp['ep'] - px) / sp['ep'] * 100
            if pnl <= -SL * 100:
                trades.append({'d':'SHORT','ets':str(sp['ts']),'ep':sp['ep'],
                    'xts':str(pxt),'xp':px,'xt':'SL','pnl':pnl-FEE*2*100})
                sp = None; lss = False
            elif pnl >= TP * 100:
                trades.append({'d':'SHORT','ets':str(sp['ts']),'ep':sp['ep'],
                    'xts':str(pxt),'xp':px,'xt':'TP','pnl':pnl-FEE*2*100})
                sp = None; lss = False

        # === 跳过首tick (不做开仓信号) ===
        if not first_tick_done:
            first_tick_done = True
            continue

        # === 七条件短路 ===
        if a1v <= 25: continue
        if a4v >= 40: continue

        # SMA20动态 = (前19根5m收盘 + 当前价) / 20
        s19 = c5[max(0,i5-19):i5].sum()
        sd = (s19 + px) / 20
        if abs((px - sd) / sd * 100) > 1.0:
            continue

        if vr < 1.0: continue

        # 动态RSI (基于5m已关闭bar的gain/loss + 当前bar增量)
        if np.isnan(ag5[i5]) or np.isnan(al5[i5]):
            continue
        # 当前bar的delta
        bar_delta = px - o5[i5]
        cg = (ag5[i5] * 13 + max(bar_delta, 0)) / 14
        cl = (al5[i5] * 13 + max(-bar_delta, 0)) / 14
        if cl <= 0: continue
        rsi = 100 - 100 / (1 + cg / cl)

        # === LONG信号 (edge detection) ===
        ln = hb and db and rsi > 40
        if ln and not lls and lp is None and sp is None:
            lp = {'ep': px, 'ts': pxt}
        lls = ln

        # === SHORT信号 (edge detection) ===
        sn = (not hb) and (not db) and rsi < 60
        if sn and not lss and sp is None and lp is None:
            sp = {'ep': px, 'ts': pxt}
        lss = sn

df_t = pd.DataFrame(trades)
print(f'\n全部 {len(df_t)} 笔')

# 筛选近3月
df_t['ets_dt'] = pd.to_datetime(df_t['ets'])
R = df_t[df_t['ets_dt'] >= cut_3m].copy()

wins = (R['pnl'] > 0).sum()
wr = wins / len(R) * 100
tp_c = (R['xt'] == 'TP').sum()
sl_c = (R['xt'] == 'SL').sum()
nL = len(R[R['d'] == 'LONG']); nS = len(R[R['d'] == 'SHORT'])
pnl_sum = R['pnl'].sum()

print(f'\n============ v4.1 现货1s精度 近3月回测 ============')
print(f'区间: {cut_3m.date()} ~ 2026-05-22')
print(f'策略: {len(R)}笔 | WR={wr:.1f}% | PnL={pnl_sum:+.1f}%')
print(f'LONG:{nL} SHORT:{nS} | TP:{tp_c} SL:{sl_c}')
if nL > 0:
    ldf = R[R['d'] == 'LONG']
    print(f'LONG: WR={(ldf["pnl"]>0).sum()/nL*100:.0f}% PnL={ldf["pnl"].sum():+.1f}%')
if nS > 0:
    sdf = R[R['d'] == 'SHORT']
    print(f'SHORT: WR={(sdf["pnl"]>0).sum()/nS*100:.0f}% PnL={sdf["pnl"].sum():+.1f}%')

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
