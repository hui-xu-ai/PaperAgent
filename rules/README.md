# 规则库（rules/）— 迁移与汇总收集

## 用途
PDF 解析识别错误的可学习修复规则。**R09 起双源架构**：
- **内置规则（builtin）随引擎分发**：位于引擎 `skill/rules/builtin/`（asset_root() 定位，
  与 templates 同级；打包后进 `_MEIPASS/share/paper_reader_skill/rules` 只读）——
  训练收敛的规则经 `promote` 固化进这里，随引擎版本发布。
- **外部学习规则（learned/user）**：本目录（RULES_DIR 可配置；打包后 exe 同目录 `rules/`
  可写，用户导入的学习规则放这里）——学习部分，不随程序打包，可跨机迁移。

## 目录（外部）
```
rules/
├── learned/<category>.json   自动学习规则（AI 审查/规则挖掘，可禁用）
├── user/<category>.json      人工规则（用户纠正，覆盖优先）
├── manifest.json             版本与统计
└── README.md
```
category：latex | formula | variable | spacing | metadata | layout（R04 执行顺序）。

## CLI 用法（tools/rules_cli.py）
```powershell
# 统计（内嵌 builtin + 外部 learned/user）
.venv\Scripts\python.exe tools\rules_cli.py list

# 导出外部规则为 bundle（单文件，可拷到其他机器）
.venv\Scripts\python.exe tools\rules_cli.py export rules_bundle_20260822.json --level learned,user

# 导入 bundle（默认写 learned 级；冲突不覆盖，返回冲突清单）
.venv\Scripts\python.exe tools\rules_cli.py import rules_bundle_20260822.json --dir .\rules_other

# 合并多来源（冲突裁决 user>builtin>learned → confidence → version）
.venv\Scripts\python.exe tools\rules_cli.py merge a.json b.json -o merged.json

# 校验规则文件/bundle
.venv\Scripts\python.exe tools\rules_cli.py validate merged.json

# 固化 learned/user → 引擎内嵌 builtin（默认 confidence≥0.9）
.venv\Scripts\python.exe tools\rules_cli.py promote
```

## 训练结果固化到 skill（promote）→ 更新 skill 流程
1. 训练收敛（语料回归通过）后，外部 learned/user 已有确认规则：
   ```powershell
   .venv\Scripts\python.exe tools\rules_cli.py promote        # 默认固化 confidence≥0.9
   .venv\Scripts\python.exe tools\rules_cli.py promote --all  # 全部固化
   ```
2. 固化写入引擎 `skill/rules/builtin/`（merge 冲突裁决 user>builtin>learned）。
3. 更新引擎版本：`D:\Python\DeepSeek\Paper_AI_Reader\skill\pyproject.toml` version bump。
4. 同步打包快照：`powershell -File tools/sync_engine_assets.ps1`（清单已含 rules）。
5. 重新打包（PaperAgent.spec）——内置规则随程序分发。

## 后续更新流程（训练结果更新时）
训练出新规则 → `promote` → 引擎版本 bump → `sync_engine_assets` → 打包 →
发布新版引擎 + 程序（用户端只更新程序即可获得新规则；用户自学的规则在 exe 同目录
`rules/` 不受影响）。

## 跨机迁移 / 汇总收集
多台机器学习到的外部规则 → `export` bundle → 拷贝 → `import`（learned/user）→
`merge b1 b2 -o merged` → `import merged` → scorecard 回归 → `promote` 固化。

## 学习闭环
解析 → 规则引擎自动修复（内嵌 builtin + 外部 learned/user **双源加载**）→ 待确认 →
AI 审查（tools/ai_review_run.py 实测）→ 规则挖掘（learned 0.7）→
feedback/corrections.json → user 规则 → 回归 → promote 固化 → 发布。
