"""
HTML 收盘日报渲染器

基于内置 string.Template 渲染（不强依赖 Jinja2，避免新增依赖）。
CSS 中的 `{` 已用 `{{` 转义，避免与 Python str.format 冲突。

公开接口:
    render_daily_report(report_data: dict) -> str
        返回完整 HTML 字符串
    save_daily_report(report_data: dict, output_dir: str) -> str
        渲染并写入 output_dir/YYYY-MM-DD.html，返回文件绝对路径
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from html import escape
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


# CSS 中的 `{` 必须写成 `{{`，`}` 必须写成 `}}`，否则 str.format 会把它当占位符解析
_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>A 股收盘日报 · {date}</title>
<style>
  :root {{
    --bg: #f7f8fa;
    --card: #ffffff;
    --text: #1f2329;
    --muted: #646a73;
    --border: #e5e6eb;
    --up: #d54941;
    --down: #2ba471;
    --primary: #3370ff;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 24px;
    font-family: -apple-system, "Helvetica Neue", "PingFang SC", "Microsoft YaHei", sans-serif;
    background: var(--bg); color: var(--text);
  }}
  .wrap {{ max-width: 1080px; margin: 0 auto; }}
  h1 {{ font-size: 26px; margin: 0 0 4px; }}
  .sub {{ color: var(--muted); margin-bottom: 20px; }}
  .card {{
    background: var(--card); border: 1px solid var(--border);
    border-radius: 10px; padding: 18px 20px; margin-bottom: 16px;
  }}
  .grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }}
  .stat {{ background: #fafbfc; border: 1px solid var(--border); border-radius: 8px; padding: 12px; }}
  .stat .label {{ color: var(--muted); font-size: 12px; }}
  .stat .value {{ font-size: 20px; font-weight: 600; margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
  th, td {{ padding: 8px 10px; border-bottom: 1px solid var(--border); text-align: left; }}
  th {{ background: #fafbfc; font-weight: 600; color: var(--muted); }}
  .up {{ color: var(--up); font-weight: 600; }}
  .down {{ color: var(--down); font-weight: 600; }}
  .stock-card {{ border-left: 3px solid var(--primary); padding: 12px 14px; margin-bottom: 12px; background: #fafbfc; border-radius: 6px; }}
  .stock-card h3 {{ margin: 0 0 6px; font-size: 16px; }}
  .stock-meta {{ color: var(--muted); font-size: 12px; margin-bottom: 8px; }}
  .seats {{ font-size: 13px; color: var(--text); }}
  .ai-comment {{ background: #eef4ff; border-left: 3px solid var(--primary); padding: 8px 10px; margin-top: 8px; border-radius: 4px; font-size: 13px; }}
  .chanlun-tag {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; margin-right: 6px; }}
  .tag-buy {{ background: #ffefef; color: #d54941; }}
  .tag-sell {{ background: #e8f7f0; color: #2ba471; }}
  .tag-flat {{ background: #f0f0f0; color: #646a73; }}
  .chanlun-sr {{ font-size: 12px; color: var(--muted); margin-top: 4px; }}
  .footer {{ color: var(--muted); font-size: 12px; text-align: center; margin-top: 24px; }}
  @media (max-width: 720px) {{
    .grid {{ grid-template-columns: repeat(2, 1fr); }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <h1>A 股收盘日报</h1>
  <div class="sub">{date} · 由 TradingAgents-CN 自动生成 · 生成时间 {generated_at}</div>

  <div class="card">
    <div class="grid">
      <div class="stat"><div class="label">上榜股票数</div><div class="value">{count}</div></div>
      <div class="stat"><div class="label">净买入合计</div><div class="value">{total_net_buy}</div></div>
      <div class="stat"><div class="label">最高涨幅</div><div class="value up">{max_change}</div></div>
      <div class="stat"><div class="label">数据状态</div><div class="value">{status}</div></div>
    </div>
  </div>

  <div class="card">
    <h2 style="margin-top:0">市场总览</h2>
    <p>{overall_comment}</p>
  </div>

  <div class="card">
    <h2 style="margin-top:0">区间套策略监控 (30min/5min/1min)</h2>
    {strategy_section}
  </div>

  <div class="card">
    <h2 style="margin-top:0">龙虎榜净买入排行</h2>
    {ranking_table}
  </div>

  <div class="card">
    <h2 style="margin-top:0">个股详情</h2>
    {stock_cards}
  </div>

  <div class="footer">本报告仅供研究学习，不构成任何投资建议。</div>
</div>
</body>
</html>
"""


def _fmt_money(v: Any) -> str:
    """金额格式化为亿/万。"""
    if v is None or v == "":
        return "—"
    try:
        n = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(n) >= 1e8:
        return f"{n / 1e8:.2f} 亿"
    if abs(n) >= 1e4:
        return f"{n / 1e4:.2f} 万"
    return f"{n:.0f}"


def _fmt_pct(v: Any) -> str:
    if v is None or v == "":
        return "—"
    try:
        return f"{float(v):.2f}%"
    except (TypeError, ValueError):
        return str(v)


def _build_ranking_table(stocks: List[Dict[str, Any]]) -> str:
    if not stocks:
        return "<p style='color:#646a73'>当日无龙虎榜数据。</p>"
    rows = []
    for i, s in enumerate(stocks, 1):
        change = s.get("change_pct")
        change_cls = "up" if (change is not None and float(change) >= 0) else "down"
        rows.append(
            f"<tr>"
            f"<td>{i}</td>"
            f"<td>{escape(str(s.get('code', '')))}</td>"
            f"<td>{escape(str(s.get('name', '')))}</td>"
            f"<td class='{change_cls}'>{_fmt_pct(change)}</td>"
            f"<td>{_fmt_money(s.get('turnover'))}</td>"
            f"<td>{_fmt_money(s.get('net_buy'))}</td>"
            f"<td>{escape(str(s.get('reason', ''))[:40])}</td>"
            f"</tr>"
        )
    return (
        "<table><thead><tr>"
        "<th>#</th><th>代码</th><th>名称</th><th>涨跌幅</th>"
        "<th>成交额</th><th>净买入</th><th>上榜原因</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def _build_chanlun_tags(s: Dict[str, Any]) -> str:
    """根据缠论分析结果生成标签 HTML。"""
    cl = s.get("chanlun")
    if not cl:
        return ""
    tags = []
    trend = cl.get("trend", "未知")
    if trend == "上涨":
        tags.append("<span class='chanlun-tag tag-buy'>📈 上涨</span>")
    elif trend == "下跌":
        tags.append("<span class='chanlun-tag tag-sell'>📉 下跌</span>")
    else:
        tags.append("<span class='chanlun-tag tag-flat'>➡️ 盘整</span>")

    signals = cl.get("signals", [])
    for sig in signals[-3:]:
        t = sig.get("type", "")
        if "买" in t:
            tags.append(f"<span class='chanlun-tag tag-buy'>{t}</span>")
        elif "卖" in t:
            tags.append(f"<span class='chanlun-tag tag-sell'>{t}</span>")

    return " ".join(tags)


def _build_chanlun_detail(s: Dict[str, Any]) -> str:
    """生成缠论详细分析 HTML。"""
    cl = s.get("chanlun")
    if not cl:
        return ""
    parts = []
    summary = cl.get("summary", "")
    if summary:
        parts.append(f"<div class='chanlun-sr'>📐 {escape(summary)}</div>")

    sr = cl.get("support_resistance", [])
    if sr:
        supports = [f"{x['price']:.2f}" for x in sr if x.get("type") == "support"][:3]
        resistances = [f"{x['price']:.2f}" for x in sr if x.get("type") == "resistance"][:3]
        if supports:
            parts.append(f"<div class='chanlun-sr'>🟢 支撑: {' / '.join(supports)}</div>")
        if resistances:
            parts.append(f"<div class='chanlun-sr'>🔴 阻力: {' / '.join(resistances)}</div>")

    return "".join(parts)


def _build_strategy_badge(action: str) -> str:
    """根据操作建议返回带颜色标签。"""
    color_map = {
        "重仓做多": "#2ba471",
        "轻仓试多": "#3370ff",
        "减仓观望": "#ed7b2f",
        "等待": "#646a73",
        "等待/做空": "#646a73",
        "数据不足": "#c9cdd4",
    }
    bg_map = {
        "重仓做多": "#e8f7f0",
        "轻仓试多": "#eef4ff",
        "减仓观望": "#fff3e8",
        "等待": "#f0f0f0",
        "等待/做空": "#f0f0f0",
        "数据不足": "#f7f8fa",
    }
    c = color_map.get(action, "#646a73")
    bg = bg_map.get(action, "#f0f0f0")
    return (
        f"<span style='display:inline-block;padding:2px 8px;border-radius:4px;"
        f"font-size:12px;font-weight:600;background:{bg};color:{c};'>"
        f"{escape(action)}</span>"
    )


def _build_strategy_section(stocks: List[Dict[str, Any]]) -> str:
    """渲染区间套策略监控板块。"""
    # 只显示有策略结果的股票
    items = [s for s in stocks if s.get("strategy")]
    if not items:
        return "<p style='color:#646a73'>暂无区间套策略数据（使用 --skip-strategy 跳过分钟级分析）。</p>"

    rows = []
    for s in items:
        stg = s["strategy"]
        advice = stg.get("advice", {})
        levels = stg.get("levels", {})

        action = advice.get("action", "—")
        confidence = advice.get("confidence", 0)
        risk = advice.get("risk_level", "—")
        reasoning = advice.get("reasoning", "")
        stop = advice.get("stop_loss")
        zone = advice.get("target_zone")

        # 各级别状态浓缩
        lv_cells = []
        for lv_name in ["30min", "5min", "1min"]:
            lv = levels.get(lv_name)
            if lv:
                sig = lv.get("latest_signal_type", "无")
                trend = lv.get("trend", "")
                lv_cells.append(f"<td><b>{escape(lv_name)}</b><br>{escape(trend)} {escape(sig)}</td>")
            else:
                lv_cells.append(f"<td><b>{escape(lv_name)}</b><br>—</td>")

        meta_parts = [f"置信度 {confidence:.0%}", f"风险 {escape(risk)}"]
        if stop:
            meta_parts.append(f"止损 {stop:.2f}")
        if zone:
            meta_parts.append(f"目标 [{zone[0]:.2f}, {zone[1]:.2f}]")

        rows.append(
            f"<div style='border-bottom:1px solid var(--border);padding:12px 0;'>"
            f"<div style='display:flex;align-items:center;gap:10px;margin-bottom:6px;'>"
            f"<b>{escape(str(s.get('name','')))} ({escape(str(s.get('code','')))})</b>"
            f"{_build_strategy_badge(action)}"
            f"<span style='color:var(--muted);font-size:12px'>{' · '.join(meta_parts)}</span>"
            f"</div>"
            f"<table style='font-size:12px;width:auto;margin-bottom:6px;'>"
            f"<tr>{''.join(lv_cells)}</tr></table>"
            f"<div style='color:var(--muted);font-size:12px;'>💡 {escape(reasoning)}</div>"
            f"</div>"
        )

    return "".join(rows)


def _build_stock_cards(stocks: List[Dict[str, Any]]) -> str:
    if not stocks:
        return "<p style='color:#646a73'>当日无龙虎榜数据。</p>"
    cards = []
    for s in stocks:
        change = s.get("change_pct")
        change_cls = "up" if (change is not None and float(change) >= 0) else "down"
        buy_seats = "、".join(s.get("buy_seats", [])[:5]) or "—"
        sell_seats = "、".join(s.get("sell_seats", [])[:5]) or "—"
        ai = (s.get("ai_comment") or "").strip()
        ai_html = (
            f"<div class='ai-comment'>🤖 {escape(ai)}</div>" if ai else ""
        )
        cl_tags = _build_chanlun_tags(s)
        cl_detail = _build_chanlun_detail(s)

        # 策略建议小标签（如果有）
        stg = s.get("strategy")
        stg_html = ""
        if stg:
            action = stg.get("advice", {}).get("action", "")
            conf = stg.get("advice", {}).get("confidence", 0)
            if action:
                stg_html = (
                    f"<div style='margin-top:6px;'>"
                    f"{_build_strategy_badge(action)}"
                    f"<span style='color:var(--muted);font-size:12px;margin-left:6px;'>"
                    f"置信度 {conf:.0%}</span></div>"
                )

        cards.append(
            f"<div class='stock-card'>"
            f"<h3>{escape(str(s.get('name', '')))} "
            f"<span style='color:#646a73;font-weight:400'>({escape(str(s.get('code', '')))})</span>"
            f"{' ' + cl_tags if cl_tags else ''}</h3>"
            f"<div class='stock-meta'>"
            f"涨跌幅 <span class='{change_cls}'>{_fmt_pct(change)}</span> · "
            f"成交额 {_fmt_money(s.get('turnover'))} · "
            f"净买入 {_fmt_money(s.get('net_buy'))}"
            f"</div>"
            f"<div class='seats'><b>买方席位:</b> {escape(buy_seats)}</div>"
            f"<div class='seats'><b>卖方席位:</b> {escape(sell_seats)}</div>"
            f"<div class='seats' style='margin-top:4px'><b>上榜原因:</b> {escape(str(s.get('reason', '')))}</div>"
            f"{cl_detail}"
            f"{stg_html}"
            f"{ai_html}"
            f"</div>"
        )
    return "".join(cards)


def render_daily_report(report_data: Dict[str, Any]) -> str:
    """
    渲染完整 HTML 报告。

    report_data 结构:
        {
            "date": "YYYY-MM-DD",
            "available": bool,
            "count": int,
            "stocks": [...],         # 见 lhb_provider
            "overall_comment": str,  # 可选，全局点评
        }
    """
    stocks = report_data.get("stocks", []) or []
    total_net_buy = sum((s.get("net_buy") or 0) for s in stocks)
    max_change = max((s.get("change_pct") or 0) for s in stocks) if stocks else 0
    status = "已发布" if report_data.get("available") else "尚未发布 / 抓取失败"

    return _HTML_TEMPLATE.format(
        date=escape(str(report_data.get("date", ""))),
        generated_at=escape(datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        count=report_data.get("count", 0),
        total_net_buy=_fmt_money(total_net_buy),
        max_change=_fmt_pct(max_change),
        status=escape(status),
        overall_comment=escape(report_data.get("overall_comment", "—")),
        strategy_section=_build_strategy_section(stocks),
        ranking_table=_build_ranking_table(stocks),
        stock_cards=_build_stock_cards(stocks),
    )


def save_daily_report(report_data: Dict[str, Any], output_dir: str) -> str:
    """渲染并保存到 output_dir/YYYY-MM-DD.html，返回文件绝对路径。"""
    os.makedirs(output_dir, exist_ok=True)
    html = render_daily_report(report_data)
    date_str = report_data.get("date") or datetime.now().strftime("%Y-%m-%d")
    file_path = os.path.abspath(os.path.join(output_dir, f"{date_str}.html"))
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info("HTML 日报已写入: %s", file_path)
    return file_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sample = {
        "date": "2026-06-19",
        "available": True,
        "count": 2,
        "overall_comment": "样例点评：游资活跃，关注次日跟风。",
        "stocks": [
            {"code": "000001", "name": "平安银行", "change_pct": 5.12, "turnover": 1.2e9,
             "net_buy": 1.2e8, "reason": "日涨幅偏离值达7%",
             "buy_seats": ["机构专用"], "sell_seats": ["XX游资"], "ai_comment": "示例点评"},
            {"code": "300750", "name": "宁德时代", "change_pct": -2.3, "turnover": 3e9,
             "net_buy": -5e7, "reason": "日跌幅偏离值达7%",
             "buy_seats": [], "sell_seats": ["机构专用"], "ai_comment": ""},
        ],
    }
    out = save_daily_report(sample, "/tmp/reports_test")
    print("rendered:", out)
