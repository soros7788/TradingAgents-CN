# 缠论交易系统 — 备份与恢复指南

> **用途**: 当TRAE/Codex订阅到期或需要更换账号时, 通过GitHub仓库完整恢复所有功能
> **仓库**: `soros7788/TradingAgents-CN`
> **最后更新**: 2026-07-26

---

## 一、系统架构概览

### 1.1 核心文件清单

```
TradingAgents-CN/
├── scripts/chanlun-workflow/
│   ├── beichi_analyzer.py                          # 核心背驰分析器 (BUG-1~5修复版)
│   ├── daily_workflow.py                           # 每日交易工作流主入口
│   ├── full_scan.py                                # 全市场候选扫描模块
│   ├── recalc.py                                   # Excel公式重算
│   ├── dl_model.pkl                                # 深度学习模型 (MLP 128→64→32→16)
│   ├── dl_scaler.pkl                               # DL模型StandardScaler
│   ├── 动态仓位资金管理法则_执行版.xlsx              # 交易执行Excel (候选池+持仓+账户)
│   ├── 周一计划_2026-07-27.md                       # 周一交易计划
│   ├── WORKFLOW.md                                 # 工作流文档
│   ├── beichi_analyzer.py.bak.20260726             # BUG修复前备份(5个版本)
│   ├── beichi_analyzer.py.bak.20260726.bug2
│   ├── beichi_analyzer.py.bak.20260726.bug3
│   ├── beichi_analyzer.py.bak.20260726.bug4
│   └── beichi_analyzer.py.bak.20260726.bug5
├── .github/workflows/
│   └── daily-trading-workflow.yml                  # GitHub Actions自动扫描
└── .gitignore                                      # 已配置: pkl/bak/md文件纳入版本控制
```

### 1.2 功能模块

| 模块 | 文件 | 功能 |
|------|------|------|
| 背驰分析 | `beichi_analyzer.py` | 缠论中枢检测+MACD背驰+DL模型预测+BUG-1~5修复 |
| 每日工作流 | `daily_workflow.py` | scan/intraday/compliance/account/holdings |
| 全市场扫描 | `full_scan.py` | 沪A+深市全量扫描, 分层候选池 |
| Excel公式 | `recalc.py` | LibreOffice headless重算Excel公式 |
| DL模型 | `dl_model.pkl` + `dl_scaler.pkl` | MLP分类器, AUC=0.72, 16维特征 |
| GitHub Actions | `daily-trading-workflow.yml` | 定时扫描+自动commit Excel |

### 1.3 BUG修复历史

| BUG | 修复内容 | 日期 |
|-----|---------|------|
| BUG-1 | 趋势背驰vs盘整背驰校正 (has_downtrend检查) | 2026-07-26 |
| BUG-2 | pre段定义: 固定根数→前中枢结束点 | 2026-07-26 |
| BUG-3 | 价格新低/新高检查 (背驰必要条件) | 2026-07-26 |
| BUG-4 | 二买条件: 中枢区间→一买低点 | 2026-07-26 |
| BUG-5 | 移除overall_dir=="up"前提 | 2026-07-26 |

---

## 二、备份方案

### 2.1 GitHub仓库备份 (自动)

所有代码和Excel文件已通过Git push到 `soros7788/TradingAgents-CN` 仓库。

**GitHub Actions自动备份**:
- 盘中扫描: 交易日10:00/11:00/13:00/14:00 (北京时间)
- 日线扫描: 交易日15:05
- 合规核查: 交易日15:30
- 每次扫描后自动commit Excel变更到仓库

### 2.2 手动备份命令

```bash
# 进入项目目录
cd TradingAgents-CN

# 添加所有变更
git add -A

# 提交
git commit -m "backup: 缠论工作流完整备份 $(date '+%Y-%m-%d %H:%M')"

# 推送到GitHub
git push origin main
```

### 2.3 GitHub Actions Secrets (需手动设置)

在GitHub仓库 Settings → Secrets and variables → Actions 中设置:

| Secret名 | 用途 | 获取方式 |
|----------|------|---------|
| `TELEGRAM_BOT_TOKEN` | Telegram推送通知 | @BotFather创建Bot获取 |
| `TELEGRAM_CHAT_ID` | Telegram聊天ID | @userinfobot获取 |

**无Telegram可不设**, 工作流会跳过推送, 不影响扫描功能。

---

## 三、换号恢复指南

### 3.1 前提条件

- 新账号有GitHub访问权限
- 新环境已安装: Python 3.10+, Git
- (可选) 新环境有TRAE或Codex订阅

### 3.2 恢复步骤

#### 步骤1: 克隆仓库

```bash
# 方式A: HTTPS (需输入GitHub用户名和密码/Token)
git clone https://github.com/soros7788/TradingAgents-CN.git

# 方式B: SSH (需配置SSH Key)
git clone git@github.com:soros7788/TradingAgents-CN.git

# 方式C: 使用Personal Access Token
git clone https://<你的Token>@github.com/soros7788/TradingAgents-CN.git
```

#### 步骤2: 安装依赖

```bash
cd TradingAgents-CN

# 安装Python依赖
pip install openpyxl numpy pandas scikit-learn==1.7.2 urllib3

# (可选) 安装LibreOffice用于Excel公式重算
# Ubuntu/Debian:
sudo apt-get install -y libreoffice-calc
# macOS:
brew install --cask libreoffice
```

#### 步骤3: 验证核心文件

```bash
# 检查关键文件是否存在
ls -la scripts/chanlun-workflow/
# 必须存在:
#   beichi_analyzer.py
#   daily_workflow.py
#   full_scan.py
#   recalc.py
#   dl_model.pkl
#   dl_scaler.pkl
#   动态仓位资金管理法则_执行版.xlsx

# 验证DL模型可加载
python3 -c "
import sys, pickle
sys.path.insert(0, 'scripts/chanlun-workflow')
from beichi_analyzer import _load_dl_model
ok = _load_dl_model()
print(f'DL模型加载: {\"成功\" if ok else \"失败\"}')
"
```

#### 步骤4: 测试运行

```bash
cd scripts/chanlun-workflow

# 测试1: 单只股票分析
python3 -c "
import sys
sys.path.insert(0, '.')
from beichi_analyzer import analyze_beichi
r = analyze_beichi('601012', level='日线')
if 'error' in r:
    print('错误:', r['error'])
else:
    print(f'隆基绿能: 中枢{len(r[\"zss\"])}个, 信号{len(r[\"signals\"])}个')
    for s in r['signals'][:3]:
        print(f'  {s[\"op\"]} type={s[\"type\"]} DL_P={s[\"dl_prob\"]:.2f} ratio={s[\"ratio\"]:.1f}%')
"

# 测试2: 合规核查
python3 daily_workflow.py compliance

# 测试3: 全市场扫描 (约2分钟)
python3 daily_workflow.py scan
```

#### 步骤5: 配置GitHub Actions (可选)

如果要在新账号下继续使用GitHub Actions自动扫描:

```bash
# 1. 在新账号下Fork或创建仓库
# 2. 设置Secrets (如果需要Telegram通知):
#    仓库 → Settings → Secrets → Actions → New secret
#    - TELEGRAM_BOT_TOKEN
#    - TELEGRAM_CHAT_ID
# 3. 启用Actions:
#    仓库 → Actions → 确认工作流已启用
```

#### 步骤6: 配置TRAE/Codex沙箱映射 (如使用TRAE)

在TRAE中打开新仓库目录,确保:
- 工作目录指向 `TradingAgents-CN/scripts/chanlun-workflow/`
- Python环境包含: openpyxl, numpy, pandas, scikit-learn

### 3.3 恢复验证清单

| 检查项 | 命令 | 预期结果 |
|--------|------|---------|
| 代码完整 | `ls scripts/chanlun-workflow/*.py` | 4个.py文件 |
| 模型文件 | `ls scripts/chanlun-workflow/*.pkl` | 2个.pkl文件 |
| Excel文件 | `ls scripts/chanlun-workflow/*.xlsx` | 1个.xlsx文件 |
| DL模型加载 | `python3 -c "from beichi_analyzer import _load_dl_model; _load_dl_model()"` | "DL模型加载成功" |
| 单股分析 | `python3 -c "from beichi_analyzer import analyze_beichi; analyze_beichi('601012')"` | 返回中枢和信号 |
| 全市场扫描 | `python3 daily_workflow.py scan` | 扫描2000+只, 写入候选池 |
| GitHub Actions | 仓库→Actions页面 | 工作流可见且可手动触发 |

---

## 四、数据安全注意事项

### 4.1 敏感数据

| 数据类型 | 位置 | 是否备份 | 说明 |
|---------|------|---------|------|
| 交易记录 | Excel持仓表 | ✅ 已备份 | 含成本价/止损价 |
| 账户资金 | Excel账户总表 | ✅ 已备份 | 含总资产/可用资金 |
| API密钥 | .env文件 | ❌ 不备份 | .gitignore排除, 需手动恢复 |
| Telegram Token | GitHub Secrets | ❌ 不在代码中 | 需在新仓库重新设置 |

### 4.2 模型文件说明

`dl_model.pkl` 和 `dl_scaler.pkl` 是训练于11373样本的MLP模型:
- 训练数据无法从代码重建
- 必须通过Git备份恢复
- scikit-learn版本必须兼容 (推荐1.7.2)

### 4.3 Excel公式依赖

Excel中的候选池分层使用公式自动计算, 依赖:
- `recalc.py` 调用LibreOffice headless重算
- GitHub Actions环境已预装LibreOffice
- 本地运行需手动安装

---

## 五、日常维护

### 5.1 定期备份

```bash
# 每次修改代码后
cd TradingAgents-CN
git add -A
git commit -m "update: 描述变更内容"
git push
```

### 5.2 GitHub Actions日志

- 仓库 → Actions → 选择工作流 → 查看运行日志
- 每次扫描的输出保存在Actions日志中(90天)
- Excel变更自动commit到main分支

### 5.3 版本回滚

如需回滚到某个BUG修复前的版本:

```bash
# 查看提交历史
git log --oneline

# 回滚到指定版本 (保留备份)
git checkout <commit-hash> -- scripts/chanlun-workflow/beichi_analyzer.py
```

或使用备份文件:
```bash
# 回滚到BUG-3修复前
cp scripts/chanlun-workflow/beichi_analyzer.py.bak.20260726.bug2 \
   scripts/chanlun-workflow/beichi_analyzer.py
```

---

## 六、紧急联系

- 仓库: https://github.com/soros7788/TradingAgents-CN
- 分支: main
- GitHub Actions: .github/workflows/daily-trading-workflow.yml
- 核心代码: scripts/chanlun-workflow/beichi_analyzer.py
- 最后备份: 2026-07-26 (BUG-1~5全修复)
