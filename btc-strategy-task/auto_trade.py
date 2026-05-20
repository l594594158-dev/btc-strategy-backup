#!/usr/bin/env python3
"""
BTC合约 趋势回调策略 v4.0
- 2秒轮询 + 多周期指标分析
- LONG顺势追多 + SHORT顺势摸顶
- 双向各1仓，信号消失后才允许重新触发
- 固定止盈+2.5% / 止损-1.5%
"""
import ccxt
import pandas as pd
import ta
import time
import json
import os
from datetime import datetime

# ========== API配置 ==========
API_KEY = "CUPwmVULosVO24NBKmoaMm0pvga2msasOa4nBhvPvybrGdA2RcXBYA4aRtGMZjWH"
SECRET = "Ozht5MjazUu4JKhSLqx4ASmTBH4wlUMdbABOblxXGyhIuof1jhrzUEr9JkWHpUHM"

binance = ccxt.binance({
    'apiKey': API_KEY,
    'secret': SECRET,
    'options': {'defaultType': 'swap', 'defaultPositionSide': 'LONG', 'marginMode': 'isolated'}
})

SYMBOL = 'BTC/USDT:USDT'

# ========== v4.0 策略参数 ==========
QTY = 0.001               # 每仓 0.001 BTC
LEVERAGE = 50             # 50x 逐仓
TP_PCT = 2.5 / 100        # 止盈 +2.5%
SL_PCT = 1.5 / 100        # 止损 -1.5%
POLL_INTERVAL = 2          # 轮询间隔（秒）

# K线请求数量（手册规定）
LIMIT_5M = 100
LIMIT_1H = 200
LIMIT_4H = 200
LIMIT_1D = 200

# ========== 文件路径 ==========
STATE_FILE = '/root/.openclaw/workspace/btc-strategy-task/databases/state.json'
NOTIFY_QUEUE = '/root/.openclaw/workspace/btc-strategy-task/databases/notify_queue.json'
WORK_LOG = '/root/.openclaw/workspace/btc-strategy-task/logs/work_log.txt'

# ========== 工具函数 ==========
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def work_log(event_type, detail):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(WORK_LOG, 'a') as f:
        f.write(f"[{ts}] {event_type} | {detail}\n")

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {'long_pos': None, 'short_pos': None, 'last_long_signal': False, 'last_short_signal': False}

def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, default=str)

def send_wechat(msg):
    try:
        os.makedirs(os.path.dirname(NOTIFY_QUEUE), exist_ok=True)
        with open(NOTIFY_QUEUE, 'w') as f:
            json.dump({'time': datetime.now().isoformat(), 'msg': msg, 'sent': False}, f)
    except Exception as e:
        log(f"⚠️ 通知写入失败: {e}")

# ========== 数据获取 ==========
def get_data():
    k5m = binance.fetch_ohlcv(SYMBOL, timeframe='5m', limit=LIMIT_5M)
    k1h = binance.fetch_ohlcv(SYMBOL, timeframe='1h', limit=LIMIT_1H)
    k4h = binance.fetch_ohlcv(SYMBOL, timeframe='4h', limit=LIMIT_4H)
    k1d = binance.fetch_ohlcv(SYMBOL, timeframe='1d', limit=LIMIT_1D)
    return k5m, k1h, k4h, k1d

# ========== v4.0 指标计算 ==========
def calc_indicators(kline_data):
    """计算多周期指标，返回dict或None(数据不足)"""
    df = pd.DataFrame(kline_data, columns=['t', 'o', 'h', 'l', 'c', 'v'])
    close = df['c'].astype(float)
    high = df['h'].astype(float)
    low = df['l'].astype(float)
    volume = df['v'].astype(float)
    lv = len(df) - 1

    if len(df) < 20:
        return None

    sma20 = ta.trend.SMAIndicator(close, 20).sma_indicator().iloc[lv]
    rsi14 = ta.momentum.RSIIndicator(close, 14).rsi().iloc[lv]

    # 成交量比值: 当前量 / 近20根均量
    avg_vol = volume.iloc[max(0, lv-19):lv+1].mean()
    vol_ratio = volume.iloc[lv] / avg_vol if avg_vol > 0 else 1.0

    # ADX
    try:
        adx_ind = ta.trend.ADXIndicator(high, low, close, window=14)
        adx = adx_ind.adx().iloc[lv]
    except:
        adx = 25

    return {
        'price': close.iloc[lv],
        'sma20': sma20,
        'rsi14': rsi14,
        'adx': adx,
        'vol_ratio': vol_ratio,
    }

# ========== v4.0 开仓信号判断 ==========
def check_signals(r_5m, r_1h, r_4h, r_1d):
    """
    v4.0 趋势回调策略信号检查
    返回: (signal, reason_dict) 或 (None, observe_msg)
      signal: 'LONG' | 'SHORT' | None
    """
    price = r_5m['price']
    rsi5 = r_5m['rsi14']
    adx_1h = r_1h['adx']
    adx_4h = r_4h['adx']
    sma_4h = r_4h['sma20']
    sma_1d = r_1d['sma20']
    sma_5m = r_5m['sma20']
    vol5 = r_5m['vol_ratio']

    # 公共条件
    pct_from_sma5 = (price - sma_5m) / sma_5m * 100
    within_sma = abs(pct_from_sma5) <= 1.0
    adx_1h_ok = adx_1h > 25
    adx_4h_ok = adx_4h < 40
    vol_ok = vol5 >= 1.0  # 全放宽

    # 趋势判断
    trend_4h = '多头' if price > sma_4h else '空头'
    trend_1d = '多头' if price > sma_1d else '空头'
    long_trend = price > sma_4h and price > sma_1d
    short_trend = price < sma_4h and price < sma_1d

    # --- LONG 顺势追多 ---
    if (long_trend and within_sma and adx_1h_ok and adx_4h_ok and vol_ok and rsi5 > 40):
        reason = {
            'name': 'LONG-顺势追多',
            'price': price,
            'sma5': sma_5m, 'pct_sma': pct_from_sma5,
            'rsi5': rsi5, 'adx1h': adx_1h, 'adx4h': adx_4h,
            'sma4h': sma_4h, 'sma1d': sma_1d,
            'vol': vol5, 'trend': f'4h{trend_4h}/1d{trend_1d}',
        }
        return 'LONG', reason

    # --- SHORT 顺势摸顶 ---
    if (short_trend and within_sma and adx_1h_ok and adx_4h_ok and vol_ok and rsi5 < 60):
        reason = {
            'name': 'SHORT-顺势摸顶',
            'price': price,
            'sma5': sma_5m, 'pct_sma': pct_from_sma5,
            'rsi5': rsi5, 'adx1h': adx_1h, 'adx4h': adx_4h,
            'sma4h': sma_4h, 'sma1d': sma_1d,
            'vol': vol5, 'trend': f'4h{trend_4h}/1d{trend_1d}',
        }
        return 'SHORT', reason

    # --- 观望原因 ---
    if not within_sma:
        obs = f"观望 | 价格距5mSMA {pct_from_sma5:+.1f}% (需±1.0%)"
    elif not adx_1h_ok:
        obs = f"观望 | 1h ADX={adx_1h:.1f}≤25 无趋势"
    elif not adx_4h_ok:
        obs = f"观望 | 4h ADX={adx_4h:.1f}≥40 过热"
    elif not long_trend and not short_trend:
        obs = f"观望 | 4h{trend_4h}/1d{trend_1d} 方向不一致"
    elif long_trend and rsi5 <= 40:
        obs = f"观望 | LONG趋势但RSI={rsi5:.1f}≤40 超卖"
    elif short_trend and rsi5 >= 60:
        obs = f"观望 | SHORT趋势但RSI={rsi5:.1f}≥60 超买"
    else:
        obs = f"观望 | 条件未完全满足"
    return None, obs

# ========== v4.0 开仓执行 ==========
def open_position(direction, reason_dict):
    """开仓 + 挂SL/TP"""
    price = reason_dict['price']
    if direction == 'LONG':
        positionSide = 'LONG'
        order_side = 'buy'
        sl_price = round(price * (1 - SL_PCT), 1)
        tp_price = round(price * (1 + TP_PCT), 1)
    else:
        positionSide = 'SHORT'
        order_side = 'sell'
        sl_price = round(price * (1 + SL_PCT), 1)
        tp_price = round(price * (1 - TP_PCT), 1)

    log(f"🚨 开{direction} | ${price:,.1f} | SL=${sl_price:,.1f} | TP=${tp_price:,.1f}")

    # 市价开仓
    try:
        order = binance.create_order(
            SYMBOL, 'market', order_side, QTY,
            params={'positionSide': positionSide}
        )
        fill_price = float(order.get('average', price))
        log(f"✅ 已开仓: {direction} {QTY}BTC @ ${fill_price:,.2f}")
    except Exception as e:
        log(f"❌ 开仓失败: {e}")
        return False

    # 挂止损单
    try:
        sl_close = 'sell' if direction == 'LONG' else 'buy'
        binance.create_order(
            SYMBOL, 'STOP_MARKET', sl_close, QTY,
            params={'stopPrice': sl_price, 'positionSide': positionSide}
        )
        log(f"✅ 止损已挂: ${sl_price:,.1f}")
    except Exception as e:
        log(f"⚠️ 挂止损失败: {e}")

    # 挂止盈单
    try:
        tp_close = 'sell' if direction == 'LONG' else 'buy'
        binance.create_order(
            SYMBOL, 'TAKE_PROFIT_MARKET', tp_close, QTY,
            params={'stopPrice': tp_price, 'positionSide': positionSide}
        )
        log(f"✅ 止盈已挂: ${tp_price:,.1f}")
    except Exception as e:
        log(f"⚠️ 挂止盈失败: {e}")

    # 更新state
    state = load_state()
    pos_key = 'long_pos' if direction == 'LONG' else 'short_pos'
    state[pos_key] = {
        'entry_price': fill_price,
        'qty': QTY,
        'sl': sl_price,
        'tp': tp_price,
        'open_time': datetime.now().isoformat(),
        'reason': reason_dict,
    }
    save_state(state)

    # 微信通知
    reason_text = (
        f"【{reason_dict['name']}】\n"
        f"价格: ${price:,.0f} | SMA5: ${reason_dict['sma5']:,.0f} ({reason_dict['pct_sma']:+.1f}%)\n"
        f"RSI5: {reason_dict['rsi5']:.1f} | 1hADX: {reason_dict['adx1h']:.1f} | 4hADX: {reason_dict['adx4h']:.1f}\n"
        f"SMA4h: ${reason_dict['sma4h']:,.0f} | SMA1d: ${reason_dict['sma1d']:,.0f}\n"
        f"趋势: {reason_dict['trend']} | 量比: {reason_dict['vol']:.1f}x"
    )
    wechat_msg = (
        f"🚨 BTC v4.0 开仓\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"方向: {'🟢LONG📈' if direction == 'LONG' else '🔴SHORT📉'}\n"
        f"数量: {QTY} BTC | 杠杆: {LEVERAGE}x\n"
        f"开仓价: ${fill_price:,.2f}\n"
        f"止损: ${sl_price:,.1f} (-{SL_PCT*100:.1f}%)\n"
        f"止盈: ${tp_price:,.1f} (+{TP_PCT*100:.1f}%)\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{reason_text}\n"
        f"⏰ {datetime.now().strftime('%H:%M:%S')}"
    )
    send_wechat(wechat_msg)
    work_log('开仓', f'{direction} {QTY}BTC @ {fill_price} | SL={sl_price} TP={tp_price}')
    return True

# ========== v4.0 平仓检测 ==========
def check_close(state):
    """检查持仓是否已平仓（SL/TP触发或手动平仓），同步state"""
    try:
        positions = binance.fetch_positions()
    except:
        return state

    long_alive = any(
        p.get('symbol') == SYMBOL and float(p.get('contracts', 0)) > 0 and p.get('side') == 'long'
        for p in positions
    )
    short_alive = any(
        p.get('symbol') == SYMBOL and float(p.get('contracts', 0)) > 0 and p.get('side') == 'short'
        for p in positions
    )

    changed = False

    if state.get('long_pos') and not long_alive:
        pos = state['long_pos']
        log(f"📤 LONG已平仓 | 开仓价=${pos['entry_price']:,.2f}")
        send_wechat(f"✅ BTC v4.0 平仓\nLONG {pos['qty']}BTC @ ${pos['entry_price']:,.2f}\n⏰ {datetime.now().strftime('%H:%M:%S')}")
        work_log('平仓', f"LONG {pos['qty']}BTC @ {pos['entry_price']}")
        state['long_pos'] = None
        state['last_long_signal'] = False  # 重置信号状态
        changed = True

    if state.get('short_pos') and not short_alive:
        pos = state['short_pos']
        log(f"📤 SHORT已平仓 | 开仓价=${pos['entry_price']:,.2f}")
        send_wechat(f"✅ BTC v4.0 平仓\nSHORT {pos['qty']}BTC @ ${pos['entry_price']:,.2f}\n⏰ {datetime.now().strftime('%H:%M:%S')}")
        work_log('平仓', f"SHORT {pos['qty']}BTC @ {pos['entry_price']}")
        state['short_pos'] = None
        state['last_short_signal'] = False
        changed = True

    if changed:
        save_state(state)
    return state

# ========== v4.0 SL/TP 挂单修复 ==========
def fix_sl_tp(state):
    """每60秒检查SL/TP挂单是否完整，缺失则补挂"""
    try:
        positions = binance.fetch_positions()
        algos = binance.fapiprivate_get_openalgoorders({'symbol': 'BTCUSDT'})
    except:
        return

    active_algos = [o for o in algos if o.get('algoStatus') not in ('CANCELED', 'FINISHED', 'EXPIRED')]

    for key, direction in [('long_pos', 'LONG'), ('short_pos', 'SHORT')]:
        pos = state.get(key)
        if not pos:
            continue
        # 确认交易所仍有该方向持仓
        exchange_has = any(
            p.get('symbol') == SYMBOL and float(p.get('contracts', 0)) > 0
            and p.get('side') == ('long' if direction == 'LONG' else 'short')
            for p in positions
        )
        if not exchange_has:
            continue

        qty = pos['qty']
        sl = pos['sl']
        tp = pos['tp']
        close_side = 'sell' if direction == 'LONG' else 'buy'

        has_sl = any(
            o.get('orderType') == 'STOP_MARKET'
            and (o.get('side') == 'SELL' if direction == 'LONG' else o.get('side') == 'BUY')
            and float(o.get('quantity', 0)) >= qty * 0.99
            for o in active_algos
        )
        has_tp = any(
            o.get('orderType') == 'TAKE_PROFIT_MARKET'
            and (o.get('side') == 'SELL' if direction == 'LONG' else o.get('side') == 'BUY')
            and float(o.get('quantity', 0)) >= qty * 0.99
            for o in active_algos
        )

        if not has_sl:
            try:
                binance.create_order(SYMBOL, 'STOP_MARKET', close_side, qty,
                    params={'stopPrice': sl, 'positionSide': direction})
                log(f"🔧 补挂SL {direction} ${sl:,.1f}")
            except Exception as e:
                log(f"❌ 补挂SL失败: {e}")
        if not has_tp:
            try:
                binance.create_order(SYMBOL, 'TAKE_PROFIT_MARKET', close_side, qty,
                    params={'stopPrice': tp, 'positionSide': direction})
                log(f"🔧 补挂TP {direction} ${tp:,.1f}")
            except Exception as e:
                log(f"❌ 补挂TP失败: {e}")

        if has_sl and has_tp:
            log(f"✅ {direction} SL/TP正常 (${sl:,.0f}/${tp:,.0f})")

# ========== 状态打印 ==========
def print_status(r_5m, r_1h, r_4h, r_1d, state, signal, reason):
    price = r_5m['price']
    now = datetime.now().strftime('%H:%M:%S')

    trend_4h = '📈多头' if price > r_4h['sma20'] else '📉空头'
    trend_1d = '📈多头' if price > r_1d['sma20'] else '📉空头'

    pct_sma = (price - r_5m['sma20']) / r_5m['sma20'] * 100

    print(f"""
╔══════════════════════════════════════════════════════════╗
║  BTC v4.0 趋势回调  {now}                        ║
╠══════════════════════════════════════════════════════════╣
║  💰 ${price:>12,.2f}  | 距SMA5 {pct_sma:+.1f}%  | RSI {r_5m['rsi14']:.1f}         ║
╠══════════════════════════════════════════════════════════╣
║  4h: {trend_4h} @SMA${r_4h['sma20']:,.0f}  ADX{r_4h['adx']:.0f}          ║
║  1d: {trend_1d} @SMA${r_1d['sma20']:,.0f}                 ║
║  1h: ADX{r_1h['adx']:.0f}  | 量比{r_5m['vol_ratio']:.1f}x                    ║""")

    lp = state.get('long_pos')
    sp = state.get('short_pos')
    if lp:
        pnl = (price - lp['entry_price']) / lp['entry_price'] * 100
        print(f"╠══════════════════════════════════════════════════════════╣")
        print(f"║  🟢 LONG {lp['qty']}BTC @ ${lp['entry_price']:,.0f}  PnL {pnl:+.2f}%         ║")
        print(f"║     SL ${lp['sl']:,.0f}  |  TP ${lp['tp']:,.0f}                       ║")
    if sp:
        pnl = (sp['entry_price'] - price) / sp['entry_price'] * 100
        print(f"╠══════════════════════════════════════════════════════════╣")
        print(f"║  🔴 SHORT {sp['qty']}BTC @ ${sp['entry_price']:,.0f}  PnL {pnl:+.2f}%        ║")
        print(f"║     SL ${sp['sl']:,.0f}  |  TP ${sp['tp']:,.0f}                       ║")
    if not lp and not sp:
        print(f"╠══════════════════════════════════════════════════════════╣")
        obs = reason if isinstance(reason, str) else '等待信号...'
        print(f"║  🎯 {obs[:52]}║")

    print(f"╚══════════════════════════════════════════════════════════╝")

# ========== 主循环 ==========
def main():
    log(f"🚀 BTC v4.0 趋势回调策略启动 | {POLL_INTERVAL}s轮询 | {LEVERAGE}x | {QTY}BTC/仓")
    log(f"止盈+{TP_PCT*100:.1f}% | 止损-{SL_PCT*100:.1f}% | 双向各1仓")
    log(f"LONG: 4h/1d多头 + 5mSMA±1% + 1hADX>25 + 4hADX<40 + RSI>40")
    log(f"SHORT: 4h/1d空头 + 5mSMA±1% + 1hADX>25 + 4hADX<40 + RSI<60")

    # v4.0: 启动时同步持仓状态
    state = load_state()
    try:
        positions = binance.fetch_positions()
        long_alive = any(p.get('symbol') == SYMBOL and float(p.get('contracts', 0)) > 0 and p.get('side') == 'long' for p in positions)
        short_alive = any(p.get('symbol') == SYMBOL and float(p.get('contracts', 0)) > 0 and p.get('side') == 'short' for p in positions)
        if not long_alive and state.get('long_pos'):
            log(f"⚠️ 交易所无LONG仓，清除state")
            state['long_pos'] = None
        if not short_alive and state.get('short_pos'):
            log(f"⚠️ 交易所无SHORT仓，清除state")
            state['short_pos'] = None
        save_state(state)
    except Exception as e:
        log(f"⚠️ 启动同步异常: {e}")

    cycle = 0
    while True:
        try:
            cycle += 1

            # 获取数据
            k5m, k1h, k4h, k1d = get_data()

            # 计算指标
            r_5m = calc_indicators(k5m)
            r_1h = calc_indicators(k1h)
            r_4h = calc_indicators(k4h)
            r_1d = calc_indicators(k1d)

            if any(v is None for v in [r_5m, r_1h, r_4h, r_1d]):
                if cycle % 6 == 0:
                    log(f"⚠️ 数据不足，跳过")
                time.sleep(POLL_INTERVAL)
                continue

            # 检查平仓（交易所持仓变化 → 同步state）
            state = check_close(state)

            # 信号判断
            signal, reason = check_signals(r_5m, r_1h, r_4h, r_1d)

            # --- 入场逻辑 ---
            # 获取当前信号状态
            long_signal_now = (signal == 'LONG')
            short_signal_now = (signal == 'SHORT')

            # 跳过重复信号（信号消失后才能重新触发）
            long_new = long_signal_now and not state.get('last_long_signal', False)
            short_new = short_signal_now and not state.get('last_short_signal', False)

            # LONG开仓
            if long_new and not state.get('long_pos'):
                log(f"🎯 LONG信号触发!")
                open_position('LONG', reason)
                state = load_state()  # 刷新state（含新仓位）

            # SHORT开仓
            if short_new and not state.get('short_pos'):
                log(f"🎯 SHORT信号触发!")
                open_position('SHORT', reason)
                state = load_state()  # 刷新state（含新仓位）

            # 更新信号状态
            state['last_long_signal'] = long_signal_now
            state['last_short_signal'] = short_signal_now
            save_state(state)

            # 打印状态
            print_status(r_5m, r_1h, r_4h, r_1d, state, signal, reason)

            # SL/TP挂单修复（每60秒）
            if cycle % 30 == 0:
                fix_sl_tp(state)

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            log("🛑 手动停止")
            break
        except Exception as e:
            log(f"❌ 异常: {e}")
            import traceback; traceback.print_exc()
            work_log('错误', str(e)[:100])
            time.sleep(10)

if __name__ == '__main__':
    main()
