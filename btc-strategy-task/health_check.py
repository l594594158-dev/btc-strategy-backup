#!/usr/bin/env python3
"""
BTC + HYPE v4.2 双策略 · 自检脚本
- 每5分钟执行一次自动检查
- 检查进程运行、API数据、持仓同步、策略状态
- 发现问题自动修复并通知
- ⚠️ 双向持仓同步: 交易所↔state.json
"""
import ccxt, os, json, subprocess, time, pandas as pd, ta
from datetime import datetime

API_KEY = "CUPwmVULosVO24NBKmoaMm0pvga2msasOa4nBhvPvybrGdA2RcXBYA4aRtGMZjWH"
SECRET = "Ozht5MjazUu4JKhSLqx4ASmTBH4wlUMdbABOblxXGyhIuof1jhrzUEr9JkWHpUHM"

# ========== 策略配置 ==========
STRATEGIES = {
    'BTC': {
        'task_dir': '/root/.openclaw/workspace/btc-strategy-task',
        'symbol': 'BTC/USDT:USDT',
        'symbol_kline': 'BTC/USDT',       # 现货K线
        'kline_source': 'spot',            # spot | swap
        'leverage': 50,
        'tp_pct': 0.025,
        'sl_pct': 0.015,
        'qty': 0.035,
        'process_pattern': 'auto_trade.py',
        'label': 'BTC v4.2',
    },
    'HYPE': {
        'task_dir': '/root/.openclaw/workspace/hype-strategy-task',
        'symbol': 'HYPE/USDT:USDT',
        'symbol_kline': 'HYPE/USDT:USDT',  # 合约K线 (无现货)
        'kline_source': 'swap',
        'leverage': 30,
        'tp_pct': 0.03,
        'sl_pct': 0.02,
        'qty': 20.0,
        'process_pattern': 'hype_trade.py',
        'label': 'HYPE v4.2',
    },
}

os.makedirs('/tmp/health_check_logs', exist_ok=True)

def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] {msg}")
    return ts

def get_binance():
    return ccxt.binance({'apiKey': API_KEY, 'secret': SECRET, 'options': {'defaultType': 'swap'}})

def get_spot():
    return ccxt.binance({'options': {'defaultType': 'spot'}})

def get_kline_client(src):
    """src='spot' 用现货客户端, 'swap' 用合约客户端"""
    if src == 'spot':
        return get_spot()
    else:
        return get_binance()

# ========== v4.2 指标计算 ==========

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

# ========== 持仓同步 ==========

def sync_exchange_to_state(cfg):
    """双向同步: 交易所仓位 → state.json"""
    task_dir = cfg['task_dir']
    state_file = f'{task_dir}/databases/state.json'
    symbol = cfg['symbol']
    tp = cfg['tp_pct']; sl = cfg['sl_pct']
    synced = {'LONG': False, 'SHORT': False}

    try:
        ex = get_binance()
        positions = ex.fetch_positions([symbol])
        state = {'long_pos': None, 'short_pos': None,
                 'last_long_signal': False, 'last_short_signal': False}
        if os.path.exists(state_file):
            with open(state_file) as f: state = json.load(f)

        for p in positions:
            amt = abs(float(p.get('contracts', 0) or 0))
            if amt > 0 and p.get('symbol') == symbol:
                d = 'LONG' if p.get('side') == 'long' else 'SHORT'
                pk = 'long_pos' if d == 'LONG' else 'short_pos'
                entry = float(p.get('entryPrice', 0))
                existing = state.get(pk)
                if existing and abs(existing.get('entry_price', 0) - entry) < 0.1:
                    continue
                sl_p = round(entry * (1 - sl if d == 'LONG' else 1 + sl), 4)
                tp_p = round(entry * (1 + tp if d == 'LONG' else 1 - tp), 4)
                state[pk] = {
                    'entry_price': entry, 'qty': amt, 'sl': sl_p, 'tp': tp_p,
                    'open_time': datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
                    'reason': {'name': f'{d}-体检同步', 'price': entry}
                }
                synced[d] = True
                log(f"  🔄 {cfg['name']}: {d} {amt} @ ${entry:,.4f} → state")

        long_ex = any(p.get('symbol')==symbol and float(p.get('contracts',0))>0 and p.get('side')=='long' for p in positions)
        short_ex = any(p.get('symbol')==symbol and float(p.get('contracts',0))>0 and p.get('side')=='short' for p in positions)
        if not long_ex and state.get('long_pos'): state['long_pos'] = None
        if not short_ex and state.get('short_pos'): state['short_pos'] = None

        with open(state_file,'w') as f:
            json.dump(state, f, indent=2, ensure_ascii=False, default=str)
    except Exception as e:
        log(f"  ⚠️ {cfg['name']} 同步异常: {e}")
    return synced

class StrategyHealthChecker:
    def __init__(self, name, cfg):
        self.name = name
        self.cfg = cfg
        self.label = cfg['label']
        self.results = []
        self.checks_ok = self.checks_fail = 0
        self._fixes = []

    def ok(self, item, detail=''):
        self.checks_ok += 1
        self.results.append({'strategy':self.name,'item':item,'status':'✅','detail':detail})
        log(f"  ✅ [{self.name}] {item}: {detail or '正常'}")

    def fail(self, item, detail='', fix=None):
        self.checks_fail += 1
        self.results.append({'strategy':self.name,'item':item,'status':'❌','detail':detail,'fix':fix})
        log(f"  ❌ [{self.name}] {item}: {detail}")
        if fix: self._fixes.append(fix)

    def check_process(self):
        try:
            r = subprocess.run(['ps','aux'], capture_output=True, text=True)
            pat = self.cfg['process_pattern']
            for line in r.stdout.split('\n'):
                if pat in line and 'grep' not in line and 'python' in line:
                    pid = line.split()[1]
                    cpu = line.split()[2]
                    self.ok('进程状态', f'PID={pid} CPU={cpu}%')
                    return True
            self.fail('进程状态', '未运行', 'restart')
        except Exception as e:
            self.fail('进程状态', str(e), 'restart')

    def check_api(self):
        try:
            client = get_kline_client(self.cfg['kline_source'])
            sym = self.cfg['symbol_kline']
            k5m = client.fetch_ohlcv(sym, '5m', limit=100)
            k1h = client.fetch_ohlcv(sym, '1h', limit=200)
            k4h = client.fetch_ohlcv(sym, '4h', limit=200)
            k1d = client.fetch_ohlcv(sym, '1d', limit=200)

            if len(k5m) < 50:
                self.fail('API-K线', f'5m数据不足{len(k5m)}根', 'retry')
                return

            r5 = calc_5m(k5m); r1 = calc_1h(k1h)
            r4 = calc_4h(k4h); rd = calc_1d(k1d)
            if any(v is None for v in [r5,r1,r4,rd]):
                self.fail('策略指标', '计算失败', 'retry')
                return

            pct_sma = (r5['price']-r5['sma20'])/r5['sma20']*100
            h4_trend = '多' if r4['close_closed']>r4['sma_closed'] else '空'
            d1_trend = '多' if rd['close_closed']>rd['sma_closed'] else '空'
            info = (f"${r5['price']:,.4f} | SMA5距{pct_sma:+.1f}% | "
                    f"RSI5={r5['rsi']:.0f} | 1hADX={r1['adx_closed']:.0f} | "
                    f"4hADX={r4['adx_closed']:.0f} | 量比={r5['vol_ratio']:.1f}x | "
                    f"4h闭K{h4_trend}/1d闭K{d1_trend}")
            self.ok('策略指标', info)

        except ccxt.NetworkError as e:
            self.fail('API网络', str(e)[:50], 'network')
        except Exception as e:
            self.fail('API异常', str(e)[:80], 'restart')

    def check_position_sync(self):
        synced = sync_exchange_to_state(self.cfg)
        try:
            ex = get_binance()
            symbol = self.cfg['symbol']
            positions = ex.fetch_positions([symbol])
            long_ex = any(p.get('symbol')==symbol and float(p.get('contracts',0))>0 and p.get('side')=='long' for p in positions)
            short_ex = any(p.get('symbol')==symbol and float(p.get('contracts',0))>0 and p.get('side')=='short' for p in positions)

            state_file = f"{self.cfg['task_dir']}/databases/state.json"
            state = json.load(open(state_file)) if os.path.exists(state_file) else {}
            st_long = state.get('long_pos') is not None
            st_short = state.get('short_pos') is not None

            if long_ex == st_long and short_ex == st_short:
                desc = f'一致 LONG={"有" if long_ex else "空"} SHORT={"有" if short_ex else "空"}'
                if any(synced.values()): desc += ' (已同步)'
                self.ok('持仓同步', desc)
            else:
                self.fail('持仓同步',
                         f'交易所LONG={long_ex}/SHORT={short_ex} vs state LONG={st_long}/SHORT={st_short}',
                         'sync_state')
        except Exception as e:
            self.fail('持仓同步', str(e)[:50], 'sync_state')

    def check_state_files(self):
        task_dir = self.cfg['task_dir']
        state_file = f'{task_dir}/databases/state.json'
        work_log = f'{task_dir}/logs/work_log.txt'
        if not os.path.exists(state_file):
            with open(state_file,'w') as f:
                json.dump({'long_pos':None,'short_pos':None,'last_long_signal':False,'last_short_signal':False}, f)
            self.ok('State文件', '已创建默认')
        else:
            with open(state_file) as f: s = json.load(f)
            qty = self.cfg['qty']
            lp = f"有{s['long_pos']['qty']}{'BTC' if self.name=='BTC' else 'HYPE'}" if s.get('long_pos') else '空'
            sp = f"有{s['short_pos']['qty']}{'BTC' if self.name=='BTC' else 'HYPE'}" if s.get('short_pos') else '空'
            self.ok('State文件', f'LONG={lp} SHORT={sp}')

        if os.path.exists(work_log):
            try:
                lines = open(work_log).readlines()
                self.ok('WorkLog', lines[-1].strip()[:60] if lines else '空')
            except:
                self.ok('WorkLog', '读取失败')
        else:
            self.ok('WorkLog', '不存在')

    def check_notify(self):
        notify_q = f"{self.cfg['task_dir']}/databases/notify_queue.json"
        try:
            if os.path.exists(notify_q):
                q = json.load(open(notify_q))
                items = q if isinstance(q, list) else [q]
                pending = sum(1 for x in items if isinstance(x, dict) and not x.get('sent', True))
                self.ok('通知队列', f'待发送{pending}条' if pending else '无积压')
            else:
                self.ok('通知队列', '无积压')
        except Exception as e:
            self.fail('通知队列', str(e)[:50])

    def do_fix(self, fix):
        try:
            task_dir = self.cfg['task_dir']
            log_file = f'{task_dir}/logs/auto_trade.log' if self.name=='BTC' else f'{task_dir}/logs/hype_trade.log'
            script = 'auto_trade.py' if self.name=='BTC' else 'hype_trade.py'
            if fix == 'restart':
                subprocess.run(['pkill','-f', script], capture_output=True)
                time.sleep(2)
                subprocess.Popen(
                    f'cd {task_dir} && nohup python3 -u {script} >> {log_file} 2>&1 &',
                    shell=True, preexec_fn=os.setsid)
                return f'{self.name} 已重启'
            elif fix == 'sync_state':
                synced = sync_exchange_to_state(self.cfg)
                done = [k for k,v in synced.items() if v]
                return f'{self.name} 已同步{",".join(done)}' if done else f'{self.name} 无需同步'
            elif fix == 'network':
                return '等待网络恢复'
            elif fix == 'retry':
                return '等待重试'
        except Exception as e:
            return f'修复失败:{e}'

    def run(self):
        log(f'── {self.label} ──')
        self.check_position_sync()
        self.check_process()
        self.check_api()
        self.check_state_files()
        self.check_notify()
        return self

def main():
    log('='*60)
    log('🔍 BTC + HYPE v4.2 双策略自检')
    log('='*60)

    all_results = []
    all_ok = 0; all_fail = 0
    all_fixes = []

    checks = []
    for name, cfg in STRATEGIES.items():
        cfg['name'] = name
        c = StrategyHealthChecker(name, cfg)
        c.run()
        checks.append(c)
        all_ok += c.checks_ok
        all_fail += c.checks_fail
        all_results.extend(c.results)
        all_fixes.extend(c._fixes)

    fixes_done = []
    for fix in set(all_fixes):
        for c in checks:
            r = c.do_fix(fix)
            if r: fixes_done.append(r)

    # 写自检日志
    report = {
        'time': datetime.now().isoformat(),
        'ok': all_ok, 'fail': all_fail,
        'items': all_results,
        'fixes': fixes_done
    }
    check_log = '/tmp/health_check_logs/check_log.json'
    logs = []
    if os.path.exists(check_log):
        try: logs = json.load(open(check_log))
        except: pass
    logs.append(report)
    with open(check_log,'w') as f:
        json.dump(logs[-50:], f, ensure_ascii=False, indent=2)

    # 写修复日志
    fix_log = '/tmp/health_check_logs/fix_log.txt'
    with open(fix_log,'a') as f:
        for item in all_results:
            if item['status'] == '❌':
                f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] ❌ [{item['strategy']}] {item['item']}: {item['detail']}\n")
        for fr in fixes_done:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] ✅ {fr}\n")

    # 有异常→发通知
    if all_fail > 0:
        snapshot = ''
        for c in checks:
            try:
                client = get_kline_client(c.cfg['kline_source'])
                sym = c.cfg['symbol_kline']
                k5m = client.fetch_ohlcv(sym, '5m', limit=100)
                k1h = client.fetch_ohlcv(sym, '1h', limit=200)
                r5 = calc_5m(k5m); r1 = calc_1h(k1h)
                if r5 and r1:
                    pct = (r5['price']-r5['sma20'])/r5['sma20']*100
                    snapshot += (f"\n📊 {c.label}: ${r5['price']:,.4f} | "
                                f"SMA5距{pct:+.1f}% | RSI{r5['rsi']:.0f} | "
                                f"1hADX{r1['adx_closed']:.0f} | 量比{r5['vol_ratio']:.1f}x")
            except:
                pass
        msg = f"🔴 双策略自检发现{all_fail}项问题{snapshot}\n"
        for item in all_results:
            if item['status'] == '❌':
                msg += f"• [{item['strategy']}] {item['item']}: {item['detail']}\n"
        if fixes_done:
            msg += f"\n🔧 已修复:\n" + '\n'.join(f'• {f}' for f in fixes_done)
        try:
            for c in checks:
                nq = f"{c.cfg['task_dir']}/databases/notify_queue.json"
                existing = []
                if os.path.exists(nq):
                    eq = json.load(open(nq))
                    existing = eq if isinstance(eq, list) else [eq]
                existing.append({'time': datetime.now().isoformat(), 'msg': msg, 'sent': False})
                with open(nq,'w') as f:
                    json.dump(existing, f, ensure_ascii=False, indent=2)
                break  # 只发一份通知
        except:
            pass

    log('='*60)
    log(f'📊 自检完成: {all_ok}✅ {all_fail}❌ {len(fixes_done)}已修复')
    for c in checks:
        log(f'  {c.label}: {c.checks_ok}✅ {c.checks_fail}❌')
    return report

if __name__ == '__main__':
    main()
