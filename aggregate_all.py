#!/usr/bin/env python3
"""llama 历史 + Strata 新账本 → usage-site/data.json

背景：2026-10-15 本机主力从 llama-server 换成 Strata。原 aggregate.py 解析的是
llama-server 日志（`slot print_timing: … prompt eval time = X ms / N tokens`），
Strata 的日志格式完全不同，但它的 /metrics 暴露了一个**带绝对时间戳的请求数组**，
比日志考古干净得多。所以本脚本：

  1) 先调用原 aggregate.py —— llama 时代的历史照旧重建（llama 日志不再增长，但历史仍在里面）
  2) 再读 Strata 请求账本 ~/playground/strata-ab/strata_requests.jsonl，并进同一形状
  3) 追加两类看板需要的派生量：
       · 缓存命中（两个引擎的计数器语义不同，必须归一化到同一对量再相加）：
           llama  —— metrics_history.jsonl 的累计计数器取增量：hit=Δcached，基数=Δcached+Δprompt
                     （prometheus HELP 明确二者不相交：read vs "cache already held"）
           Strata —— 请求账本逐条：hit=reused，基数=prompt_tokens（=prompt_total，整体提示长度）
         统一写成 days[].cached / days[].cache_total，页面只做 Σcached/Σcache_total
       · 峰值吞吐：days[].dec_peak / dec_peak_at —— 该日**单请求** decode_tok_s 的最大值
     （小时均值会显著低估峰值：同一引擎单请求 59.3 t/s，按小时只有 26 t/s）
  4) 探测本机引擎，写 data.engine（页面展示"当前使用的引擎"）
  5) 原子写回 data.json

槽位：Strata 引擎没有批处理路径（单请求），所以它的流量全部记在 s0，s1 保留 llama 时代的历史值。
小时桶：若该小时 llama 已有数据则保留，否则用 Strata 的。
"""
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'data.json')
LEDGER = os.path.expanduser('~/playground/strata-ab/strata_requests.jsonl')
LLAMA_METRICS = os.path.expanduser('~/playground/ggufs/metrics_history.jsonl')
# 探测引擎时需要一个 API key：优先读环境变量 LLM_API_KEY_FILE 指向的文件，
# 其次回退到默认路径。密钥文件永远不要提交进仓库（已在 .gitignore 之外单独存放）。
KEYFILE = os.environ.get('LLM_API_KEY_FILE') or os.path.expanduser('~/playground/ggufs/all_api_keys.txt')
BASE = os.environ.get('STRATA_URL', 'http://127.0.0.1:8080')


def api_key():
    try:
        with open(KEYFILE) as f:
            for line in f:
                s = line.strip()
                if s and not s.startswith('#'):
                    return s
    except Exception:
        pass
    return ''


def http_json(path, key, timeout=6):
    req = urllib.request.Request(BASE + path)
    if key:
        req.add_header('Authorization', 'Bearer ' + key)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8', 'replace'))


def http_text(path, key, timeout=6):
    req = urllib.request.Request(BASE + path)
    if key:
        req.add_header('Authorization', 'Bearer ' + key)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', 'replace')


def probe_engine(prev):
    """探测本机在跑的引擎。失败时保留上一次的结果（只把 up 置 false）。"""
    key = api_key()
    info = dict(prev or {})
    try:
        m = http_json('/v1/models', key)
        first = (m.get('data') or [{}])[0]
        info['model'] = first.get('id') or info.get('model')
        meta = first.get('meta') or {}
        if meta.get('n_ctx'):
            info['ctx'] = int(meta['n_ctx'])
        arch = first.get('architecture') or {}
        mods = arch.get('input_modalities') or []
        info['vision'] = 'image' in mods
        # 引擎身份的判别式：Strata 的 prometheus 里有 strata:* 计数器，llama.cpp 没有
        prom = http_text('/metrics/prometheus', key)
        info['name'] = 'Strata' if 'strata:' in prom else 'llama.cpp'
        base = 'Metal' if 'strata:' in prom else 'llama.cpp'
        info['backend'] = base
        try:
            j = http_json('/metrics', key)
            if j.get('version'):
                info['version'] = str(j['version'])
        except Exception:
            pass
        info['up'] = True
        info['checked'] = datetime.now().isoformat(timespec='seconds')
    except Exception as e:
        info['up'] = False
        info['error'] = type(e).__name__
        info['checked'] = datetime.now().isoformat(timespec='seconds')
    return info


# --- 1) llama 历史（保持原样）---
subprocess.run([sys.executable, os.path.join(HERE, 'aggregate.py')], capture_output=True, timeout=300)
try:
    data = json.load(open(OUT))
except Exception:
    data = {'days': [], 'hours': []}

days = {d['date']: d for d in data.get('days', [])}
hours = {h['hour']: h for h in data.get('hours', [])}
llama_days = len(days)
llama_hours = len(hours)

# --- 2) Strata 账本 ---
agg_days = {}   # date -> [prompt_tokens, output_tokens, prompt_ms, decode_ms]
agg_hours = {}  # 'YYYY-MM-DDTHH' -> [prompt_ms, prompt_tok, decode_ms, decode_tok]
cache_strata = {}   # date -> [hit_tokens, basis_tokens]
peak = {}           # date -> [max_decode_tok_s, 'HH:MM']
mtp_days = {}       # date -> [drafts_offered, drafts_accepted]
ttft_by_day = {}    # date -> [prompt_ms, ...]
scatter = []        # 每请求：{t, rate, depth, acc} —— 速率 vs 投机接受率（本机掉速的真因）
top_req = []        # 峰值榜候选
# 上下文长度分档（反映真实工作形态：这台机器绝大多数是 3-10 万 token 的长会话）
DEPTH_BUCKETS = [(0, 1000, '<1K'), (1000, 8000, '1–8K'), (8000, 32000, '8–32K'),
                 (32000, 100000, '32–100K'), (100000, 10 ** 9, '>100K')]
depth_hist = {name: 0 for _, _, name in DEPTH_BUCKETS}
n_req = 0
if os.path.exists(LEDGER):
    with open(LEDGER, errors='replace') as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            t = r.get('time')
            if t is None:
                continue
            ts = datetime.fromtimestamp(float(t))
            day = ts.strftime('%Y-%m-%d')
            hr = ts.strftime('%Y-%m-%dT%H')
            pt = int(r.get('prompt_tokens') or 0)
            oo = int(r.get('output_tokens') or 0)
            pms = float(r.get('prompt_ms') or 0.0)
            dms = float(r.get('decode_ms') or 0.0)
            a = agg_days.setdefault(day, [0, 0, 0.0, 0.0])
            a[0] += pt; a[1] += oo; a[2] += pms; a[3] += dms
            h = agg_hours.setdefault(hr, [0.0, 0, 0.0, 0])
            h[0] += pms; h[1] += pt; h[2] += dms; h[3] += oo
            # 缓存命中：Strata 口径 hit=reused，基数=prompt_tokens
            c = cache_strata.setdefault(day, [0, 0])
            c[0] += int(r.get('reused') or 0)
            c[1] += pt
            # 投机解码（MTP）按天累计
            _m = mtp_days.setdefault(day, [0, 0])
            _m[0] += int(r.get('drafts_offered') or 0)
            _m[1] += int(r.get('drafts_accepted') or 0)
            # 上下文长度分档
            for _lo, _hi, _name in DEPTH_BUCKETS:
                if _lo <= pt < _hi:
                    depth_hist[_name] += 1
                    break
            # 首 token 延迟（prompt_ms = 预填耗时 = TTFT）
            if r.get('prompt_ms'):
                ttft_by_day.setdefault(day, []).append(float(r['prompt_ms']))
            # 单请求峰值 + 散点
            dts = r.get('decode_tok_s')
            if oo >= 8 and dts:          # 太短的回答速率无意义，剔除噪声
                dts = float(dts)
                cur = peak.get(day)
                if cur is None or dts > cur[0]:
                    peak[day] = [round(dts, 1), ts.strftime('%H:%M')]
                _off, _acc = int(r.get('drafts_offered') or 0), int(r.get('drafts_accepted') or 0)
                scatter.append({'t': int(t), 'rate': round(dts, 1), 'depth': pt,
                                'acc': round(_acc / _off, 3) if _off else None, 'out': oo})
                top_req.append({'t': ts.strftime('%m-%d %H:%M'), 'rate': round(dts, 1),
                                'depth': pt, 'out': oo})
            n_req += 1

for day, (pt, oo, pms, dms) in agg_days.items():
    d = days.setdefault(day, {'date': day, 's0': 0, 's1': 0, 'count': 0, 'prompt': 0, 'gen': 0})
    d['s0'] = d.get('s0', 0) + pt
    d['prompt'] = d.get('prompt', 0) + pt
    d['gen'] = d.get('gen', 0) + oo
    d['count'] = d.get('s0', 0) + d.get('s1', 0)

for hr, (pms, pt, dms, oo) in agg_hours.items():
    if hr in hours:
        continue  # llama 已有该小时（两个源时间不重叠）；宁可不覆盖历史
    hours[hr] = {
        'hour': hr,
        's0_pre_tps': round(pt / (pms / 1000.0), 1) if pms else 0,
        's0_dec_tps': round(oo / (dms / 1000.0), 1) if dms else 0,
        's1_pre_tps': 0,
        's1_dec_tps': 0,
    }

# --- 3) 缓存命中：llama 时代的累计计数器取增量 ---
llama_cache_days = 0
if os.path.exists(LLAMA_METRICS):
    prev = None
    with open(LLAMA_METRICS, errors='replace') as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            ts = r.get('ts')
            if not ts or r.get('prompt') is None or r.get('cached') is None:
                continue
            try:
                day = datetime.strptime(ts[:19], '%Y-%m-%dT%H:%M:%S').strftime('%Y-%m-%d')
            except Exception:
                continue
            cur = (int(r.get('cached') or 0), int(r.get('prompt') or 0), r.get('pid'))
            # 只按"值的增量"取，不看 pid。实测：计数器会跨 pid 连续（同一进程被读到不同 pid，
            # 如 3178→77399 值从 41,249,300 接到 41,225,600），也会小幅回落（读抖动）。
            # 把这两种当"重启"并全额计入会系统性放大 —— 实测 311M vs 按进程分段真实 186M（1.67×）。
            # 规则：涨 → 取差；腰斩以下 → 当真重启、计当前累计；其余 → 计 0。
            if prev is None:
                dh, db = cur[0], cur[0] + cur[1]
            elif cur[0] >= prev[0] and cur[1] >= prev[1]:
                dh, db = cur[0] - prev[0], (cur[0] + cur[1]) - (prev[0] + prev[1])
            elif cur[0] < prev[0] * 0.5:
                dh, db = cur[0], cur[0] + cur[1]
            else:
                dh, db = 0, 0
            if db > 0:
                d = days.setdefault(day, {'date': day, 's0': 0, 's1': 0, 'count': 0, 'prompt': 0, 'gen': 0})
                d['cached'] = d.get('cached', 0) + dh
                d['cache_total'] = d.get('cache_total', 0) + db
                llama_cache_days += 1
            prev = cur

# Strata 侧的缓存命中并进同名的 day 记录（同一天可能两段都有，直接相加）
for day, (hit, base) in cache_strata.items():
    d = days.setdefault(day, {'date': day, 's0': 0, 's1': 0, 'count': 0, 'prompt': 0, 'gen': 0})
    d['cached'] = d.get('cached', 0) + hit
    d['cache_total'] = d.get('cache_total', 0) + base

# 峰值吞吐（单请求）
for day, (v, at) in peak.items():
    d = days.setdefault(day, {'date': day, 's0': 0, 's1': 0, 'count': 0, 'prompt': 0, 'gen': 0})
    if v > (d.get('dec_peak') or 0):
        d['dec_peak'], d['dec_peak_at'] = v, at

# --- 3b) 投机解码：llama 时代同样从累计计数器取增量（与缓存同一套"只看值增量"规则）---
llama_last_ts = None
if os.path.exists(LLAMA_METRICS):
    prev = None
    with open(LLAMA_METRICS, errors='replace') as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            ts = r.get('ts')
            if not ts or r.get('draft') is None or r.get('accepted') is None:
                continue
            llama_last_ts = ts
            try:
                day = datetime.strptime(ts[:19], '%Y-%m-%dT%H:%M:%S').strftime('%Y-%m-%d')
            except Exception:
                continue
            cur = (int(r.get('draft') or 0), int(r.get('accepted') or 0))
            if prev is None:
                do, da = cur
            elif cur[0] >= prev[0] and cur[1] >= prev[1]:
                do, da = cur[0] - prev[0], cur[1] - prev[1]
            elif cur[0] < prev[0] * 0.5:
                do, da = cur
            else:
                do, da = 0, 0
            if do:
                m = mtp_days.setdefault(day, [0, 0])
                m[0] += do
                m[1] += da
            prev = cur

for day, (off, acc) in mtp_days.items():
    if not off:
        continue
    d = days.setdefault(day, {'date': day, 's0': 0, 's1': 0, 'count': 0, 'prompt': 0, 'gen': 0})
    d['draft_offered'] = d.get('draft_offered', 0) + off
    d['draft_accepted'] = d.get('draft_accepted', 0) + acc

# 首 token 延迟（按天 p50/p90/max；prompt_ms 即预填耗时）
ttft_days = {}
for day, vals in ttft_by_day.items():
    if not vals:
        continue
    v = sorted(vals)
    ttft_days[day] = {
        'p50': round(v[int(len(v) * 0.5)] / 1000, 2),
        'p90': round(v[min(len(v) - 1, int(len(v) * 0.9))] / 1000, 2),
        'max': round(v[-1] / 1000, 2),
        'n': len(v),
    }

# --- 3c) 服务压力：并发=1 到底有没有真的排队（决定要不要移植批处理的唯一依据）---
pressure = {'samples': [], 'busy_pct': 0, 'deferred_n': 0, 'deferred_total': 0, 'req_per_hour': 0}
MON = os.path.expanduser('~/playground/strata-ab/strata-monitor.jsonl')
if os.path.exists(MON):
    rows = []
    with open(MON, errors='replace') as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    if rows:
        busy = sum(1 for r in rows if (r.get('queue_processing') or 0) > 0)
        defr = [int(r.get('queue_deferred') or 0) for r in rows]
        # 每个采样点的"本区间新增请求数"：悬停要看的是这个。累计值本身没意义，
        # 而且引擎重启会把计数器清零（首末相减会得出负数/1 req/h 这种假值）。
        samples = []
        prev_q = None
        for r in rows:
            q = int(r.get('strata_requests') or 0)
            dq = 0 if prev_q is None else (q - prev_q if q >= prev_q else q)
            samples.append({'t': str(r.get('ts', ''))[11:16],
                            'busy': int(r.get('queue_processing') or 0),
                            'deferred': int(r.get('queue_deferred') or 0),
                            'reqs': q, 'dreq': max(0, dq)})
            prev_q = q
        pressure = {
            'samples': samples,
            'busy_pct': round(busy / len(rows) * 100),
            'deferred_n': sum(1 for x in defr if x),
            'deferred_total': sum(defr),
            'req_per_hour': 0,
        }
        if len(rows) >= 2:
            try:
                a = datetime.fromisoformat(str(rows[0]['ts'])[:19])
                b = datetime.fromisoformat(str(rows[-1]['ts'])[:19])
                hrs = (b - a).total_seconds() / 3600.0
                # 逐样本取"正向增量"再求和：该计数器是引擎进程内累计的，引擎重启会归零。
                # 实测首末相减会得出 1 req/h，而真实约 48 req/h（切换引擎那次把计数器清了）。
                dr = 0
                for i in range(1, len(rows)):
                    d0 = int(rows[i - 1].get('strata_requests') or 0)
                    d1 = int(rows[i].get('strata_requests') or 0)
                    dr += max(0, d1 - d0) if d1 >= d0 else d1
                if hrs > 0:
                    pressure['req_per_hour'] = round(dr / hrs)
            except Exception:
                pass

# --- 3d) 来源账本：本机 / 局域网 / frp 隧道 各服务了多少请求、排队多久、占用多久 -------------
# 由 serve/server.py 的 PriorityGate 在每次回合结束时追加一行（来源取自 HTTP 对端地址）。
# 排队时长就是优先级的直接效果：本机/局域网应该接近 0，frp 会更长。
SRC = os.path.expanduser('~/playground/usage-site/source_ledger.jsonl')
_src = {}
src_days = {}          # 'YYYY-MM-DD' -> {source: [请求数, 输出 token]}
_fail = {}             # 来源 -> 失败/放弃次数（长期健康度的核心指标）
_finish_days = {}      # 'YYYY-MM-DD' -> {finish: 次数} —— 健康度趋势面板的数据源
if os.path.exists(SRC):
    with open(SRC, errors='replace') as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            k = r.get('source') or 'internal'
            if float(r.get('hold_ms') or 0) <= 0:
                # 没有 wait/hold 的行分两类，不能一刀切丢掉：
                #  · 失败/放弃（_json / handle_one_request 写的）→ 单独计数
                #  · 幽灵微回合 → 丢掉，避免灌水
                if r.get('http_status') or r.get('finish') in ('error', 'abandoned'):
                    _fail[k] = _fail.get(k, 0) + 1
                    # 失败也要进健康度趋势 —— 否则"错误"永远只有一个累计数、没有趋势
                    _fday = str(r.get('ts', ''))[:10]
                    if _fday:
                        _fs2 = r.get('finish') or ('http_%s' % r.get('http_status'))
                        _finish_days.setdefault(_fday, {})
                        _finish_days[_fday][_fs2] = _finish_days[_fday].get(_fs2, 0) + 1
                continue
            b = _src.setdefault(k, {'n': 0, 'wait': [], 'hold': [], 'out': 0, 'in': 0, 'peers': {},
                                    'ctx': [], 'pre': [], 'dec': []})
            b['n'] += 1
            _peer = r.get('peer')
            if _peer:
                b['peers'][_peer] = b['peers'].get(_peer, 0) + 1
            b['wait'].append(float(r.get('wait_ms') or 0))
            b['hold'].append(float(r.get('hold_ms') or 0))
            b['out'] += int(r.get('generated') or 0)
            b['in'] += int(r.get('prompt_tokens') or 0)     # 输入(上下文)总量：看谁在烧预填
            if r.get('prompt_tokens'):
                b['ctx'].append(int(r['prompt_tokens']))    # 上下文长度 = 引擎报的总 prompt
            if r.get('pre_tok_s'):
                b['pre'].append(float(r['pre_tok_s']))      # 真 prefill t/s（引擎计时）
            if r.get('dec_tok_s'):
                b['dec'].append(float(r['dec_tok_s']))      # 真 decode t/s（引擎计时）
            # 按天×来源：[请求数, 输出 token, 输入 token]
            # 输入也要 —— 否则日历切到"输入"维度时无数据可画。
            _day = str(r.get('ts', ''))[:10]
            if _day:
                _e = src_days.setdefault(_day, {}).setdefault(k, [0, 0, 0])
                _e[0] += 1
                _e[1] += int(r.get('generated') or 0)
                _e[2] += int(r.get('prompt_tokens') or 0)
            # 按天×收尾状态：{"2026-10-15": {"stop": 41, "cancel": 2, "length": 1, "error": 3}}
            # 健康度趋势唯一的数据源 —— 只记"跑完的回合"的时代，失败/取消根本看不见。
            if _day:
                _fs = r.get('finish') or 'ghost'
                _finish_days.setdefault(_day, {})
                _finish_days[_day][_fs] = _finish_days[_day].get(_fs, 0) + 1


def _pct(vals, q):
    if not vals:
        return 0
    s = sorted(vals)
    return round(s[min(len(s) - 1, int(len(s) * q))])


traffic_by_source = {k: {'n': v['n'],
                         'wait_p50_ms': _pct(v['wait'], .5), 'wait_p90_ms': _pct(v['wait'], .9),
                         'hold_p50_ms': _pct(v['hold'], .5), 'out_tokens': v['out'], 'in_tokens': v['in'],
                         'fail': _fail.get(k, 0),
                         'peers': v['peers'],
                         # 细项连同样本数一起给出 —— 样本少时 P90 会跳，页面上得能看出可信度
                         'ctx_p50': _pct(v['ctx'], .5), 'ctx_p90': _pct(v['ctx'], .9), 'ctx_n': len(v['ctx']),
                         'pre_p50': _pct(v['pre'], .5), 'pre_p90': _pct(v['pre'], .9), 'pre_n': len(v['pre']),
                         'dec_p50': _pct(v['dec'], .5), 'dec_p90': _pct(v['dec'], .9), 'dec_n': len(v['dec'])}
                     for k, v in _src.items()}

engines = []
if llama_last_ts:
    engines.append({'name': 'llama.cpp', 'to': llama_last_ts[:16]})
# 用"账本首条"作为 Strata 起点，而不是首个通过测速筛选的请求 ——
# 后者比真实切换晚 17 分钟（前几条输出 <8 token），会把切换时刻说错。
if os.path.exists(LEDGER):
    with open(LEDGER, errors='replace') as f:
        for line in f:
            try:
                _r = json.loads(line)
            except Exception:
                continue
            if _r.get('time'):
                engines.append({'name': 'Strata',
                                'from': datetime.fromtimestamp(float(_r['time'])).strftime('%Y-%m-%dT%H:%M')})
                break

# --- 日汇总进 SQLite（可长期保留，与明细清理解耦）---
# 为什么是数据库而不是 JSON 文件：① 事务保证 —— 聚合器中途被杀不会留下半截文件，
#    而"读全量 → 改 → 覆写"的 JSON 恰好有这个风险（本聚合器每 2 分钟被 cron 拉一次）；
#    ② 可查询 —— 将来算月度/季度趋势不必把全部明细读进内存。
# 明细（source_ledger.jsonl）继续是 append-only JSONL：写它的是服务进程，让服务为记账承担锁风险不值得。
import sqlite3
DB = os.path.expanduser('~/playground/usage-site/usage.db')
_con = sqlite3.connect(DB)
_con.execute('PRAGMA journal_mode=WAL')
_con.execute('CREATE TABLE IF NOT EXISTS rollup_days (day TEXT PRIMARY KEY, src TEXT, finish TEXT)')
rollup = {}
for _day, _srcj, _finj in _con.execute('SELECT day, src, finish FROM rollup_days'):
    rollup[_day] = {}
    if _srcj:
        try: rollup[_day]['src'] = json.loads(_srcj)
        except Exception: pass
    if _finj:
        try: rollup[_day]['finish'] = json.loads(_finj)
        except Exception: pass
for _day, _per in src_days.items():
    rollup.setdefault(_day, {})['src'] = _per
for _day, _fin in _finish_days.items():
    rollup.setdefault(_day, {})['finish'] = _fin
# 幂等 upsert：同一天重复跑写回同样的值，不会累积副本
_con.executemany(
    'INSERT INTO rollup_days (day, src, finish) VALUES (?, ?, ?) '
    'ON CONFLICT(day) DO UPDATE SET src=excluded.src, finish=excluded.finish',
    [(_d, json.dumps(_v.get('src') or {}, ensure_ascii=False),
         json.dumps(_v.get('finish') or {}, ensure_ascii=False)) for _d, _v in rollup.items()])
_con.commit()
_con.close()

# 把按天×来源并进每天的记录（页面直接读 days[].src 画占比/柱）。
# 数据来源是 rollup 而不是"本次账本"—— 这样即使明细被清理，历史仍在。
for _day, _rec in rollup.items():
    _per = _rec.get('src')
    if not _per:
        continue
    _d = days.setdefault(_day, {'date': _day, 's0': 0, 's1': 0, 'count': 0, 'prompt': 0, 'gen': 0})
    _d['src'] = _per
    # 来源账本（Strata 时代）的 token 必须并进当天总量 —— 否则日汇总/热力图/态势行
    # 只算 llama 日志那部分，Strata 的消耗被整段漏掉。
    # 实测：10-15 的 prompt 与 cache_total 完全相同（14,373,289），而账本里还有 425,473
    # 输入没被算进去。今天只差 3%，等 Strata 累积起来会变成全量少算。
    _d['prompt'] = (_d.get('prompt') or 0) + sum((v[2] or 0) for v in _per.values())
    _d['gen'] = (_d.get('gen') or 0) + sum((v[1] or 0) for v in _per.values())
    _d['count'] = (_d.get('count') or 0) + sum(((v[1] or 0) + (v[2] or 0)) for v in _per.values())

# --- 峰值与长尾：直接从账本重建两张榜 ---
# 旧表只有 时间/rate/上下文/输出 —— 看不出"谁跑的""排了多久""占用了多久"。
# 账本里这些字段都有（source/ua/wait_ms/hold_ms/reused），所以重建而不是扩展旧表。
def _f(v, d=0.0):
    try:
        return float(v)
    except Exception:
        return d
def _i(v, d=0):
    try:
        return int(float(v))
    except Exception:
        return d

_reqs = []
if os.path.exists(SRC):
    with open(SRC, errors='replace') as f:
        for line in f:
            try:
                o = json.loads(line)
            except Exception:
                continue
            if not o.get('ts'):
                continue
            w = _f(o.get('wait_ms'))
            # 幽灵行（引擎内部微回合）不进榜：它没服务任何用户请求
            if w <= 0 and (o.get('finish') or '') == 'ghost':
                continue
            _rt = _f(o.get('dec_tok_s'))
            if not (0 < _rt < 500):        # 0 = 没测到；>500 = 掉速异常值
                _rt = 0.0
            _reqs.append({
                't': str(o.get('ts'))[:16].replace('T', ' '),
                'src': o.get('source') or '?',
                'ua': (o.get('ua') or '')[:28],
                'rate': round(_rt, 1),
                'pre': round(_f(o.get('pre_tok_s')), 1),
                'ctx': _i(o.get('prompt_tokens')),
                'gen': _i(o.get('generated')),
                'wait': round(w),
                'hold': round(_f(o.get('hold_ms')) / 1000.0, 1),
                'reused': _i(o.get('reused')),
                'finish': o.get('finish') or '',
            })
# 快榜：真的生成了内容（≥8 输出 token），排掉探针与空响应
data['req_top'] = sorted([x for x in _reqs if x['gen'] >= 8 and x['rate'] > 0],
                         key=lambda x: -x['rate'])[:8]
# 长尾榜：按**占用时长**排，不是按 t/s —— 单序列引擎里"慢"就是"占着那一格有多久"，
# 失败/卡死的请求 rate=0 因而永远进不了快榜，但它们正是最该被看见的。
data['req_slow'] = sorted([x for x in _reqs if x['hold'] >= 1], key=lambda x: -x['hold'])[:8]

data['days'] = [days[k] for k in sorted(days)]
data['hours'] = [hours[k] for k in sorted(hours)]
data['finish_days'] = {d: r['finish'] for d, r in rollup.items() if r.get('finish')}
                                        # 健康度趋势：同样取自 rollup —— 明细清理后历史仍在
data['updated'] = datetime.now().isoformat(timespec='seconds')
data['depth_hist'] = depth_hist
data['scatter'] = sorted(scatter, key=lambda x: x['t'])
data['top_requests'] = sorted(top_req, key=lambda x: -x['rate'])[:8]
data['ttft'] = ttft_days
data['pressure'] = pressure
data['engines'] = engines
data['traffic_by_source'] = traffic_by_source
data['sources'] = {
    'llama': {'days': llama_days, 'hours': llama_hours, 'cache_samples': llama_cache_days},
    'strata': {'requests': n_req, 'days': len(agg_days), 'hours': len(agg_hours)},
}
# --- 4) 当前引擎 ---
data['engine'] = probe_engine(data.get('engine'))

tmp = OUT + '.tmp'
with open(tmp, 'w') as f:
    json.dump(data, f, ensure_ascii=False)
os.replace(tmp, OUT)

eng = data['engine']
print(f'data.json: {len(data["days"])} days ({llama_days} llama + {len(agg_days)} strata), '
      f'{len(data["hours"])} hours, {n_req} strata requests')
if data['days']:
    print(f'  区间 {data["days"][0]["date"]} .. {data["days"][-1]["date"]}')
print(f'  缓存日数: llama {llama_cache_days} + strata {len(cache_strata)}；峰值日数 {len(peak)}')
print(f'  引擎: {eng.get("name")} / {eng.get("backend")} / {eng.get("model")} / '
      f'ctx {eng.get("ctx")} / vision {eng.get("vision")} / up={eng.get("up")}')
