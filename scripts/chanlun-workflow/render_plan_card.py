#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""render_plan_card.py —— 交易计划卡片(每日输出格式).
模板: 同目录 trade_plan_card_template.html (PAGE/MAIN/EMPTY 三区,占位符见模板头注释)
输入: trend_pool_graded_YYYYMMDD.json (+ beichi_turn_pool_YYYYMMDD.json 可选)
输出: trade_plan_card_YYYYMMDD.html
逻辑 R1: 选头名(A优先,无A则B,都没有则"空仓等待")
逻辑 R2: 价格梯子动态刻度(现价±6%),标签左右分栏不重叠
逻辑 R3: 买入区=[中枢下沿,中枢上沿x1.01];止损=中枢下沿x0.997
逻辑 R4: 环境章全部数据驱动,不硬编码指数判断
逻辑 R5: 观察名单=B级第2-3名+转折池dlp前2
逻辑 R6: 风控纪律块。仓位提示按等级动态(A级单票≤30%用户上限;B级防守轻仓取上限一半≤15%;总仓≤70%);硬止损=现价x0.85预估(实际按成交价),与技术止损取紧者
"""
import argparse, json, glob, os, re, sys
from datetime import datetime

TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         'trade_plan_card_template.html')


def load_sections(path):
    txt = open(path).read()
    secs = {}
    for m in re.finditer(r'<!-- SECTION:(\w+) -->(.*?)(?=<!-- (?:SECTION:\w+|END) -->)',
                         txt, re.S):
        secs[m.group(1)] = m.group(2).strip()
    if set(secs) != {'PAGE', 'MAIN', 'EMPTY'}:
        raise ValueError('模板缺区: ' + ','.join(sorted(secs)))
    return secs


def fill(tmpl, mapping):
    for k, v in mapping.items():
        tmpl = tmpl.replace(k, v)
    return tmpl


def pct(p, lo, hi):
    return round((hi - p) / (hi - lo) * 100, 1)


def build_axis(lo, hi):
    out = []
    for i in range(6):
        v = round(lo + (hi - lo) * i / 5, 2)
        t = pct(v, lo, hi)
        tr = 'transform:translateY(-100%);' if i == 5 else 'transform:translateY(-50%);'
        out.append(
            '<div style="position:absolute;top:' + str(t) + '%;' + tr +
            'font-size:10px;color:var(--hatch-widget-muted);">' + format(v, '.2f') + '</div>'
        )
    return ''.join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pool-dir', required=True)
    ap.add_argument('--date', required=True, help='YYYYMMDD')
    ap.add_argument('--out-dir', required=True)
    ap.add_argument('--template', default=TEMPLATE)
    a = ap.parse_args()

    secs = load_sections(a.template)
    pats = sorted(glob.glob(os.path.join(a.pool_dir, 'trend_pool_graded_' + a.date + '.json')))
    if not pats:
        print('[card] 找不到分级JSON,先跑 grade_pool_plan.py', file=sys.stderr)
        return 2
    g = json.load(open(pats[-1]))
    bk = g['buckets']
    counts = g['counts']

    # v5 守门 (2026-10-05): 按 regime 过滤候选
    # 从同日期 trend pool 读 regime
    _regime = "UNKNOWN"
    try:
        _tp = sorted(glob.glob(os.path.join(a.pool_dir, 'trend_pool_watchlist_' + a.date + '.json')))
        if _tp:
            _regime = json.load(open(_tp[-1])).get('market_regime', 'UNKNOWN')
    except Exception:
        pass

    def _v5_pass(r):
        """v5 守门员：决定做不做。返回 True=通过，False=踢掉"""
        v5 = r.get('v5_state', '无')
        if _regime == 'BEAR':
            # 熊市：只做趋势向上，其余空仓
            return v5 == '趋势' and r.get('v5_direction') == '趋势-向上'
        elif _regime == 'BULL':
            # 牛市：v5 过滤踢掉盘整股
            return v5 != '盘整'
        # 震荡/未知：不踢，只标注
        return True

    # R1: 选头名（先 v5 守门过滤，再 A优先）
    _a_list = [r for r in bk['A'] if _v5_pass(r)]
    _b_list = [r for r in bk['B'] if _v5_pass(r)]
    top, grade = None, None
    if _a_list:
        top, grade = _a_list[0], 'A'
    elif _b_list:
        top, grade = _b_list[0], 'B'
    _v5_filtered = (len(bk['A']) - len(_a_list)) + (len(bk['B']) - len(_b_list))
    if _v5_filtered:
        print('[card] v5 守门过滤: 踢掉 %d 只 (regime=%s)' % (_v5_filtered, _regime))

    if top:
        zs = (top.get('zs') or '').split('-')
        zd, zg = float(zs[0]), float(zs[1])
        cur = float(top['cur'])
        buy_lo = round(zd, 2)
        buy_hi = round(zg * 1.01, 2)
        stop = round(zd * 0.997, 2)
        hard_stop = round(cur * 0.85, 2)
        pos_txt = '单票 ≤30% · 总仓位 ≤70%' if grade == 'A' else '单票 ≤15%(轻仓,取上限一半) · 总仓位 ≤70%'
        lo, hi = round(cur * 0.94, 2), round(cur * 1.06, 2)
        main_card = fill(secs['MAIN'], {
            '__CODE__': top['code'],
            '__CUR__': format(cur, '.2f'),
            '__GRADE__': grade,
            '__DLP__': format(top['dlp'], '.3f'),
            '__REASON__': top['reason'] + ' [v5:' + top.get('v5_state', '无') + ']',
            '__ENTRY__': top['entry'],
            '__ZS__': top['zs'],
            '__BUY_LO__': format(buy_lo, '.2f'),
            '__BUY_HI__': format(buy_hi, '.2f'),
            '__STOP__': format(stop, '.2f'),
            '__HARD_STOP__': format(hard_stop, '.2f'),
            '__POS__': pos_txt,
            '__BUY_TOP__': str(pct(buy_hi, lo, hi)),
            '__BUY_H__': str(round(pct(buy_lo, lo, hi) - pct(buy_hi, lo, hi), 1)),
            '__ZS_TOP__': str(pct(zg, lo, hi)),
            '__ZS_H__': str(round(pct(zd, lo, hi) - pct(zg, lo, hi), 1)),
            '__CUR_TOP__': str(pct(cur, lo, hi)),
            '__STOP_TOP__': str(pct(stop, lo, hi)),
            '__DLP_PCT__': str(round(min(top['dlp'], 1) * 100, 1)),
            '__AXIS__': build_axis(lo, hi),
        })
        if grade == 'A':
            pill_txt, pill_bg = '积极', '#16a34a'
        else:
            pill_txt, pill_bg = '防守 · 轻仓', '#b45309'
    else:
        main_card = secs['EMPTY']
        pill_txt, pill_bg = '空仓等待', '#6b7280'

    watch = []
    for r in bk['B'][1:3]:
        # v5 标注 (2026-10-05)
        _v5tag = r.get('v5_state', '无')
        watch.append((r['code'], 'B级 dlp ' + format(r['dlp'], '.3f') + ' · v5:' + _v5tag + ' · ' + r['entry']))
    bp = sorted(glob.glob(os.path.join(a.pool_dir, 'beichi_turn_pool_' + a.date + '.json')))
    if bp:
        items = sorted(json.load(open(bp[-1]))['items'], key=lambda x: -(x.get('dlp') or 0))[:2]
        for x in items:
            _v5tag = x.get('v5_state', '无')
            watch.append((x['code'], '转折池 dlp ' + format(x['dlp'], '.3f') + ' · v5:' + _v5tag + ' · 大级别无确认,只看'))
    rows = ''.join(
        '<div style="font-size:12px;padding:6px 0;border-bottom:1px solid var(--hatch-widget-border);">'
        '<b>' + c + '</b> <span style="color:var(--hatch-widget-muted);">' + d + '</span></div>'
        for c, d in watch
    ) or '<div style="font-size:12px;color:var(--hatch-widget-muted);">无</div>'

    dn = datetime.strptime(a.date, '%Y%m%d').strftime('%Y-%m-%d')
    html = fill(secs['PAGE'], {
        '__DATE_NICE__': dn,
        '__PILL_TXT__': pill_txt,
        '__PILL_BG__': pill_bg,
        '__N_A__': str(counts['A']),
        '__N_B__': str(counts['B']),
        '__MAIN_CARD__': main_card,
        '__WATCH_ROWS__': rows,
        '__SRC__': g.get('src_union', ''),
    })
    out = os.path.join(a.out_dir, 'trade_plan_card_' + a.date + '.html')
    open(out, 'w').write(html)
    print('[card] ' + os.path.basename(out) + ' head=' + (top['code'] if top else '无'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
