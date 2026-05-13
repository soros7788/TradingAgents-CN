import sys
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

try:
    import akshare as ak
except Exception as e:
    print("缺少依赖：akshare")
    print("请确认 workflow 里安装：pip install akshare pandas")
    raise e


# =========================
# 自选股
# =========================
WATCHLIST = [
    {"code": "002463", "name": "沪电股份"},
    {"code": "300750", "name": "宁德时代"},
    {"code": "688981", "name": "中芯国际"},
]

# =========================
# 参数
# =========================
TOL = 0.01          # A/B 条件允许 1% 误差
BREAK_BUF = 0.003  # 突破确认缓冲 0.3%
FAIL_BUF = 0.01    # 证伪缓冲 1%


def is_main_board(code: str) -> bool:
    """
    主板粗筛：
    沪主板：600 / 601 / 603 / 605
    深主板：000 / 001 / 002 / 003
    """
    return code.startswith(("600", "601", "603", "605", "000", "001", "002", "003"))


def near(a: float, b: float, tol: float = TOL) -> bool:
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) / max(a, b) <= tol


def safe_float(value, default=0.0) -> float:
    try:
        if value is None:
            return default
        if str(value).strip() in ["", "-", "nan", "None"]:
            return default
        return float(value)
    except Exception:
        return default


def get_spot_map() -> dict:
    """
    东方财富 A 股实时行情。
    """
    spot = ak.stock_zh_a_spot_em()
    if spot is None or spot.empty:
        raise RuntimeError("实时行情为空")

    spot["代码"] = spot["代码"].astype(str).str.zfill(6)
    return spot.set_index("代码").to_dict("index")


def get_hist(code: str, days: int = 90) -> pd.DataFrame:
    """
    日线历史行情，前复权。
    """
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

    df = df.tail(days).copy()

    for col in ["开盘", "收盘", "最高", "最低", "成交量", "成交额"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["最高", "最低", "收盘"])

    if len(df) < 60:
        raise RuntimeError(f"日线不足 60 根，当前 {len(df)} 根")

    return df


def analyze_one(code: str, input_name: str, spot_map: dict) -> dict:
    spot = spot_map.get(code, {})

    name = str(spot.get("名称", input_name))
    current = safe_float(spot.get("最新价", 0))
    pct = safe_float(spot.get("涨跌幅", 0))
    amount = safe_float(spot.get("成交额", 0))

    result = {
        "code": code,
        "name": name,
        "current": current,
        "pct": pct,
        "amount_yi": amount / 100000000 if amount else 0,
        "status": "ERROR",
        "reason": "",
        "details": {},
    }

    # C：非 ST + 主板
    if "ST" in name.upper():
        result["status"] = "过滤"
        result["reason"] = "ST 股票，跳过"
        return result

    if not is_main_board(code):
        result["status"] = "过滤"
        result["reason"] = "非主板，跳过"
        return result

    hist = get_hist(code)

    h5 = hist["最高"].tail(5).max()
    h20 = hist["最高"].tail(20).max()
    h60 = hist["最高"].tail(60).max()

    l2 = hist["最低"].tail(2).min()
    l5 = hist["最低"].tail(5).min()

    close = hist["收盘"].iloc[-1]

    # 如果实时价取不到，就用最后收盘价代替
    if current <= 0:
        current = close
        result["current"] = current

    structure_high = h60
    support_low = l5

    # A：5日最高价 ≈ 20日最高价 ≈ 60日最高价
    cond_a = near(h5, h20) and near(h20, h60)

    # B：近2日最低价 ≈ 近5日最低价
    cond_b = near(l2, l5)

    # C：主板 + 非 ST
    cond_c = True

    # D：当前价突破最近结构最高价
    cond_d = current > structure_high * (1 + BREAK_BUF)

    # 证伪：当前价跌破近5日低点
    failed = current < support_low * (1 - FAIL_BUF)

    result["details"] = {
        "h5": h5,
        "h20": h20,
        "h60": h60,
        "l2": l2,
        "l5": l5,
        "close": close,
        "structure_high": structure_high,
        "support_low": support_low,
        "A": cond_a,
        "B": cond_b,
        "C": cond_c,
        "D": cond_d,
        "failed": failed,
    }

    if failed:
        result["status"] = "证伪"
        result["reason"] = f"当前价 {current:.2f} 跌破支撑 {support_low:.2f}"
    elif cond_a and cond_b and cond_c and cond_d:
        result["status"] = "突破"
        result["reason"] = f"当前价 {current:.2f} 突破结构高点 {structure_high:.2f}"
    elif cond_a and cond_b and cond_c:
        result["status"] = "等待"
        result["reason"] = f"结构有效，等待突破 {structure_high:.2f}"
    elif cond_a and not cond_b:
        result["status"] = "观察"
        result["reason"] = "高点压缩存在，但低点承接不足"
    else:
        result["status"] = "未成型"
        result["reason"] = "不满足 5/20/60 高点重合"

    return result


def format_one(r: dict) -> str:
    d = r.get("details", {})

    if d:
        return (
            f"{r['code']} {r['name']}｜{r['status']}\n"
            f"现价：{r['current']:.2f}｜涨跌幅：{r['pct']:.2f}%｜成交额：{r['amount_yi']:.2f}亿\n"
            f"原因：{r['reason']}\n"
            f"A高点重合：{d.get('A')}｜B低点承接：{d.get('B')}｜D突破：{d.get('D')}｜证伪：{d.get('failed')}\n"
            f"5高：{d.get('h5', 0):.2f}｜20高：{d.get('h20', 0):.2f}｜60高：{d.get('h60', 0):.2f}\n"
            f"2低：{d.get('l2', 0):.2f}｜5低：{d.get('l5', 0):.2f}\n"
            f"结构高点：{d.get('structure_high', 0):.2f}｜支撑低点：{d.get('support_low', 0):.2f}"
        )

    return (
        f"{r['code']} {r['name']}｜{r['status']}\n"
        f"原因：{r['reason']}"
    )


def format_result(results: list[dict]) -> str:
    now = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")

    trigger = [r for r in results if r["status"] == "突破"]
    wait = [r for r in results if r["status"] == "等待"]
    failed = [r for r in results if r["status"] == "证伪"]
    observe = [r for r in results if r["status"] == "观察"]
    other = [r for r in results if r["status"] not in ["突破", "等待", "证伪", "观察"]]

    lines = []
    lines.append("A股等突破自动扫描")
    lines.append(f"时间：{now}")
    lines.append("")
    lines.append(f"突破：{len(trigger)} 只")
    lines.append(f"等待：{len(wait)} 只")
    lines.append(f"证伪：{len(failed)} 只")
    lines.append(f"观察：{len(observe)} 只")
    lines.append(f"其他：{len(other)} 只")
    lines.append("")

    def add_group(title: str, items: list[dict]):
        if not items:
            return
        lines.append(title)
        for item in items:
            lines.append(format_one(item))
            lines.append("")

    add_group("一、突破信号", trigger)
    add_group("二、等待突破", wait)
    add_group("三、证伪退出", failed)
    add_group("四、观察", observe)
    add_group("五、其他", other)

    lines.append("规则：")
    lines.append("A：5日最高 ≈ 20日最高 ≈ 60日最高")
    lines.append("B：近2日最低 ≈ 近5日最低")
    lines.append("C：主板 + 非ST")
    lines.append("D：当前价突破结构高点")
    lines.append("")
    lines.append("提示：仅用于策略观察，不构成投资建议。")

    return "\n".join(lines)


def main():
    try:
        spot_map = get_spot_map()
        results = []

        for item in WATCHLIST:
            try:
                results.append(analyze_one(item["code"], item["name"], spot_map))
            except Exception as e:
                results.append({
                    "code": item["code"],
                    "name": item["name"],
                    "current": 0,
                    "pct": 0,
                    "amount_yi": 0,
                    "status": "ERROR",
                    "reason": str(e),
                    "details": {},
                })

        print(format_result(results))

    except Exception:
        print("A股等突破扫描失败")
        print(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
