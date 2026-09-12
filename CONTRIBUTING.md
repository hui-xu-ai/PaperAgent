# 贡献指南（Contributing）

感谢愿意参与 PaperAgent！本文件写清"怎么改才不会被退回"。

## 提交前必读

1. **先开 Issue 对齐**：新功能/大改先描述场景与验收标准（"手怎么动、看到什么"），
   避免辛苦写完发现方向不一致。小 bug 可直接 PR。
2. **不自带数据/密钥**：`data/`、`knowledge_base/`、`library/`、`work/`、`release/`、`dist/`、`.env`
   均已 git 忽略；**PR 里出现用户文献、API Key、本机绝对路径会被直接关闭**。
3. **不过度提交**：不允许 `git add -A` / `git add .`；请显式列出改动的文件。

## 开发环境

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pip install -e packages\paperparse
.\.venv\Scripts\python.exe -m pip install -e packages\paperkb
Copy-Item .env.example .env    # 填 DEEPSEEK_API_KEY / MINERU_API_KEY
```

## 提交门槛（缺一不可）

```powershell
& '.venv\Scripts\python.exe' -m pytest -q                       # 三包全量单测，必须 0 failed
& '.venv\Scripts\python.exe' tools\release_check.py --full      # 发布闸门（版本/数据兼容/文档）
```

- 改前端：刷新页面即可看到效果；改后端：**必须重启进程**（无热重载）。
- 改目录布局/数据格式/打包配置：同一提交内同步 `docs/`、`AGENTS.md` 与 `paperparse`/`paperkb` 的版本号。
- 数据与版本契约见 `docs/DATA-LAYOUT.md`、`docs/VERSIONING.md`（改动前先读，守卫测试会拦）。

## PR 描述模板

- **现象 / 需求**：一句话
- **根因**（修 bug 必填）：`文件:行号` + 证据（复现步骤或实测输出）
- **改动面**：列出文件
- **验证档位**：单测 / 真实链路实测 / 用户验收，三档如实声明（"单测过" ≠ "真机验证过"）

## 分支与提交信息

- 分支：`fix/<主题>` / `feat/<主题>` / `docs/<主题>`
- 提交信息三段式：主题行 + 验证方式 + 影响面（例：`fix(review): 复核页 PDF 页图定位不再依赖暂存路径`）

## 许可

提交即表示你同意以 **MIT License**（见 `LICENSE`）授权你的贡献。
