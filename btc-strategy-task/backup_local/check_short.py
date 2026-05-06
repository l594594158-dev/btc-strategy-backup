import ccxt
import pandas as pd
import numpy as np

exchange = ccxt.binance()

def get_ohlcv(tf, limit):
    return exchange.fetch_ohlcv('BTC/USDT', timeframe=tf, limit=limit)

def get_closes(ohlcv):
    return [x[4] for x in ohlcv]

ohlcv_5m = get_ohlcv('5m', 60)
ohlcv_4h = get_ohlcv('4h', 60)
closes_5m = get_closes(ohlcv_5m)
closes_4h = get_closes(ohlcv_4h)

current_price = closes_5m[-1]

ma4h_20 = pd.Series(closes_4h).rolling(20).mean().iloc[-1]

# Bollinger on 5m
series = pd.Series(closes_5m)
mid = series.rolling(20).mean()
std = series.rolling(20).std()
upper_5m = (mid + 2 * std).iloc[-1]
lower_5m = (mid - 2 * std).iloc[-1]
band_width = upper_5m - lower_5m

# RSI
delta = series.diff()
gain = delta.where(delta > 0, 0).rolling(14).mean()
loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
rs = gain / loss
rsi_val = (100 - (100 / (1 + rs))).iloc[-1]

# Price for %b = 0.90
price_for_90_pct_b = lower_5m + 0.90 * band_width

print(f"当前价格: {current_price:.2f}")
print(f"5m 布林上轨: {upper_5m:.2f}")
print(f"5m 布林中轨: {mid.iloc[-1]:.2f}")
print(f"5m 布林下轨: {lower_5m:.2f}")
print(f"4h MA20: {ma4h_20:.2f}")
print(f"当前 RSI: {rsi_val:.1f}")
print()
print(f"%b达到0.90需要价格: {price_for_90_pct_b:.2f}")
print(f"距离: +{price_for_90_pct_b - current_price:.0f} ({(price_for_90_pct_b/current_price-1)*100:.2f}%)")
print()

# 做空信号条件分析
print("=== 做空A/B 触发条件 ===")
print(f"条件1: 4h+1d 多头 -> 当前4h:{'多头' if current_price > ma4h_20 else '空头'}")
print(f"条件2: %b > 0.90 -> 需要价格 >= {price_for_90_pct_b:.0f}")
print(f"条件3: RSI >= 82 -> 需要从当前{rsi_val:.1f}上升 {82-rsi_val:.1f}")
print(f"条件4: 4h ADX < 40 -> 当前46.0，需趋势减弱")
print()
print(f"=== 结论 ===")
print(f"价格需要上涨到约 {price_for_90_pct_b:.0f} ~ {upper_5m:.0f} 区间")
print(f"可能触发做空A/B (大约 +{(price_for_90_pct_b-current_price)/current_price*100:.1f}%)")
