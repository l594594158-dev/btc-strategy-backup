#!/usr/bin/env python3
"""v4.1 回测 - 合约1m数据版 (就近复刻)"""
import pandas as pd, numpy as np, ta, time

print('加载合约1m数据...')
df_1m = pd.read_pickle('backtest_data/btc_1m_fut_6m.pkl')
df_1m.columns = ['open','high','low','close','volume']
for c in df_1m.columns:
    df_1m[c] = df_1m[c].astype(float)
ts_1m = df_1m.index
close_1m = df_1m['close'].values
print(f'{len(df_1m):,} rows | {ts_1m[0]} ~ {ts_1m[-1]}')

print('构建K线...')
g5 = df_1m.resample('5min', label='right', closed='right')
df5 = g5.agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna()
g1h = df_1m.resample('1h', label='right', closed='right')
df1h = g1h.agg({'open':'first','high':'max','low':'min','close':'last'}).dropna()
g4h = df_1m.resample('4h', label='right', closed='right')
df4h = g4h.agg({'open':'first','high':'max','low':'min','close':'last'}).dropna()
g1d = df_1m.resample('1D', label='right', closed='right')
df1d = g1d.agg({'open':'first','high':'max','low':'min','close':'last'}).dropna()

c5 = df5['close'].astype(float).values; v5 = df5['volume'].astype(float).values; ts5 = df5.index
c4h = df4h['close'].astype(float).values; h4h = df4h['high'].astype(float).values; l4h = df4h['low'].astype(float).values; ts4h = df4h.index
c1d = df1d['close'].astype(float).values; h1d = df1d['high'].astype(float).values; l1d = df1d['low'].astype(float).values; ts1d = df1d.index
c1h = df1h['close'].astype(float).values; h1h = df1h['high'].astype(float).values; l1h = df1h['low'].astype(float).values; ts1h = df1h.index
n5 = len(c5)

print(f'5m:{n5}根 1h:{len(c1h)}根 4h:{len(c4h)}根 1d:{len(c1d)}根')

print('预计算指标...')
def build_tf(c, h, l):
    n = len(c)
    cc = np.full(n, np.nan); sc = np.full(n, np.nan); ac = np.full(n, np.nan)
    sf = ta.trend.SMAIndicator(pd.Series(c), 20).sma_indicator().values
    try: af = ta.trend.ADXIndicator(pd.Series(h), pd.Series(l), pd.Series(c), 14).adx().values
    except: af = np.full(n, 25.0)
    for i in range(20, n):
        cc[i] = c[i]; sc[i] = sf[i] if not np.isnan(sf[i]) else np.nan
        ac[i] = af[i] if not np.isnan(af[i]) else 25.0
    return cc, sc, ac

c4hc, s4hc, a4hc = build_tf(c4h, h4h, l4h)
c1dc, s1dc, _ = build_tf(c1d, h1d, l1d)
_, _, a1hc = build_tf(c1h, h1h, l1h)

def map_to_5m(src_ts, src_vals):
    r = np.full(n5, np.nan)
    for i in range(1, n5):
        t = ts5[i]; p = np.searchsorted(src_ts, t, side='right') - 1
        if 0 <= p < len(src_vals): r[i] = src_vals[p]
    return r

c4h5 = map_to_5m(ts4h, c4hc); s4h5 = map_to_5m(ts4h, s4hc); a4h5 = map_to_5m(ts4h, a4hc)
c1d5 = map_to_5m(ts1d, c1dc); s1d5 = map_to_5m(ts1d, s1dc)
a1h5 = map_to_5m(ts1h, a1hc)

# RSI avg_gain/loss
ag5 = np.full(n5, np.nan); al5 = np.full(n5, np.nan)
for i in range(15, n5):
    d = c5[i-1] - c5[i-2]
    if i == 15:
        ag5[i] = np.mean([max(c5[j]-c5[j-1],0) for j in range(1,15)])
        al5[i] = np.mean([max(c5[j-1]-c5[j],0) for j in range(1,15)])
    else:
        ag5[i] = (ag5[i-1]*13 + max(d,0))/14
        al5[i] = (al5[i-1]*13 + max(-d,0))/14

# 成交量比率
vol_r5 = np.full(n5, 1.0)
for i in range(21, n5):
    avg = v5[i-20:i].mean()
    vol_r5[i] = v5[i-1] / avg if avg > 0 else 1.0

SL = 0.015; TP = 0.025; FEE = 0.0008
I5_START = 5000  # skip first 5k 5m bars = ~17 days warmup (合约1m数据只有6个月)

print(f'回测 {n5-I5_START} 根5m bar...')

long_pos = None; short_pos = None
last_ls = False; last_ss = False
trades = []
freeze_ok = 0; sig_l = 0; sig_s = 0

t0 = time.time()
for i5 in range(I5_START, n5):
    if i5 % 10000 == 0:
        elapsed = (time.time()-t0)/60
        print(f'  [{i5}/{n5}] {elapsed:.1f}min | trades:{len(trades)}')
    
    # 冻结指标
    if np.isnan(c4h5[i5]) or np.isnan(s4h5[i5]) or np.isnan(a4h5[i5]): continue
    if np.isnan(c1d5[i5]) or np.isnan(s1d5[i5]): continue
    if np.isnan(a1h5[i5]) or np.isnan(a4h5[i5]): continue
    freeze_ok += 1
    
    h4_bull = c4h5[i5] > s4h5[i5]
    d1_bull = c1d5[i5] > s1d5[i5]
    adx1h_v = a1h5[i5]; adx4h_v = a4h5[i5]
    vol_r = vol_r5[i5]
    
    # 遍历5m bar内的1m点
    t_start = ts5[i5] - pd.Timedelta(minutes=5)
    t_end = ts5[i5]
    s0 = np.searchsorted(ts_1m, t_start)
    se = np.searchsorted(ts_1m, t_end)
    if se > s0: se -= 1
    
    for js in range(s0, min(se+1, len(close_1m))):
        px = close_1m[js]
        px_ts = ts_1m[js]
        
        # 出场
        if long_pos is not None:
            pnl = (px - long_pos['ep']) / long_pos['ep'] * 100
            if pnl <= -SL*100:
                trades.append({'d':'LONG','ets':long_pos['ts'],'ep':long_pos['ep'],
                              'xts':px_ts,'xp':px,'xt':'SL','pnl':pnl-FEE*2*100})
                long_pos = None; last_ls = False
            elif pnl >= TP*100:
                trades.append({'d':'LONG','ets':long_pos['ts'],'ep':long_pos['ep'],
                              'xts':px_ts,'xp':px,'xt':'TP','pnl':pnl-FEE*2*100})
                long_pos = None; last_ls = False
        
        if short_pos is not None:
            pnl = (short_pos['ep'] - px) / short_pos['ep'] * 100
            if pnl <= -SL*100:
                trades.append({'d':'SHORT','ets':short_pos['ts'],'ep':short_pos['ep'],
                              'xts':px_ts,'xp':px,'xt':'SL','pnl':pnl-FEE*2*100})
                short_pos = None; last_ss = False
            elif pnl >= TP*100:
                trades.append({'d':'SHORT','ets':short_pos['ts'],'ep':short_pos['ep'],
                              'xts':px_ts,'xp':px,'xt':'TP','pnl':pnl-FEE*2*100})
                short_pos = None; last_ss = False
        
        # 入场: 仅检查除首点外的所有点
        
        # 短路判断
        if adx1h_v <= 25: continue
        if adx4h_v >= 40: continue
        
        # SMA范围
        sum19 = c5[max(0,i5-19):i5].sum()
        sma_dyn = (sum19 + px) / 20
        pct = (px - sma_dyn) / sma_dyn * 100
        if abs(pct) > 1.0: continue
        
        # 量
        if vol_r < 1.0: continue
        
        # RSI动态
        if np.isnan(ag5[i5]) or np.isnan(al5[i5]): continue
        diff = px - c5[i5-1]
        cg = (ag5[i5]*13 + max(diff,0))/14
        cl = (al5[i5]*13 + max(-diff,0))/14
        if cl <= 0: continue
        rsi = 100 - 100/(1+cg/cl)
        
        # LONG
        ls_now = h4_bull and d1_bull and rsi > 40
        if ls_now and not last_ls and long_pos is None:
            long_pos = {'ep': px, 'ts': px_ts}; sig_l += 1
        last_ls = ls_now
        
        # SHORT
        ss_now = (not h4_bull) and (not d1_bull) and rsi < 60
        if ss_now and not last_ss and short_pos is None:
            short_pos = {'ep': px, 'ts': px_ts}; sig_s += 1
        last_ss = ss_now

# 结果
df_t = pd.DataFrame(trades)
print(f'\n=== 合约1m数据回测结果 ===')
print(f'冻结OK:{freeze_ok} | L信号:{sig_l} | S信号:{sig_s}')
if len(df_t):
    wins = (df_t['pnl']>0).sum(); wr = wins/len(df_t)*100
    tp_c = (df_t['xt']=='TP').sum(); sl_c = (df_t['xt']=='SL').sum()
    nL = (df_t['d']=='LONG').sum(); nS = (df_t['d']=='SHORT').sum()
    print(f'总交易:{len(df_t)} | L:{nL} S:{nS} | 胜率:{wr:.1f}% ({wins}W/{len(df_t)-wins}L)')
    total_pnl = df_t['pnl'].sum()
    print(f'总盈亏:{total_pnl:.1f}% | 均:{total_pnl/len(df_t):.2f}%')
    print(f'TP:{tp_c} SL:{sl_c}')
    if nL:
        ldf = df_t[df_t['d']=='LONG']
        print(f'LONG: {nL}笔 WR={(ldf["pnl"]>0).sum()/nL*100:.1f}% PnL={ldf["pnl"].sum():.1f}%')
    if nS:
        sdf = df_t[df_t['d']=='SHORT']
        print(f'SHORT: {nS}笔 WR={(sdf["pnl"]>0).sum()/nS*100:.1f}% PnL={sdf["pnl"].sum():.1f}%')
    
    # 月度
    df_t['ets_dt'] = pd.to_datetime(df_t['ets'])
    df_t['ym'] = df_t['ets_dt'].dt.strftime('%Y-%m')
    print('\n月度:')
    for m in sorted(df_t['ym'].unique()):
        sub = df_t[df_t['ym']==m]
        w = (sub['pnl']>0).sum()
        wr_m = w/len(sub)*100
        pnl_m = sub['pnl'].sum()
        print(f'  {m}: {len(sub)}笔 WR={wr_m:.0f}% PnL={pnl_m:+.1f}%')
    
    df_t.drop(columns=['ets_dt','ym'], inplace=True)
    df_t.to_csv('backtest_data/backtest_v41_fut_1m.csv', index=False)

print(f'\n耗时: {(time.time()-t0)/60:.1f}min')
