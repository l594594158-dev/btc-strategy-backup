#!/usr/bin/env python3
"""
BTC合约 自动交易策略 v2.14
- 2秒轮询 + 多周期指标分析
- 自定义止盈止损
- 开仓理由记录 + 微信通知
- v2.14: 开仓价格验证(做空>100根低点1.01x,做多<100根高点0.99x)
- v2.13: 轮询2秒, 移动止盈保持5秒
- v2.11.9: 深度修复幽灵仓位bug - 交易所与state一致性验证 - 旧格式reason标记为bot_recovered
"""
import ccxt
import pandas as pd
import ta
import time
import json
import os
import subprocess
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
QTY = 0.030
LEVERAGE = 20
STATE_FILE = '/root/.openclaw/workspace/btc-strategy-task/databases/state.json'
ALERT_FILE = '/root/.openclaw/workspace/btc-strategy-task/databases/last_alert.json'
WORK_LOG = '/root/.openclaw/workspace/btc-strategy-task/logs/work_log.txt'
STATS_FILE = '/root/.openclaw/workspace/btc-strategy-task/databases/trade_stats.json'

# ========== v2.0 新增风控参数 ==========
MAX_CONSECUTIVE_LOSS = 3      # 连续亏损达到此数则暂停交易
LOSS_COOLDOWN_MINUTES = 30    # 连续亏损后冷却时间（分钟）
MIN_RSI_SHORT = 82            # 做空最低RSI要求（更极端才进）
MIN_RSI_LONG = 35              # 做多最高RSI要求
STOP_LOSS_PCT = 3.0 / 100     # 止损百分比（3.0%）
TAKE_PROFIT_PCT = 5.0 / 100   # 止盈百分比（5%，全仓一次性）
MAX_POSITIONS_PER_DIR = 3     # 单方向最大仓位数量（v2.8）
MAX_TOTAL_QTY = 0.12            # v2.14.3: 单方向最大总持仓量(BTC)，替代无效的仓位计数

# ========== 轮询间隔 ==========
POLL_INTERVAL = 2                   # 价格/信号轮询间隔（秒）

# ========== v2.9: 移动止盈参数（集成进主策略）==========
TRAIL_ACTIVATION_PCT = 1.0 / 100   # 激活条件：超出开仓价1.0%
TRAIL_TRIGGER_PCT = 0.6 / 100      # 执行条件：从峰值回落0.6%
TRAIL_INTERVAL = 5                  # 移动止盈检查间隔（秒）
PRICE_VALIDATION_PCT = 1.5 / 100    # v2.14.1: 开仓价格验证阈值（1.5%）

# ========== 工具 ==========
def validate_entry_price(direction, entry_price):
    """
    v2.14: 开仓价格保护
    - 做空: 开仓价需 > 100根5m最低价 × 1.01（不在底部做空）
    - 做多: 开仓价需 < 100根5m最高价 × 0.99（不在顶部做多）
    返回: (valid: bool, reason: str)
    """
    try:
        k5m = binance.fetch_ohlcv(SYMBOL, '5m', limit=100)
        highs = [x[2] for x in k5m]
        lows = [x[3] for x in k5m]
        h100 = max(highs)
        l100 = min(lows)
        if direction == 'short':
            threshold = l100 * (1 + PRICE_VALIDATION_PCT)
            if entry_price > threshold:
                return True, f"${entry_price:,.0f} > {l100:.0f}×1.01=${threshold:,.0f} ✅"
            else:
                return False, f"${entry_price:,.0f} ≤ {l100:.0f}×1.01=${threshold:,.0f} ❌ 距底部太近"
        else:  # long
            threshold = h100 * (1 - PRICE_VALIDATION_PCT)
            if entry_price < threshold:
                return True, f"${entry_price:,.0f} < {h100:.0f}×0.99=${threshold:,.0f} ✅"
            else:
                return False, f"${entry_price:,.0f} ≥ {h100:.0f}×0.99=${threshold:,.0f} ❌ 距顶部太近"
    except Exception as e:
        log(f"⚠️ 价格验证异常，放行: {e}")
        return True, "验证异常，放行"

# ========== 工具 ==========
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def work_log(event_type, detail):
    """写结构化工作日志（开仓/平仓/错误）"""
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] [{event_type}] {detail}\n"
    with open(WORK_LOG, 'a') as f:
        f.write(line)

def save_state(state):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)
        # v2.13: 验证写入成功
        with open(STATE_FILE) as f:
            verified = json.load(f)
        if 'positions' in verified and len(verified.get('positions', [])) != len(state.get('positions', [])):
            log(f"❌ save_state验证失败！期望{len(state.get('positions',[]))}仓，实际{len(verified.get('positions',[]))}仓，重试...")
            raise Exception("save_state verification mismatch")
        log(f"💾 state已写入: in_position={state.get('in_position')} positions={len(state.get('positions',[]))}仓")
    except Exception as e:
        log(f"❌ save_state失败: {e}，重试...")
        import time as time_module
        time_module.sleep(0.5)
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)
        log(f"✅ save_state重试成功")

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            s = json.load(f)
        # v2.11.9: 兼容旧格式（单仓位），自动升级为positions数组
        # 如果reason是"幽灵仓位同步"或空，说明是旧版遗留的仓位，标记为bot_recovered
        old_reason = s.get('reason', '')
        if old_reason in ('幽灵仓位同步', '', None):
            old_reason = 'bot_recovered'
        if 'positions' not in s and s.get('in_position') and s.get('entry_price', 0) > 0:
            s['positions'] = [{
                'entry_price': s['entry_price'],
                'qty': s.get('qty', QTY),
                'direction': s.get('direction', 'long'),
                'stop_loss': s.get('stop_loss', 0),
                'tp': s.get('tp1', s.get('tp', 0)),
                'sl_algo_id': s.get('sl_algo_id'),
                'tp_algo_id': s.get('tp_algo_id'),
                'reason': old_reason,
                'atr': s.get('atr', 0),
                'open_time': s.get('open_time', ''),
            }]
        if 'positions' not in s:
            s['positions'] = []
        # v2.11.12: 修复幽灵仓位标签 - 如果positions数组中有遗留的'幽灵仓位同步'标签，同步更新为bot_recovered
        for pos in s['positions']:
            if pos.get('reason', '') in ('幽灵仓位同步', '', None):
                pos['reason'] = 'bot_recovered'
        return s
    return {'in_position': False, 'positions': []}

def load_stats():
    """加载交易统计（含连续亏损计数）"""
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE) as f:
            return json.load(f)
    return {'consecutive_losses': 0, 'total_trades': 0, 'last_loss_time': None}

def save_stats(stats):
    with open(STATS_FILE, 'w') as f:
        json.dump(stats, f)

NOTIFY_QUEUE = '/root/.openclaw/workspace/btc-strategy-task/databases/notify_queue.json'

def notify_alert(msg):
    """写入告警文件，供agent推送微信"""
    with open(ALERT_FILE, 'w') as f:
        json.dump({'time': datetime.now().isoformat(), 'msg': msg}, f)

def send_wechat_msg(msg):
    """直接发送微信消息 - 写入notify_queue供heartbeat发送"""
    try:
        import urllib.request
        with open(NOTIFY_QUEUE, 'w') as f:
            json.dump({'time': datetime.now().isoformat(), 'msg': msg, 'sent': False}, f)
    except:
        pass

def get_data():
    # v2.11.14: K线数据获取 - 只用limit=200，让Binance返回最近200根（足够任何指标）
    # 200 x 5m = 16.7小时，200 x 1h = 8.3天，200 x 4h = 33天，200 x 1d = 200天
    # 稳定可靠，避免since参数配合小lookback导致数据不足的问题
    k5m = binance.fetch_ohlcv(SYMBOL, timeframe='5m', limit=200)
    k1h = binance.fetch_ohlcv(SYMBOL, timeframe='1h', limit=200)
    k4h = binance.fetch_ohlcv(SYMBOL, timeframe='4h', limit=200)
    k1d = binance.fetch_ohlcv(SYMBOL, timeframe='1d', limit=200)
    return k5m, k1h, k4h, k1d

def calc(df):
    close = df['c']
    high = df['h']
    low = df['l']
    volume = df['v']  # 成交量
    lv = len(df) - 1

    # ========== v2.11.1: 数据量不足时直接跳过（修复ATR崩溃）==========
    if len(df) < 14:
        return None

    ma7 = ta.trend.SMAIndicator(close, 7).sma_indicator().iloc[lv]
    ma25 = ta.trend.SMAIndicator(close, 25).sma_indicator().iloc[lv]
    macd_ind = ta.trend.MACD(close)
    macd = macd_ind.macd().iloc[lv]
    macd_sig = macd_ind.macd_signal().iloc[lv]
    rsi = ta.momentum.RSIIndicator(close).rsi().iloc[lv]
    bb = ta.volatility.BollingerBands(close)
    bb_u = bb.bollinger_hband().iloc[lv]
    bb_l = bb.bollinger_lband().iloc[lv]
    bb_m = bb.bollinger_mavg().iloc[lv]
    atr = ta.volatility.AverageTrueRange(high, low, close).average_true_range().iloc[lv]
    price = close.iloc[lv]
    pctb = (price - bb_l) / (bb_u - bb_l) if (bb_u - bb_l) != 0 else 0.5

    # ========== 新增优化1: ADX趋势强度 ==========
    # ADX > 25 表示市场有趋势，< 25 震荡市指标信号易失效
    try:
        adx_ind = ta.trend.ADXIndicator(high, low, close, window=14)
        adx = adx_ind.adx().iloc[lv]
        adx_pos = adx_ind.adx_pos().iloc[lv]
        adx_neg = adx_ind.adx_neg().iloc[lv]
    except Exception as e:
        adx = 25  # 默认放行
        adx_pos = 25
        adx_neg = 25

    # ========== 新增优化2: 成交量确认 ==========
    # 当前成交量 > 近20根K线平均量的1.5倍 = 放量
    avg_volume = volume.iloc[max(0, lv-20):lv+1].mean()
    current_volume = volume.iloc[lv]
    volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1
    is_volume_surge = volume_ratio > 1.5

    # ========== 趋势判断：价格为主，NaN时降级 ==========
    # price > MA7 = 多头，price < MA7 = 空头（更稳定）
    # MACD/Signal仅作辅助参考（NaN时才降级）
    import math
    if math.isnan(macd) or math.isnan(macd_sig):
        macd_bullish = price > ma7
        macd_bearish = price < ma7
    else:
        macd_bullish = price > ma7
        macd_bearish = price < ma7

    return {
        'price': price, 'ma7': ma7, 'ma25': ma25,

        'macd': macd, 'macd_sig': macd_sig, 'rsi': rsi,
        'bb_u': bb_u, 'bb_l': bb_l, 'bb_m': bb_m,
        'atr': atr, 'pctb': pctb,
        'adx': adx, 'adx_pos': adx_pos, 'adx_neg': adx_neg,
        'volume_ratio': volume_ratio, 'is_volume_surge': is_volume_surge,
        'bullish': macd_bullish,
        'bearish': macd_bearish
    }

def check_entry(data):
    """
    v2.0 开仓信号分析 + 理由说明
    优化:
    - 做空增加RSI>=82确认（不只是%b>0.85）
    - 做多增加RSI确认（回调到位才进）
    - 止损改为2.5×ATR
    - 止盈分3批
    
    v2.1 新增优化:
    - 成交量确认：放量(>1.5x均值)才有效
    - ADX过滤：ADX>25市场有趋势才操作
    - 动态止盈：根据ATR波动率动态调整
    """
    r5m = data['5m']
    r1h = data['1h']
    r4h = data['4h']
    rd  = data['1d']

    price = r5m['price']
    atr = r5m['atr']  # 统一用5m ATR（和ADX同一周期）
    pctb = r5m['pctb']
    rsi5m = r5m['rsi']
    rsi1h = r1h['rsi']
    adx5m = r5m['adx']
    adx1h = r1h['adx']
    vol_ratio = r5m['volume_ratio']

    # ========== 通用过滤条件 ==========
    # 注: ADX<25的震荡市不再过滤全部信号，仅过滤顺势信号(做多B/做空A)，
    # 逆势信号(做多A/做空B/震荡做多/震荡做空)在ADX低时反而是最佳均值回归时机

    # 过滤: 必须放量（所有信号共享）
    if not r5m['is_volume_surge']:
        observe = f"观望 | 成交量缩量(vol={vol_ratio:.1f}x)"
        return None, observe, price, atr

    # ========== 主周期 ADX 趋势强度（方案2新增）============
    adx4h = r4h['adx']
    adx1d = rd['adx']

    # === 做多分析：大周期空头 + 5m超卖反弹（逆势信号，需4h ADX不能太高）===
    if not r4h['bullish'] and not rd['bullish'] and pctb <= 0.15:
        # 方案2：逆势信号需趋势不能过强，4h ADX > 40 说明空头趋势很强，不做逆势
        if adx4h >= 40:
            observe = f"观望 | 4h ADX={adx4h:.1f}>=40 空头趋势过强，逆势做多风险大"
            return None, observe, price, atr

        bb_l = r5m['bb_l']
        dist = (price - bb_l) / price * 100

        # RSI也进入深度超卖区间才进（确认真超卖）
        if rsi5m < 25:
            sl = price * (1 - STOP_LOSS_PCT)
            tp1 = price * (1 + TAKE_PROFIT_PCT)

            entry_reason = (
                f"【做多-v2.2】大周期空头+5m超卖反弹\n"
                f"理由: 4h+1d均线空头,价格跌至布林下轨偏离{dist:.1f}%\n"
                f"5m %b={pctb:.3f} + RSI={rsi5m:.1f} 双超卖确认\n"
                f"1h ADX={adx1h:.1f}>25主趋势确认 | 放量({vol_ratio:.1f}x)\n"
                f"4h ADX={adx4h:.1f}<40空头趋势未过强\n"
                f"固定止盈止损(百分比)\n"
                f"入场: ${price:,.2f}\n"
                f"止损: ${sl:,.2f} (-{STOP_LOSS_PCT*100:.1f}%)\n"
                f"止盈: ${tp1:,.2f} (+{TAKE_PROFIT_PCT*100:.1f}%) 全仓一次性"
            )
            return 'long', entry_reason, price, atr

    # === 震荡做多：弱趋势+5m超卖反弹（均值回归）===
    # v2.4新增: ADX<25说明趋势很弱，价格到布林下轨+RSI低迷是最佳均值回归做多机会
    if adx1h < 25 and not r4h['bullish'] and not rd['bullish'] and pctb <= 0.15 and rsi5m <= 30:
        bb_l = r5m['bb_l']
        dist = (price - bb_l) / price * 100
        sl = price * (1 - STOP_LOSS_PCT)
        tp1 = price * (1 + TAKE_PROFIT_PCT)

        entry_reason = (
            f"【做多-v2.4】震荡市+5m超卖反弹(均值回归)\n"
            f"理由: 4h+1d均线空头但ADX={adx1h:.1f}<25趋势极弱\n"
            f"价格触及布林下轨偏离{dist:.1f}%\n"
            f"5m %b={pctb:.3f} + RSI={rsi5m:.1f} 双超卖确认(RSI<=30)\n"
            f"1h ADX<25确认震荡市，逆势均值回归概率高\n"
            f"放量({vol_ratio:.1f}x)确认\n"
            f"固定止盈止损(百分比)\n"
            f"入场: ${price:,.2f}\n"
            f"止损: ${sl:,.2f} (-{STOP_LOSS_PCT*100:.1f}%)\n"
            f"止盈: ${tp1:,.2f} (+{TAKE_PROFIT_PCT*100:.1f}%) 全仓一次性"
        )
        return 'long', entry_reason, price, atr

    # === 做多分析：大周期多头 + 回调支撑（顺势信号）===
    if adx1h > 25 and r4h['bullish'] and rd['bullish'] and pctb <= 0.15:
        if rsi5m >= 30 and rsi5m <= 40:
            bb_l = r5m['bb_l']
            dist = (price - bb_l) / price * 100
            sl = price * (1 - STOP_LOSS_PCT)
            tp1 = price * (1 + TAKE_PROFIT_PCT)

            entry_reason = (
                f"【做多-v2.2】大周期多头+5m回调支撑\n"
                f"理由: 4h+1d均线多头,价格回踩布林下轨偏离{dist:.1f}%\n"
                f"5m RSI={rsi5m:.1f} 回调到位(30~40区间)\n"
                f"1h ADX={adx1h:.1f}>25主趋势确认 | 放量({vol_ratio:.1f}x)\n"
                f"固定止盈止损(百分比)\n"
                f"入场: ${price:,.2f}\n"
                f"止损: ${sl:,.2f} (-{STOP_LOSS_PCT*100:.1f}%)\n"
                f"止盈: ${tp1:,.2f} (+{TAKE_PROFIT_PCT*100:.1f}%) 全仓一次性"
            )
            return 'long', entry_reason, price, atr

    # === 做空分析：大周期多头 + 5m超买（顺势信号）===
    # v2.4: 增加4h ADX<40限制，趋势过强时不做顺势摸顶
    # v2.0优化: 同时要求RSI>=MIN_RSI_SHORT(82)，不只是%b>0.85
    if r4h['bullish'] and rd['bullish'] and pctb > 0.85 and rsi5m >= MIN_RSI_SHORT and adx4h < 40:
        bb_u = r5m['bb_u']
        dist = (bb_u - price) / price * 100
        sl = price * (1 + STOP_LOSS_PCT)
        tp1 = price * (1 - TAKE_PROFIT_PCT)

        entry_reason = (
            f"【做空-v2.2】大周期多头+5m极强超买\n"
            f"理由: 4h+1d均线多头,价格触及布林上轨偏离{dist:.1f}%\n"
            f"5m %b={pctb:.3f} + RSI={rsi5m:.1f} 双超买确认(RSI>={MIN_RSI_SHORT})\n"
            f"4h ADX={adx4h:.1f}<40趋势未过强 | 放量({vol_ratio:.1f}x)\n"
            f"固定止盈止损(百分比)\n"
            f"入场: ${price:,.2f}\n"
            f"止损: ${sl:,.2f} (+{STOP_LOSS_PCT*100:.1f}%)\n"
            f"止盈: ${tp1:,.2f} (-{TAKE_PROFIT_PCT*100:.1f}%) 全仓一次性"
        )
        return 'short', entry_reason, price, atr

    # === 震荡做空：弱趋势+5m极强超买（均值回归）===
    # v2.4新增: ADX<25说明趋势很弱，价格到布林上轨+RSI高企是最佳均值回归做空机会
    if adx1h < 25 and r4h['bullish'] and rd['bullish'] and pctb > 0.85 and rsi5m >= 70:
        bb_u = r5m['bb_u']
        dist = (bb_u - price) / price * 100
        sl = price * (1 + STOP_LOSS_PCT)
        tp1 = price * (1 - TAKE_PROFIT_PCT)

        entry_reason = (
            f"【做空-v2.4】震荡市+5m极强超买(均值回归)\n"
            f"理由: 4h+1d均线多头但ADX={adx1h:.1f}<25趋势极弱\n"
            f"价格触及布林上轨偏离{dist:.1f}%\n"
            f"5m %b={pctb:.3f} + RSI={rsi5m:.1f} 双超买确认(RSI>=70)\n"
            f"1h ADX<25确认震荡市，逆势均值回归概率高\n"
            f"放量({vol_ratio:.1f}x)确认\n"
            f"固定止盈止损(百分比)\n"
            f"入场: ${price:,.2f}\n"
            f"止损: ${sl:,.2f} (+{STOP_LOSS_PCT*100:.1f}%)\n"
            f"止盈: ${tp1:,.2f} (-{TAKE_PROFIT_PCT*100:.1f}%) 全仓一次性"
        )
        return 'short', entry_reason, price, atr

    # === 做空分析：大周期空头 + 5m反弹压力（逆势信号，需4h ADX不能太高）===
    if not r4h['bullish'] and not rd['bullish'] and pctb > 0.85 and rsi5m >= MIN_RSI_SHORT:
        # 方案2：逆势信号需趋势不能过强，4h ADX > 40 说明空头趋势很强，反弹做空风险大
        if adx4h >= 40:
            observe = f"观望 | 4h ADX={adx4h:.1f}>=40 空头趋势过强，反弹做空风险大"
            return None, observe, price, atr

        bb_u = r5m['bb_u']
        dist = (bb_u - price) / price * 100
        sl = price * (1 + STOP_LOSS_PCT)
        tp1 = price * (1 - TAKE_PROFIT_PCT)

        entry_reason = (
            f"【做空-v2.2】大周期空头+5m反弹压力\n"
            f"理由: 4h+1d均线空头,价格反弹至布林上轨偏离{dist:.1f}%\n"
            f"5m %b={pctb:.3f} + RSI={rsi5m:.1f} 双超买确认(RSI>={MIN_RSI_SHORT})\n"
            f"1h ADX={adx1h:.1f}>25主趋势确认 | 放量({vol_ratio:.1f}x)\n"
            f"4h ADX={adx4h:.1f}<40空头趋势未过强\n"
            f"固定止盈止损(百分比)\n"
            f"入场: ${price:,.2f}\n"
            f"止损: ${sl:,.2f} (+{STOP_LOSS_PCT*100:.1f}%)\n"
            f"止盈: ${tp1:,.2f} (-{TAKE_PROFIT_PCT*100:.1f}%) 全仓一次性"
        )
        return 'short', entry_reason, price, atr

    # 观望
    observe = f"观望 | 4h{'多头' if r4h['bullish'] else '空头'} | %b={pctb:.3f} | RSI={rsi5m:.1f} | 1h ADX={adx1h:.1f} | vol={vol_ratio:.1f}x"
    return None, observe, price, atr

def place_sl_tp_for_entry(direction, entry_price, qty, reason, atr):
    """
    v2.9.4 为单个仓位挂独立的SL/TP（幂等挂单，不重复）
    挂单前先查已有订单，同价格+同数量则跳过，避免重复挂单
    返回 (sl_price, tp_price, sl_algo_id, tp_algo_id)
    """
    positionSide = 'LONG' if direction == 'long' else 'SHORT'
    close_side = 'sell' if direction == 'long' else 'buy'

    sl_price = round(entry_price * (1 - STOP_LOSS_PCT), 1) if direction == 'long' else round(entry_price * (1 + STOP_LOSS_PCT), 1)
    tp_price = round(entry_price * (1 + TAKE_PROFIT_PCT), 1) if direction == 'long' else round(entry_price * (1 - TAKE_PROFIT_PCT), 1)

    # ========== v2.9.4: 幂等挂单——先查已有订单，同价格+同数量则跳过 ==========
    active_algos = [
        o for o in binance.fapiprivate_get_openalgoorders({'symbol': 'BTCUSDT'})
        if o.get('algoStatus') not in ('CANCELED', 'FINISHED', 'EXPIRED', None)
    ]

    # 检查SL是否已存在（同价格±1, 同数量±1%）
    side_filter = 'SELL' if direction == 'long' else 'BUY'
    sl_exists = any(
        abs(float(o.get('triggerPrice', 0)) - sl_price) <= 1 and
        abs(float(o.get('quantity', 0)) - qty) <= qty * 0.01
        for o in active_algos
        if o.get('orderType') == 'STOP_MARKET' and o.get('side') == side_filter
    )
    tp_exists = any(
        abs(float(o.get('triggerPrice', 0)) - tp_price) <= 1 and
        abs(float(o.get('quantity', 0)) - qty) <= qty * 0.01
        for o in active_algos
        if o.get('orderType') == 'TAKE_PROFIT_MARKET' and o.get('side') == side_filter
    )

    sl_algo_id = None
    tp_algo_id = None

    if not sl_exists:
        try:
            sl_order = binance.create_order(
                SYMBOL, 'STOP_MARKET',
                close_side, qty,
                params={'stopPrice': sl_price, 'positionSide': positionSide, 'newOrderRespType': 'FULL'}
            )
            sl_algo_id = sl_order.get('info', {}).get('algoId') or sl_order.get('id')
            log(f"✅ 止损单已挂: SL=${sl_price} x {qty} BTC, algoId={sl_algo_id}")
        except Exception as e:
            log(f"⚠️ 止损单挂单失败: {e}")
    else:
        log(f"⏭️ 止损单已存在 (SL=${sl_price} x {qty} BTC)，跳过")

    if not tp_exists:
        try:
            tp_order = binance.create_order(
                SYMBOL, 'TAKE_PROFIT_MARKET',
                close_side, qty,
                params={'stopPrice': tp_price, 'positionSide': positionSide, 'newOrderRespType': 'FULL'}
            )
            tp_algo_id = tp_order.get('info', {}).get('algoId') or tp_order.get('id')
            log(f"✅ 止盈单已挂: TP=${tp_price} x {qty} BTC, algoId={tp_algo_id}")
        except Exception as e:
            log(f"⚠️ 止盈单挂单失败: {e}")
    else:
        log(f"⏭️ 止盈单已存在 (TP=${tp_price} x {qty} BTC)，跳过")

    return sl_price, tp_price, sl_algo_id, tp_algo_id


def cancel_all_sl_tp_for_direction(direction):
    """撤销指定方向的所有SL/TP条件单"""
    positionSide = 'LONG' if direction == 'long' else 'SHORT'
    try:
        # 查询该方向的所有条件单
        algos = binance.fapiprivate_get_openalgoorders({'symbol': 'BTCUSDT'})
        active_algos = [o for o in algos if o.get('algoStatus') == 'NEW']
        for o in active_algos:
            o_ps = o.get('positionSide', '')
            o_type = o.get('orderType', '')
            if o_ps == positionSide and o_type in ('STOP_MARKET', 'TAKE_PROFIT_MARKET'):
                try:
                    algo_id = int(o.get('algoId'))
                    binance.fapiPrivateDeleteAlgoOrder({'symbol': 'BTCUSDT', 'algoId': algo_id})
                    log(f"  🗑️ 撤销旧条件单: algoId={algo_id} {o_type} @ {o.get('triggerPrice')}")
                except Exception as e:
                    log(f"  ⚠️ 撤销失败 algoId={o.get('algoId')}: {e}")
    except Exception as e:
        log(f"⚠️ 查询条件单失败: {e}")


def open_position(direction, entry_price, atr, reason, qty):
    """v2.10 开仓 + 补仓时撤销旧SL/TP，以新均价重新挂单"""
    positionSide = 'LONG' if direction == 'long' else 'SHORT'
    close_side = 'sell' if direction == 'long' else 'buy'

    binance.set_leverage(LEVERAGE, SYMBOL)

    # ========== v2.12: 所有仓位之间必须保持1.5%间隔（不分方向）==========
    # 开仓前检查所有现有持仓，新仓价格与任何现有仓的偏离都必须>1.5%
    exchange_pos = [p for p in binance.fetch_positions()
                    if p.get('symbol') == SYMBOL and float(p.get('contracts', 0)) > 0]
    
    new_order = None  # 先不下单，等间隔验证通过再下
    candidate_price = entry_price  # 预估价格（实际以下单后成交价为准）
    
    if exchange_pos:
        # 有持仓时：验证间隔，不合格则拒绝开仓（不先下单）
        for ep in exchange_pos:
            existing_entry = float(ep['entryPrice'])
            existing_dir = ep.get('side', '').lower()
            
            # 计算价格间隔（两个方向都要满足1.5%）
            if direction == 'long':
                # 新仓做多：检查是否比现有仓够低（对做多有利）
                gap_pct = (existing_entry - candidate_price) / existing_entry * 100
            else:
                # 新仓做空：检查是否比现有仓够高（对做空有利）
                gap_pct = (candidate_price - existing_entry) / existing_entry * 100
            
            if gap_pct < 1.5:
                log(f"⛔ 开仓间隔不足({gap_pct:.2f}% < 1.5%) | 新仓={candidate_price} 现有仓={existing_entry}({existing_dir})")
                return None
        
        log(f"📊 开仓间隔验证通过，共{len(exchange_pos)}个现有仓")

    # 间隔验证通过，市价开仓
    if direction == 'long':
        order = binance.create_order(SYMBOL, 'market', 'buy', qty, params={'positionSide': positionSide})
    else:
        order = binance.create_order(SYMBOL, 'market', 'sell', qty, params={'positionSide': positionSide})

    avg_price = order.get('average', entry_price)
    filled_qty = float(order.get('filled', qty))
    log(f"✅ 开仓成功: {direction.upper()} +{filled_qty} BTC @ ${avg_price:,.2f}" + (" (首仓)" if not exchange_pos else " (追加)"))

    # ========== v2.12: 补仓时撤销旧SL/TP，以新均价重新挂单 ==========
    total_qty = filled_qty
    if exchange_pos:
        # 有持仓 = 补仓，计算所有仓的平均价
        existing_entries = [(float(p['contracts']), float(p['entryPrice'])) for p in exchange_pos]
        total_value = sum(q * e for q, e in existing_entries) + filled_qty * avg_price
        total_qty = sum(q for q, e in existing_entries) + filled_qty
        new_avg_price = total_value / total_qty

        log(f"📊 补仓后均价: ${new_avg_price:,.2f} | 总数量: {total_qty} BTC")

        # 撤销旧SL/TP
        log(f"🗑️ 撤销所有旧SL/TP条件单...")
        cancel_all_sl_tp_for_direction(direction)

        # 以新均价挂SL/TP
        sl_price = round(new_avg_price * (1 - STOP_LOSS_PCT), 1) if direction == 'long' else round(new_avg_price * (1 + STOP_LOSS_PCT), 1)
        tp_price = round(new_avg_price * (1 + TAKE_PROFIT_PCT), 1) if direction == 'long' else round(new_avg_price * (1 - TAKE_PROFIT_PCT), 1)

        sl_algo_id = None
        tp_algo_id = None

        try:
            sl_order = binance.create_order(
                SYMBOL, 'STOP_MARKET', close_side, total_qty,
                params={'stopPrice': sl_price, 'positionSide': positionSide, 'newOrderRespType': 'FULL'}
            )
            sl_algo_id = sl_order.get('info', {}).get('algoId') or sl_order.get('id')
            log(f"✅ 新止损单已挂: SL=${sl_price} x {total_qty} BTC, algoId={sl_algo_id}")
        except Exception as e:
            log(f"⚠️ 新止损单挂单失败: {e}")

        try:
            tp_order = binance.create_order(
                SYMBOL, 'TAKE_PROFIT_MARKET', close_side, total_qty,
                params={'stopPrice': tp_price, 'positionSide': positionSide, 'newOrderRespType': 'FULL'}
            )
            tp_algo_id = tp_order.get('info', {}).get('algoId') or tp_order.get('id')
            log(f"✅ 新止盈单已挂: TP=${tp_price} x {total_qty} BTC, algoId={tp_algo_id}")
        except Exception as e:
            log(f"⚠️ 新止盈单挂单失败: {e}")

        # 更新state：合并为单仓位
        state = load_state()
        if 'positions' not in state:
            state['positions'] = []
        state['positions'] = [{
            'entry_price': new_avg_price,
            'qty': total_qty,
            'direction': direction,
            'stop_loss': sl_price,
            'tp': tp_price,
            'sl_algo_id': sl_algo_id,
            'tp_algo_id': tp_algo_id,
            'reason': reason,
            'atr': atr,
            'open_time': datetime.now().isoformat(),
            'notified': True,  # 开仓即通知，健康检查不再补发
        }]
        state['in_position'] = True
        save_state(state)
        log(f"📊 state已更新: 均价=${new_avg_price:,.2f}, 数量={total_qty} BTC, SL=${sl_price}, TP=${tp_price}")

        # 发送微信通知（补仓也通知）
        wechat_msg = (
            f"🚨 BTC补仓通知（合并仓）\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"方向: {'🟢【做多-LONG】📈' if direction == 'long' else '🔴【做空-SHORT】📉'}\n"
            f"杠杆: {LEVERAGE}x\n"
            f"数量: +{filled_qty} BTC（合并后共 {total_qty} BTC）\n"
            f"合并均价: ${new_avg_price:,.2f}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"止损: ${sl_price:,.2f} (-{STOP_LOSS_PCT*100:.1f}%)\n"
            f"止盈: ${tp_price:,.2f} (+{TAKE_PROFIT_PCT*100:.1f}%)\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"📋 开仓理由:\n{reason}\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        try:
            with open(NOTIFY_QUEUE, 'w') as f:
                json.dump({'time': datetime.now().isoformat(), 'msg': wechat_msg, 'sent': False}, f)
        except Exception as e:
            log(f"⚠️ 通知写入失败: {e}")
        notify_alert(wechat_msg)

        return

    # 无持仓（首仓），挂独立SL/TP
    sl_price, tp_price, sl_algo_id, tp_algo_id = place_sl_tp_for_entry(direction, avg_price, filled_qty, reason, atr)

    # 更新state：追加到positions列表
    state = load_state()
    if 'positions' not in state:
        state['positions'] = []

    pos_entry = {
        'entry_price': avg_price,
        'qty': filled_qty,
        'direction': direction,
        'stop_loss': sl_price,
        'tp': tp_price,
        'sl_algo_id': sl_algo_id,
        'tp_algo_id': tp_algo_id,
        'reason': reason,
        'atr': atr,
        'open_time': datetime.now().isoformat(),
        'notified': True,  # 开仓即通知，健康检查不再补发
    }
    state['positions'].append(pos_entry)
    state['in_position'] = True
    save_state(state)

    # 发送微信通知
    total_qty = sum(p['qty'] for p in state['positions'])
    wechat_msg = (
        f"🚨 BTC开仓通知（累计{len(state['positions'])}仓）\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"方向: {'🟢【做多-LONG】📈' if direction == 'long' else '🔴【做空-SHORT】📉'}\n"
        f"杠杆: {LEVERAGE}x\n"
        f"数量: +{filled_qty} BTC（合计 {total_qty} BTC）\n"
        f"开仓价: ${avg_price:,.2f}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"止损: ${sl_price:,.2f} (-{STOP_LOSS_PCT*100:.1f}%)\n"
        f"止盈: ${tp_price:,.2f} (+{TAKE_PROFIT_PCT*100:.1f}%)\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📋 开仓理由:\n{reason}\n"
        f"⏰ {datetime.now().strftime('%H:%M:%S')}"
    )
    try:
        with open(NOTIFY_QUEUE, 'w') as f:
            json.dump({'time': datetime.now().isoformat(), 'msg': wechat_msg, 'sent': False}, f)
    except Exception as e:
        log(f"⚠️ 通知写入失败: {e}")
    notify_alert(wechat_msg)

    log(f"✅ 当前共 {len(state['positions'])} 个仓位，合计 {total_qty} BTC")
    return state

    # 发送微信通知
    wechat_msg = (
        f"🚨 BTC开仓通知\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"方向: {'🟢【做多-LONG】📈' if direction == 'long' else '🔴【做空-SHORT】📉'}\n"
        f"杠杆: {LEVERAGE}x\n"
        f"数量: {qty} BTC\n"
        f"开仓价: ${avg_price:,.2f}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"止损: ${sl_price:,.2f} (-{abs(sl_price-avg_price)/avg_price*100:.1f}%)\n"
        f"止盈: ${tp1_price:,.2f} (+{abs(tp1_price-avg_price)/avg_price*100:.1f}%) 全仓\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📋 开仓理由:\n{reason}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"⏰ {datetime.now().strftime('%H:%M:%S')}"
    )
    # 直接写入待发送文件（守护进程检测到此文件后推送微信）
    try:
        with open(NOTIFY_QUEUE, 'w') as f:
            json.dump({'time': datetime.now().isoformat(), 'msg': wechat_msg, 'sent': False}, f)
        log(f"🚨 微信通知已写入待发送队列")
    except Exception as e:
        log(f"⚠️ 通知写入失败: {e}")
    notify_alert(wechat_msg)

    # 工作日志
    work_log("开仓", f"{direction.upper()} | 数量:{qty} | 价格:{avg_price} | SL:{sl_price} | TP1:{tp1_price} | ATR:{atr:.2f} | 理由:{reason[:50]}")

    return state

def close_position():
    """v2.7 平仓（全平）+ 更新连续亏损统计"""
    pos = binance.fetch_positions()
    for p in pos:
        if p.get('symbol') == SYMBOL and float(p.get('contracts', 0)) > 0:
            qty = float(p['contracts'])
            side = p['side']
            positionSide = 'LONG' if side == 'long' else 'SHORT'

            close = binance.create_order(
                SYMBOL, 'market',
                'sell' if side == 'long' else 'buy',
                qty,
                params={'positionSide': positionSide}
            )

            entry = float(p.get('entryPrice', 0))
            close_price = close.get('average', 0)
            pnl = (close_price - entry) * qty if side == 'long' else (entry - close_price) * qty

            # 更新连续亏损计数
            stats = load_stats()
            if pnl < 0:
                stats['consecutive_losses'] += 1
                stats['last_loss_time'] = datetime.now().isoformat()
                log(f"🔴 亏损! 连续亏损次数: {stats['consecutive_losses']}")
                if stats['consecutive_losses'] >= MAX_CONSECUTIVE_LOSS:
                    cooldown_end = time.time() + LOSS_COOLDOWN_MINUTES * 60
                    stats['cooldown_until'] = cooldown_end
                    log(f"🚨 连续亏损{MAX_CONSECUTIVE_LOSS}次，暂停交易{LOSS_COOLDOWN_MINUTES}分钟")
            else:
                stats['consecutive_losses'] = 0
            stats['total_trades'] += 1
            save_stats(stats)

            log(f"✅ 平仓完成! 盈亏: ${pnl:+.4f}")
            work_log("平仓", f"{side.upper()} | 数量:{qty} | 平仓价:{close_price} | 开仓价:{entry} | PnL:{pnl:+.4f}")

            # v2.7：清空所有仓位记录
            save_state({'in_position': False, 'positions': [], 'last_close_time': time.time()})

            wechat_msg = (
                f"✅ BTC平仓通知\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"方向: {side.upper()}\n"
                f"平仓价: ${close_price:,.2f}\n"
                f"盈亏: ${pnl:+.4f}\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"连续亏损: {stats['consecutive_losses']}次\n"
                f"⏰ {datetime.now().strftime('%H:%M:%S')}"
            )
            notify_alert(wechat_msg)
            send_wechat_msg(wechat_msg)
            return True
    return False


def check_and_fix_orders():
    """
    每60秒检查持仓对应的止盈止损单
    发现异常（丢失/被撤销）自动重新挂单
    """
    try:
        pos = binance.fetch_positions()
        has_pos = any(p.get('symbol') == SYMBOL and float(p.get('contracts', 0)) > 0 for p in pos)
        
        if not has_pos:
            return

        for p in pos:
            if p.get('symbol') != SYMBOL or float(p.get('contracts', 0)) <= 0:
                continue

            entry = float(p['entryPrice'])
            qty = float(p['contracts'])
            side = p['side']
            positionSide = 'LONG' if side == 'long' else 'SHORT'
            close_side = 'sell' if side == 'long' else 'buy'

            if side == 'short':
                sl = round(entry * (1 + STOP_LOSS_PCT), 1)
                tp1 = round(entry * (1 - TAKE_PROFIT_PCT), 1)
            else:
                sl = round(entry * (1 - STOP_LOSS_PCT), 1)
                tp1 = round(entry * (1 + TAKE_PROFIT_PCT), 1)

            try:
                open_orders = binance.fetch_open_orders(SYMBOL)
                open_prices = set()
                for o in open_orders:
                    p = o.get('price')
                    sp = o.get('stopPrice')
                    if p:
                        open_prices.add(float(p))
                    if sp:
                        open_prices.add(float(sp))
            except:
                open_prices = set()

            # SL/TP 2个挂单
            order_targets = [
                (sl, qty, '止损', 'STOP'),
                (tp1, qty, '止盈', 'TAKE_PROFIT'),
            ]

            missing = []
            for target_price, target_qty, name, order_type in order_targets:
                if target_price not in open_prices:
                    missing.append((target_price, target_qty, name, order_type))

            if missing:
                log(f"⚠️ 检测到缺失挂单: {[m[0] for m in missing]}")
                for target_price, target_qty, name, order_type in missing:
                    try:
                        o = binance.create_order(
                            SYMBOL, order_type,
                            close_side, target_qty,
                            price=target_price,
                            params={'stopPrice': target_price, 'positionSide': positionSide}
                        )
                        log(f"  ✅ 补挂{name} ${target_price:,.2f} x {target_qty}")
                    except Exception as e:
                        log(f"  ❌ 补挂{name}失败: {e}")
            else:
                log(f"✅ 止盈止损单检查正常 (2/2 全部在挂)")
    except Exception as e:
        log(f"⚠️ 检查修复异常: {e}")


def print_status(data, state):
    r5m = data['5m']
    r1h = data['1h']
    r4h = data['4h']
    rd  = data['1d']
    price = r5m['price']
    pctb = r5m['pctb']
    rsi = r5m['rsi']
    bb_s = "🔴超买" if pctb > 0.85 else "🟢超卖" if pctb < 0.2 else "⚖️正常"
    rsi_s = "🔴" if rsi > 70 else "🟢" if rsi < 30 else "⚖️"

    now = datetime.now().strftime('%H:%M:%S')
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║  BTC AUTO-TRADE  {now}                        ║
╠══════════════════════════════════════════════════════════════╣
║  💰 {price:>12,.2f}  |  ATR: {r1h['atr']:>6,.0f}  |  %b: {pctb:.3f} {bb_s}       ║
╠══════════════════════════════════════════════════════════════╣
║  4h: {'📈' if r4h['bullish'] else '📉'}  |  1d: {'📈' if rd['bullish'] else '📉'}  |  RSI: {rsi_s}{rsi:.1f}        ║""")

    positions = state.get('positions', [])
    if positions and state.get('in_position'):
        # v2.7: 多仓位显示
        total_qty = sum(p['qty'] for p in positions)
        # 计算平均入场价
        total_value = sum(p['entry_price'] * p['qty'] for p in positions)
        avg_entry = total_value / total_qty if total_qty > 0 else 0
        direction = positions[0]['direction']
        d = direction.upper()

        # 按最新仓计算pnl
        latest_entry = positions[-1]['entry_price']
        pnl_pct = (price - latest_entry) / latest_entry * 100 if direction == 'long' else (latest_entry - price) / latest_entry * 100
        pnl_icon = "📈" if pnl_pct > 0 else "📉"

        print(f"╠══════════════════════════════════════════════════════════════╣")
        print(f"║  📊 持仓: {d} x {len(positions)}仓 共{total_qty} BTC  |  {pnl_icon}{pnl_pct:+.2f}%    ║")
        # 显示各仓位的SL/TP
        for i, p in enumerate(positions):
            print(f"║  仓{i+1}: SL${p['stop_loss']:,.0f} TP${p['tp']:,.0f} @ ${p['entry_price']:,.0f} ({p['qty']}BTC)  ║")
        print(f"╠══════════════════════════════════════════════════════════════╣")
        print(f"║  🎯 {positions[-1].get('reason','')[:52]}║")
    else:
        _, observe, _, _ = check_entry(data)
        print(f"╠══════════════════════════════════════════════════════════════╣")
        print(f"║  🎯 {observe[:52]}                          ║")
        print(f"╠══════════════════════════════════════════════════════════════╣")
        print(f"║  ⚪ 观望中                                              ║")

    print(f"╚══════════════════════════════════════════════════════════════╝")

# ========== 主循环 ==========
def main():
    log(f"🚀 BTC自动交易启动 v2.12 | {POLL_INTERVAL}秒轮询 | {LEVERAGE}x | {QTY} BTC")
    log(f"v2.10: 补仓撤销旧SL/TP，以新均价重新挂单 | 有信号就开仓追加")
    stats = load_stats()
    if stats.get('consecutive_losses', 0) > 0:
        log(f"⚠️ 当前连续亏损: {stats['consecutive_losses']}次")

    # v2.11.9: 启动时验证state与交易所一致性
    state = load_state()
    exchange_pos = binance.fetch_positions()
    exchange_qty = sum(float(p.get('contracts', 0)) for p in exchange_pos if p.get('symbol') == SYMBOL)
    positions = state.get('positions', [])

    if positions and exchange_qty == 0:
        # 交易所无仓但state有仓 = 异常，清空state
        log(f"⚠️ 检测到state有{len(positions)}个仓位但交易所无仓，清空state")
        save_state({'in_position': False, 'positions': [], 'last_close_time': time.time()})
        state = {'in_position': False, 'positions': []}
    elif positions:
        total_qty = sum(p['qty'] for p in positions)
        log(f"⚠️ 检测到 {len(positions)} 个仓位，合计 {total_qty} BTC")
        for i, p in enumerate(positions):
            log(f"  仓{i+1}: {p['direction']} {p['qty']} BTC @ ${p['entry_price']:,.2f} | SL=${p['stop_loss']:,.0f} TP=${p['tp']:,.0f}")

    cycle = 0
    last_trail_check = 0  # 移动止盈上次检查时间戳
    while True:
        try:
            cycle += 1
            k5m, k1h, k4h, k1d = get_data()
            df5m = pd.DataFrame(k5m, columns=['t','o','h','l','c','v'])
            df1h = pd.DataFrame(k1h, columns=['t','o','h','l','c','v'])
            df4h = pd.DataFrame(k4h, columns=['t','o','h','l','c','v'])
            df1d = pd.DataFrame(k1d, columns=['t','o','h','l','c','v'])

            data = {
                '5m': calc(df5m),
                '1h': calc(df1h),
                '4h': calc(df4h),
                '1d': calc(df1d)
            }

            # v2.11.1: 任何周期数据不足则跳过本轮（避免ATR崩溃）
            if any(v is None for v in data.values()):
                if cycle % 6 == 0:
                    log(f"⚠️ 数据不足，跳过本轮 | 5m={len(df5m)} 1h={len(df1h)} 4h={len(df4h)} 1d={len(df1d)}")
                time.sleep(POLL_INTERVAL)
                continue

            state = load_state()

            # v2.11.3: state['positions'] 是bot自己管理的仓位的唯一来源
            # 禁止从交易所读取持仓来更新或覆盖state，保留每个仓位的原始reason
            # 交易所持仓检查仅用于判断是否有持仓，不用于更新state
            exchange_pos = binance.fetch_positions()
            exchange_qty = sum(float(p.get('contracts', 0)) for p in exchange_pos if p.get('symbol') == SYMBOL)
            state_qty = sum(p['qty'] for p in state.get('positions', []))
            has_pos = exchange_qty > 0

            # 修复 in_position 状态一致性（基于交易所实际持仓）
            state['in_position'] = has_pos

            # v2.11.9: 交易所无仓但state有仓 = 异常，清空state
            if not has_pos and state.get('positions'):
                log(f"⚠️ 交易所无仓但state有{len(state['positions'])}个仓位，清空state")
                save_state({'in_position': False, 'positions': [], 'last_close_time': time.time()})
                state = {'in_position': False, 'positions': []}
            elif not has_pos and not state.get('positions'):
                state['in_position'] = False

            # 打印状态
            print_status(data, state)

            # ========== 手动行情参考通知（已关闭）==========
            if False and has_pos:
                last_alert = state.get('last_manual_alert', 0)
                if time.time() - last_alert > 1800:  # 30分钟冷却
                    r5m = data['5m']
                    r1h = data['1h']
                    r4h = data['4h']
                    rd = data['1d']
                    price = r5m['price']
                    pctb = r5m['pctb']
                    rsi5m = r5m['rsi']
                    adx1h = r1h['adx']
                    vol_ratio = r5m['volume_ratio']
                    bullish_4h = r4h['bullish']
                    bullish_1d = rd['bullish']
                    adx4h = r4h['adx']

                    # 检查做多条件（仅通知，不开仓）
                    long_ok = (
                        pctb < 0.2 and
                        rsi5m < 40 and
                        adx1h > 25 and
                        vol_ratio > 1.5
                    )

                    # 检查做空条件（仅通知，不开仓）
                    short_ok = (
                        pctb > 0.85 and
                        rsi5m >= 82 and
                        adx1h > 25 and
                        vol_ratio > 1.5
                    )

                    if long_ok or short_ok:
                        direction = "做多" if long_ok else "做空"
                        log(f"📲 发送手动参考通知: {direction}")
                        wechat_msg = (
                            f"📢 【手动参考信号】\n"
                            f"当前价格: ${price:,.0f}\n"
                            f"方向: {direction}\n"
                            f"5m %b: {pctb:.3f} | RSI: {rsi5m:.1f}\n"
                            f"1h ADX: {adx1h:.1f} | 成交量: {vol_ratio:.1f}x\n"
                            f"4h: {'多头' if bullish_4h else '空头'} | 1d: {'多头' if bullish_1d else '空头'}\n"
                            f"⚠️ 仅供参考，自行决策"
                        )
                        notify_alert(wechat_msg)
                        send_wechat_msg(wechat_msg)
                        state['last_manual_alert'] = time.time()
                        save_state(state)

            # ========== v2.9: 移动止盈（集成版）==========
            # ========== v2.11.4: 移动止盈（仅追踪state中的bot仓位）==========
            # 移动止盈只追踪bot在state['positions']中自己开的仓
            # 手动仓位不在state中，完全不受影响，与幽灵仓位问题彻底切割
            bot_positions = state.get('positions', [])
            now_ts = time.time()
            if has_pos and bot_positions and now_ts - last_trail_check >= TRAIL_INTERVAL:
                price = data['5m']['price']
                trail_closed = []
                for p in bot_positions:
                    direction = p['direction']
                    entry = p['entry_price']
                    qty = p['qty']
                    peak_key = f"peak_{entry}"
                    if peak_key not in state:
                        state[peak_key] = entry
                    activation_price = entry * (1 + TRAIL_ACTIVATION_PCT) if direction == 'long' else entry * (1 - TRAIL_ACTIVATION_PCT)
                    current_peak = state[peak_key]
                    if direction == 'long':
                        if price > current_peak:
                            state[peak_key] = price
                            current_peak = price
                            save_state(state)
                        if current_peak >= activation_price:
                            drawdown = (current_peak - price) / current_peak * 100
                            if drawdown >= TRAIL_TRIGGER_PCT * 100:
                                log(f"🟢 移动止盈触发！LONG {qty} BTC 从峰值${current_peak}回落{drawdown:.2f}%")
                                try:
                                    close_order = binance.create_order(
                                        SYMBOL, 'market', 'sell', qty,
                                        params={'positionSide': 'LONG'}
                                    )
                                    log(f"✅ 移动止盈市价平仓完成: {close_order['id']}")
                                    work_log("移动止盈平仓", f"LONG {qty} BTC @ {price} 从峰值${current_peak}回落{drawdown:.2f}%")
                                    notify_alert(f"🟢 移动止盈平仓\nLONG {qty} BTC @ ${price:,.0f}\n峰值${current_peak}回落{drawdown:.2f}%")
                                    send_wechat_msg(f"🟢 移动止盈平仓\nLONG {qty} BTC @ ${price:,.0f}\n峰值${current_peak}回落{drawdown:.2f}%")
                                    trail_closed.append(entry)
                                except Exception as e:
                                    log(f"❌ 移动止盈平仓失败: {e}")
                    else:  # short
                        if price < current_peak:
                            state[peak_key] = price
                            current_peak = price
                            save_state(state)
                        if current_peak <= activation_price:
                            drawdown = (price - current_peak) / current_peak * 100
                            if drawdown >= TRAIL_TRIGGER_PCT * 100:
                                log(f"🟢 移动止盈触发！SHORT {qty} BTC 从峰值${current_peak}回升{drawdown:.2f}%")
                                try:
                                    close_order = binance.create_order(
                                        SYMBOL, 'market', 'buy', qty,
                                        params={'positionSide': 'SHORT'}
                                    )
                                    log(f"✅ 移动止盈市价平仓完成: {close_order['id']}")
                                    work_log("移动止盈平仓", f"SHORT {qty} BTC @ {price} 从峰值${current_peak}回升{drawdown:.2f}%")
                                    notify_alert(f"🟢 移动止盈平仓\nSHORT {qty} BTC @ ${price:,.0f}\n峰值${current_peak}回升{drawdown:.2f}%")
                                    send_wechat_msg(f"🟢 移动止盈平仓\nSHORT {qty} BTC @ ${price:,.0f}\n峰值${current_peak}回升{drawdown:.2f}%")
                                    trail_closed.append(entry)
                                except Exception as e:
                                    log(f"❌ 移动止盈平仓失败: {e}")
                # 只从state中移除bot已平仓的仓位（手动仓不在state中，无需处理）
                if trail_closed:
                    state['positions'] = [p for p in state['positions'] if p['entry_price'] not in trail_closed]
                    for e in trail_closed:
                        state.pop(f"peak_{e}", None)
                    if not state['positions']:
                        save_state({'in_position': False, 'positions': [], 'last_close_time': time.time()})
                        state = {'in_position': False, 'positions': []}
                    else:
                        save_state(state)
                last_trail_check = time.time()

            # ========== v2.7: 有持仓时，每分钟检查各仓位SL/TP是否完整 ==========
            if has_pos and cycle % 6 == 0:
                try:
                    positions = state.get('positions', [])
                    if not positions:
                        continue

                    exchange_algos = binance.fapiprivate_get_openalgoorders({'symbol': 'BTCUSDT'})
                    active_algos = [o for o in exchange_algos
                                    if o.get('algoStatus') not in ('CANCELED', 'FINISHED', 'EXPIRED', None)]

                    missing_count = 0
                    for i, p in enumerate(positions):
                        # 检查该仓位的SL和TP是否在交易所存在
                        sl_price = p.get('stop_loss', 0)
                        tp_price = p.get('tp', 0)
                        qty = p.get('qty', 0)

                        # 检查交易所是否存在SL/TP（不看价格，只看方向+数量是否匹配）
                        # 这样用户手动改价也不会触发重复挂单
                        has_sl = any(
                            float(o.get('quantity', 0)) >= qty * 0.99
                            for o in active_algos
                            if o.get('orderType') == 'STOP_MARKET' and
                               (o.get('side') == 'SELL' if p['direction'] == 'long' else o.get('side') == 'BUY')
                        )
                        has_tp = any(
                            float(o.get('quantity', 0)) >= qty * 0.99
                            for o in active_algos
                            if o.get('orderType') == 'TAKE_PROFIT_MARKET' and
                               (o.get('side') == 'SELL' if p['direction'] == 'long' else o.get('side') == 'BUY')
                        )

                        # SL/TP任一为0视为手动仓位，跳过自动挂单（防止对手动仓位重复挂单）
                        if not sl_price or not tp_price:
                            if cycle % 6 == 0:
                                log(f"⏭️ 仓{i+1} SL/TP未设置(手动仓位)，跳过自动挂单")
                            continue

                        if not has_sl or not has_tp:
                            log(f"⚠️ 仓{i+1} SL/TP缺失 (SL={'✅' if has_sl else '❌'} TP={'✅' if has_tp else '❌'})，重新挂单")
                            # 重新为该仓位挂SL/TP（追加模式，不影响其他仓位）
                            place_sl_tp_for_entry(
                                p['direction'], p['entry_price'], qty,
                                p.get('reason', ''), p.get('atr', 0)
                            )
                            missing_count += 1

                    if missing_count == 0:
                        log(f"✅ SL/TP检查正常 (交易所查询) {len(positions)}个仓位全部完整")
                except Exception as e:
                    log(f"⚠️ SL/TP检查异常: {e}")

            # ========== v2.6: 有持仓也继续开仓（移除has_pos限制）============
            # 有仓位时点位到了也开仓，每次新开仓重新设置全仓SL/TP
            # 连续亏损保护冷却（30分钟）仍然保留
            stats = load_stats()
            cooldown_until = stats.get('cooldown_until', 0)
            if time.time() < cooldown_until:
                remaining = int(cooldown_until - time.time())
                if cycle % 6 == 0:  # 每分钟提示一次
                    log(f"⏳ 连续亏损保护冷却中，还需 {remaining//60}分{remaining%60}秒")
            else:
                sig, reason, price, atr = check_entry(data)

                if sig:
                    # 信号去抖：同一方向开仓后冷却300秒，防止信号重复触发
                    state = load_state()
                    last_sig = state.get('last_signal_time', {})
                    if last_sig.get(sig, 0) + 300 > time.time():
                        log(f"⏳ {sig}信号冷却中，跳过")
                    else:
                        # ========== v2.12.5: 反方向开仓保护 + 单方向上限制 ==========
                        positions = state.get('positions', [])
                        blocked = False
                        if positions:
                            existing_dir = positions[0].get('direction')
                            if (sig == 'long' and existing_dir == 'short') or (sig == 'short' and existing_dir == 'long'):
                                log(f"⛔ 已有{existing_dir.upper()}持仓，拒绝{sig.upper()}反方向开仓")
                                state.setdefault('last_signal_time', {})[sig] = time.time()
                                save_state(state)
                                blocked = True
                        if not blocked:
                            # v2.14.3: 检查同方向总持仓量（替代无效的仓位计数）
                            total_qty_same_dir = sum(p.get('qty', 0) for p in positions if p.get('direction') == sig)
                            if total_qty_same_dir + QTY > MAX_TOTAL_QTY + 0.001:
                                log(f"⛔ {sig}方向总持仓{total_qty_same_dir}BTC + 新{QTY}BTC > 上限{MAX_TOTAL_QTY}BTC，跳过开仓")
                                state.setdefault('last_signal_time', {})[sig] = time.time()
                                save_state(state)
                            else:
                                log(f"🚨 触发信号! {sig} | {reason.split(chr(10))[0]}")
                                # v2.14: 开仓价格验证
                                price_valid, price_valid_msg = validate_entry_price(sig, price)
                                if not price_valid:
                                    log(f"⛔ 价格验证不通过: {price_valid_msg}")
                                    state.setdefault('last_signal_time', {})[sig] = time.time()
                                    save_state(state)
                                    continue
                                log(f"✅ 价格验证通过: {price_valid_msg}")
                                try:
                                    open_position(sig, price, atr, reason, QTY)
                                    # v2.12.6: open_position内部已保存state，需重新加载后再更新last_signal_time
                                    state = load_state()
                                    state.setdefault('last_signal_time', {})[sig] = time.time()
                                    save_state(state)
                                except Exception as e:
                                    log(f"❌ 开仓失败: {e}")

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            log("🛑 停止")
            state = load_state()
            if state.get('in_position'):
                if input("仍有持仓，是否平仓？(Y/n): ") != 'n':
                    close_position()
            break
        except Exception as e:
            log(f"❌ 异常: {e}")
            import traceback; traceback.print_exc()
            work_log("错误", str(e)[:100])
            time.sleep(10)

if __name__ == "__main__":
    main()
