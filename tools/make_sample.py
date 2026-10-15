#!/usr/bin/env python3
"""把真实 data.json 变成可以提交到公开仓库的样片 data.sample.json。

设计上**用白名单**：只挑选页面确实需要的字段重新拼装，而不是"复制一份再删敏感项"。
复制+删除的写法一旦漏了某个嵌套字段（对端、UA、路径），敏感内容就跟着上公网了；
白名单则保证未列入的字段物理上不可能出现在输出里。

脱敏的四件事：
  1) 对端 IP → RFC 5737 文档保留地址（192.0.2.x / 198.51.100.x / 203.0.113.x）。
     127.0.0.1 保留原样 —— 它是公开常量，删了反而看不出「本机 / 局域网 / 隧道」的区别。
  2) 引擎与模型名 → 通用名（local-engine / example-model）。UA 一律清空。
  3) 所有 token / 次数 → 同一个系数缩放（默认 0.5）。用**单一系数**而不是逐项随机：
     逐项随机会破坏"总量 = 各来源之和"这类内部一致性，样片看起来就像坏的。
  4) 速率类（t/s）允许小抖动，因为它们的绝对值不参与任何求和。

用法：  python3 tools/make_sample.py [源 data.json] [输出 data.sample.json]
"""
import json
import os
import random
import sys

SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser('~/playground/usage-site/data.json')
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.expanduser('~/playground/usage-site/data.sample.json')

SCALE = 0.5
rnd = random.Random(20261015)          # 固定种子：样片可复现

DOC_IPS = ['192.0.2.10', '198.51.100.7', '203.0.113.23']
_IPMAP = {}
_IDX = [0]


def doc_ip(x):
    """对端地址 → 文档保留地址；127.0.0.1 原样保留。"""
    x = str(x)
    if x in ('127.0.0.1', 'localhost'):
        return x
    if x not in _IPMAP:
        _IPMAP[x] = DOC_IPS[_IDX[0] % len(DOC_IPS)]
        _IDX[0] += 1
    return _IPMAP[x]


def n(v):
    """计数/总量类：统一系数缩放，保留整数。"""
    if v is None:
        return None
    try:
        return round(float(v) * SCALE)
    except (TypeError, ValueError):
        return v


def rate(v):
    """速率类：统一系数 + 小抖动（不参与求和，抖动无害）。"""
    if v is None:
        return None
    try:
        return round(float(v) * SCALE * rnd.uniform(0.9, 1.1), 1)
    except (TypeError, ValueError):
        return v


with open(SRC) as f:
    d = json.load(f)

out = {}

# ---- 逐日：只保留页面读取的字段，src 里的三元组跟着缩放 ----
out['days'] = []
for day in d.get('days', []):
    srcs = {}
    for k, v in (day.get('src') or {}).items():
        if isinstance(v, (list, tuple)) and len(v) >= 3:
            srcs[k] = [n(v[0]), n(v[1]), n(v[2])]
    out['days'].append({
        'date': day.get('date'),
        'prompt': n(day.get('prompt')),
        'gen': n(day.get('gen')),
        'count': n(day.get('count')),
        'cached': n(day.get('cached')),
        'cache_total': n(day.get('cache_total')),
        'dec_peak': rate(day.get('dec_peak')),
        'dec_peak_at': day.get('dec_peak_at'),
        'src': srcs,
    })

# ---- 逐小时：速率抖动 ----
out['hours'] = [{
    'hour': h.get('hour'),
    's0_dec_tps': rate(h.get('s0_dec_tps')),
    's1_dec_tps': rate(h.get('s1_dec_tps')),
    's0_pre_tps': rate(h.get('s0_pre_tps')),
    's1_pre_tps': rate(h.get('s1_pre_tps')),
} for h in d.get('hours', [])]

# ---- 来源统计：对端地址换成文档地址，其余数字统一缩放 ----
tbs = {}
for k, v in (d.get('traffic_by_source') or {}).items():
    if not isinstance(v, dict):
        continue
    peers = {doc_ip(ip): n(cnt) for ip, cnt in (v.get('peers') or {}).items()}
    tbs[k] = {kk: (n(vv) if isinstance(vv, (int, float)) else vv) for kk, vv in v.items() if kk != 'peers'}
    tbs[k]['peers'] = peers
out['traffic_by_source'] = tbs

# ---- 峰值与长尾：来源名保留（本机/局域网/隧道是通用词），UA 清空 ----
def req(r):
    return {
        't': r.get('t'), 'src': r.get('src'), 'ua': '',
        'rate': rate(r.get('rate')), 'pre': rate(r.get('pre')),
        'ctx': n(r.get('ctx')), 'gen': n(r.get('gen')),
        'wait': n(r.get('wait')), 'hold': round(float(r.get('hold') or 0) * rnd.uniform(0.9, 1.1), 1),
        'reused': n(r.get('reused')), 'finish': r.get('finish'),
    }

out['req_top'] = [req(r) for r in (d.get('req_top') or [])]
out['req_slow'] = [req(r) for r in (d.get('req_slow') or [])]

# ---- 健康度：按天结局计数，统一缩放 ----
out['finish_days'] = {day: {k: n(v) for k, v in m.items()}
                      for day, m in (d.get('finish_days') or {}).items()}

# ---- 上下文长度分布 ----
out['depth_hist'] = [{'k': b.get('k'), 'n': n(b.get('n'))} if isinstance(b, dict) else b
                     for b in (d.get('depth_hist') or [])]

# ---- 引擎：通用名。两个时代必须给**不同**的通用名，否则看板上的切换说明会读成
#      "10-15 11:23 之前是 local-engine，之后是 local-engine" —— 外人会当成 bug。
out['engines'] = []
for _e in (d.get('engines') or []):
    # 注意方向：有 to 表示"这段已结束"=旧时代；to 为 null 才是进行中的当前引擎。
    out['engines'].append({'name': 'legacy-engine' if _e.get('to') else 'local-engine',
                           'from': _e.get('from'), 'to': _e.get('to')})
out['engine'] = {'name': 'local-engine', 'backend': 'Metal', 'model': 'example-model',
                 'ctx': (d.get('engine') or {}).get('ctx'), 'vision': (d.get('engine') or {}).get('vision'),
                 'up': True, 'checked': None}

# ---- 压力采样：只留时刻与状态，去掉任何客户端细节 ----
p = d.get('pressure') or {}
out['pressure'] = {
    'busy_pct': p.get('busy_pct'), 'deferred_n': n(p.get('deferred_n')),
    'deferred_total': n(p.get('deferred_total')), 'req_per_hour': p.get('req_per_hour'),
    'samples': [{'t': s.get('t'), 'busy': s.get('busy'), 'deferred': s.get('deferred'),
                 'dreq': n(s.get('dreq'))} for s in (p.get('samples') or [])],
}

# ---- 明确丢弃：逐请求散点、旧格式榜单、缓存日明细、任何来源系统字段 ----
out['discarded_fields'] = sorted(set(d.keys()) - set(out.keys()))
out['updated'] = '2026-10-15T12:00:00'
out['sample'] = True

with open(OUT, 'w') as f:
    json.dump(out, f, ensure_ascii=False, indent=0)

print(f'样片: {OUT}  ({os.path.getsize(OUT) / 1024:.1f} KB)')
print(f'  缩放系数 {SCALE}；丢弃字段: {", ".join(out["discarded_fields"]) or "无"}')
print(f'  对端地址映射: { {k: v for k, v in _IPMAP.items()} }')
