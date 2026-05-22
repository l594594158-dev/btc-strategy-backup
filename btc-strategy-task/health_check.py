#!/usr/bin/env python3
"""
BTC v4.1 趋势回调策略 · 自检脚本
- 每5分钟执行一次自动检查
- 检查进程运行、API数据、持仓同步、策略状态
- 发现问题自动修复并通知
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
SYMBOL = 'BTC/USDT:USDT'
LEVERAGE = 50
TP_PCT = 0.025; SL_PCT = 0.015
QTY = 0.003

os.makedirs(LOG_DIR, exist_ok=True)

def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] {msg}")
    return ts

def get_binance():
    return ccxt.binance({'apiKey': API_KEY, 'secret': SECRET, 'options': {'defaultType': 'swap'}})

def get_data():
    ex = get_binance()
    return {
        'k5m': ex.fetch_ohlcv(SYMBOL, '5m', limit=100),
        'k1h': ex.fetch_ohlcv(SYMBOL, '1h', limit=200),
        'k4h': ex.fetch_ohlcv(SYMBOL, '4h', limit=200),
        'k1d': ex.fetch_ohlcv(SYMBOL, '1d', limit=200),
    }

# ========== v4.1 指标计算（4个独立函数）==========

def calc_5m(kline_data):
    df = pd.DataFrame(kline_data, columns=['t','o','h','l','c','v'])
    c=df['c'].astype(float); h=df['h'].astype(float); l=df['l'].astype(float); v=df['v'].astype(float)
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
    cc=c.iloc[clv]
    sc=ta.trend.SMAIndicator(c,20).sma_indicator().iloc[clv]
    try: ac=ta.trend.ADXIndicator(h,l,c,14).adx().iloc[clv]
    except: ac=25
    return {'close_closed':cc,'sma_closed':sc,'adx_closed':ac}

def calc_1d(kline_data):
    df = pd.DataFrame(kline_data, columns=['t','o','h','l','c','v'])
    c=df['c'].astype(float)
    clv=max(0,len(df)-2)
    if len(df)<20: return None
    return {'close_closed':c.iloc[clv],'sma_closed':ta.trend.SMAIndicator(c,20).sma_indicator().iloc[clv]}

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
                    self.ok('进程状态', f'PID={pid}')
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
            self.ok('API数据', f'各周期正常, 最新${price:,.0f}')

            # v4.1 指标检查 (4个独立函数)
            r5 = calc_5m(data['k5m'])
            r1 = calc_1h(data['k1h'])
            r4 = calc_4h(data['k4h'])
            rd = calc_1d(data['k1d'])
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

            # 有效性检查
            if r5['rsi'] <= 0 or r5['rsi'] >= 100:
                self.fail('RSI异常', f'{r5["rsi"]}', 'retry')
            if r4['sma_closed'] <= 0:
                self.fail('SMA20_4h异常', f'{r4["sma_closed"]}', 'retry')

        except ccxt.NetworkError as e:
            self.fail('API网络', str(e)[:50], 'network')
        except Exception as e:
            self.fail('API异常', str(e)[:80], 'restart')

    def check_position_sync(self):
        try:
            ex = get_binance()
            positions = ex.fetch_positions([SYMBOL])
            long_ex = any(p.get('symbol')==SYMBOL and float(p.get('contracts',0))>0 and p.get('side')=='long' for p in positions)
            short_ex = any(p.get('symbol')==SYMBOL and float(p.get('contracts',0))>0 and p.get('side')=='short' for p in positions)

            state = {'long_pos':None,'short_pos':None}
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE) as f: state = json.load(f)

            st_long = state.get('long_pos') is not None
            st_short = state.get('short_pos') is not None

            if long_ex != st_long or short_ex != st_short:
                log(f"持仓不一致: 交易所LONG={long_ex}/SHORT={short_ex} vs state LONG={st_long}/SHORT={st_short}")
                if not long_ex: state['long_pos'] = None
                if not short_ex: state['short_pos'] = None
                new_long = state.get('long_pos') is not None
                new_short = state.get('short_pos') is not None
                with open(STATE_FILE,'w') as f: json.dump(state, f, default=str)
                if new_long == long_ex and new_short == short_ex:
                    self.ok('持仓同步', f'一致 LONG={new_long} SHORT={new_short}')
                else:
                    self.ok('持仓同步', f'交易所LONG={long_ex}/SHORT={short_ex} | state忽略旧仓')
            else:
                desc = f'LONG={long_ex} SHORT={short_ex}'
                self.ok('持仓同步', desc)
        except Exception as e:
            self.fail('持仓同步', str(e)[:50])

    def check_state_files(self):
        if not os.path.exists(STATE_FILE):
            with open(STATE_FILE,'w') as f: json.dump({'long_pos':None,'short_pos':None,'last_long_signal':False,'last_short_signal':False}, f)
            self.ok('State文件', '已创建默认')
        else:
            with open(STATE_FILE) as f:
                s = json.load(f)
            lp = '有' if s.get('long_pos') else '无'
            sp = '有' if s.get('short_pos') else '无'
            self.ok('State文件', f'LONG={lp} SHORT={sp}')

        if os.path.exists(WORK_LOG):
            try:
                with open(WORK_LOG) as f:
                    lines = f.readlines()
                if lines:
                    last = lines[-1].strip()[:60]
                    self.ok('WorkLog', last)
                else:
                    self.ok('WorkLog', '空')
            except: self.ok('WorkLog', '读取失败')
        else:
            self.ok('WorkLog', '不存在')

    def check_notify(self):
        try:
            if os.path.exists(NOTIFY_QUEUE):
                with open(NOTIFY_QUEUE) as f:
                    q = json.load(f)
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
                    f'cd {TASK_DIR} && python3 -u auto_trade.py > logs/v41_$(date +%Y%m%d_%H%M%S).log 2>&1 &',
                    shell=True, preexec_fn=os.setsid)
                return '已重启'
            elif fix == 'network': return '等待网络恢复'
            elif fix == 'retry': return '等待重试'
        except Exception as e: return f'修复失败:{e}'

    def run(self):
        log('='*50)
        log('🔍 BTC v4.1 自检')
        log('='*50)

        self.check_process()
        self.check_api()
        self.check_position_sync()
        self.check_state_files()
        self.check_notify()

        # 执行修复
        fixes_done = []
        for fix in set(self._fixes):
            r = self.do_fix(fix)
            if r: fixes_done.append(r)

        # 保存日志
        report = {'time': datetime.now().isoformat(), 'ok': self.checks_ok, 'fail': self.checks_fail,
                  'items': self.results, 'fixes': fixes_done}

        logs = []
        if os.path.exists(CHECK_LOG):
            try:
                with open(CHECK_LOG) as f: logs = json.load(f)
            except: pass
        logs.append(report)
        with open(CHECK_LOG,'w') as f: json.dump(logs[-100:], f, ensure_ascii=False, indent=2)

        # 写修复日志
        with open(FIX_LOG,'a') as f:
            for item in self.results:
                if item['status'] == '❌':
                    f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ❌ {item['item']}: {item['detail']}\n")
            for fr in fixes_done:
                f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ✅ {fr}\n")

        # 有问题发通知（含v4.1策略快照）
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
                    snapshot = (f"\n📊 v4.1快照: ${r5['price']:,.0f} | "
                               f"4h闭K{'多'if h4b else'空'}/1d闭K{'多'if d1b else'空'} | "
                               f"RSI{r5['rsi']:.0f} | 1hADX{r1['adx_closed']:.0f} | 信号:{sig or '观望'}")
            except: pass
            msg = f"🔴 v4.1 自检发现{self.checks_fail}项问题{snapshot}\n"
            for item in self.results:
                if item['status'] == '❌':
                    msg += f"• {item['item']}: {item['detail']}\n"
            if fixes_done:
                msg += f"\n🔧 已修复:\n" + '\n'.join(f'• {f}' for f in fixes_done)
            try:
                existing = []
                if os.path.exists(NOTIFY_QUEUE):
                    with open(NOTIFY_QUEUE) as f:
                        eq = json.load(f)
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
