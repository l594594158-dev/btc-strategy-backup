#!/usr/bin/env python3
"""
v4.1 实盘复刻回测 — 1:1 对齐 auto_trade.py
· 5m指标: 动态SMA20/RSI (含当前K线) + 闭K成交量
· 1h指标: 闭K ADX  
· 4h指标: 闭K close/sma/adx
· 1d指标: 闭K close/sma
· 七条件短路判断 (顺序: 1hADX→4hADX→SMA范围→量→方向+RSI)
· 信号边缘检测 (信号消失→重新出现才开仓)
· 双向独立/同向单仓
· 主动出场: pnl ≤ -1.5% 止损 / ≥ +2.5% 止盈
· 价格百分比pnl (非杠杆)
"""
import pandas as pd
import numpy as np
import ta
import time
from datetime import datetime

t0 = time.time()
STRIDE = 1  # 逐秒扫描 (保证偶数秒对齐)
MONTHS = 6   # 回测最近多少个月

# ========== 参数 (严格对齐 auto_trade.py) ==========
TP_PCT = 2.5 / 100   # +2.5%
SL_PCT = 1.5 / 100   # -1.5%
FEE    = 0.08 / 100  # 0.08% taker (单边)

# ========== 加载 ==========
print("⏳ 加载1s数据...")
df_full = pd.read_pickle('/root/.openclaw/workspace/btc-strategy-task/backtest_data/btc_1s_1y.pkl')
# 用全量数据算指标（保证warmup充足），只扫描近6个月
CUT_START = df_full.index[-1] - pd.Timedelta(days=MONTHS*30.5)
df_1s = df_full[df_full.index >= CUT_START]
price_1s = df_1s['close'].values.astype(float)
ts_1s    = df_1s.index
n_1s     = len(df_1s)
print(f"✅ 指标: 全量 {len(df_full):,}行 | 回测: {MONTHS}个月 {n_1s:,}行")
print(f"   区间: {df_1s.index[0]} ~ {df_1s.index[-1]}")

# ========== 构建多周期K线 (全量数据, 保证指标warmup) ==========
print("⏳ 构建K线(全量)...")
for tf_name, rule in [('5m','5min'),('1h','1h'),('4h','4h'),('1d','1D')]:
    g = df_full.resample(rule, label='right', closed='right')
    df_tf = g.agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna()
    globals()[f'df_{tf_name}'] = df_tf
    globals()[f'c_{tf_name}']  = df_tf['close'].astype(float).values
    globals()[f'h_{tf_name}']  = df_tf['high'].astype(float).values
    globals()[f'l_{tf_name}']  = df_tf['low'].astype(float).values
    globals()[f'v_{tf_name}']  = df_tf['volume'].astype(float).values
    globals()[f'ts_{tf_name}'] = df_tf.index
    globals()[f'n_{tf_name}']  = len(df_tf)
    print(f"  {tf_name}: {len(df_tf)} 根")
n5 = len(c_5m)

# ========== 1s → 5m bar 映射 ==========
print("⏳ 构建1s→5m映射...")
# 找到6个月窗口对应的5m bar起止索引
I5_START = max(30, np.searchsorted(ts_5m, ts_1s[0]))
print(f"   5m bar范围: {I5_START} ~ {n5-1} (跳过前{I5_START}根warmup)")
# 每个5m bar对应1s范围 (仅6个月窗口)
bar_1s_start = np.zeros(n5, dtype=int)
bar_1s_end   = np.zeros(n5, dtype=int)
for i in range(I5_START, n5):
    t_end = ts_5m[i]
    t_start = t_end - pd.Timedelta(minutes=5)
    sidx = np.searchsorted(ts_1s, t_start)
    eidx = np.searchsorted(ts_1s, t_end)
    bar_1s_start[i] = max(0, sidx)
    bar_1s_end[i]   = min(n_1s, eidx)

# ========== 预计算闭K指标 (不做动态) ==========
print("⏳ 预计算指标...")

# 5m 闭K: SMA, RSI, 量比 (向量化)
sma5_full   = ta.trend.SMAIndicator(pd.Series(c_5m), 20).sma_indicator().values
rsi5_full   = ta.momentum.RSIIndicator(pd.Series(c_5m), 14).rsi().values
sma5_closed_all = np.full(n5, np.nan)
rsi5_closed_all = np.full(n5, np.nan)
for i in range(20, n5):
    sma5_closed_all[i] = sma5_full[i-1]  # 闭K SMA
    rsi5_closed_all[i] = rsi5_full[i-1]  # 闭K RSI

vol_ratio_5m = np.full(n5, np.nan)
for i in range(21, n5):  # 需要20根闭K均量
    avg_v = v_5m[i-20:i].mean()  # 20根闭K
    vol_ratio_5m[i] = v_5m[i-1] / avg_v if avg_v > 0 else 1.0

# 闭K AVG gain/loss (用于动态RSI增量更新)
avg_gain_5m = np.full(n5, np.nan)
avg_loss_5m = np.full(n5, np.nan)
for i in range(15, n5):
    diff = c_5m[i-1] - c_5m[i-2]
    if i == 15:
        # 初始化: 前14组的均值
        gains = [max(c_5m[j] - c_5m[j-1], 0) for j in range(1, 15)]
        losses = [max(c_5m[j-1] - c_5m[j], 0) for j in range(1, 15)]
        avg_gain_5m[i] = np.mean(gains)
        avg_loss_5m[i] = np.mean(losses)
    else:
        gain = max(diff, 0)
        loss = max(-diff, 0)
        avg_gain_5m[i] = (avg_gain_5m[i-1] * 13 + gain) / 14
        avg_loss_5m[i] = (avg_loss_5m[i-1] * 13 + loss) / 14

# 1h/4h/1d 闭K指标 (向量化, O(n))
def build_tf_indicators(c, h, l):
    n = len(c)
    c_closed = np.full(n, np.nan)
    sma_closed = np.full(n, np.nan)
    adx_closed = np.full(n, np.nan)
    sma_full = ta.trend.SMAIndicator(pd.Series(c), 20).sma_indicator().values
    try:
        adx_full = ta.trend.ADXIndicator(pd.Series(h), pd.Series(l), pd.Series(c), 14).adx().values
    except:
        adx_full = np.full(n, 25.0)
    for i in range(20, n):
        c_closed[i] = c[i]
        sma_closed[i] = sma_full[i] if not np.isnan(sma_full[i]) else np.nan
        adx_closed[i] = adx_full[i] if not np.isnan(adx_full[i]) else 25.0
    return c_closed, sma_closed, adx_closed

c4h_closed, sma4h_closed, adx4h_closed = build_tf_indicators(c_4h, h_4h, l_4h)
c1d_closed, sma1d_closed, _            = build_tf_indicators(c_1d, h_1d, l_1d)
_, _, adx1h_closed = build_tf_indicators(c_1h, h_1h, l_1h)

# 前向填充到5m
def map_to_5m(src_ts, src_vals):
    result = np.full(n5, np.nan)
    for i5 in range(1, n5):
        t = ts_5m[i5]  # 当前5m bar的结束时间 (此时的上周期闭K刚关)
        pos = np.searchsorted(src_ts, t, side='right') - 1
        if pos >= 0 and pos < len(src_vals):
            result[i5] = src_vals[pos]
    return result

adx1h_5m = map_to_5m(ts_1h, adx1h_closed)
adx4h_5m = map_to_5m(ts_4h, adx4h_closed)
c4h_5m   = map_to_5m(ts_4h, c4h_closed)
sma4h_5m = map_to_5m(ts_4h, sma4h_closed)
c1d_5m   = map_to_5m(ts_1d, c1d_closed)
sma1d_5m = map_to_5m(ts_1d, sma1d_closed)

# 5m闭K SMA20前19根之和 (用于动态SMA20: (sum19 + px) / 20)
sum19 = np.full(n5, np.nan)
for i in range(20, n5):
    sum19[i] = c_5m[i-19:i].sum()

print(f"✅ 准备完成, {time.time()-t0:.1f}s")
print(f"⏳ 回测 {n5-I5_START} 根5m bar (每{STRIDE}根1s采样)...\n")

# ========== 回测主循环 ==========
trades = []
long_pos = None
short_pos = None
last_ls = False  # 上帧LONG信号
last_ss = False  # 上帧SHORT信号
l_sig_cnt = s_sig_cnt = 0
frozen_ok  = 0
last_report_t = time.time()

for i5 in range(I5_START, n5):  # 全量指标warmup + 仅扫描6个月
    # ===== 冷冻指标 (闭K, 本bar不变) =====
    adx1h_val = adx1h_5m[i5]
    adx4h_val = adx4h_5m[i5]
    c4h_val   = c4h_5m[i5]
    sma4h_val = sma4h_5m[i5]
    c1d_val   = c1d_5m[i5]
    sma1d_val = sma1d_5m[i5]
    vol_basis = vol_ratio_5m[i5]  # 闭K量比
    sum19_val = sum19[i5]
    sma5_closed_prev = sma5_closed_all[i5]  # 闭K SMA20
    
    # ===== 方向 (闭K) =====
    h4_bull = (c4h_val > sma4h_val) if not (np.isnan(c4h_val) or np.isnan(sma4h_val)) else None
    d1_bull = (c1d_val > sma1d_val) if not (np.isnan(c1d_val) or np.isnan(sma1d_val)) else None
    
    # ===== 1s扫描本bar =====
    s1 = bar_1s_start[i5]
    e1 = bar_1s_end[i5]
    
    for js in range(s1, e1, STRIDE):
        px = price_1s[js]
        
        # ─── 主动出场 (有仓位必须检查) ───
        if long_pos is not None:
            pnl = (px - long_pos['ep']) / long_pos['ep'] * 100
            exit_trigger = (pnl <= -SL_PCT*100) or (pnl >= TP_PCT*100)
            if exit_trigger:
                et = 'SL' if pnl <= -SL_PCT*100 else 'TP'
                trades.append({'d':'LONG','ets':long_pos['ts'],'ep':long_pos['ep'],
                              'xts':ts_1s[js],'xp':px,'xt':et,
                              'pnl':pnl - FEE*2*100})
                long_pos = None; last_ls = False
        
        if short_pos is not None:
            pnl = (short_pos['ep'] - px) / short_pos['ep'] * 100
            exit_trigger = (pnl <= -SL_PCT*100) or (pnl >= TP_PCT*100)
            if exit_trigger:
                et = 'SL' if pnl <= -SL_PCT*100 else 'TP'
                trades.append({'d':'SHORT','ets':short_pos['ts'],'ep':short_pos['ep'],
                              'xts':ts_1s[js],'xp':px,'xt':et,
                              'pnl':pnl - FEE*2*100})
                short_pos = None; last_ss = False
        
        # 入场仅在偶数秒检查 (模拟2s轮询对齐)
        # 出场不受限制 (任何秒都可触发SL/TP)
        if int(ts_1s[js].timestamp()) % 2 != 0:
            continue
        
        # ─── 短路判断 (七条件, 顺序严格) ───
        if np.isnan(adx1h_val) or np.isnan(adx4h_val):
            last_ls = last_ss = False; continue
        
        # ① 1h ADX ≤ 25
        if adx1h_val <= 25:
            last_ls = last_ss = False; continue
        
        # ② 4h ADX ≥ 40
        if adx4h_val >= 40:
            last_ls = last_ss = False; continue
        
        # ③ 动态SMA20 ±1%
        if np.isnan(sum19_val) or np.isnan(sma5_closed_prev):
            continue
        sma_dyn = (sum19_val + px) / 20
        pct_sma = (px - sma_dyn) / sma_dyn * 100
        if abs(pct_sma) > 1.0:
            last_ls = last_ss = False; continue
        
        # ④ 缩量
        if np.isnan(vol_basis) or vol_basis < 1.0:
            last_ls = last_ss = False; continue
        
        # ⑤ 方向不一致
        if h4_bull is None or d1_bull is None:
            continue
        if h4_bull != d1_bull:
            last_ls = last_ss = False; continue
        
        frozen_ok += 1
        
        # ⑥ 动态RSI (增量Wilder)
        if i5 < 15 or np.isnan(avg_gain_5m[i5]) or np.isnan(avg_loss_5m[i5]):
            continue
        
        prev_close = c_5m[i5-1]
        diff = px - prev_close
        gain = max(diff, 0)
        loss = max(-diff, 0)
        cur_avg_gain = (avg_gain_5m[i5] * 13 + gain) / 14
        cur_avg_loss = (avg_loss_5m[i5] * 13 + loss) / 14
        if cur_avg_loss == 0:
            rsi_now = 100.0
        else:
            rs = cur_avg_gain / cur_avg_loss
            rsi_now = 100 - 100/(1 + rs)
        
        # ─── 信号 (严格对齐 check_entry) ───
        ls_now = h4_bull and d1_bull and rsi_now > 40
        ss_now = (not h4_bull) and (not d1_bull) and rsi_now < 60
        
        # ─── 入场 (边缘检测: 信号消失→重新出现才开) ───
        if ls_now and not last_ls and long_pos is None:
            l_sig_cnt += 1
            long_pos = {'ep': px, 'ts': ts_1s[js]}
        
        if ss_now and not last_ss and short_pos is None:
            s_sig_cnt += 1
            short_pos = {'ep': px, 'ts': ts_1s[js]}
        
        last_ls = ls_now
        last_ss = ss_now
    
    # 进度报告
    now = time.time()
    if now - last_report_t > 15 or i5 == n5 - 1:
        el = (now - t0)/60
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {i5}/{n5} ({100*i5/n5:.0f}%) | "
              f"冻结OK:{frozen_ok} | L信号:{l_sig_cnt} | S信号:{s_sig_cnt} | 交易:{len(trades)} | {el:.1f}分")
        last_report_t = now

# 强制平仓
for pos, nm in [(long_pos,'LONG'),(short_pos,'SHORT')]:
    if pos:
        last_px = price_1s[-1]
        pnl = (last_px-pos['ep'])/pos['ep']*100 if nm=='LONG' else (pos['ep']-last_px)/pos['ep']*100
        trades.append({'d':nm,'ets':pos['ts'],'ep':pos['ep'],'xts':ts_1s[-1],
                       'xp':last_px,'xt':'EOD','pnl':pnl-FEE*2*100})

# ========== 汇总 ==========
elapsed = (time.time()-t0)/60
print(f"\n{'='*60}")
print(f"✅ 回测完成! {elapsed:.1f}分 | 实盘复刻逻辑")
print(f"{'='*60}")

if not trades:
    print("⚠️ 无交易")
    exit()

df_t = pd.DataFrame(trades)
print(f"\n总交易: {len(df_t)}  |  LONG {len(df_t[df_t['d']=='LONG'])} | SHORT {len(df_t[df_t['d']=='SHORT'])}")

win = df_t[df_t['pnl']>0]; loss = df_t[df_t['pnl']<=0]
print(f"胜率: {len(win)/len(df_t)*100:.1f}% ({len(win)}W/{len(loss)}L)")
print(f"总盈亏: {df_t['pnl'].sum():+.1f}% | 均盈亏: {df_t['pnl'].mean():+.2f}%")
print(f"最大盈: {df_t['pnl'].max():+.2f}% | 最大亏: {df_t['pnl'].min():+.2f}%")

for d in ['LONG','SHORT']:
    sub = df_t[df_t['d']==d]
    if len(sub):
        w = (sub['pnl']>0).sum()
        print(f"{d}: {len(sub)}笔 胜率{w/len(sub)*100:.1f}% 总盈亏{sub['pnl'].sum():+.1f}%")

print(f"\n出场: TP={len(df_t[df_t['xt']=='TP'])} SL={len(df_t[df_t['xt']=='SL'])} EOD={len(df_t[df_t['xt']=='EOD'])}")
c=0; mc=0
for t in df_t['pnl']:
    if t<=0: c+=1; mc=max(mc,c)
    else: c=0
print(f"最大连亏: {mc}")

out = '/root/.openclaw/workspace/btc-strategy-task/backtest_data/backtest_v41_1s.csv'
df_t.to_csv(out, index=False)
print(f"💾 {out}")
