#!/usr/bin/env python3
"""
BTC回测数据加载器
用法:
  from btc_data_loader import load_data, load_1m
  df_5m, df_1h, df_4h, df_1d = load_data()      # 全周期(已含指标)
  df_1m = load_1m()                              # 1分钟原始数据
  
  也可以直接从回测脚本调用:
  python3 btc_data_loader.py                     # 查看数据概况
  python3 btc_data_loader.py --1m                # 查看1m数据概况
"""
import pickle, os, sys
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backtest_data')

def load_data():
    """加载全周期数据: 5m(含指标)+1h+4h+1d"""
    path = os.path.join(DATA_DIR, 'btc_data_4y.pkl')
    if not os.path.exists(path):
        raise FileNotFoundError(f"数据文件不存在: {path}\n请先运行 backtest_v4.py 下载数据")
    with open(path, 'rb') as f:
        data = pickle.load(f)
    return data['5m'], data['1h'], data['4h'], data['1d']

def load_1m():
    """加载1分钟原始K线数据"""
    path = os.path.join(DATA_DIR, 'btc_1m_4y.pkl')
    if not os.path.exists(path):
        raise FileNotFoundError(f"1m数据文件不存在: {path}\n请先运行 backtest_v4_1m.py 下载数据")
    with open(path, 'rb') as f:
        return pickle.load(f)

def load_5m_with_indicators():
    """加载5m数据并计算全部指标（用于回测）"""
    import pandas as pd, numpy as np, ta
    
    df_5m_raw, df_1h, df_4h, df_1d = load_data()
    df_5m = df_5m_raw.copy()
    
    close_5m = df_5m['close'].astype(float)
    high_5m = df_5m['high'].astype(float)
    low_5m = df_5m['low'].astype(float)
    
    # 5m指标
    df_5m['sma20_5m'] = ta.trend.SMAIndicator(close_5m, 20).sma_indicator()
    df_5m['rsi_5m'] = ta.momentum.RSIIndicator(close_5m, 14).rsi()
    df_5m['pct_sma'] = (close_5m - df_5m['sma20_5m']) / df_5m['sma20_5m'] * 100
    
    # 高周期指标 → 5m映射
    c1h = df_1h['close'].astype(float); h1h = df_1h['high'].astype(float); l1h = df_1h['low'].astype(float)
    c4h = df_4h['close'].astype(float); h4h = df_4h['high'].astype(float); l4h = df_4h['low'].astype(float)
    c1d = df_1d['close'].astype(float)
    
    df_1h['adx_1h'] = ta.trend.ADXIndicator(h1h, l1h, c1h, 14).adx()
    df_4h['sma20_4h'] = ta.trend.SMAIndicator(c4h, 20).sma_indicator()
    df_4h['adx_4h'] = ta.trend.ADXIndicator(h4h, l4h, c4h, 14).adx()
    df_4h['close_4h'] = c4h
    df_1d['sma20_1d'] = ta.trend.SMAIndicator(c1d, 20).sma_indicator()
    df_1d['close_1d'] = c1d
    
    for col, src in [('adx_1h', df_1h), ('sma20_4h', df_4h), ('adx_4h', df_4h), 
                      ('close_4h', df_4h), ('sma20_1d', df_1d), ('close_1d', df_1d)]:
        df_5m[col] = src[col].reindex(df_5m.index, method='ffill')
    
    df_5m.dropna(inplace=True)
    return df_5m

if __name__ == '__main__':
    if '--1m' in sys.argv:
        df = load_1m()
        print(f"1分钟K线: {len(df):,} 根")
    else:
        df5, df1h, df4h, df1d = load_data()
        print(f"5m:  {len(df5):,} 根 | {df5.index[0]} ~ {df5.index[-1]}")
        print(f"1h:  {len(df1h):,} 根 | {df1h.index[0]} ~ {df1h.index[-1]}")
        print(f"4h:  {len(df4h):,} 根 | {df4h.index[0]} ~ {df4h.index[-1]}")
        print(f"1d:  {len(df1d):,} 根 | {df1d.index[0]} ~ {df1d.index[-1]}")
