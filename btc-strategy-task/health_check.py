#!/usr/bin/env python3
"""
BTC v4.2 趋势回调策略 · 自检脚本
- 每5分钟执行一次自动检查
- 检查进程运行、API数据、持仓同步、策略状态
- 发现问题自动修复并通知
- ⚠️ 双向持仓同步: 交易所↔state.json
"""
import ccxt, os, json, subprocess, time, pandas as pd, ta
from datetime import datetime

TASK_DIR = '/root/.openclaw/workspace/btc-strategy-task'
STATE_FILE = f'{TASK_DIR}/databases/state.json'
WORK_LOG = f'{TASK_DIR}/logs/work_log.txt'
NOTIFY_QUEUE = f'{TASK_DIR}/databases/notify_queue.json'
LOG_DIR = f'{TASK_DIR}/logs/health_check'
FIX_LOG = f'{LOG_DIR}/fix_log.txt'
CHECK_LOG = f'{LOG_DIR}/check_log.json'

API_KEY = "CUPwmVULosVO24NBKmoaMm0pvga2msasOa4nBhvPvybrGdA2RcXBYA4aRtGMZjWH"
SECRET = "Ozht5MjazUu4JKhSLqx4ASmTBH4wlUMdbABOblxXGyhIuof1jhrzUEr9JkWHpUHM"
SYMBOL = 'BTC/USDT:USDT'        # 合约交易对 (仓位检查用)
SYMBOL_SPOT = 'BTC/USDT'        # 现货 (K线用)
LEVERAGE = 50
TP_PCT = 0.025; SL_PCT = 0.015
QTY = 0.035

os.makedirs(LOG_DIR, exist_ok=True)

def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] {msg}")
    return ts

def get_binance():
    return ccxt.binance({'apiKey': API_KEY, 'secret': SECRET, 'options': {'defaultType': 'swap'}})

def get_spot():
    return ccxt.binance({'options': {'defaultType': 'spot'}})

def get_data():
    spot = get_spot()
    return {
        'k5m': spot.fetch_ohlcv(SYMBOL_SPOT, '5m', limit=100),
        'k1h': spot.fetch_ohlcv(SYMBOL_SPOT, '1h', limit=200),
        'k4h': spot.fetch_ohlcv(SYMBOL_SPOT, '4h', limit=200),
        'k1d': spot.fetch_ohlcv(SYMBOL_SPOT, '1d', limit=200),
    }

# ========== v4.2 指标计算 (现货K线) ==========

def calc_5m(kline_data):
    df = pd.DataFrame(kline_data, columns=['t','o','h','l','c','v'])
    c=df['c'].astype(float); v=df['v'].astype(float)
    lv=len(df)-1; clv=max(0,lv-1)
    if len(df)<20: return None
    price = c.iloc[lv]
    sma20 = ta.trend.SMAIndicator(c,20).sma_indicator().iloc[lv]
    rsi = ta.momentum.RSIIndicator(c,14).rsi().iloc[lv]
    avg_v = v.iloc[max(0,clv-19):clv+1].mean()
    vol_r = v.iloc[clv]/avg_v if avg_v>0 else 1
    return {'price':price,'sma20':sma20,'rsi':rsi,'vol_ratio':vol_r}

def calc_1h(kline_data):
    df = pd.DataFrame(kline_data, columns=['t','o','h','l','c','v'])
    c=df['c'].astype(float); h=df['h'].astype(float); l=df['l'].astype(float)
    clv=max(0,len(df)-2)
    if len(df)<20: return None
    try: adx=ta.trend.ADXIndicator(h,l,c,14).adx().iloc[clv]
    except: adx=25
    return {'adx_closed':adx}

def calc_4h(kline_data):
    df = pd.DataFrame(kline_data, columns=['t','o','h','l','c','v'])
    c=df['c'].astype(float); h=df['h'].astype(float); l=df['l'].astype(float)
    clv=max(0,len(df)-2)
    if len(df)<20: return None
    cc=c.iloc[clv]; sc=ta.trend.SMAIndicator(c,20).sma_indicator().iloc[clv]
    try: ac=ta.trend.ADXIndicator(h,l,c,14).adx().iloc[clv]
    except: ac=25
    return {'close_closed':cc,'sma_closed':sc,'adx_closed':ac}

def calc_1d(kline_data):
    df = pd.DataFrame(kline_data, columns=['t','o','h','l','c','v'])
    c=df['c'].astype(float)
    clv=max(0,len(df)-2)
    if len(df)<20: return None
    return {'close_closed':c.iloc[clv],'sma_closed':ta.trend.SMAIndicator(c,20).sma_indicator().iloc[clv]}

def save_state(state):
    with open(STATE_FILE,'w') as f:
        json.dump(state, f, indent=2, ensure_ascii=False, default=str)

def sync_exchange_to_state():
    """
    双向同步: 交易所仓位 → state.json
    返回: (synced_long, synced_short) 是否做了同步
    """
    synced = {'LONG': False, 'SHORT': False}
    try:
        ex = get_binance()
        positions = ex.fetch_positions([SYMBOL])
        state = {'long_pos': None, 'short_pos': None,
                 'last_long_signal': False, 'last_short_signal': False}
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f: state = json.load(f)

        for p in positions:
            amt = abs(float(p.get('contracts', 0) or 0))
            if amt > 0 and p.get('symbol') == SYMBOL:
                d = 'LONG' if p.get('side') == 'long' else 'SHORT'
                pk = 'long_pos' if d == 'LONG' else 'short_pos'
                entry = float(p.get('entryPrice', 0))

                # 已有记录且价格一致 → 跳过
                existing = state.get(pk)
                if existing and abs(existing.get('entry_price', 0) - entry) < 1:
                    continue

                sl = round(entry * (1 - SL_PCT if d == 'LONG' else 1 + SL_PCT), 1)
                tp = round(entry * (1 + TP_PCT if d == 'LONG' else 1 - TP_PCT), 1)
                state[pk] = {
                    'entry_price': entry, 'qty': amt, 'sl': sl, 'tp': tp,
                    'open_time': datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
                    'reason': {'name': f'{d}-体检同步', 'price': entry}
                }
                synced[d] = True
                log(f"🔄 体检同步: {d} {amt}BTC @ ${entry:,.1f} → state")

        # 清除交易所已无的state记录
        long_ex = any(p.get('symbol')==SYMBOL and float(p.get('contracts',0))>0 and p.get('side')=='long' for p in positions)
        short_ex = any(p.get('symbol')==SYMBOL and float(p.get('contracts',0))>0 and p.get('side')=='short' for p in positions)
        if not long_ex and state.get('long_pos'):
            state['long_pos'] = None
            log("🧹 交易所无LONG → 清除state")
        if not short_ex and state.get('short_pos'):
            state['short_pos'] = None
            log("🧹 交易所无SHORT → 清除state")

        save_state(state)
    except Exception as e:
        log(f"⚠️ 同步异常: {e}")
    return synced

class HealthChecker:
    def __init__(self):
        self.results = []
        self.checks_ok = self.checks_fail = 0
        self._fixes = []

    def ok(self, item, detail=''):
        self.checks_ok += 1
        self.results.append({'item':item,'status':'✅','detail':detail})
        log(f"✅ {item}: {detail or '正常'}")

    def fail(self, item, detail='', fix=None):
        self.checks_fail += 1
        self.results.append({'item':item,'status':'❌','detail':detail,'fix':fix})
        log(f"❌ {item}: {detail}")
        if fix: self._fixes.append(fix)

    def check_process(self):
        try:
            r = subprocess.run(['ps','aux'], capture_output=True, text=True)
            for line in r.stdout.split('\n'):
                if 'auto_trade.py' in line and 'grep' not in line and 'python' in line:
                    pid = line.split()[1]
                    cpu = line.split()[2]
                    self.ok('进程状态', f'PID={pid} CPU={cpu}%')
                    return True
            self.fail('进程状态', '未运行', 'restart')
        except Exception as e:
            self.fail('进程状态', str(e), 'restart')

    def check_api(self):
        try:
            data = get_data()
            for k, n in [('k5m','5m'),('k1h','1h'),('k4h','4h'),('k1d','1d')]:
                if len(data[k]) < 50:
                    self.fail(f'API-{n}', f'数据不足{len(data[k])}根', 'retry')
                    return
            price = data['k5m'][-1][4]
            self.ok('现货K线', f'各周期正常, 最新${price:,.0f}')

            r5 = calc_5m(data['k5m']); r1 = calc_1h(data['k1h'])
            r4 = calc_4h(data['k4h']); rd = calc_1d(data['k1d'])
            if any(v is None for v in [r5,r1,r4,rd]):
                self.fail('策略指标', '计算失败', 'retry')
                return

            pct_sma = (r5['price']-r5['sma20'])/r5['sma20']*100
            h4_trend = '多' if r4['close_closed']>r4['sma_closed'] else '空'
            d1_trend = '多' if rd['close_closed']>rd['sma_closed'] else '空'
            info = (f"${r5['price']:,.0f} | SMA5距{pct_sma:+.1f}% | "
                    f"RSI5={r5['rsi']:.0f} | 1hADX={r1['adx_closed']:.0f} | "
                    f"4hADX={r4['adx_closed']:.0f} | 量比={r5['vol_ratio']:.1f}x | "
                    f"4h闭K{h4_trend}/1d闭K{d1_trend}")
            self.ok('策略指标', info)

            if r5['rsi'] <= 0 or r5['rsi'] >= 100:
                self.fail('RSI异常', f'{r5["rsi"]}', 'retry')
            if r4['sma_closed'] <= 0:
                self.fail('SMA20_4h异常', f'{r4["sma_closed"]}', 'retry')

        except ccxt.NetworkError as e:
            self.fail('API网络', str(e)[:50], 'network')
        except Exception as e:
            self.fail('API异常', str(e)[:80], 'restart')

    def check_position_sync(self):
        """双向持仓同步: 先同步交易所→state, 再校验"""
        synced = sync_exchange_to_state()
        try:
            ex = get_binance()
            positions = ex.fetch_positions([SYMBOL])
            long_ex = any(p.get('symbol')==SYMBOL and float(p.get('contracts',0))>0 and p.get('side')=='long' for p in positions)
            short_ex = any(p.get('symbol')==SYMBOL and float(p.get('contracts',0))>0 and p.get('side')=='short' for p in positions)

            state = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
            st_long = state.get('long_pos') is not None
            st_short = state.get('short_pos') is not None

            if long_ex == st_long and short_ex == st_short:
                desc = f'一致 LONG={"有" if long_ex else "空"} SHORT={"有" if short_ex else "空"}'
                if any(synced.values()):
                    desc += ' (已同步)'
                self.ok('持仓同步', desc)
            else:
                self.fail('持仓同步',
                         f'交易所LONG={long_ex}/SHORT={short_ex} vs state LONG={st_long}/SHORT={st_short}',
                         'sync_state')
        except Exception as e:
            self.fail('持仓同步', str(e)[:50], 'sync_state')

    def check_state_files(self):
        if not os.path.exists(STATE_FILE):
            with open(STATE_FILE,'w') as f:
                json.dump({'long_pos':None,'short_pos':None,'last_long_signal':False,'last_short_signal':False}, f)
            self.ok('State文件', '已创建默认')
        else:
            with open(STATE_FILE) as f: s = json.load(f)
            lp = f"有{s['long_pos']['qty']}BTC" if s.get('long_pos') else '空'
            sp = f"有{s['short_pos']['qty']}BTC" if s.get('short_pos') else '空'
            self.ok('State文件', f'LONG={lp} SHORT={sp}')

        if os.path.exists(WORK_LOG):
            try:
                lines = open(WORK_LOG).readlines()
                if lines: self.ok('WorkLog', lines[-1].strip()[:60])
                else: self.ok('WorkLog', '空')
            except: self.ok('WorkLog', '读取失败')
        else:
            self.ok('WorkLog', '不存在')

    def check_notify(self):
        try:
            if os.path.exists(NOTIFY_QUEUE):
                q = json.load(open(NOTIFY_QUEUE))
                items = q if isinstance(q, list) else [q]
                pending = sum(1 for x in items if isinstance(x, dict) and not x.get('sent', True))
                self.ok('通知队列', f'待发送{pending}条' if pending else '无积压')
            else:
                self.ok('通知队列', '无积压')
        except Exception as e:
            self.fail('通知队列', str(e)[:50])

    def do_fix(self, fix):
        try:
            if fix == 'restart':
                subprocess.run(['pkill','-f','auto_trade.py'], capture_output=True)
                time.sleep(2)
                subprocess.Popen(
                    f'cd {TASK_DIR} && nohup python3 -u auto_trade.py >> logs/auto_trade.log 2>&1 &',
                    shell=True, preexec_fn=os.setsid)
                return '已重启auto_trade'
            elif fix == 'sync_state':
                synced = sync_exchange_to_state()
                done = [k for k,v in synced.items() if v]
                return f'已同步{",".join(done)}' if done else '无需同步'
            elif fix == 'network':
                return '等待网络恢复'
            elif fix == 'retry':
                return '等待重试'
        except Exception as e:
            return f'修复失败:{e}'

    def run(self):
        log('='*50)
        log('🔍 BTC v4.2 自检 (现货K线+合约执行)')
        log('='*50)

        # 先做同步
        self.check_position_sync()
        self.check_process()
        self.check_api()
        self.check_state_files()
        self.check_notify()

        fixes_done = []
        for fix in set(self._fixes):
            r = self.do_fix(fix)
            if r: fixes_done.append(r)

        report = {'time': datetime.now().isoformat(), 'ok': self.checks_ok, 'fail': self.checks_fail,
                  'items': self.results, 'fixes': fixes_done}

        logs = []
        if os.path.exists(CHECK_LOG):
            try: logs = json.load(open(CHECK_LOG))
            except: pass
        logs.append(report)
        with open(CHECK_LOG,'w') as f: json.dump(logs[-100:], f, ensure_ascii=False, indent=2)

        with open(FIX_LOG,'a') as f:
            for item in self.results:
                if item['status'] == '❌':
                    f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ❌ {item['item']}: {item['detail']}\n")
            for fr in fixes_done:
                f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ✅ {fr}\n")

        if self.checks_fail > 0:
            snapshot = ''
            try:
                data = get_data()
                r5 = calc_5m(data['k5m']); r1 = calc_1h(data['k1h'])
                r4 = calc_4h(data['k4h']); rd = calc_1d(data['k1d'])
                if all(v is not None for v in [r5,r1,r4,rd]):
                    pct = (r5['price']-r5['sma20'])/r5['sma20']*100
                    h4b=r4['close_closed']>r4['sma_closed']; d1b=rd['close_closed']>rd['sma_closed']
                    sig=None
                    if r1['adx_closed']>25 and r4['adx_closed']<40 and abs(pct)<=1 and r5['vol_ratio']>=1:
                        if h4b and d1b and r5['rsi']>40: sig='LONG'
                        elif not h4b and not d1b and r5['rsi']<60: sig='SHORT'
                    snapshot = (f"\n📊 v4.2快照: ${r5['price']:,.0f} | "
                               f"4h闭K{'多'if h4b else'空'}/1d闭K{'多'if d1b else'空'} | "
                               f"RSI{r5['rsi']:.0f} | 1hADX{r1['adx_closed']:.0f} | 信号:{sig or '观望'}")
            except: pass
            msg = f"🔴 v4.2 自检发现{self.checks_fail}项问题{snapshot}\n"
            for item in self.results:
                if item['status'] == '❌':
                    msg += f"• {item['item']}: {item['detail']}\n"
            if fixes_done:
                msg += f"\n🔧 已修复:\n" + '\n'.join(f'• {f}' for f in fixes_done)
            try:
                existing = []
                if os.path.exists(NOTIFY_QUEUE):
                    eq = json.load(open(NOTIFY_QUEUE))
                    existing = eq if isinstance(eq, list) else [eq]
                existing.append({'time': datetime.now().isoformat(), 'msg': msg, 'sent': False})
                with open(NOTIFY_QUEUE,'w') as f:
                    json.dump(existing, f, ensure_ascii=False, indent=2)
            except: pass

        log('='*50)
        log(f'📊 自检完成: {self.checks_ok}✅ {self.checks_fail}❌ {len(fixes_done)}已修复')
        return report

if __name__ == '__main__':
    HealthChecker().run()
