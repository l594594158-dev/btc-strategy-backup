#!/usr/bin/env python3
"""
BTC v4.0 趋势回调策略 · 历史回测
=================================
- 数据: 币安 BTC/USDT 永续合约 (2022.06 ~ 2026.05)
- 多周期: 5m(入场) / 1h(ADX) / 4h(SMA20+ADX) / 1d(SMA20)
- LONG顺势追多 + SHORT顺势摸顶，双向可同时各持1仓
- TP +2.5% / SL -1.5% / 杠杆95x / 每仓0.01 BTC

用法:
  python3 backtest_v4.py          # 运行回测（数据已缓存则跳过下载）
  python3 backtest_v4.py --fresh  # 强制重新下载数据
  python3 backtest_v4.py --report # 仅输出报告（已有数据）
"""

import ccxt
import pandas as pd
import numpy as np
import ta
import sys
import os
import time
import pickle
from datetime import datetime, timedelta

# ==================== 策略参数 ====================
SYMBOL = 'BTC/USDT:USDT'
QTY = 0.01          # 每仓 0.01 BTC
LEVERAGE = 95
TP_PCT = 2.5 / 100  # 止盈 +2.5%
SL_PCT = 1.5 / 100  # 止损 -1.5%
FEE = 0.04 / 100    # 币安手续费 0.04% (maker/taker 取保守)

# 多周期配置
TF_ENTRY = '5m'     # 入场信号周期
DATA_DIR = '/root/.openclaw/workspace/btc-strategy-task/backtest_data'
START_DATE = '2022-06-01'
END_DATE   = '2026-05-20'

# ==================== 数据下载 ====================
os.makedirs(DATA_DIR, exist_ok=True)

def download_tf(exchange, tf, since_ms, limit=1000):
    """下载单个周期的全部数据（币安单次上限1000根）"""
    all_klines = []
    current_since = since_ms
    tf_ms = exchange.parse_timeframe(tf) * 1000
    end_ms = int(datetime.now().timestamp() * 1000)
    
    while current_since < end_ms:
        try:
            klines = exchange.fetch_ohlcv(SYMBOL, tf, since=current_since, limit=limit)
            if not klines:
                break
            all_klines.extend(klines)
            last_ts = klines[-1][0]
            print(f"  {tf}: 已下载 {len(all_klines)} 根K线, "
                  f"最新 {datetime.fromtimestamp(last_ts/1000).strftime('%Y-%m-%d %H:%M')}")
            if len(klines) < limit or last_ts >= end_ms:
                break
            current_since = last_ts + tf_ms
            time.sleep(0.3)
        except Exception as e:
            print(f"  ⚠️ 下载{tf}出错: {e}, 等待5秒重试...")
            time.sleep(5)
    return all_klines

def load_or_download(fresh=False):
    """加载缓存数据或重新下载"""
    cache_file = os.path.join(DATA_DIR, 'btc_data_4y.pkl')
    
    if not fresh and os.path.exists(cache_file):
        print(f"📦 从缓存加载数据: {cache_file}")
        with open(cache_file, 'rb') as f:
            data = pickle.load(f)
        print(f"  5m: {len(data['5m'])} 根 | 1h: {len(data['1h'])} 根 | 4h: {len(data['4h'])} 根 | 1d: {len(data['1d'])} 根")
        return data
    
    print("📥 正在从币安下载近4年BTC数据...")
    exchange = ccxt.binance({
        'enableRateLimit': True,
        'options': {'defaultType': 'swap'}
    })
    
    since_ms = exchange.parse8601(f'{START_DATE}T00:00:00Z')
    
    data = {}
    for tf in ['5m', '1h', '4h', '1d']:
        print(f"\n下载 {tf} 周期...")
        klines = download_tf(exchange, tf, since_ms)
        df = pd.DataFrame(klines, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
        df['ts'] = pd.to_datetime(df['ts'], unit='ms')
        df.set_index('ts', inplace=True)
        # 去重
        df = df[~df.index.duplicated(keep='last')]
        data[tf] = df
        print(f"  ✅ {tf}: {len(df)} 根K线, {df.index[0]} ~ {df.index[-1]}")
    
    # 保存缓存
    with open(cache_file, 'wb') as f:
        pickle.dump(data, f)
    print(f"\n💾 数据已缓存至 {cache_file}")
    
    return data

# ==================== 指标计算 ====================
def calc_indicators(df_5m, df_1h, df_4h, df_1d):
    """计算所有所需指标并合并到5m数据框"""
    print("\n📊 计算技术指标...")
    
    # === 5m 指标 ===
    close_5m = df_5m['close'].astype(float)
    high_5m  = df_5m['high'].astype(float)
    low_5m   = df_5m['low'].astype(float)
    vol_5m   = df_5m['volume'].astype(float)
    
    df_5m['sma20_5m'] = ta.trend.SMAIndicator(close_5m, 20).sma_indicator()
    df_5m['rsi_5m']   = ta.momentum.RSIIndicator(close_5m, 14).rsi()
    
    # 价格距离5m SMA20百分比
    df_5m['pct_from_sma20_5m'] = (close_5m - df_5m['sma20_5m']) / df_5m['sma20_5m'] * 100
    
    # === 1h 指标 ===
    close_1h = df_1h['close'].astype(float)
    high_1h  = df_1h['high'].astype(float)
    low_1h   = df_1h['low'].astype(float)
    
    adx_1h = ta.trend.ADXIndicator(high_1h, low_1h, close_1h, window=14)
    df_1h['adx_1h'] = adx_1h.adx()
    df_1h['rsi_1h'] = ta.momentum.RSIIndicator(close_1h, 14).rsi()
    
    # === 4h 指标 ===
    close_4h = df_4h['close'].astype(float)
    high_4h  = df_4h['high'].astype(float)
    low_4h   = df_4h['low'].astype(float)
    
    df_4h['sma20_4h'] = ta.trend.SMAIndicator(close_4h, 20).sma_indicator()
    adx_4h = ta.trend.ADXIndicator(high_4h, low_4h, close_4h, window=14)
    df_4h['adx_4h'] = adx_4h.adx()
    
    # === 1d 指标 ===
    close_1d = df_1d['close'].astype(float)
    df_1d['sma20_1d'] = ta.trend.SMAIndicator(close_1d, 20).sma_indicator()
    
    # === 将高周期指标映射到5m ===
    # 使用 forward fill: 用前一根已完成的高周期K线指标值
    for col, src_df in [('adx_1h', df_1h), ('rsi_1h', df_1h), ('sma20_4h', df_4h), ('adx_4h', df_4h), ('sma20_1d', df_1d)]:
        # 重新索引到5m时间轴，用前值填充
        reindexed = src_df[col].reindex(df_5m.index, method='ffill')
        df_5m[col] = reindexed
    
    # 确保close_4h和close_1d用于判断也在5m上
    df_5m['close_4h'] = df_4h['close'].reindex(df_5m.index, method='ffill')
    df_5m['close_1d'] = df_1d['close'].reindex(df_5m.index, method='ffill')
    
    # 删除NaN行
    before = len(df_5m)
    df_5m.dropna(inplace=True)
    after = len(df_5m)
    print(f"  有效数据行: {after} (丢弃 {before-after} 行NaN)")
    
    return df_5m

# ==================== 回测引擎 ====================
def run_backtest(df):
    """
    用户版本回测逻辑:
    - 每根5m K线: 先用cl检查出场，再用cl判断入场
    - 入场: 信号K线当根收盘价
    - 出场: 当前K线收盘价算盈亏，>=+2.5%止盈，<=-1.5%止损
    - 同方向最多1仓，跳过重复信号
    """
    print("\n🔬 开始回测（用户版本）...")
    
    trades = []
    long_pos = None       # {'entry_price', 'entry_time'}
    short_pos = None
    long_last_signal = False
    short_last_signal = False
    
    total_rows = len(df)
    last_report_pct = 0
    
    for i in range(len(df)):
        row = df.iloc[i]
        cl = row['close']
        ts = df.index[i]
        
        pct_done = i * 100 // total_rows
        if pct_done > last_report_pct and pct_done % 10 == 0:
            print(f"  进度: {pct_done}% ({i}/{total_rows})  |  已交易 {len(trades)} 笔")
            last_report_pct = pct_done
        
        # ==== 先检查出场（cl判断触发，理论价平仓）====
        if long_pos is not None:
            pnl_pct = (cl - long_pos['entry_price']) / long_pos['entry_price'] * 100
            if pnl_pct >= 2.5:
                exit_price = long_pos['entry_price'] * 1.025  # 理论止盈价
                trades.append({
                    'direction': 'LONG',
                    'entry_time': long_pos['entry_time'],
                    'exit_time': ts,
                    'entry_price': long_pos['entry_price'],
                    'exit_price': exit_price,
                    'pnl_pct': 2.50,
                    'exit_type': 'TP',
                })
                long_pos = None
            elif pnl_pct <= -1.5:
                exit_price = long_pos['entry_price'] * 0.985  # 理论止损价
                trades.append({
                    'direction': 'LONG',
                    'entry_time': long_pos['entry_time'],
                    'exit_time': ts,
                    'entry_price': long_pos['entry_price'],
                    'exit_price': exit_price,
                    'pnl_pct': -1.50,
                    'exit_type': 'SL',
                })
                long_pos = None
        
        if short_pos is not None:
            pnl_pct = (short_pos['entry_price'] - cl) / short_pos['entry_price'] * 100
            if pnl_pct >= 2.5:
                exit_price = short_pos['entry_price'] * 0.975  # 理论止盈价
                trades.append({
                    'direction': 'SHORT',
                    'entry_time': short_pos['entry_time'],
                    'exit_time': ts,
                    'entry_price': short_pos['entry_price'],
                    'exit_price': exit_price,
                    'pnl_pct': 2.50,
                    'exit_type': 'TP',
                })
                short_pos = None
            elif pnl_pct <= -1.5:
                exit_price = short_pos['entry_price'] * 1.015  # 理论止损价
                trades.append({
                    'direction': 'SHORT',
                    'entry_time': short_pos['entry_time'],
                    'exit_time': ts,
                    'entry_price': short_pos['entry_price'],
                    'exit_price': exit_price,
                    'pnl_pct': -1.50,
                    'exit_type': 'SL',
                })
                short_pos = None
        
        # ==== 再判断入场信号（用cl算全部指标）====
        sma20_5m   = row['sma20_5m']
        rsi_5m_val = row['rsi_5m']
        adx_1h     = row['adx_1h']
        sma20_4h   = row['sma20_4h']
        adx_4h     = row['adx_4h']
        sma20_1d   = row['sma20_1d']
        close_4h   = row['close_4h']
        close_1d   = row['close_1d']
        pct_sma    = row['pct_from_sma20_5m']
        
        within_sma = abs(pct_sma) <= 1.0
        adx_1h_ok  = adx_1h > 25
        adx_4h_ok  = adx_4h < 40
        
        # LONG信号
        long_signal_now = (close_4h > sma20_4h and close_1d > sma20_1d and
                          within_sma and adx_1h_ok and adx_4h_ok and rsi_5m_val > 40)
        # SHORT信号
        short_signal_now = (close_4h < sma20_4h and close_1d < sma20_1d and
                           within_sma and adx_1h_ok and adx_4h_ok and rsi_5m_val < 60)
        
        # 跳过重复信号（边缘检测）
        long_new = long_signal_now and not long_last_signal
        short_new = short_signal_now and not short_last_signal
        
        # 入场 @ 当根收盘价
        if long_pos is None and long_new:
            long_pos = {'entry_price': cl, 'entry_time': ts}
        if short_pos is None and short_new:
            short_pos = {'entry_price': cl, 'entry_time': ts}
        
        long_last_signal = long_signal_now
        short_last_signal = short_signal_now
    
    # 末位强平
    last_close = df.iloc[-1]['close']
    for pos, direction in [(long_pos, 'LONG'), (short_pos, 'SHORT')]:
        if pos is not None:
            exit_price = last_close
            if direction == 'LONG':
                pnl_pct = (exit_price - pos['entry_price']) / pos['entry_price'] * 100
            else:
                pnl_pct = (pos['entry_price'] - exit_price) / pos['entry_price'] * 100
            trades.append({
                'direction': direction,
                'entry_time': pos['entry_time'],
                'exit_time': df.index[-1],
                'entry_price': pos['entry_price'],
                'exit_price': exit_price,
                'pnl_pct': pnl_pct,
                'exit_type': 'FORCE',
            })
    
    print(f"\n✅ 回测完成: {len(trades)} 笔交易")
    return trades

# ==================== 统计分析 ====================
def analyze(trades, capital=1000):
    """统计分析"""
    print("\n" + "="*60)
    print("📊 BTC v4.0 趋势回调策略 · 回测报告")
    print("="*60)
    
    if not trades:
        print("无交易记录")
        return
    
    df = pd.DataFrame(trades)
    
    wins = df[df['pnl_pct'] > 0]
    losses = df[df['pnl_pct'] <= 0]
    longs = df[df['direction'] == 'LONG']
    shorts = df[df['direction'] == 'SHORT']
    
    n_total = len(df)
    n_wins = len(wins)
    n_losses = len(losses)
    win_rate = n_wins / n_total * 100
    
    avg_win = wins['pnl_pct'].mean() if n_wins > 0 else 0
    avg_loss = losses['pnl_pct'].mean() if n_losses > 0 else 0
    avg_profit = df['pnl_pct'].mean()
    
    total_pnl_pct = df['pnl_pct'].sum()
    
    # 含手续费的复利计算（每笔扣除双边手续费）
    net_pnl = []
    equity = capital
    equity_curve = [equity]
    for pnl in df['pnl_pct']:
        net = (pnl / 100 - FEE * 2)  # 双边手续费
        equity *= (1 + net)
        equity_curve.append(equity)
        net_pnl.append(net * 100)
    
    final_equity = equity
    total_return = (final_equity / capital - 1) * 100
    
    # 最大回撤
    equity_series = pd.Series(equity_curve)
    peak = equity_series.expanding().max()
    drawdown = (equity_series - peak) / peak * 100
    max_dd = drawdown.min()
    
    # 最大连亏次数
    streak = 0
    max_streak = 0
    max_loss_sum = 0
    current_loss_sum = 0
    for pnl in df['pnl_pct']:
        if pnl <= 0:
            streak += 1
            current_loss_sum += pnl
            max_streak = max(max_streak, streak)
            max_loss_sum = min(max_loss_sum, current_loss_sum)
        else:
            streak = 0
            current_loss_sum = 0
    
    # 盈亏比
    profit_factor = abs(wins['pnl_pct'].sum() / losses['pnl_pct'].sum()) if n_losses > 0 else float('inf')
    if n_wins > 0 and n_losses > 0:
        win_loss_ratio = abs(avg_win / avg_loss)
    else:
        win_loss_ratio = float('inf') if n_wins > 0 else 0
    
    # 夏普比率（年化）
    net_pnl_series = pd.Series(net_pnl)
    if net_pnl_series.std() > 0:
        sharpe = (net_pnl_series.mean() / net_pnl_series.std()) * np.sqrt(len(df) / 4)  # 4年
    else:
        sharpe = 0
    
    # 时间分布
    df['entry_time'] = pd.to_datetime(df['entry_time'])
    df['year'] = df['entry_time'].dt.year
    df['month'] = df['entry_time'].dt.month
    
    # 按年统计
    yearly = df.groupby('year').agg(
        笔数=('pnl_pct', 'count'),
        胜率=('pnl_pct', lambda x: (x > 0).sum() / len(x) * 100),
        总盈亏=('pnl_pct', 'sum'),
        平均盈亏=('pnl_pct', 'mean'),
    )
    
    # 输出报告
    print(f"\n{'─'*50}")
    print("📋 基本信息")
    print(f"{'─'*50}")
    print(f"  回测区间: {df['entry_time'].min().strftime('%Y-%m-%d')} ~ {df['entry_time'].max().strftime('%Y-%m-%d')}")
    print(f"  数据截止: {df['exit_time'].max().strftime('%Y-%m-%d %H:%M')}")
    
    print(f"\n{'─'*50}")
    print("📈 核心指标")
    print(f"{'─'*50}")
    print(f"  交易笔数:   {n_total}")
    print(f"  胜率:       {win_rate:.1f}%  ({n_wins}胜/{n_losses}负)")
    print(f"  总盈亏:     {total_pnl_pct:+.1f}%  (纯信号累加)")
    print(f"  复利总收益: {total_return:+.1f}%  (含0.04%双边手续费)")
    print(f"  最大回撤:   {max_dd:.1f}%")
    print(f"  最大连亏:   {max_streak}次  (累计{max_loss_sum:.1f}%)")
    print(f"  平均盈利:   {avg_win:+.2f}%")
    print(f"  平均亏损:   {avg_loss:+.2f}%")
    print(f"  盈亏比:     {win_loss_ratio:.2f}:1")
    print(f"  盈利因子:   {profit_factor:.2f}")
    print(f"  夏普比率:   {sharpe:.2f}")
    
    print(f"\n{'─'*50}")
    print("📊 方向统计")
    print(f"{'─'*50}")
    print(f"  LONG:  {len(longs)}笔  胜率{(longs['pnl_pct']>0).sum()/len(longs)*100:.1f}%  总盈亏{longs['pnl_pct'].sum():+.1f}%")
    print(f"  SHORT: {len(shorts)}笔  胜率{(shorts['pnl_pct']>0).sum()/len(shorts)*100:.1f}%  总盈亏{shorts['pnl_pct'].sum():+.1f}%")
    
    print(f"\n{'─'*50}")
    print("📋 逐年统计")
    print(f"{'─'*50}")
    print(yearly.to_string())
    
    print(f"\n{'─'*50}")
    print("🎯 出场方式")
    print(f"{'─'*50}")
    exit_stats = df['exit_type'].value_counts()
    for k, v in exit_stats.items():
        avg_pnl = df[df['exit_type']==k]['pnl_pct'].mean()
        print(f"  {k}: {v}笔  平均{avg_pnl:+.2f}%")
    
    # 权益曲线
    print(f"\n{'─'*50}")
    print("📉 权益曲线 (前10笔+后10笔)")
    print(f"{'─'*50}")
    print(f"  初始: ${capital:,.0f}")
    for i, eq in enumerate(equity_curve[:10]):
        print(f"  #{i}: ${eq:,.2f}")
    if len(equity_curve) > 20:
        print(f"  ...")
        for i, eq in enumerate(equity_curve[-10:]):
            idx = len(equity_curve) - 10 + i
            print(f"  #{idx}: ${eq:,.2f}")
    print(f"  最终: ${equity_curve[-1]:,.2f}")
    
    # 月度分布
    monthly = df.groupby('month')['pnl_pct'].agg(['count', 'sum', 'mean'])
    monthly.index = ['1月','2月','3月','4月','5月','6月','7月','8月','9月','10月','11月','12月'][:len(monthly)]
    print(f"\n{'─'*50}")
    print("📅 月度盈亏分布")
    print(f"{'─'*50}")
    print(monthly.to_string())
    
    return {
        'trades': df,
        'equity_curve': equity_curve,
        'max_dd': max_dd,
        'total_return': total_return,
        'win_rate': win_rate,
        'sharpe': sharpe,
    }

# ==================== 主函数 ====================
if __name__ == '__main__':
    fresh = '--fresh' in sys.argv
    
    start_ts = time.time()
    
    # 1. 加载数据
    data = load_or_download(fresh)
    
    # 2. 计算指标
    df_5m = calc_indicators(data['5m'].copy(), data['1h'], data['4h'], data['1d'])
    
    # 3. 运行回测
    trades = run_backtest(df_5m)
    
    # 4. 分析结果
    results = analyze(trades)
    
    elapsed = time.time() - start_ts
    print(f"\n⏱️ 总耗时: {elapsed:.1f}秒")
    
    # 5. 保存结果
    pd.DataFrame(trades).to_csv(os.path.join(DATA_DIR, 'backtest_trades.csv'), index=False)
    print(f"💾 交易明细已保存: {DATA_DIR}/backtest_trades.csv")
