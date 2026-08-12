# TRAE Work → TRAE Code 迁移说明

> 生成时间：2026-08-11 22:30
> 项目：`TradingAgents-CN` — 缠论量化交易系统

---

## 1. 项目目标

一个基于**缠论背驰理论**的A股量化交易系统，运行在 GitHub Actions 上，每日自动执行以下流程：

| 时间(北京时间) | 任务 | 说明 |
|---|---|---|
| 10:00/11:00/13:00/14:00 | 盘中扫描 | 30min候选池更新 + 持仓止损检查 |
| 14:30 | 尾盘扫描 | 收盘前最后检查，防尾盘急跌漏检 |
| 15:05 | 日线全市场扫描 | 2234只股票背驰分析 → 候选池更新 |
| 15:30 | 合规核查 | 持仓合规检查 + 去弱留强 |
| 16:00 | 持仓背驰确认 | 顶背驰检测 → 清仓/减仓预警 |
| 16:30 | 交易计划生成 | 多级别信号 + 加仓计算器 → 明日计划 |

核心逻辑：
- 使用 **新浪财经API** 获取K线数据
- 缠论中枢识别 + MACD面积比背驰判定
- **DL模型**（MLPClassifier）和 **EP模型** 计算背驰概率
- 递归合成：5min → 30min → 日线（省67% API调用）
- 持仓数据存储在 `trade-workbook.xlsx`（Excel文件）

---

## 2. 已完成的核心修改

### 2.1 超时保护体系 (2026-08-11)

| 文件 | 修改内容 |
|---|---|
| `full_scan.py` | 全局 `socket.setdefaulttimeout(30)`，每只股票30秒超时（ThreadPoolExecutor），每100只保存断点 |
| `concurrent_prefetch.py` | `as_completed` 加30秒全局超时，超时后 `shutdown(wait=False, cancel_futures=True)` |
| `beichi_analyzer.py` | 全局 `socket.setdefaulttimeout(30)` |
| `daily_workflow.py` | 所有 `print()` 加 `flush=True` |

### 2.2 并发预取 V2 (2026-08-09)

- 新增 `concurrent_prefetch.py`，20线程并发预取5min数据到持久缓存
- 递归合成：5min → 30min → 日线（从 `chanlun_recursive.py` 合成）
- API调用从 3级别×2886只=8658次 降至 1级别×2886只=2886次（省67%）

### 2.3 断点续跑 (2026-08-11)

- `full_scan.py` 每扫描100只股票写入 `.scan_checkpoint` 进度文件
- 中断后重新运行时自动检测断点，跳过已扫描部分
- 文件路径：`scripts/chanlun-workflow/.scan_checkpoint`

### 2.4 背驰判定升级 (2026-08-08 ~ 2026-08-10)

- 多级别tier分级：核心池/观察池/边缘池（基于30min一买准入）
- 30min几何双中枢趋势背驰检测（顶背驰+底背驰）
- 30min几何单中枢盘整背驰检测
- 三买豁免机制：B点回调的卖单信号不构成风险

### 2.4.1 有效双中枢判定 (2026-08-12)

**用户规则**：7个中枢不一定是有效双中枢，需考虑中枢是否重叠。

- 有效双中枢 = 两个中枢价格区间**不重叠且依次分离**（沿同一方向排列）
  - 上涨：`c1.zg < c2.zd`（前中枢上沿 < 后中枢下沿）
  - 下跌：`c1.zd > c2.zg`（前中枢下沿 > 后中枢上沿）
- 大量重叠/扩张的中枢本质是**大级别盘整**，不算有效双中枢 → 归 ABC 体系
- 新增 `is_valid_double_center(zss, direction)` 统一判定函数
- 同步修正 `detect_double_center_top_divergence` / `detect_double_center_divergence`：
  - 移除旧版"仅数值上移/下移"的放宽逻辑（曾把重叠中枢误判为双中枢）
  - 结果新增 `valid_double_center` 字段并传播到 `min30_double_center_top` / `min30_double_center`
- **买卖区间分类**：有效双中枢 → 123体系（一买~三卖）；单中枢/重叠盘整 → ABC体系（A买/A卖），`point_class` 字段标识

### 2.4.2 三买确认 Bug 修复 (2026-08-12)

排查贤丰控股(002141)三买状态时发现并修复三个 bug，改动集中在 `beichi_analyzer.py`：

**Bug1a：三买"一会儿有一会儿无"抖动**
- 根因：`sell_conflict = min30_sell_dl_p > 0.5` 单阈值硬切，而 `min30_sell_dl_p` 对最新一根K线形态极敏感（实测 0.08↔0.85 跳变），导致三买在确认/拒绝间震荡
- 修复：新增模块级 `_SELL_HYSTERESIS_BAND` 滞回带，进入拒绝需 `>0.6`、恢复确认需 `<0.4`，中间区(0.4~0.6)保持上次状态

**Bug1b：缓存粘滞导致"今天有/明天无"假象**
- 根因：`analyze_beichi` 进程级内存缓存**无过期时间**，同一进程内多次调用永远返回第一次结果
- 修复：缓存改为 `(时间戳, 结果)` 结构，超过 60 秒自动失效重拉；单次运行内部调用仍复用（避免重复请求）

**Bug2：确认三买时已错过买区间仍报确认**
- 根因：三买靠历史K线确认（回踩低点>ZG），等几何成立时价格往往已逃离 `optimal_buy` 区间，但 `CONFIRMED_THIRD_BUY` 仍成立
- 修复：确认分支新增现价检查，`price > optimal_buy_max` 时标记 `buy_opportunity_passed=True`、`status=OPPORTUNITY_PASSED`、`is_confirmed=False`，附 `passed_note` 提示"勿追高"
- 验证：002141 现价 6.98 > 买区上沿 6.22，正确触发错过标记

### 2.5 持仓背驰确认 V2 (2026-08-10)

- 新增 `position_divergence.py`（533行）
- 五点优先级判定：一买破位 > 双中枢顶背驰 > 单中枢顶背驰 > 三买豁免 > 看空信号
- 输出：清仓预警 + 减仓预警 + 结构化报告

### 2.6 交易计划生成 V1 (2026-08-09)

- 新增 `trading_plan.py`（33KB）
- 加仓计算器 `calc_add_position()`：公式 `x = S * (target - E) / (P - target)`
- 四舍五入豁免机制

### 2.7 GitHub Actions 工作流

- `daily-trading-workflow.yml`：主工作流，定时触发6个时间段
- `tradingagents-telegram.yml`：盘中高频推送（09:35-14:35，每30分钟）
- **超时从45分钟延长到90分钟**（2026-08-11修复）

### 2.8 持仓数据同步 (2026-08-11)

- `trade-workbook.xlsx` 持仓表已同步券商实际数据：
  - 删除台华新材、沃华医药（不在实际持仓）
  - 贤丰控股：200股@5.515 → 300股@6.047
  - 总资产：18,228.42 → 22,103.58
  - 可用现金：12,746.42 → 19,498.58

---

## 3. 还没完成的问题

### 3.1 scan 命令预取顺序错误（BUG-10）

**问题**：`daily_workflow.py` 第3408-3417行，`scan` 命令的 `--prefetch` 参数在 `run_full_scan()` **之后**才调用 `prefetch_candidate_pool()`，预取对扫描无帮助。

**状态**：`run_full_scan()` 内部已硬编码 `prefetch=True`，所以不影响全量扫描。但 `--prefetch` CLI参数是摆设。

### 3.2 候选池持久化更新问题

**问题**：候选池数据写入Excel后，GitHub Actions 通过 md5 对比判断是否提交。如果候选池内容无变化则不提交，导致后续工作流读取到旧数据。

**状态**：当前设计是"有变化才提交"，这是合理的。但需确认候选池确实能检测到变化。

### 3.3 持仓表与交易记录交叉验证

**问题**：`get_today_holdings()` 按代码去重（取最后出现的行），依赖持仓表中的行顺序。如果同一股票出现多次，取最后一行。但未与交易记录做交叉验证。

**状态**：功能正常，但缺少交易记录回测验证逻辑。

### 3.4 速率限制优化不彻底

**问题**：`full_scan.py` 的 `time.sleep(0.05)` 已改为缓存命中时跳过，但只在 `(i+1)%5==0` 时检查缓存。第一次全量扫描时缓存未命中，仍需 sleep。

**状态**：首次扫描约2860只×每5只sleep 50ms = 约28秒。后续扫描因缓存命中全部跳过。

---

## 4. 涉及的文件

### 4.1 核心Python文件

| 文件 | 大小 | 行数(约) | 功能 |
|---|---|---|---|
| `scripts/chanlun-workflow/beichi_analyzer.py` | 163KB | ~3500 | 缠论背驰分析核心（中枢识别、信号检测、ML模型） |
| `scripts/chanlun-workflow/daily_workflow.py` | 165KB | ~3518 | 主工作流入口（全部10个命令分发、合规检查、Telegram推送） |
| `scripts/chanlun-workflow/full_scan.py` | 23KB | ~517 | 全市场候选扫描（沪A+深市、预取、超时保护、断点续跑） |
| `scripts/chanlun-workflow/position_divergence.py` | 21KB | ~533 | 持仓背驰确认V2（顶背驰检测、五点判定） |
| `scripts/chanlun-workflow/trading_plan.py` | 33KB | ~900 | 交易计划生成（加仓计算器、多级别信号） |
| `scripts/chanlun-workflow/concurrent_prefetch.py` | 8KB | ~225 | 并发数据预取V2（20线程、超时保护） |
| `scripts/chanlun-workflow/chanlun_recursive.py` | 19KB | ~400 | 递归K线合成（5min→30min→日线） |
| `scripts/chanlun-workflow/abc_buy_sell_judge.py` | 8KB | ~200 | ABC买卖区间判定 |
| `scripts/chanlun-workflow/third_buy_sell_judge.py` | 6KB | ~150 | 三买/三卖判定 |

### 4.2 GitHub Actions 工作流文件

| 文件 | 大小 | 功能 |
|---|---|---|
| `.github/workflows/daily-trading-workflow.yml` | 7KB | 主工作流（6个定时触发、90分钟超时） |
| `.github/workflows/tradingagents-telegram.yml` | 5KB | Telegram推送工作流（9个定时触发、35分钟超时） |
| `.github/workflows/scan.py` | 5KB | 旧版扫描脚本（未使用，保留历史） |

### 4.3 ML模型文件

| 文件 | 大小 | 功能 |
|---|---|---|
| `scripts/chanlun-workflow/dl_model.pkl` | 322KB | DL模型（MLPClassifier） |
| `scripts/chanlun-workflow/dl_scaler.pkl` | 834B | DL模型标准化器 |
| `scripts/chanlun-workflow/ep_model.pkl` | 334KB | EP模型（MLPClassifier） |
| `scripts/chanlun-workflow/ep_scaler.pkl` | 930B | EP模型标准化器 |
| `scripts/chanlun-workflow/ep_train_meta.pkl` | 980B | EP训练元数据 |

### 4.4 数据文件

| 文件 | 大小 | 功能 |
|---|---|---|
| `scripts/chanlun-workflow/trade-workbook.xlsx` | 67KB | 交易工作簿（持仓表、账户总表、候选池、交易记录等9个sheet） |
| `.github/workflows/watchlist.json` | 132B | 监控列表（东风股份、沪电股份、上证指数） |

### 4.5 文档文件

| 文件 | 功能 |
|---|---|
| `scripts/chanlun-workflow/5.5_review_fixes_summary.md` | 5.5版本复核修复总结 |
| `scripts/chanlun-workflow/README.md` | 项目说明 |
| `scripts/chanlun-workflow/WORKFLOW.md` | 工作流说明 |

---

## 5. 每个文件要改什么

### 5.1 `daily_workflow.py` — 需要修改

**Bug 修复**：`scan` 命令的预取顺序错误（第3408-3417行）

当前代码：
```python
elif cmd == "scan":
    use_prefetch = "--prefetch" in sys.argv
    clean_excel()
    scan_data = run_full_scan()          # ← 先扫描
    if use_prefetch:
        prefetch_candidate_pool()        # ← 后预取（顺序反了）
```

应改为：
```python
elif cmd == "scan":
    use_prefetch = "--prefetch" in sys.argv
    if use_prefetch:
        from concurrent_prefetch import prefetch_candidate_pool
        prefetch_candidate_pool(max_workers=20)  # ← 先预取
    clean_excel()
    scan_data = run_full_scan()                   # ← 后扫描
```

### 5.2 `full_scan.py` — 无需修改（已稳定）

当前状态：包含超时保护、断点续跑、缓存优化sleep、分层候选池、全市场诊断。功能完整。

### 5.3 `concurrent_prefetch.py` — 无需修改（已稳定）

当前状态：包含超时保护、线程安全进度报告、持仓预取/全市场预取。功能完整。

### 5.4 `position_divergence.py` — 无需修改（已稳定）

当前状态：V2版本，五点判定优先级，完整报告输出。功能完整。

### 5.5 `trading_plan.py` — 无需修改（已稳定）

当前状态：V1版本，加仓计算器、多级别信号、计划生成。功能完整。

### 5.6 `beichi_analyzer.py` — 谨慎修改（核心算法）

**注意**：这是最核心的缠论分析引擎，163KB、3500行。修改必须极其谨慎，建议：
- 不修改中枢识别算法
- 不修改背驰判定逻辑
- 不修改缓存机制
- 如果确实需要修改，先阅读 `daily_workflow.py` 中 `check_compliance()` 函数（第600-900行）了解信号使用方式

### 5.7 `.github/workflows/daily-trading-workflow.yml` — 无需修改（已稳定）

当前状态：timeout-minutes已调整为90分钟，cron表达式匹配正确，md5对比防重复提交。

### 5.8 `.github/workflows/tradingagents-telegram.yml` — 无需修改（已稳定）

当前状态：9个定时触发，35分钟超时，内嵌Python推送脚本。

---

## 6. 不能改动的地方

### 6.1 算法约束

| 禁止修改 | 原因 |
|---|---|
| **中枢识别参数**（`min_amp_map`、`min_w_map`） | 经过大量测试调整，改后可能产生大量噪声中枢 |
| **背驰判定阈值**（ratio < 60% 确认、DL_P > 0.8 确认） | 与全市场扫描、合规检查、候选池分层联动，需统一修改 |
| **递归合成逻辑**（`chanlun_recursive.py`） | 5min→30min→日线合成是省67% API调用的基础 |
| **ML模型文件**（4个 `.pkl` 文件） | 模型训练独立于代码，修改代码不影响模型 |

### 6.2 数据约束

- **Excel 文件结构**：`trade-workbook.xlsx` 的9个sheet名称和列位置不能改，`daily_workflow.py` 中 `get_today_holdings()` 依赖硬编码的列索引
- **列索引映射**（第299-350行 `get_today_holdings`）：
  - 列2=股票名称，列3=代码，列4=是否WAIVED
  - 列7=持股数量，列8=持仓均价，列9=当前价
  - 列13=浮盈亏比，列14=持仓占比
  - 列16=止损价，列28=操作信号，列35=T+1锁定
- **账户总表列索引**（第485行 `get_account_summary`）：
  - 列2=总资产，列3=可用现金，列4=持仓市值

### 6.3 环境约束

| 约束 | 说明 |
|---|---|
| **Python 3.11** | GitHub Actions 环境固定，`setup-python@v5` 指定 |
| **scikit-learn 1.7.2** | 模型用此版本训练的，升级可能破坏 `.pkl` 兼容性 |
| **新浪财经API** | 仅支持 HTTP，不支持 HTTPS 的某些接口；返回 GBK 编码 |
| **GitHub Actions 磁盘** | 持久缓存位于 `~/.cache/chanlun_kline/`，每次checkout后缓存重置 |
| **Telegram 消息长度** | `send_telegram()` 自动拆分长消息，每段不超过4096字符 |

### 6.4 文件名约束

| 文件名 | 不能改的原因 |
|---|---|
| `trade-workbook.xlsx` | GitHub Actions artifact 上传、git commit 路径、md5对比都硬编码此文件名 |
| `beichi_analyzer.py` | 其他所有文件（`full_scan.py`、`daily_workflow.py`、`concurrent_prefetch.py`）都 `from beichi_analyzer import` |

---

## 7. 已知 Bug 和验证方式

### 7.1 Bug-10: scan 命令预取顺序错误

| 项目 | 内容 |
|---|---|
| **严重程度** | 低（不影响功能，仅影响性能） |
| **位置** | `daily_workflow.py` 第3408-3417行 |
| **现象** | `--prefetch` 参数在 `run_full_scan()` 之后才调用预取，预取对扫描无帮助 |
| **根因** | 代码顺序错误：先执行扫描，再调用预取 |
| **修复方式** | 将 `prefetch_candidate_pool()` 移到 `run_full_scan()` 之前 |
| **验证方式** | 运行 `python daily_workflow.py scan --prefetch`，观察日志中预取是否在扫描之前完成 |

### 7.2 Bug-11: 全市场无 DL_P>0.8 时自动诊断

| 项目 | 内容 |
|---|---|
| **严重程度** | 中 |
| **位置** | `full_scan.py` 第406-447行 |
| **现象** | 全市场扫描结果为0时，自动执行诊断检查（数据源、抽样验证） |
| **当前状态** | 诊断逻辑已实现，但不会阻断扫描流程。如果确实遇到bug，候选池保持昨日数据 |
| **验证方式** | 手动运行 `python full_scan.py`，观察确认信号数量。如果为0且全市场>100只，查看诊断报告 |

### 7.3 Bug-12: 新浪 API 偶尔返回空数据

| 项目 | 内容 |
|---|---|
| **严重程度** | 中 |
| **位置** | `beichi_analyzer.py` 中 `fetch_kline_sina()` |
| **现象** | 新浪API偶尔返回空数据（概率约1-2%），导致单只股票分析失败 |
| **当前状态** | 超时保护已处理（30秒超时跳过），不影响整体扫描 |
| **验证方式** | 检查扫描日志中的 `失败` 计数，正常应 < 50/2234 |

### 7.4 Bug-13: 候选池 md5 对比可能漏提交

| 项目 | 内容 |
|---|---|
| **严重程度** | 低 |
| **位置** | `daily-trading-workflow.yml` 第126-136行 |
| **现象** | 如果候选池数据变化但Excel的二进制md5未变（概率极低），则不会提交 |
| **当前状态** | 极低概率，暂不处理 |
| **验证方式** | 检查GitHub Actions日志中 `md5对比` 的提示 |

---

## 8. 下一步在 Code 项目中的执行顺序

### 8.1 第一优先级：修复 Bug-10

```bash
# 文件：daily_workflow.py 第3408-3417行
# 修改：将 prefetch_candidate_pool() 移到 run_full_scan() 之前

# 修改前：
elif cmd == "scan":
    use_prefetch = "--prefetch" in sys.argv
    clean_excel()
    scan_data = run_full_scan()
    if use_prefetch:
        from concurrent_prefetch import prefetch_candidate_pool
        prefetch_candidate_pool(max_workers=20)

# 修改后：
elif cmd == "scan":
    use_prefetch = "--prefetch" in sys.argv
    if use_prefetch:
        from concurrent_prefetch import prefetch_candidate_pool
        prefetch_candidate_pool(max_workers=20)
    clean_excel()
    scan_data = run_full_scan()
```

### 8.2 第二优先级：验证全量扫描

```bash
# 本地测试（从项目根目录执行）
cd /workspace/TradingAgents-CN
PYTHONPATH=scripts/chanlun-workflow python scripts/chanlun-workflow/full_scan.py --prefetch

# 预期结果：
# - 沪A: ~976只
# - 深市: ~1258只
# - 预取完成: 命中xxx 获取xxx 耗时xx秒
# - 进度: 2234/2234 (确认N只 接近N只)
# - 分层: 核心N只 + 观察N只 + 边缘N只
```

### 8.3 第三优先级：验证持仓背驰确认

```bash
cd /workspace/TradingAgents-CN
PYTHONPATH=scripts/chanlun-workflow python scripts/chanlun-workflow/position_divergence.py

# 预期结果：
# - 读取当前持仓（贤丰控股、东风股份）
# - 输出每只股票的顶背驰状态
# - 汇总统计
```

### 8.4 第四优先级：验证交易计划生成

```bash
cd /workspace/TradingAgents-CN
PYTHONPATH=scripts/chanlun-workflow python scripts/chanlun-workflow/trading_plan.py

# 或通过工作流入口：
cd /workspace/TradingAgents-CN
PYTHONPATH=scripts/chanlun-workflow python scripts/chanlun-workflow/daily_workflow.py plan
```

### 8.5 第五优先级：检查 GitHub Actions 运行

在 GitHub 仓库中检查：
1. `Actions` → `Daily Trading Workflow` → 最新一次运行
2. 确认 `scan_then_compliance` 在 15:05 触发
3. 确认 `divergence` 在 16:00 触发
4. 确认 `plan` 在 16:30 触发
5. 确认 `tradingagents-telegram.yml` 的盘中推送正常

### 8.6 第六优先级：持仓数据更新

如果券商实际持仓有变化，更新 `trade-workbook.xlsx`：

```python
# 关键列索引：
# 持仓表：列2=名称, 列3=代码, 列7=股数, 列8=成本, 列9=现价
# 账户总表：列2=总资产, 列3=可用现金

from openpyxl import load_workbook
wb = load_workbook('scripts/chanlun-workflow/trade-workbook.xlsx')
# ... 修改数据 ...
wb.save('scripts/chanlun-workflow/trade-workbook.xlsx')
```

---

## 附录：关键函数速查表

| 函数 | 文件 | 行号 | 功能 |
|---|---|---|---|
| `main()` | `daily_workflow.py` | 3392 | 命令分发入口（10个命令） |
| `run_full_scan()` | `daily_workflow.py` | 1356 | 全市场扫描入口 |
| `get_today_holdings()` | `daily_workflow.py` | 299 | 读取持仓表（按代码去重） |
| `get_account_summary()` | `daily_workflow.py` | 485 | 读取账户摘要 |
| `check_compliance()` | `daily_workflow.py` | ~600 | 买入合规检查 |
| `send_telegram()` | `daily_workflow.py` | 2223 | Telegram消息推送 |
| `full_scan()` | `full_scan.py` | 238 | 全市场扫描主函数 |
| `scan_one()` | `full_scan.py` | 139 | 单只股票背驰扫描 |
| `fetch_sha_list()` | `full_scan.py` | 73 | 获取沪A股票列表 |
| `fetch_sza_prices()` | `full_scan.py` | 99 | 获取深市股票现价 |
| `analyze_beichi()` | `beichi_analyzer.py` | 1010 | 缠论背驰分析 |
| `detect_multilevel_buy_signals()` | `beichi_analyzer.py` | 2651 | 多级别买点信号检测 |
| `fetch_kline_sina()` | `beichi_analyzer.py` | ~100 | 新浪K线数据获取 |
| `_kline_cache_load()` | `beichi_analyzer.py` | 62 | 持久缓存读取 |
| `_kline_cache_save()` | `beichi_analyzer.py` | 76 | 持久缓存写入 |
| `position_divergence_report()` | `position_divergence.py` | 286 | 持仓背驰确认报告 |
| `_confirm_holding_divergence()` | `position_divergence.py` | 37 | 单只持仓顶背驰确认 |
| `generate_plan_from_workflow()` | `trading_plan.py` | 818 | 交易计划生成 |
| `calc_add_position()` | `trading_plan.py` | 30 | 加仓计算器 |
| `prefetch_stocks()` | `concurrent_prefetch.py` | 89 | 并发预取K线数据 |
| `prefetch_holdings()` | `concurrent_prefetch.py` | 175 | 预取持仓股数据 |
| `prefetch_candidate_pool()` | `concurrent_prefetch.py` | 192 | 预取全市场数据 |