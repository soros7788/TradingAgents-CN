# 缠论量化交易系统 (Chanlun Workflow)

基于缠论背驰分析的 A 股量化交易系统，集成机器学习信号预测、动态仓位管理、合规审计和自动化 CI/CD。

## 系统架构

```
┌─────────────┐     ┌──────────────────┐     ┌──────────────┐
│  full_scan  │────▶│ beichi_analyzer  │────▶│ daily_workflow│
│  全市场扫描  │     │  背驰分析+ML预测  │     │  交易工作流   │
└─────────────┘     └──────────────────┘     └──────┬───────┘
                                                     │
                    ┌──────────────────┐            │
                    │     recalc       │◀───────────┘
                    │  Excel公式重算   │
                    └──────────────────┘
```

### 核心模块

| 文件 | 行数 | 功能 |
|------|------|------|
| `beichi_analyzer.py` | 1882 | 中枢检测、背驰分析、ML信号预测 (DL模型 + EP模型) |
| `daily_workflow.py` | 2953 | 主工作流：扫描、盘中信号、合规审计、仓位管理 |
| `full_scan.py` | 251 | 全市场扫描（沪A + 深市） |
| `recalc.py` | 234 | LibreOffice headless 公式重算 |
| `test_ep_entry.py` | 42 | EP模型集成测试 |

### ML 模型

| 模型 | 用途 | 算法 | AUC |
|------|------|------|-----|
| DL模型 | 背驰方向预测 | MLPClassifier | 0.72 |
| EP模型 | 二买反转概率 | MLPClassifier (V2) | 0.68 |

## 安装

```bash
pip install -r requirements.txt
# 还需要 LibreOffice (用于 Excel 公式重算)
sudo apt-get install libreoffice
```

## 使用

```bash
# 全市场扫描（更新候选池）
python daily_workflow.py --mode scan

# 盘中信号扫描
python daily_workflow.py --mode intraday

# 合规审计
python daily_workflow.py --mode compliance

# 完整工作流（扫描 + 信号 + 合规 + 计划）
python daily_workflow.py --mode full
```

## Excel 工作簿结构

| 工作表 | 用途 |
|--------|------|
| 账户总表 | 每日资产记录、收益率计算 |
| 持仓表 | 持仓明细、盈亏跟踪 |
| 候选池 | 市场扫描结果（核心/观察/边缘三层） |
| 交易记录 | 买卖记录 |
| 心态日志 | 每日心态记录 |
| 执行清单 | P0/P1/P2 级操作指令 |
| 周复盘 | 周度复盘汇总 |
| 候选池历史 | 候选池变更历史（防回溯合规假阳性） |

## 风控机制

- **一买低点风控**: 接近一买低点时评分惩罚（<3% 评分×0.4，已破位 评分=0）
- **P0 强制清仓**: 破一买低点无条件清仓，有升级告警机制
- **动态仓位上限**: 根据信号强度动态调整单股仓位上限
- **合规审计**: 买入前检查候选池、卖出前检查计划、止损纪律检查
- **回溯合规检测**: 对比候选池历史，防止事后补录导致的假阳性

## CI/CD

GitHub Actions 自动化：
- 每日 9:25 / 11:30 / 14:55 定时扫描
- 自动 commit 候选池和信号更新
- Telegram 推送交易计划
- Artifact 下载 Excel 工作簿

## BUG 修复记录

系统经过 75+ 处 BUG 修复，涵盖：
- 中枢检测噪音过滤（min_amp_pct 优化）
- 二买信号确认逻辑
- 一买低点风控机制
- 回溯合规假阳性
- P0 清仓升级机制
- Excel 公式联动修复

详见代码中 `BUG修复` 注释标记。

## 文档

- [WORKFLOW.md](WORKFLOW.md) - 工作流详细说明
- [BACKUP_RECOVERY_GUIDE.md](BACKUP_RECOVERY_GUIDE.md) - 系统架构与备份恢复指南
