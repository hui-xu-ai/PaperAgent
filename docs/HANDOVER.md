# PaperAgent 交接文档（新会话指南）

> 目的：让新会话**不盲目读代码**——先读本文档定位文件与接口，再按需深入。
> 关联：`.dsh-memory/`（记忆系统：SHORT-TERM.md 状态 / INDEX.md 索引 / project/ 方案）。
> 2026-08-22 架构统一：**所有代码与环境均在 PaperAgent 沙盒内**，单一 git 仓库
> （外部 Paper_AI_Reader 引擎已冻结为存档，不再被引用；Web2MD 能力已并入算法包）。

## 1. 仓库与代码地图（单一仓库 PaperAgent）

| 模块 | 位置 | 角色 |
|---|---|---|
| **统一算法包 paperparse** | `packages/paperparse/`（pip install -e；import paperparse） | PDF 解析 + HTML 识别 + 自我训练，唯一算法来源 |
| **主 agent 应用** | `backend/`（FastAPI：`app/services/` 容器 DI、`app/api/`、`app/plugins/`、`engine_assets/`、`tests/`） | 文献管理系统，只经 `paperparse.api` 调用算法 |
| **前端** | `frontend/` | 静态页（index.html + app.js + style.css） |
| **运维/学习脚本** | `tools/`（scorecard/parse_offline/rules_cli/parse_batch 等，import paperparse） | 评分/离线解析/规则管理 |
| **学习数据** | `learning_workspace/`（独立 git：corpus/feedback/review/rules_learned/train_pdfs） | 语料/反馈/规则学习 |
| **规则外部根** | `rules/`（learned/user 可写；builtin 单一来源在包内） | 外部规则 |
| **HTML 遗留** | `Web2MD/` | 能力已并入 `paperparse/html/`，待退役 |
| **记忆** | `.dsh-memory/`（独立 git） | 会话记忆/方案/教训 |

## 2. 算法包 paperparse 结构

- 门面：`paperparse/api.py`（parse_pdf/process_pdf/translate/export/query/doc_summary 等）
- `paperparse/core/`（27 模块）：pdf_validate/mineru_client/pymupdf_fallback/layout/layout_skeleton/
  stitch_code/document_builder/rule_engine/rule_library/markdown_render/metadata/ai_review/
  **html_elements（HTML 元素化，H1+HT2.1 修复版）**
- `paperparse/html/`（M2 合并）：`parse_html`（元素化）+ `html_to_markdown/Converter`（HTML→MD，原 Web2MD）
- `paperparse/middleware/`（7）：orchestrator/stages/schema/errors/audit/batch_runner
- `paperparse/llm/`（12）：client/prompts/summarize/calibrate_translate/web_roundtrip/cost 等
- 资源：包内 `rules`（builtin 单一来源）/`templates`/`prompts`/`tools`/`memory`/`SKILL*`
  （`paperparse.config.asset_root()` → `packages/paperparse`）

## 3. 解析管线（S0→S7）

入口 `paperparse.api` → `middleware/orchestrator` → stages（S0 validate → S1 parse（mineru-v4/pymupdf）
→ S1.5 skeleton → S2 layout → S3 stitch → S3.5 calibrate → S4 figures → S5 metadata →
S6 build（rule_engine）→ S7 render → S7.5 AI review）。断点续跑：intermediate/*.json。

## 4. 规则系统

- 三级：builtin（包内 rules/，8 条）/ learned / user（rules/ 外部）。
- 引擎：`paperparse.core.rule_engine`（6 类执行器+置信度门控）；固化 `tools/rules_cli.py promote`。

## 5. 学习系统

- `learning_workspace/corpus/`（pdfs/raw/gold/baseline/reports）；scorecard 6 类别。
- HTML 整合（M6 方向）：`paperparse/html` 元素化 → `corpus/html/<doi>/` 训练标注 → 比对学习。

## 6. 关键配置（.env）

- `MINERU_API_KEY/MINERU_BASE_URL/MINERU_MODEL_VERSION`、`DEEPSEEK_API_KEY/DEEPSEEK_BASE_URL/DEEPSEEK_MODEL`
- `RULES_DIR`（默认 `rules/`）、`ENGINE_WORK_ROOT`（默认 output 等）

## 7. 常用命令

```powershell
# 测试
.venv\Scripts\python.exe -m pytest packages\paperparse\tests      # 291 用例
.venv\Scripts\python.exe -m pytest backend\tests                  # 62 用例（勿与上面同进程混跑曾污染，已修）
# 离线解析 / 评分 / 规则
.venv\Scripts\python.exe tools\parse_offline.py <pdf> --full-md <full.md> --content-list <c.json> --out-dir ...
.venv\Scripts\python.exe tools\scorecard.py --candidate <paper.md> [--gold ...]
.venv\Scripts\python.exe tools\rules_cli.py list --dir packages\paperparse\rules
# 打包
.venv\Scripts\pyinstaller.exe PaperAgent.spec --noconfirm
```

## 8. 记忆索引

- `SHORT-TERM.md`（当前状态/下一步）、`INDEX.md`（条目定位）、`project/PROJECT.md`（任务清单）、
  `project/ARCHITECTURE.md`（含统一架构重构 M0-M6）、`LONG-TERM/LESSONS.md`（L-001..L-004）。

## 9. 当前状态与遗留

- 统一架构：M1-M4 完成（paperparse 迁入/HTML 模块合并/门面适配去 shim/rules 单一来源）；M5 文档更新中；
  **M6 HTML 整合待做**（SD 双通道适配器 → `paperparse/html/sd.py`、PyMuPDF 补图、双输出到 `corpus/html/<doi>/`）。
- 遗留：Web2MD 退役；engine_assets 快照与包资源重复（打包走包资源后快照可撤）；打包实测（spec 已更新待验证）。
