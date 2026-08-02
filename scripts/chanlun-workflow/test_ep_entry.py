#!/usr/bin/env python3
"""测试EP_L二买确认逻辑"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import beichi_analyzer as ba

# 显式预加载模型
ba._load_dl_model()
ba._load_ep_model()

# 抑制日志
import io
old_stdout = sys.stdout
sys.stdout = io.StringIO()

results = []
for code in ['605369', '002454', '002370', '002224', '603055', '002593', '000513']:
    ml = ba.detect_multilevel_buy_signals(code)
    tier = ml.get('tier', 'N/A')
    entry = ml.get('entry', 'N/A')
    dl_p = ml.get('daily_dl_p', 0)
    ep_30 = ml.get('30min_ep_p', 0)
    ep_5 = ml.get('5min_ep_p', 0)
    dl_30 = ml.get('30min_dl_p', 0)
    dl_5 = ml.get('5min_dl_p', 0)
    ermai = ml.get('ermai')
    sanmai = ml.get('sanmai')
    extra = ""
    if ermai and ermai.get('valid'):
        method = ermai.get('confirm_method', '')
        ep = ermai.get('ep_prob', 0)
        extra = f" ★二买({method} EP={ep:.3f})"
    if sanmai and sanmai.get('valid'):
        method = sanmai.get('confirm_method', '')
        ep = sanmai.get('ep_prob', 0)
        extra = f" ★三买({method} EP={ep:.3f})"
    line = f"{code} | {tier:4s} | {entry} | DL日={dl_p:.2f} DL30={dl_30:.2f} DL5={dl_5:.2f} | EP30={ep_30:.3f} EP5={ep_5:.3f}{extra}"
    results.append(line)

sys.stdout = old_stdout
for r in results:
    print(r)
