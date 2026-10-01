#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
grade_pool_plan.py —— 顺势池双系统分级清单

输入: 顺势池 trend_pool_watchlist_YYYYMMDD.json + 双系统 union dualscan_union_*.json
输出: trend_pool_graded_YYYYMMDD.json + trend_pool_graded_YYYYMMDD.md (分级清单)

================================================================
【总逻辑】池子只回答"趋势延续",双系统回答"信号分级",区间套回答"买卖点"。
五步流水线,每一步的取舍理由都写在下面 L1-L5,改规则只改阈值常量。
================================================================
"""
import argparse, json, glob, os, sys
from collections import Counter
from datetime import datetime

# ---------------- 阈值常量(改规则只改这里) ----------------
DLP_A      = 0.618  # A级背驰力度门槛:与转折池同口径,低于此的"共振"不可信
DEV_NEAR   = 3.0    # dev_pct<=3%:价格贴着周中枢上沿,类二买形态,优先
DEV_FAR    = 10.0   # dev_pct>10%:远离中枢,追高,等回调再看

# 【逻辑 L1】方向分流
# 为什么:顺势池的口径是"R0+R1双pass且趋势延续",不分方向。
# 1614只有效里 734 above / 662 below / 218 inside。做多只取 above;
# below 的顺的是下跌方向,A股做空受限,转入回避名单,不进多头清单。
# inside 的只看放量突破 zg,不在本次清单内。
DIR_LONG = 'above'

# 【逻辑 L2】信号 join
# 为什么:池子记录没有 R0/R1/R2 方向和 dlp,必须按 code 关联双系统 union。
# 关联不上的直接丢弃(不猜、不补),保证清单每只都有信号依据。
def load_signals(pool_dir, date_compact):
    """取当日最新的 dualscan_union_YYYYMMDD_*.json,返回 {code:信号}。"""
    pats = sorted(glob.glob(os.path.join(pool_dir, f'dualscan_union_{date_compact}_*.json')))
    if not pats:
        return None, None
    u = json.load(open(pats[-1]))
    sig = {}
    for x in u.get('dual', []):
        rs = x.get('recursive_summary') or {}
        sig[x['code']] = dict(
            gate = x.get('gate'),
            dlp  = x.get('dlp') or 0.0,
            r1   = rs.get('r1_dir'),      # 本级别方向:多头要求 bullish
            r2   = rs.get('r2_dir'),      # 大级别方向:冲突一票否决,缺席中性
            uni  = bool(rs.get('three_way_unison')),  # 三向共振:R0/R1/R2同向
        )
    return sig, pats[-1]

# 【逻辑 L3】A/B/C/JEV 分级
# 为什么:
#  A级 = PASS + R1 bullish + 三向共振 + dlp>=0.618,四者缺一不可。
#       注意:R2当前结构性缺席(覆盖率<1%),三向共振几乎恒为false,
#       所以 A级经常挂零——这是数据现状,不是bug,不要放水。
#  B级 = PASS + R1 bullish + R2不为bearish。
#       R2 缺席按用户决议"中性不扣分",只有明确 bearish 才一票否决。
#  C级 = BLOCKED + 三向共振:趋势未确认但多级别同向,只观察。
#  JEV  = PASS_JEVN:双引擎判PASS但被JEV二次门禁降级,单独跟踪,不进主清单。
def grade(code, p, s):
    dlp, uni = s['dlp'], s['uni']
    if s['gate'] == 'PASS' and s['r1'] == 'bullish' and uni and dlp >= DLP_A:
        return 'A', f'PASS+R1多+三向共振+dlp={dlp:.3f}≥{DLP_A}'
    if s['gate'] == 'PASS' and s['r1'] == 'bullish' and s['r2'] != 'bearish':
        why = f'PASS+R1多+dlp={dlp:.3f}'
        why += '(R2缺席,中性)' if s['r2'] != 'bullish' else '+R2同向'
        return 'B', why
    if s['gate'] == 'BLOCKED' and uni:
        return 'C', 'BLOCKED但三向共振,趋势未确认,只观察'
    if s['gate'] == 'PASS_JEVN':
        return 'JEV', 'JEV二次门禁降级,单独跟踪'
    return None, None

# 【逻辑 L4】区间套定买卖点
# 为什么:分级只回答"能不能做",买卖点看价格相对周中枢的位置。
#  dev_pct小 = 回调到中枢上沿附近 = 类二买,优先;
#  dev_pct大 = 追高,即使信号好也要等回调;
#  viol_adverse = 逆向级别背离计数,有一个就降一档(多一分不确定性);
#  fresh_days大 = 锚陈旧,中枢可能失效,降级。
def entry_note(p):
    dev = p.get('dev_pct') or 0
    adv = p.get('viol_adverse') or 0
    if dev <= DEV_NEAR:   pos = '贴中枢上沿(类二买)'
    elif dev <= DEV_FAR:  pos = '偏离适中'
    else:                 pos = '偏离过大,追高,等回调'
    flag = f',逆向背离{adv}个(降档)' if adv else ''
    stale = f",锚陈旧{fresh}日(降级)" if (fresh := p.get('fresh_days') or 0) > 20 else ''
    return f'{pos}{flag}{stale}'

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pool-dir', required=True)
    ap.add_argument('--date', required=True, help='YYYYMMDD,池子与union的日期')
    ap.add_argument('--out-dir', required=True)
    a = ap.parse_args()
    dc = a.date

    pats = sorted(glob.glob(os.path.join(a.pool_dir, f'trend_pool_watchlist_{dc}.json')))
    if not pats:
        print(f'[grade] 找不到顺势池 trend_pool_watchlist_{dc}.json', file=sys.stderr); return 2
    pool = {r['code']: r for r in json.load(open(pats[-1]))['records']
            if r.get('effective') and r.get('weekly_pos') == DIR_LONG}  # L1

    sig, src = load_signals(a.pool_dir, dc)  # L2
    if sig is None:
        print(f'[grade] 找不到 dualscan_union_{dc}_*.json', file=sys.stderr); return 2

    buckets = {'A': [], 'B': [], 'C': [], 'JEV': []}
    for code, p in pool.items():
        s = sig.get(code)
        if not s: continue  # L2:关联不上就丢弃
        g, why = grade(code, p, s)  # L3
        if not g: continue
        buckets[g].append(dict(code=code, dlp=round(s['dlp'], 4), r1=s['r1'], r2=s['r2'],
                               gate=s['gate'], dev=round(p.get('dev_pct') or 0, 2),
                               zs=p.get('zs_range'), cur=p.get('cur'),
                               reason=why, entry=entry_note(p)))  # L4
    for g in buckets: buckets[g].sort(key=lambda r: -r['dlp'])

    js = os.path.join(a.out_dir, f'trend_pool_graded_{dc}.json')
    json.dump(dict(date=dc, src_union=os.path.basename(src), counts={k: len(v) for k, v in buckets.items()},
                   buckets=buckets), open(js, 'w'), ensure_ascii=False, indent=1)

    # 【逻辑 L5】环境提示(模板,不硬编码指数判断)
    # 为什么:指数大级别方向由指数模块/人工判断,脚本不猜。
    # 清单只给分级与位置,仓位建议按模板输出,由人结合指数方向执行。
    md = [f'# 顺势池分级清单 ({dc})', '',
          f'> 数据源: {os.path.basename(src)} | above有效 {len(pool)} 只 | '
          + ' '.join(f'{k}={len(v)}' for k, v in buckets.items()), '',
          '## 分级逻辑(阈值见脚本头部常量)',
          f'- A级: PASS+R1多+三向共振+dlp≥{DLP_A}(R2缺席时经常挂零,属数据现状)',
          '- B级: PASS+R1多,R2冲突一票否决,缺席中性',
          '- C级: BLOCKED+三向共振,只观察', '- JEV: 二次门禁降级,单独跟踪', '',
          '## 买卖点逻辑', f'- dev≤{DEV_NEAR}%:贴中枢上沿(类二买,优先)',
          f'- dev>{DEV_FAR}%:追高,等回调', '- 每个逆向背离降一档,锚超20日降级', '',
          '## 环境提示', '指数大级别下跌时:B级降半仓或只观察,A级亦谨慎;',
          '单票≤30%、总仓位≤70%、-15%硬止损。', '']
    for g in ('A', 'B', 'C', 'JEV'):
        md.append(f'## {g}级 ({len(buckets[g])}只)')
        md.append('| 代码 | dlp | R1/R2 | 偏离 | 位置/中枢 | 定级理由 | 买卖点 |')
        md.append('|---|---|---|---|---|---|---|')
        for r in buckets[g]:
            md.append(f"| {r['code']} | {r['dlp']} | {r['r1']}/{r['r2']} | {r['dev']}% | {r['cur']}/{r['zs']} | {r['reason']} | {r['entry']} |")
        md.append('')
    mo = os.path.join(a.out_dir, f'trend_pool_graded_{dc}.md')
    open(mo, 'w').write('\n'.join(md))
    print(f'[grade] A={len(buckets["A"])} B={len(buckets["B"])} C={len(buckets["C"])} JEV={len(buckets["JEV"])} -> {os.path.basename(mo)}')
    return 0

if __name__ == '__main__':
    sys.exit(main())
