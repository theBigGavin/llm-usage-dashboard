#!/usr/bin/env python3
"""聚合 llama-server 日志 → usage-site/data.json(按天 × slot 的 token 消耗)

对齐策略:
- 日志分多段(每次重启一段,以 "启动 llama-server" 行分隔),行时间戳是相对该段的秒数
- 最后一段 anchor = 当前进程 lstart(ps)
- 往前逐段反推:anchor[i] = anchor[i+1] - (seg[i] 最后相对秒 + 启动开销 ~210s)
- watchdog.log 的 "已恢复" 时间做校准:与反推值差 <10min 的段采用 watchdog 实测值
输出 data.json: {"updated": iso, "days": [{"date","s0","s1","count"}]}
"""
import json, os, re, subprocess
from datetime import datetime, timedelta

LOG = os.path.expanduser('~/playground/ggufs/server_iq4.log')
WLOG = os.path.expanduser('~/playground/ggufs/watchdog.log')
OUT = os.path.expanduser('~/playground/usage-site/data.json')

# 时间戳是 分.秒.毫秒.微秒 —— 不是秒！第二字段最大 59、三四字段最大 999 即证；
# 186.31.118.566 = 186 分 31 秒。按秒解读会把每段内部时间线压扁 60 倍，日/小时归属全错。
ts_re = re.compile(r'^(\d+)\.[\d.]+ ')
TS_SECONDS_PER_UNIT = 60   # 首字段单位 -> 秒
seg_re = re.compile(r'^启动 llama-server')
prompt_re = re.compile(r'slot print_timing: id\s+(\d+) \| task \d+ \| prompt eval time =\s*([\d.]+) ms /\s*(\d+) tokens')
gen_re = re.compile(r'slot print_timing: id\s+(\d+) \| task \d+ \|        eval time =\s*([\d.]+) ms /\s*(\d+) tokens')
wd_re = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) 已恢复')
stamp_re = re.compile(r'^启动时刻 (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})')
BOOT_COST_S = 210  # 87G 模型加载约 3.5 分钟

def proc_start():
    try:
        pid = subprocess.run(['pgrep', '-nf', 'llama-server'], capture_output=True, text=True).stdout.strip().splitlines()[0]
        st = subprocess.run(['ps', '-o', 'lstart=', '-p', pid], capture_output=True, text=True).stdout.strip()
        return datetime.strptime(st, '%a %b %d %H:%M:%S %Y')
    except Exception:
        return None

def main():
    lines = open(LOG, errors='replace').read().splitlines()
    segs = [i for i, l in enumerate(lines) if seg_re.match(l)]
    if not segs:
        print('no segments'); return

    # 每段最后相对时间（已换算成秒）
    last_ts = []
    for si, start in enumerate(segs):
        end = segs[si+1] if si+1 < len(segs) else len(lines)
        mx = 0
        for l in lines[start:end]:
            m = ts_re.match(l)
            if m: mx = max(mx, int(m.group(1)))
        last_ts.append(mx * TS_SECONDS_PER_UNIT)

    # 反推 anchors
    anchors = [None] * len(segs)
    anchors[-1] = proc_start()
    if anchors[-1] is None:
        anchors[-1] = datetime.fromtimestamp(os.stat(LOG).st_mtime) - timedelta(seconds=last_ts[-1])
    for si in range(len(segs) - 2, -1, -1):
        anchors[si] = anchors[si+1] - timedelta(seconds=last_ts[si] + BOOT_COST_S)

    # watchdog 校准:靠近反推值的段换成实测时刻
    wd = [datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')
          for l in open(WLOG, errors='replace') for m in [wd_re.match(l)] if m]
    for si in range(len(segs)):
        near = [w for w in wd if abs((w - anchors[si]).total_seconds()) < 600]
        if near: anchors[si] = near[-1]

    # 启动脚本自 2026-10-13 起在启动头下一行写绝对时刻（启动时刻 <ISO>）：
    # 有它就用它做该段的锚点，精确、无累积误差；这是唯一可信的锚。
    stamped = 0
    for si, start in enumerate(segs):
        end = segs[si+1] if si+1 < len(segs) else len(lines)
        for l in lines[start:end]:
            m = stamp_re.match(l)
            if m:
                anchors[si] = datetime.strptime(m.group(1), '%Y-%m-%dT%H:%M:%S')
                stamped += 1
                break

    # 收敛到日志真实存活区间：段长回推不可信（同一日志里 3376 段的时长合计 842 天，
    # 却装在一个 14 天的文件里；部分重启未写头导致多段被合并）。保持段的相对次序，
    # 把整条链等比压缩进 [文件出生时间, 当前段起点]，避免把数据摊到不存在的日期上。
    try:
        birth = datetime.fromtimestamp(os.stat(LOG).st_birthtime)
    except AttributeError:
        birth = datetime.fromtimestamp(os.stat(LOG).st_ctime)
    scale = 1.0
    if anchors[0] < birth < anchors[-1]:
        # 有精确锚点(启动时刻)时只把「出生时间之前」的部分线性压回真实窗口，
        # 不动已精确锚定的段；没有精确锚点时整条链等比压缩。
        first_ok = next((i for i, a in enumerate(anchors) if a >= birth), None)
        if stamped and first_ok:
            lo, hi = anchors[0], anchors[first_ok]
            if hi > lo:
                step = (hi - birth) / (first_ok + 1)
                for i in range(first_ok):
                    anchors[i] = birth + step * (i + 1)
        else:
            span_chain = (anchors[-1] - anchors[0]).total_seconds()
            span_real = (anchors[-1] - birth).total_seconds()
            if span_chain > 0:
                scale = span_real / span_chain
                anchors = [anchors[-1] - timedelta(seconds=(anchors[-1] - a).total_seconds() * scale)
                           for a in anchors]

    # 聚合: daily[date] = [s0, s1, prompt, gen];  hourly[iso_hour] = [s0_ms, s0_tok, s1_ms, s1_tok]
    daily = {}
    hourly = {}
    for si, start in enumerate(segs):
        end = segs[si+1] if si+1 < len(segs) else len(lines)
        base = anchors[si]
        for l in lines[start:end]:
            m = ts_re.match(l)
            if not m: continue
            mp, mg = prompt_re.search(l), gen_re.search(l)
            if not mp and not mg: continue
            mm = mp or mg
            slot = int(mm.group(1)); ms = float(mm.group(2)); n = int(mm.group(3))
            sc = scale if si < len(segs) - 1 else 1.0   # 当前段锚点精确(进程启动时刻)，偏移不压缩
            ts = base + timedelta(seconds=int(m.group(1)) * TS_SECONDS_PER_UNIT * sc)
            day = ts.strftime('%Y-%m-%d')
            b = daily.setdefault(day, [0, 0, 0, 0])
            b[slot if slot < 2 else 0] += n
            b[2 if mp else 3] += n
            # 小时桶: prefill 和 decode 分开记
            hr = ts.strftime('%Y-%m-%dT%H')
            h = hourly.setdefault(hr, [0,0, 0,0, 0,0, 0,0])  # s0_pre_ms,s0_pre_tok, s0_dec_ms,s0_dec_tok, s1_pre_ms,s1_pre_tok, s1_dec_ms,s1_dec_tok
            si2 = 0 if slot == 0 else 4
            if mp: h[si2] += ms; h[si2+1] += n
            if mg: h[si2+2] += ms; h[si2+3] += n

    days = [{'date': d, 's0': v[0], 's1': v[1], 'count': v[0]+v[1], 'prompt': v[2], 'gen': v[3]}
            for d, v in sorted(daily.items())]
    hours = [{'hour': h,
              's0_pre_tps': round(v[1] / (v[0]/1000), 1) if v[0] else 0,
              's0_dec_tps': round(v[3] / (v[2]/1000), 1) if v[2] else 0,
              's1_pre_tps': round(v[5] / (v[4]/1000), 1) if v[4] else 0,
              's1_dec_tps': round(v[7] / (v[6]/1000), 1) if v[6] else 0}
             for h, v in sorted(hourly.items())]
    json.dump({'updated': datetime.now().isoformat(timespec='seconds'), 'days': days, 'hours': hours},
              open(OUT + '.tmp', 'w'), ensure_ascii=False)
    os.replace(OUT + '.tmp', OUT)
    print(f'{len(days)} days; span {days[0]["date"]}..{days[-1]["date"]}' if days else 'empty')

if __name__ == '__main__':
    main()
