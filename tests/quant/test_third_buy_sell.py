#!/usr/bin/env python3
"""
第三类买卖点（三买/三卖）确认与买卖区间判定测试

测试场景:
  1. 三买确认成功 — 回踩低点 > ZG
  2. 三买中枢扩展失败 — 回踩低点进入 [ZD, ZG] 区间
  3. 三买重回中枢失败 — 回踩低点 < ZD
  4. 三卖确认成功 — 反弹高点 < ZD
  5. 三卖中枢扩展失败 — 反弹高点进入 [ZG, ZD] 区间
  6. 三卖重回中枢失败 — 反弹高点 > ZG
  7. 次级别走势未完备
  8. 零值边界测试
"""
import sys, os
import pandas as pd

# 确保项目根目录在 sys.path 中
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# 先尝试从独立模块导入（轻量，无外部依赖）
_judge_path = os.path.join(_project_root, 'scripts', 'chanlun-workflow')
if _judge_path not in sys.path:
    sys.path.insert(0, _judge_path)

try:
    from third_buy_sell_judge import evaluate_third_buy_sell
except ImportError:
    # 回退到 tradingagents 模块导入（需完整项目依赖）
    from tradingagents.daily_report.chanlun_strategy import evaluate_third_buy_sell


def make_segment(highs, lows):
    """构造模拟K线段 DataFrame"""
    return pd.DataFrame({
        "high": highs,
        "low": lows,
    })


def test_third_buy_confirmed():
    """【测试 1】三买确认成功: L_pullback > ZG"""
    center = {"zg": 10.0, "zd": 9.0}
    leave = make_segment([10.5, 11.0, 10.8], [10.0, 10.5, 10.3])
    pullback = make_segment([10.6, 10.4, 10.5], [10.3, 10.1, 10.3])

    result = evaluate_third_buy_sell(center, leave, pullback, "third_buy", True)

    assert result["is_confirmed"] is True, f"预期确认成功，但结果={result['status']}"
    assert result["status"] == "CONFIRMED_THIRD_BUY", f"状态码错误: {result['status']}"
    assert result["invalid_boundary"] == 10.0, f"失效边界应为 ZG=10.0，实际={result['invalid_boundary']}"
    assert result["optimal_buy_min"] == 10.01, f"黄金买入区间下限应为 10.01，实际={result['optimal_buy_min']}"
    assert result["optimal_buy_max"] == 10.38, (
        f"黄金买入区间上限应为 10.38(=10.0+(11.0-10.0)*0.382=10.382→round=10.38)，实际={result['optimal_buy_max']}"
    )
    assert result["cushion"] > 0, f"安全缓冲垫应为正数，实际={result['cushion']}"
    assert "三买确认成功" in result["reason"], f"判定原因异常: {result['reason']}"

    print(f"  ✅ 三买确认成功: is_confirmed={result['is_confirmed']} | status={result['status']}")
    print(f"     确认区间: invalid_boundary={result['invalid_boundary']}, "
          f"optimal_buy=[{result['optimal_buy_min']}, {result['optimal_buy_max']}], "
          f"cushion={result['cushion']}")
    print(f"     判定原因: {result['reason']}")


def test_third_buy_center_expansion():
    """【测试 2】三买中枢扩展失败: ZG ≥ L_pullback ≥ ZD"""
    center = {"zg": 10.0, "zd": 9.0}
    leave = make_segment([10.5, 11.0, 10.8], [10.0, 10.5, 10.3])
    pullback = make_segment([10.3, 10.0, 10.2], [9.8, 9.5, 9.6])

    result = evaluate_third_buy_sell(center, leave, pullback, "third_buy", True)

    assert result["is_confirmed"] is False, "预期确认失败，但返回成功"
    assert result["status"] == "REJECTED_CENTER_EXPANSION", f"状态码错误: {result['status']}"
    assert "中枢震荡" in result["reason"] or "中枢扩展" in result["reason"], f"判定原因异常: {result['reason']}"

    print(f"  ✅ 三买中枢扩展失败: is_confirmed={result['is_confirmed']} | status={result['status']}")
    print(f"     判定原因: {result['reason']}")


def test_third_buy_center_reentry():
    """【测试 3】三买重回中枢失败: L_pullback < ZD"""
    center = {"zg": 10.0, "zd": 9.0}
    leave = make_segment([10.5, 11.0, 10.8], [10.0, 10.5, 10.3])
    pullback = make_segment([10.3, 10.0, 9.5], [9.8, 8.5, 8.8])

    result = evaluate_third_buy_sell(center, leave, pullback, "third_buy", True)

    assert result["is_confirmed"] is False, "预期确认失败，但返回成功"
    assert result["status"] == "REJECTED_CENTER_REENTRY", f"状态码错误: {result['status']}"
    assert "重回中枢" in result["reason"], f"判定原因异常: {result['reason']}"

    print(f"  ✅ 三买重回中枢失败: is_confirmed={result['is_confirmed']} | status={result['status']}")
    print(f"     判定原因: {result['reason']}")


def test_third_sell_confirmed():
    """【测试 4】三卖确认成功: H_rebound < ZD"""
    center = {"zg": 10.0, "zd": 9.0}
    leave = make_segment([9.5, 9.0, 9.2], [9.0, 8.5, 8.8])
    pullback = make_segment([8.9, 8.8, 8.9], [8.7, 8.5, 8.6])

    result = evaluate_third_buy_sell(center, leave, pullback, "third_sell", True)

    assert result["is_confirmed"] is True, f"预期确认成功，但结果={result['status']}"
    assert result["status"] == "CONFIRMED_THIRD_SELL", f"状态码错误: {result['status']}"
    assert result["invalid_boundary"] == 9.0, f"失效边界应为 ZD=9.0，实际={result['invalid_boundary']}"
    assert result["optimal_sell_min"] == 8.81, (
        f"黄金卖出区间下限应为 8.81(=9.0-(9.0-8.5)*0.382=8.809→round=8.81)，实际={result['optimal_sell_min']}"
    )
    assert result["optimal_sell_max"] == 8.99, f"黄金卖出区间上限应为 8.99，实际={result['optimal_sell_max']}"
    assert result["cushion"] > 0, f"安全缓冲垫应为正数，实际={result['cushion']}"
    assert "三卖确认成功" in result["reason"], f"判定原因异常: {result['reason']}"

    print(f"  ✅ 三卖确认成功: is_confirmed={result['is_confirmed']} | status={result['status']}")
    print(f"     确认区间: invalid_boundary={result['invalid_boundary']}, "
          f"optimal_sell=[{result['optimal_sell_min']}, {result['optimal_sell_max']}], "
          f"cushion={result['cushion']}, cushion_ratio={result['cushion_ratio']}")
    print(f"     判定原因: {result['reason']}")


def test_third_sell_center_expansion():
    """【测试 5】三卖中枢扩展失败: ZD ≤ H_rebound ≤ ZG"""
    center = {"zg": 11.0, "zd": 10.0}
    leave = make_segment([9.5, 9.0, 9.2], [9.0, 8.5, 8.8])
    pullback = make_segment([10.3, 10.5, 10.4], [10.0, 10.2, 10.1])

    result = evaluate_third_buy_sell(center, leave, pullback, "third_sell", True)

    assert result["is_confirmed"] is False, "预期确认失败，但返回成功"
    assert result["status"] == "REJECTED_CENTER_EXPANSION", f"状态码错误: {result['status']}"

    print(f"  ✅ 三卖中枢扩展失败: is_confirmed={result['is_confirmed']} | status={result['status']}")
    print(f"     判定原因: {result['reason']}")


def test_third_sell_center_reentry():
    """【测试 6】三卖重回中枢失败: H_rebound > ZG"""
    center = {"zg": 9.0, "zd": 10.0}
    leave = make_segment([9.5, 9.0, 9.2], [9.0, 8.5, 8.8])
    pullback = make_segment([10.5, 10.2, 10.3], [10.0, 9.8, 9.9])

    result = evaluate_third_buy_sell(center, leave, pullback, "third_sell", True)

    assert result["is_confirmed"] is False, "预期确认失败，但返回成功"
    assert result["status"] == "REJECTED_CENTER_REENTRY", f"状态码错误: {result['status']}"

    print(f"  ✅ 三卖重回中枢失败: is_confirmed={result['is_confirmed']} | status={result['status']}")
    print(f"     判定原因: {result['reason']}")


def test_sub_level_not_perfect():
    """【测试 7】次级别走势未完备"""
    center = {"zg": 10.0, "zd": 9.0}
    leave = make_segment([10.5, 11.0], [10.0, 10.5])
    pullback = make_segment([10.6, 10.4], [10.3, 10.1])  # 只有2行，不足3笔

    result = evaluate_third_buy_sell(center, leave, pullback, "third_buy", False)

    assert result["is_confirmed"] is True, f"几何条件满足应确认，但结果={result['status']}"
    assert "次级别走势未完备" in result["reason"], f"应提示次级别风险，实际={result['reason']}"

    print(f"  ✅ 次级别未完备警告: status={result['status']}")
    print(f"     判定原因: {result['reason']}")


def test_zero_boundary():
    """【测试 8】零值边界测试"""
    center = {"zg": 0.0, "zd": -1.0}
    leave = make_segment([0.5, 1.0, 0.8], [0.0, 0.5, 0.3])
    pullback = make_segment([0.6, 0.4, 0.5], [0.3, 0.1, 0.2])

    result = evaluate_third_buy_sell(center, leave, pullback, "third_buy", True)

    assert result["is_confirmed"] is True, f"零值边界应确认成功，实际={result['status']}"
    assert result["cushion_ratio"] == 0.0, f"ZG=0 时 cushion_ratio 应为 0，实际={result['cushion_ratio']}"

    print(f"  ✅ 零值边界测试通过: status={result['status']}, cushion_ratio={result['cushion_ratio']}")


if __name__ == "__main__":
    print("=" * 70)
    print("【测试 4】第三类买卖点（三买/三卖）确认与买卖区间判定测试")
    print("=" * 70)

    tests = [
        ("三买确认成功", test_third_buy_confirmed),
        ("三买中枢扩展失败", test_third_buy_center_expansion),
        ("三买重回中枢失败", test_third_buy_center_reentry),
        ("三卖确认成功", test_third_sell_confirmed),
        ("三卖中枢扩展失败", test_third_sell_center_expansion),
        ("三卖重回中枢失败", test_third_sell_center_reentry),
        ("次级别走势未完备", test_sub_level_not_perfect),
        ("零值边界测试", test_zero_boundary),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        print(f"\n[{name}]:")
        try:
            fn()
            passed += 1
            print(f"  → PASS")
        except AssertionError as e:
            print(f"  ❌ FAIL: {e}")
            failed += 1
        except Exception as e:
            print(f"  ❌ ERROR: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{'=' * 70}")
    print(f"测试结果: {passed}/{passed + failed} 通过")
    if failed > 0:
        print(f"⚠️  {failed} 个测试失败")
    else:
        print("✅ 全部测试通过")
    print(f"{'=' * 70}")