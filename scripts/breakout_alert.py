from datetime import datetime
from zoneinfo import ZoneInfo
import sys

import pandas as pd
import akshare as ak


WATCHLIST = [
    {"code": "002463", "name": "沪电股份"},
    {"code": "300750", "name": "宁德时代"},
    {"code": "688981", "name": "中芯国际"},
]

TOL = 0.01
BREAK_BUF = 0.003
FAIL_BUF = 0.01


def is_main_board(code: str) -> bool:
    return code.startswith(("600", "601", "603", "605", "000", "001", "002", "003"))


def near(a: float, b: float, tol: float = TOL) -> bool:
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) / max(a, b) <= tol


def get_hist(code: str) -> pd.DataFrame:
    today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")

    df = ak.stock_zh_a_hist(
        symbol=code,
        period="daily",
        start_date="20240101",
        end_date=today,
        adjust="qfq",
    )

    if df is None or df.empty:
        raise RuntimeError("历史行情为空")

    df = df.tail(90).copy()

    for col in ["开盘", "收盘", "最高", "最低", "成交量", "成交额"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["最高", "最低", "收盘"])

    if len(df) < 60:
        raise RuntimeError(f"日线不足60根，当前{len(df)}根")

    return df


def analyze_one(code: str, name: str) -> dict:
    result = {
        "code": code,
        "name": name,
        "status": "ERROR",
        "reason": "",
    }

    if not is_main_board(code):
        result["status"] = "过滤"
        result["reason"] = "非主板，跳过"
        return result

    df = get_hist(code)

    current = float(df["收盘"].iloc[-1])
    h5 = float(df["最高"].tail(5).max())
    h20 = float(df["最高"].tail(20).max())
    h60 = float(df["最高"].tail(60).max())
    l2 = float(df["最低"].tail(2).min())
    l5 = float(df["最低"].tail(5).min())

    amount = float(df["成交额"].iloc[-1]) if "成交额" in df.columns else 0

    cond_a = near(h5, h20) and near(h20, h60)
    cond_b = near(l2, l5)
    cond_d = current > h60 * (1 + BREAK_BUF)
    failed = current < l5 * (1 - FAIL_BUF)

    if failed:
        status = "证伪"
        reason = f"收盘价 {current:.2f} 跌破支撑 {l5:.2f}"
    elif cond_a and cond_b and cond_d:
        status = "突破"
        reason = f"收盘价 {current:.2f} 突破结构高点 {h60:.2f}"
    elif cond_a and cond_b:
        status = "等待"
        reason = f"结构有效，等待突破 {h60:.2f}"
    elif cond_a:
        status = "观察"
        reason = "高点压缩存在，但低点承接不足"
    else:
        status = "未成型"
        reason = "不满足5/20/60高点重合"

    result.update({
        "status": status,
        "reason": reason,
        "current": current,
        "amount_yi": amount / 100000000 if amount else 0,
        "h5": h5,
        "h20": h20,
        "h60": h60,
        "l2": l2,
        "l5": l5,
        "A": cond_a,
        "B": cond_b,
        "D": cond_d,
        "failed": failed,
    })

    return result


def format_result(results):
    now = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")

    lines = []
    lines.append("A股等突破自动扫描")
    lines.append(f"时间：{now}")
    lines.append("")
    lines.append(f"突破：{sum(r['status'] == '突破' for r in results)}")
    lines.append(f"等待：{sum(r['status'] == '等待' for r in results)}")
    lines.append(f"证伪：{sum(r['status'] == '证伪' for r in results)}")
    lines.append(f"观察：{sum(r['status'] == '观察' for r in results)}")
    lines.append("")

    for r in results:
        lines.append(f"{r['code']} {r['name']}｜{r['status']}")
        lines.append(f"原因：{r['reason']}")

        if "current" in r:
            lines.append(f"价格：{r['current']:.2f}｜成交额：{r['amount_yi']:.2f}亿")
            lines.append(f"A高点重合：{r['A']}｜B低点承接：{r['B']}｜D突破：{r['D']}｜证伪：{r['failed']}")
            lines.append(f"5高：{r['h5']:.2f}｜20高：{r['h20']:.2f}｜60高：{r['h60']:.2f}")
            lines.append(f"2低：{r['l2']:.2f}｜5低：{r['l5']:.2f}")
        lines.append("")

    lines.append("规则：")
    lines.append("A：5日最高≈20日最高≈60日最高")
    lines.append("B：近2日最低≈近5日最低")
    lines.append("C：主板+非ST")
    lines.append("D：当前价突破结构高点")
    lines.append("")
    lines.append("提示：仅用于策略观察，不构成投资建议。")

    return "\n".join(lines)


def main():
    results = []

    for item in WATCHLIST:
        try:
            results.append(analyze_one(item["code"], item["name"]))
        except Exception as e:
            results.append({
                "code": item["code"],
                "name": item["name"],
                "status": "ERROR",
                "reason": f"{type(e).__name__}: {e}",
            })

    print(format_result(results))

    # 不让单只股票失败导致 Actions 失败
    sys.exit(0)


if __name__ == "__main__":
    main()
