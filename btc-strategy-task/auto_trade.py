#!/usr/bin/env python3
"""
BTC合约 趋势回调策略 v4.1
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
· 方向用闭K: 4h/1d取已关闭收盘价，冻结不跳
· ADX用闭K: 1h/4h ADX取已关闭，冻结不跳
· 成交量用闭K: 验证上一根5m放量
· 5m指标动态: SMA20/RSI含当前K线实时响应
· 双向独立: LONG/SHORT各自管理
· 同向单仓: 同方向只允许1仓
· 双层保护: 本地state.json + 交易所fetch_positions
· 双出层: 主动pnl检查 + 被动SL/TP挂单
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
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

# ========== 全局参数 ==========
QTY = 0.003               # 每仓 0.003 BTC
LEVERAGE = 50             # 50x 逐仓
TP_PCT = 2.5 / 100        # 止盈 +2.5%
SL_PCT = 1.5 / 100        # 止损 -1.5%
POLL_INTERVAL = 2          # 轮询间隔（秒）

# ========== K线请求数量 ==========
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

def load_state():
    try:
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    except:
        return {}

def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2, default=str)

def work_log(action, detail):
    os.makedirs(os.path.dirname(WORK_LOG), exist_ok=True)
    with open(WORK_LOG, 'a') as f:
        f.write(f"[{datetime.now().isoformat()}] {action}: {detail}\n")

def send_wechat(msg):
    """写入通知队列，由cron每分钟发送"""
    try:
        os.makedirs(os.path.dirname(NOTIFY_QUEUE), exist_ok=True)
        queue = []
        if os.path.exists(NOTIFY_QUEUE):
            with open(NOTIFY_QUEUE, 'r') as f:
                try:
                    queue = json.load(f)
                except:
                    queue = []
        queue.append(msg)
        with open(NOTIFY_QUEUE, 'w') as f:
            json.dump(queue, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"⚠️ 通知写入失败: {e}")

# ========== 一、数据获取 ==========
def get_data():
    k5m = binance.fetch_ohlcv(SYMBOL, timeframe='5m', limit=LIMIT_5M)
    k1h = binance.fetch_ohlcv(SYMBOL, timeframe='1h', limit=LIMIT_1H)
    k4h = binance.fetch_ohlcv(SYMBOL, timeframe='4h', limit=LIMIT_4H)
    k1d = binance.fetch_ohlcv(SYMBOL, timeframe='1d', limit=LIMIT_1D)
    return k5m, k1h, k4h, k1d

# ========== 二、指标计算（4个独立函数）==========

def calc_5m(kline_data):
    """
    5m指标: 动态(含当前K线) + 闭K成交量
    返回: dict 5个字段, 或None
    """
    df = pd.DataFrame(kline_data, columns=['t','o','h','l','c','v'])
    close = df['c'].astype(float)
    high  = df['h'].astype(float)
    low   = df['l'].astype(float)
    vol   = df['v'].astype(float)

    lv = len(df) - 1           # 当前未关闭K线
    clv = max(0, lv - 1)       # 最近已关闭K线

    if len(df) < 20:
        return None

    # 动态指标 iloc[lv]
    price = close.iloc[lv]
    sma20 = ta.trend.SMAIndicator(close, 20).sma_indicator().iloc[lv]
    rsi   = ta.momentum.RSIIndicator(close, 14).rsi().iloc[lv]

    # 闭K成交量: vol[clv] / mean(vol[clv-19:clv+1])
    avg_vol = vol.iloc[max(0, clv-19):clv+1].mean()
    vol_ratio = vol.iloc[clv] / avg_vol if avg_vol > 0 else 1.0

    return {
        'price': price,
        'sma20': sma20,
        'rsi': rsi,
        'vol_ratio': vol_ratio,
    }

def calc_1h(kline_data):
    """
    1h指标: 闭K ADX
    返回: dict 1个字段, 或None
    """
    df = pd.DataFrame(kline_data, columns=['t','o','h','l','c','v'])
    close = df['c'].astype(float)
    high  = df['h'].astype(float)
    low   = df['l'].astype(float)

    clv = max(0, len(df) - 2)  # 最近已关闭K线

    if len(df) < 20:
        return None

    try:
        adx_ind = ta.trend.ADXIndicator(high, low, close, window=14)
        adx_closed = adx_ind.adx().iloc[clv]
    except:
        adx_closed = 25

    return {'adx_closed': adx_closed}

def calc_4h(kline_data):
    """
    4h指标: 闭K close/sma/adx
    返回: dict 3个字段, 或None
    """
    df = pd.DataFrame(kline_data, columns=['t','o','h','l','c','v'])
    close = df['c'].astype(float)
    high  = df['h'].astype(float)
    low   = df['l'].astype(float)

    clv = max(0, len(df) - 2)  # 最近已关闭K线

    if len(df) < 20:
        return None

    close_closed = close.iloc[clv]
    sma_closed   = ta.trend.SMAIndicator(close, 20).sma_indicator().iloc[clv]
    try:
        adx_closed = ta.trend.ADXIndicator(high, low, close, window=14).adx().iloc[clv]
    except:
        adx_closed = 25

    return {
        'close_closed': close_closed,
        'sma_closed': sma_closed,
        'adx_closed': adx_closed,
    }

def calc_1d(kline_data):
    """
    1d指标: 闭K close/sma
    返回: dict 2个字段, 或None
    """
    df = pd.DataFrame(kline_data, columns=['t','o','h','l','c','v'])
    close = df['c'].astype(float)

    clv = max(0, len(df) - 2)  # 最近已关闭K线

    if len(df) < 20:
        return None

    close_closed = close.iloc[clv]
    sma_closed   = ta.trend.SMAIndicator(close, 20).sma_indicator().iloc[clv]

    return {
        'close_closed': close_closed,
        'sma_closed': sma_closed,
    }

# ========== 三、七条入场条件（短路判断）==========

def check_entry(r5, r1, r4, rd):
    """
    短路判断顺序:
    1. 1hADX≤25 → 观望
    2. 4hADX≥40 → 观望
    3. 不在回调范围 → 观望
    4. 缩量 → 观望
    5. 4h多+1d多+RSI>40 → LONG
    6. 4h空+1d空+RSI<60 → SHORT
    7. 4h/1d方向不一致 → 观望

    返回: (signal, reason_dict_or_msg)
      signal: 'LONG' | 'SHORT' | None
    """
    price    = r5['price']
    sma5     = r5['sma20']
    rsi5     = r5['rsi']
    vol5     = r5['vol_ratio']

    adx_1h   = r1['adx_closed']
    adx_4h   = r4['adx_closed']
    close_4h = r4['close_closed']
    sma_4h   = r4['sma_closed']
    close_1d = rd['close_closed']
    sma_1d   = rd['sma_closed']

    # === 短路1: 1h ADX ≤ 25 ===
    if adx_1h <= 25:
        return None, f"观望 | 1hADX={adx_1h:.1f}≤25 无趋势"

    # === 短路2: 4h ADX ≥ 40 ===
    if adx_4h >= 40:
        return None, f"观望 | 4hADX={adx_4h:.1f}≥40 过热"

    # === 短路3: 回调范围 ===
    pct_sma = (price - sma5) / sma5 * 100
    if abs(pct_sma) > 1.0:
        return None, f"观望 | 价距5mSMA {pct_sma:+.1f}% (需±1%)"

    # === 短路4: 缩量 ===
    if vol5 < 1.0:
        return None, f"观望 | 量比{vol5:.1f}x 缩量"

    # === 方向判断 (闭K) ===
    h4_bull = close_4h > sma_4h   # ① 4h方向
    d1_bull = close_1d > sma_1d   # ② 1d方向

    # === 5. LONG 顺势追多 ===
    if h4_bull and d1_bull and rsi5 > 40:
        return 'LONG', {
            'name': 'LONG-顺势追多',
            'price': price, 'sma5': sma5, 'pct_sma': pct_sma,
            'rsi5': rsi5, 'adx_1h': adx_1h, 'adx_4h': adx_4h,
            'close_4h': close_4h, 'sma_4h': sma_4h,
            'close_1d': close_1d, 'sma_1d': sma_1d,
            'vol_ratio': vol5,
        }

    # === 6. SHORT 顺势摸顶 ===
    if (not h4_bull) and (not d1_bull) and rsi5 < 60:
        return 'SHORT', {
            'name': 'SHORT-顺势摸顶',
            'price': price, 'sma5': sma5, 'pct_sma': pct_sma,
            'rsi5': rsi5, 'adx_1h': adx_1h, 'adx_4h': adx_4h,
            'close_4h': close_4h, 'sma_4h': sma_4h,
            'close_1d': close_1d, 'sma_1d': sma_1d,
            'vol_ratio': vol5,
        }

    # === 7. 方向不一致 ===
    if h4_bull != d1_bull:
        trend4 = '多' if h4_bull else '空'
        trend1 = '多' if d1_bull else '空'
        return None, f"观望 | 4h{trend4}/1d{trend1} 方向不一致"

    # LONG趋势但RSI不够
    if h4_bull and rsi5 <= 40:
        return None, f"观望 | 4h多1d多 但RSI={rsi5:.1f}≤40"
    # SHORT趋势但RSI不够
    if not h4_bull and rsi5 >= 60:
        return None, f"观望 | 4h空1d空 但RSI={rsi5:.1f}≥60"

    return None, "观望 | 条件未完全满足"

# ========== 四、开仓（双层保护）==========

def do_open(direction, reason):
    """
    开仓: 交易所层 + 本地层 双层保护
    1. fetch_positions() 确认交易所无同向仓
    2. state.json 确认本地无同向仓
    3. 市价开仓 → 挂SL/TP → 写state → 发通知
    """
    # === 交易所层: 确认无同向持仓 ===
    try:
        positions = binance.fetch_positions()
        side_check = 'long' if direction == 'LONG' else 'short'
        has_pos = any(
            p.get('symbol') == SYMBOL
            and float(p.get('contracts', 0)) > 0
            and p.get('side') == side_check
            for p in positions
        )
        if has_pos:
            log(f"⚠️ 交易所已有{direction}仓，拒绝开仓")
            return False
    except Exception as e:
        log(f"❌ 交易所检查失败: {e}")
        return False

    # === 本地层: 确认state无同向持仓 ===
    state = load_state()
    pos_key = 'long_pos' if direction == 'LONG' else 'short_pos'
    if state.get(pos_key):
        log(f"⚠️ state已有{direction}仓，拒绝开仓")
        return False

    price = reason['price']

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

    # === 被动层: 挂SL/TP单 ===
    ensure_sl_tp(direction, QTY, sl_price, tp_price)

    # 更新state
    state[pos_key] = {
        'entry_price': fill_price,
        'qty': QTY,
        'sl': sl_price,
        'tp': tp_price,
        'open_time': datetime.now().isoformat(),
        'reason': reason,
    }
    save_state(state)

    # 微信通知
    reason_text = (
        f"【{reason['name']}】\n"
        f"价格: ${price:,.0f} | SMA5: ${reason['sma5']:,.0f} ({reason['pct_sma']:+.1f}%)\n"
        f"RSI5: {reason['rsi5']:.1f} | 1hADX: {reason['adx_1h']:.1f} | 4hADX: {reason['adx_4h']:.1f}\n"
        f"4h闭K: ${reason['close_4h']:,.0f} vs SMA${reason['sma_4h']:,.0f} | "
        f"1d闭K: ${reason['close_1d']:,.0f} vs SMA${reason['sma_1d']:,.0f}\n"
        f"量比: {reason['vol_ratio']:.1f}x"
    )
    wechat_msg = (
        f"🚨 BTC v4.1 开仓\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"方向: {'🟢LONG📈' if direction == 'LONG' else '🔴SHORT📉'}\n"
        f"数量: {QTY} BTC | 杠杆: {LEVERAGE}x\n"
        f"开仓价: ${fill_price:,.2f}\n"
        f"止损: ${sl_price:,.1f} ({'-'}{SL_PCT*100:.1f}%)\n"
        f"止盈: ${tp_price:,.1f} (+{TP_PCT*100:.1f}%)\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{reason_text}\n"
        f"⏰ {datetime.now().strftime('%H:%M:%S')}"
    )
    send_wechat(wechat_msg)
    work_log('开仓', f'{direction} {QTY}BTC @ {fill_price} | SL={sl_price} TP={tp_price}')
    return True

# ========== 五、被动出场: SL/TP 挂单 ==========

def ensure_sl_tp(direction, qty, sl_price, tp_price):
    """
    被动挂止损止盈单，不重复挂。
    先检查已有活动条件单，缺失则补挂。
    """
    try:
        algos = binance.fapiprivate_get_openalgoorders({'symbol': 'BTCUSDT'})
        active_algos = [
            o for o in algos
            if o.get('algoStatus') not in ('CANCELED', 'FINISHED', 'EXPIRED')
        ]
    except Exception as e:
        log(f"⚠️ 查询挂单失败: {e}")
        return

    pos_side = direction
    pos_close = 'SELL' if direction == 'LONG' else 'BUY'

    # 检查止损
    has_sl = any(
        a.get('positionSide') == pos_side
        and a.get('closePosition', False) != True
        and a.get('algoType') == 'STOP_MARKET'
        for a in active_algos
    )
    if not has_sl:
        try:
            close_side = 'sell' if direction == 'LONG' else 'buy'
            binance.create_order(
                SYMBOL, 'STOP_MARKET', close_side, qty,
                params={'stopPrice': sl_price, 'positionSide': pos_side}
            )
            log(f"✅ 止损已挂: ${sl_price:,.1f}")
        except Exception as e:
            log(f"⚠️ 挂止损失败: {e}")

    # 检查止盈
    has_tp = any(
        a.get('positionSide') == pos_side
        and a.get('algoType') == 'TAKE_PROFIT_MARKET'
        for a in active_algos
    )
    if not has_tp:
        try:
            close_side = 'sell' if direction == 'LONG' else 'buy'
            binance.create_order(
                SYMBOL, 'TAKE_PROFIT_MARKET', close_side, qty,
                params={'stopPrice': tp_price, 'positionSide': pos_side}
            )
            log(f"✅ 止盈已挂: ${tp_price:,.1f}")
        except Exception as e:
            log(f"⚠️ 挂止盈失败: {e}")

# ========== 六、主动出场: pnl检查 ==========

def manage_positions(state):
    """
    主动出场管理 (每2秒):
    LONG:  pnl ≤ -1.5% 止损 / pnl ≥ +2.5% 止盈
    SHORT: pnl ≤ -1.5% 止损 / pnl ≥ +2.5% 止盈
    """
    try:
        positions = binance.fetch_positions()
    except:
        return state

    changed = False

    for pos_key, direction in [('long_pos', 'LONG'), ('short_pos', 'SHORT')]:
        pos = state.get(pos_key)
        if not pos:
            continue

        entry = pos['entry_price']
        qty = pos['qty']

        # 找对应交易所持仓
        side = 'long' if direction == 'LONG' else 'short'
        match = None
        for p in positions:
            if (p.get('symbol') == SYMBOL
                    and float(p.get('contracts', 0)) > 0
                    and p.get('side') == side):
                match = p
                break

        if not match:
            # 交易所已无持仓 → 被动平仓(由check_close处理)
            continue

        mark_price = float(match.get('markPrice', 0))
        if mark_price <= 0:
            continue

        # pnl计算 (价格百分比)
        if direction == 'LONG':
            pnl_pct = (mark_price - entry) / entry * 100
        else:
            pnl_pct = (entry - mark_price) / entry * 100

        # === 止损 ===
        if pnl_pct <= -SL_PCT * 100:
            log(f"🛑 {direction}触发主动止损 | pnl={pnl_pct:+.2f}% | 标记价=${mark_price:,.1f}")
            try:
                close_side = 'sell' if direction == 'LONG' else 'buy'
                binance.create_order(
                    SYMBOL, 'market', close_side, qty,
                    params={'positionSide': direction, 'reduceOnly': True}
                )
                send_wechat(
                    f"🛑 BTC v4.1 主动止损\n"
                    f"{direction} {qty}BTC @ ${entry:,.2f}\n"
                    f"触发价: ${mark_price:,.1f} | pnl: {pnl_pct:+.2f}%\n"
                    f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                )
                work_log('主动止损', f'{direction} {qty}BTC @ {entry} | pnl={pnl_pct:+.2f}%')
                state[pos_key] = None
                state[f'last_{direction.lower()}_signal'] = False
                changed = True
            except Exception as e:
                log(f"❌ 主动止损失败: {e}")

        # === 止盈 ===
        elif pnl_pct >= TP_PCT * 100:
            log(f"🎯 {direction}触发主动止盈 | pnl={pnl_pct:+.2f}% | 标记价=${mark_price:,.1f}")
            try:
                close_side = 'sell' if direction == 'LONG' else 'buy'
                binance.create_order(
                    SYMBOL, 'market', close_side, qty,
                    params={'positionSide': direction, 'reduceOnly': True}
                )
                send_wechat(
                    f"🎯 BTC v4.1 主动止盈\n"
                    f"{direction} {qty}BTC @ ${entry:,.2f}\n"
                    f"触发价: ${mark_price:,.1f} | pnl: {pnl_pct:+.2f}%\n"
                    f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                )
                work_log('主动止盈', f'{direction} {qty}BTC @ {entry} | pnl={pnl_pct:+.2f}%')
                state[pos_key] = None
                state[f'last_{direction.lower()}_signal'] = False
                changed = True
            except Exception as e:
                log(f"❌ 主动止盈失败: {e}")

    if changed:
        save_state(state)
    return state

# ========== 七、被动平仓检测 ==========

def check_close(state):
    """检查被动平仓(SL/TP触发或手动)，同步state"""
    try:
        positions = binance.fetch_positions()
    except:
        return state

    long_alive = any(
        p.get('symbol') == SYMBOL and float(p.get('contracts', 0)) > 0
        and p.get('side') == 'long'
        for p in positions
    )
    short_alive = any(
        p.get('symbol') == SYMBOL and float(p.get('contracts', 0)) > 0
        and p.get('side') == 'short'
        for p in positions
    )

    changed = False

    if state.get('long_pos') and not long_alive:
        pos = state['long_pos']
        log(f"📤 LONG已平仓 | 开仓=${pos['entry_price']:,.2f}")
        send_wechat(f"✅ BTC v4.1 平仓\nLONG {pos['qty']}BTC @ ${pos['entry_price']:,.2f}\n⏰ {datetime.now().strftime('%H:%M:%S')}")
        work_log('平仓', f"LONG {pos['qty']}BTC @ {pos['entry_price']}")
        state['long_pos'] = None
        state['last_long_signal'] = False
        changed = True

    if state.get('short_pos') and not short_alive:
        pos = state['short_pos']
        log(f"📤 SHORT已平仓 | 开仓=${pos['entry_price']:,.2f}")
        send_wechat(f"✅ BTC v4.1 平仓\nSHORT {pos['qty']}BTC @ ${pos['entry_price']:,.2f}\n⏰ {datetime.now().strftime('%H:%M:%S')}")
        work_log('平仓', f"SHORT {pos['qty']}BTC @ {pos['entry_price']}")
        state['short_pos'] = None
        state['last_short_signal'] = False
        changed = True

    if changed:
        save_state(state)
    return state

# ========== 八、状态打印 ==========

def print_status(r5, r1, r4, rd, state, signal, reason):
    price  = r5['price']
    sma5   = r5['sma20']
    rsi5   = r5['rsi']
    vol5   = r5['vol_ratio']
    adx_1h = r1['adx_closed']
    adx_4h = r4['adx_closed']
    c4h    = r4['close_closed']
    s4h    = r4['sma_closed']
    c1d    = rd['close_closed']
    s1d    = rd['sma_closed']

    pct_sma = (price - sma5) / sma5 * 100
    trend4 = '多' if c4h > s4h else '空'
    trend1 = '多' if c1d > s1d else '空'

    long_pos = state.get('long_pos')
    short_pos = state.get('short_pos')
    pos_str = ''
    if long_pos: pos_str += f'🟢L {long_pos["qty"]}BTC @ ${long_pos["entry_price"]:,.0f} '
    if short_pos: pos_str += f'🔴S {short_pos["qty"]}BTC @ ${short_pos["entry_price"]:,.0f} '
    if not pos_str: pos_str = '空仓'

    sig_str = f'🎯{signal}' if signal else '⏳无'
    obs_str = reason if isinstance(reason, str) and not signal else ''

    print(f"""
╔════════════════════════════════════════════╗
║  BTC v4.1 趋势回调  |  {pos_str}
╠════════════════════════════════════════════╣
║  💰 ${price:>12,.2f}  | 距SMA5 {pct_sma:+.1f}%  | RSI {rsi5:.1f}         ║
║  4h闭K: ${c4h:,.0f} vs SMA${s4h:,.0f} → {trend4}头          ║
║  1d闭K: ${c1d:,.0f} vs SMA${s1d:,.0f} → {trend1}头          ║
║  1hADX: {adx_1h:.1f}  | 4hADX: {adx_4h:.1f}  | 量比: {vol5:.1f}x           ║
║  信号: {sig_str}  | {obs_str} ║
╚════════════════════════════════════════════╝""")
    # 简洁版日志
    log(f"${price:,.0f} | 5mSMA{sma5:,.0f}({pct_sma:+.1f}%) | RSI{rsi5:.1f} | 1hADX{adx_1h:.1f} 4hADX{adx_4h:.1f} | 量{vol5:.1f}x | 4h闭{c4h:,.0f}/SMA{s4h:,.0f}| 1d闭{c1d:,.0f}/SMA{s1d:,.0f} | {sig_str}")

# ========== 九、主循环 ==========

def main():
    log("═══════════ BTC v4.1 启动 ═══════════")
    log(f"QTY={QTY} | LEV={LEVERAGE}x | TP=+{TP_PCT*100}% SL=-{SL_PCT*100}%")
    log(f"LONG: 4h多+1d多(闭K) + 5mSMA±1% + 1hADX>25(闭) + 4hADX<40(闭) + RSI>40 + 量≥1.0(闭)")
    log(f"SHORT: 4h空+1d空(闭K) + 5mSMA±1% + 1hADX>25(闭) + 4hADX<40(闭) + RSI<60 + 量≥1.0(闭)")

    # 启动时同步持仓状态
    state = load_state()
    try:
        positions = binance.fetch_positions()
        long_alive = any(
            p.get('symbol') == SYMBOL and float(p.get('contracts', 0)) > 0
            and p.get('side') == 'long'
            for p in positions
        )
        short_alive = any(
            p.get('symbol') == SYMBOL and float(p.get('contracts', 0)) > 0
            and p.get('side') == 'short'
            for p in positions
        )
        if not long_alive and state.get('long_pos'):
            log("⚠️ 交易所无LONG仓，清除state")
            state['long_pos'] = None
        if not short_alive and state.get('short_pos'):
            log("⚠️ 交易所无SHORT仓，清除state")
            state['short_pos'] = None
        save_state(state)
    except Exception as e:
        log(f"⚠️ 启动同步异常: {e}")

    cycle = 0
    while True:
        try:
            cycle += 1

            # ① 获取数据
            k5m, k1h, k4h, k1d = get_data()

            # ② 计算指标 (4个独立函数)
            r5 = calc_5m(k5m)
            r1 = calc_1h(k1h)
            r4 = calc_4h(k4h)
            rd = calc_1d(k1d)

            if any(v is None for v in [r5, r1, r4, rd]):
                if cycle % 6 == 0:
                    log("⚠️ 数据不足，跳过")
                time.sleep(POLL_INTERVAL)
                continue

            # ③ 主动检查平仓 + 被动平仓检测
            state = manage_positions(state)
            state = check_close(state)

            # ④ 信号判断 (短路)
            signal, reason = check_entry(r5, r1, r4, rd)

            # ⑤ 入场逻辑 (同向跳过)
            long_signal_now = (signal == 'LONG')
            short_signal_now = (signal == 'SHORT')

            long_new = long_signal_now and not state.get('last_long_signal', False)
            short_new = short_signal_now and not state.get('last_short_signal', False)

            if long_new and not state.get('long_pos'):
                log("🎯 LONG信号触发!")
                do_open('LONG', reason)
                state = load_state()

            if short_new and not state.get('short_pos'):
                log("🎯 SHORT信号触发!")
                do_open('SHORT', reason)
                state = load_state()

            # ⑥ 更新信号状态
            state['last_long_signal'] = long_signal_now
            state['last_short_signal'] = short_signal_now
            save_state(state)

            # ⑦ 打印状态
            print_status(r5, r1, r4, rd, state, signal, reason)

            # ⑧ 每60秒补挂SL/TP
            if cycle % 30 == 0:
                for pos_key, direction in [('long_pos', 'LONG'), ('short_pos', 'SHORT')]:
                    pos = state.get(pos_key)
                    if pos:
                        ensure_sl_tp(direction, pos['qty'], pos['sl'], pos['tp'])

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            log("═══════════ 手动停止 ═══════════")
            break
        except Exception as e:
            log(f"❌ 主循环异常: {e}")
            time.sleep(5)

if __name__ == '__main__':
    main()
