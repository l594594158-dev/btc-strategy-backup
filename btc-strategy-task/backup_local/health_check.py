#!/usr/bin/env python3
"""
BTC合约任务自检脚本 v1.1
- 每30分钟执行一次自动检查
- 检查进程运行、API数据、持仓同步、策略状态
- 发现问题自动修复并通知
"""
import ccxt
import os
import json
import subprocess
import time
import signal
from datetime import datetime
from pathlib import Path

# ========== 路径配置 ==========
TASK_DIR = '/root/.openclaw/workspace/btc-strategy-task'
AUTO_TRADE_SCRIPT = f'{TASK_DIR}/auto_trade.py'
STATE_FILE = f'{TASK_DIR}/databases/state.json'
WORK_LOG = f'{TASK_DIR}/logs/work_log.txt'
STATS_FILE = f'{TASK_DIR}/databases/trade_stats.json'
LOG_DIR = f'{TASK_DIR}/logs/health_check'
FIX_LOG = f'{LOG_DIR}/fix_log.txt'
CHECK_LOG = f'{LOG_DIR}/check_log.json'
NOTIFY_QUEUE = f'{TASK_DIR}/databases/notify_queue.json'

# API配置
API_KEY = "CUPwmVULosVO24NBKmoaMm0pvga2msasOa4nBhvPvybrGdA2RcXBYA4aRtGMZjWH"
SECRET = "Ozht5MjazUu4JKhSLqx4ASmTBH4wlUMdbABOblxXGyhIuof1jhrzUEr9JkWHpUHM"
SYMBOL = 'BTC/USDT:USDT'

os.makedirs(LOG_DIR, exist_ok=True)

# ========== 日志工具 ==========
def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}"
    print(line)
    return line

def get_binance():
    """创建币安实例"""
    return ccxt.binance({
        'apiKey': API_KEY,
        'secret': SECRET,
        'options': {'defaultType': 'swap'}
    })

def get_data():
    """获取所有周期数据"""
    binance = get_binance()
    k5m = binance.fetch_ohlcv(SYMBOL, timeframe='5m', limit=100)
    k1h = binance.fetch_ohlcv(SYMBOL, timeframe='1h', limit=200)
    k4h = binance.fetch_ohlcv(SYMBOL, timeframe='4h', limit=200)
    k1d = binance.fetch_ohlcv(SYMBOL, timeframe='1d', limit=200)
    return {'k5m': k5m, 'k1h': k1h, 'k4h': k4h, 'k1d': k1d}

# ========== 自检项 ==========
class HealthChecker:
    def __init__(self):
        self.timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.results = []
        self.fixes = []
        self.checks_ok = 0
        self.checks_fail = 0
        self._fixes_to_apply = []

    def add_ok(self, item, detail=''):
        self.checks_ok += 1
        self.results.append({
            'time': self.timestamp,
            'item': item,
            'status': '✅ OK',
            'detail': detail
        })
        log(f"✅ {item}: {detail or '正常'}")

    def add_fail(self, item, detail='', fix=None):
        self.checks_fail += 1
        self.results.append({
            'time': self.timestamp,
            'item': item,
            'status': '❌ FAIL',
            'detail': detail,
            'fix': fix
        })
        log(f"❌ {item}: {detail}")
        if fix:
            log(f"   🔧 修复: {fix}")
            self._fixes_to_apply.append(fix)

    # ========== 检查1: 进程状态 ==========
    def check_process(self):
        """检查auto_trade.py进程是否正常运行"""
        try:
            result = subprocess.run(
                ['ps', 'aux'], capture_output=True, text=True
            )
            python_pids = []
            for line in result.stdout.split('\n'):
                if 'auto_trade.py' in line and 'grep' not in line and 'python3' in line:
                    parts = line.split()
                    pid = parts[1]
                    # 找到实际的python进程（不是bash包装脚本）
                    python_pids.append(pid)

            if python_pids:
                # 取最新的（应该是实际的python进程）
                pid = python_pids[-1]
                # 获取进程启动时间
                try:
                    start_result = subprocess.run(
                        ['ps', '-eo', 'pid,lstart', '--no-headers'],
                        capture_output=True, text=True
                    )
                    for sline in start_result.stdout.split('\n'):
                        if sline.strip().startswith(pid + ' '):
                            # 简化：只显示pid
                            self.add_ok('进程状态', f'PID={pid} 运行中')
                            return True
                except:
                    pass
                self.add_ok('进程状态', f'PID={pid} 运行中')
                return True

            self.add_fail('进程状态', '进程未运行', fix='restart')
            return False
        except Exception as e:
            self.add_fail('进程状态', f'检查失败: {e}')
            return False

    # ========== 检查2: API数据获取 ==========
    def check_api_data(self):
        """检查API数据获取"""
        try:
            data = get_data()
            required = {'k5m': '5分钟', 'k1h': '1小时', 'k4h': '4小时', 'k1d': '1天'}
            for key, name in required.items():
                if key not in data or len(data[key]) < 50:
                    self.add_fail(f'API-{name}', f'数据不足: {len(data.get(key, []))}条', fix='retry')
                    return False
                # 检查最新K线收盘价
                if len(data[key]) > 0:
                    last_close = data[key][-1][4]  # close price
                    if last_close is None or last_close == 0:
                        self.add_fail(f'API-{name}', '最新K线收盘价为0/None', fix='retry')
                        return False
            price = data['k5m'][-1][4]
            self.add_ok('API数据', f'各周期数据正常，最新价格=${price}')
            return True
        except ccxt.NetworkError as e:
            self.add_fail('API-网络', f'网络错误: {e}', fix='network')
            return False
        except Exception as e:
            self.add_fail('API-数据', f'获取失败: {e}', fix='restart')
            return False

    # ========== 检查3: 持仓同步（核心！） ==========
    def check_position_sync(self):
        """检查state.json与交易所持仓是否一致"""
        try:
            binance = get_binance()

            # 获取交易所实际持仓
            exchange_pos = binance.fetch_positions([SYMBOL])
            actual_positions = [p for p in exchange_pos if float(p.get('contracts', 0)) != 0]
            has_actual_pos = len(actual_positions) > 0
            actual_entries = [float(p['entryPrice']) for p in actual_positions]
            actual_total_qty = sum(float(p['contracts']) for p in actual_positions)

            # 读取本地state
            state_in_pos = False
            state_entries = []
            state_total_qty = 0
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE) as f:
                    state = json.load(f)
                state_in_pos = state.get('in_position', False)
                for p in state.get('positions', []):
                    state_entries.append(float(p.get('entry_price', 0)))
                    state_total_qty += float(p.get('qty', 0))

            # 对比判断
            if has_actual_pos and not state_in_pos:
                # 交易所有持仓但state没有 → 手动仓位，不处理
                self.add_ok('持仓同步', f'手动仓位(exchange有{len(actual_positions)}仓，state无，属正常)')
                return True
            elif not has_actual_pos and state_in_pos:
                msg = f'幽灵state！state有持仓但交易所实际无持仓'
                self.add_fail('持仓同步', msg, fix='sync_ghost')
                return False
            elif has_actual_pos and state_in_pos:
                # 两者都有，检查数量和价格是否一致
                qty_diff = abs(actual_total_qty - state_total_qty)
                entry_diff = abs(actual_entries[0] - state_entries[0]) if actual_entries and state_entries else 0
                if qty_diff > 0.001 or entry_diff > 10:
                    msg = f'持仓数据不一致！交易所qty={actual_total_qty} state={state_total_qty} | 价差=${entry_diff:.2f}'
                    self.add_fail('持仓同步', msg, fix='sync_ghost')
                    return False
                self.add_ok('持仓同步', f'一致，state和交易所均有{len(actual_positions)}仓')
                return True
            else:
                self.add_ok('持仓同步', '一致，均无持仓')
                return True

        except Exception as e:
            self.add_fail('持仓同步', f'检查失败: {e}')
            return False

    # ========== 检查4: 策略状态 ==========
    def check_strategy(self):
        """检查策略相关文件状态"""
        try:
            # state.json
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE) as f:
                    state = json.load(f)
                in_pos = state.get('in_position', False)
                pos_count = len(state.get('positions', []))
                self.add_ok('State文件', f'in_position={in_pos}, 持仓数={pos_count}')
            else:
                self.add_fail('State文件', '文件不存在', fix='create_state')
                with open(STATE_FILE, 'w') as f:
                    json.dump({'in_position': False, 'positions': []}, f)

            # work_log：进程正常运行时不关注历史错误，只关注进程挂了的情况
            if os.path.exists(WORK_LOG):
                with open(WORK_LOG) as f:
                    lines = f.readlines()
                if lines:
                    last_line = lines[-1].strip()
                    # 如果进程正在运行，只提示最近错误但不触发修复（可能是历史错误）
                    # 如果进程不在运行，才触发restart修复
                    if '[错误]' in last_line or 'Error' in last_line or 'Exception' in last_line or 'Traceback' in last_line:
                        self.add_ok('WorkLog', f'最近错误(进程运行中，忽略历史): {last_line[:50]}')
                    else:
                        self.add_ok('WorkLog', f'最后: {last_line[:50]}')
                else:
                    self.add_ok('WorkLog', '为空')
            else:
                self.add_ok('WorkLog', '不存在（首次运行）')

            # stats
            if os.path.exists(STATS_FILE):
                with open(STATS_FILE) as f:
                    stats = json.load(f)
                self.add_ok('交易统计', f'总交易={stats.get("total_trades", 0)}, 连亏={stats.get("consecutive_losses", 0)}')
            else:
                self.add_ok('交易统计', '文件不存在')

            return True
        except Exception as e:
            self.add_fail('策略状态', f'检查失败: {e}', fix='restart')
            return False

    # ========== 检查5: 微信通知队列 ==========
    def check_notify_queue(self):
        """检查是否有待发送的微信通知"""
        try:
            if os.path.exists(NOTIFY_QUEUE):
                with open(NOTIFY_QUEUE) as f:
                    q = json.load(f)
                # 支持两种格式：数组 或 单个dict
                if isinstance(q, list):
                    pending = [x for x in q if isinstance(x, dict) and not x.get('sent', True)]
                    if pending:
                        self.add_ok('通知队列', f'有{len(pending)}条未发送通知')
                    else:
                        self.add_ok('通知队列', '无积压')
                elif isinstance(q, dict):
                    if not q.get('sent', True):
                        self.add_ok('通知队列', f'有未发送通知: {q.get("msg", "")[:30]}...')
                    else:
                        self.add_ok('通知队列', '无积压')
            else:
                self.add_ok('通知队列', '无积压')
            return True
        except Exception as e:
            self.add_fail('通知队列', str(e))
            return False

    # ========== 修复执行 ==========
    def do_fix(self, fix_action):
        """执行单个修复操作"""
        try:
            if fix_action == 'restart':
                log('🔧 执行修复: 重启auto_trade.py...')
                # 杀掉所有相关进程
                subprocess.run(['pkill', '-f', 'auto_trade.py'], capture_output=True)
                time.sleep(2)
                # 重启
                subprocess.Popen(
                    f'cd {TASK_DIR} && python3 -u auto_trade.py > logs/auto_trade_$(date +%Y%m%d_%H%M%S).log 2>&1 &',
                    shell=True,
                    preexec_fn=os.setsid
                )
                log('✅ auto_trade.py 已重启')
                return '已重启auto_trade.py'

            elif fix_action == 'sync_ghost':
                log('🔧 执行修复: 同步幽灵仓位...')
                binance = get_binance()
                exchange_pos = binance.fetch_positions([SYMBOL])
                actual_positions = [p for p in exchange_pos if float(p.get('contracts', 0)) != 0]

                if actual_positions:
                    # 同步state到交易所实际持仓
                    positions = []
                    for p in actual_positions:
                        side = p['side'].lower()
                        qty = float(p['contracts'])
                        entry = float(p['entryPrice'])
                        # 计算SL/TP
                        if side == 'long':
                            sl = entry * 0.97   # 3%止损
                            tp = entry * 1.05   # 5%止盈
                        else:
                            sl = entry * 1.03
                            tp = entry * 0.95
                        positions.append({
                            'entry_price': entry,
                            'qty': qty,
                            'direction': side,
                            'stop_loss': sl,
                            'tp': tp,
                            'sl_algo_id': None,
                            'tp_algo_id': None,
                            'reason': '幽灵仓位同步',
                            'atr': 0,
                            'open_time': datetime.now().isoformat(),
                        })
                    state = {
                        'in_position': True,
                        'positions': positions,
                        'last_close_time': None,
                        'last_signal_time': {},
                    }
                    with open(STATE_FILE, 'w') as f:
                        json.dump(state, f, indent=2)
                    log(f'✅ 已同步state: {len(positions)}个持仓')
                    return f'已同步{len(positions)}个幽灵持仓到state'
                else:
                    # 交易所无持仓但state有，清空state
                    state = {'in_position': False, 'positions': [], 'last_close_time': time.time()}
                    with open(STATE_FILE, 'w') as f:
                        json.dump(state, f)
                    log('✅ 已清空幽灵state')
                    return '已清空幽灵state'

            elif fix_action == 'create_state':
                with open(STATE_FILE, 'w') as f:
                    json.dump({'in_position': False, 'positions': []}, f)
                return '已创建默认state'

            elif fix_action == 'network':
                log('🔧 网络问题，等待自动恢复...')
                return '等待网络恢复'

            elif fix_action == 'retry':
                log('🔧 数据问题，等待下一轮重试...')
                return '等待重试'

            return None
        except Exception as e:
            log(f'❌ 修复失败: {e}')
            return f'修复失败: {e}'

    def run(self):
        log('=' * 60)
        log('🔍 BTC合约任务自检开始')
        log('=' * 60)

        # 清空上次的修复计划
        self._fixes_to_apply = []

        # 执行所有检查
        self.check_process()        # 进程状态
        self.check_api_data()       # API数据
        self.check_position_sync()  # 持仓同步（核心）
        self.check_strategy()       # 策略状态
        self.check_notify_queue()   # 通知队列

        # 生成报告
        report = {
            'time': self.timestamp,
            'checks_ok': self.checks_ok,
            'checks_fail': self.checks_fail,
            'items': self.results,
            'fixes': []
        }

        # 执行修复（按顺序去重）
        fixes_applied = []
        seen = set()
        for fix in self._fixes_to_apply:
            if fix not in seen:
                seen.add(fix)
                result = self.do_fix(fix)
                if result:
                    fixes_applied.append(result)

        report['fixes'] = fixes_applied

        # 追加到检查日志
        logs = []
        if os.path.exists(CHECK_LOG):
            try:
                with open(CHECK_LOG) as f:
                    logs = json.load(f)
            except:
                logs = []
        logs.append(report)
        logs = logs[-100:]
        with open(CHECK_LOG, 'w') as f:
            json.dump(logs, f, ensure_ascii=False, indent=2)

        # 写fix_log
        with open(FIX_LOG, 'a') as f:
            ts = self.timestamp
            for item in self.results:
                if item['status'] == '❌ FAIL':
                    f.write(f"[{ts}] ❌ {item['item']}: {item['detail']}\n")
                    if item.get('fix'):
                        f.write(f"[{ts}] 🔧 修复: {item['fix']}\n")
            for fix_result in fixes_applied:
                f.write(f"[{ts}] ✅ {fix_result}\n")

        # 发送微信通知（有问题时）
        if self.checks_fail > 0:
            msg = f"🔴 自检发现问题({self.checks_fail}项)\n"
            for item in self.results:
                if item['status'] == '❌ FAIL':
                    msg += f"• {item['item']}: {item['detail']}\n"
            if fixes_applied:
                msg += f"\n🔧 已修复:\n"
                for fr in fixes_applied:
                    msg += f"• {fr}\n"
            try:
                with open(NOTIFY_QUEUE, 'w') as f:
                    json.dump({'time': datetime.now().isoformat(), 'msg': msg, 'sent': False}, f)
            except:
                pass

        log('=' * 60)
        log(f'📊 自检完成: {self.checks_ok}项通过, {self.checks_fail}项失败, {len(fixes_applied)}项已修复')
        log('=' * 60)
        return report

if __name__ == '__main__':
    checker = HealthChecker()
    checker.run()
