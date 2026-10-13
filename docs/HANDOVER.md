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
| **前端** | `frontend/` | 静态页（index.html + 10 个 JS 模块 + style.css；2026-09-19 从单文件 app.js 拆分为模块化结构） |
| **运维/学习脚本** | `tools/`（scorecard/parse_offline/rules_cli/parse_batch 等，import paperparse） | 评分/离线解析/规则管理 |
| **学习数据** | `learning_workspace/`（独立 git：corpus/feedback/review/rules_learned/train_pdfs） | 语料/反馈/规则学习 |
| **规则外部根** | `rules/`（learned/user 可写；builtin 单一来源在包内） | 外部规则 |
| **HTML 遗留** | `Web2MD/` | 能力已并入 `paperparse/html/`，待退役 |
| **记忆** | `.dsh-memory/`（独立 git） | 会话记忆/方案/教训 |

### 1.1 前端模块结构（2026-09-19 拆分）

`frontend/` 为单页应用，所有 JS 通过 `<script>` 标签加载（非 ES module），跨文件调用均为运行时函数调用。
加载顺序在 `index.html` 底部定义：

| 文件 | 行数 | 职责 |
|---|---|---|
| `js/utils.js` | ~100 | api() fetch 封装、DOM 辅助（$/$$）、格式化（日期/文件大小） |
| `js/ui.js` | ~300 | 模态框、菜单、拖拽分隔条、面板停靠/弹出、确认/输入对话框 |
| `js/sessions.js` | ~400 | 会话列表渲染、新建/归档/批量删除、日期过滤 |
| `js/papers.js` | ~760 | 文献库列表/搜索/类型芯片/日期分组/导入、状态常量（STATUS_MAP/KIND_META 等） |
| `js/diary.js` | ~170 | 阅读日记：日历视图、日志视图、心得笔记 |
| `js/settings.js` | ~1100 | 设置中心 6 tab（模型/解析/知识库/界面/插件/关于）、翻译模型、外观/字体、lightbox |
| `js/reader.js` | ~1100 | 阅读器：附件管理、知识库面板/标签、Markdown 渲染、PDF 预览、frontmatter 属性 |
| `js/kb.js` | ~880 | 知识库管理：列表/目录树、编译进度、磁盘同步、回收站、元数据管理 |
| `js/chat.js` | ~540 | 对话：消息渲染/发送、等待反馈、回收站、输入上下文 |
| `js/lit-admin.js` | ~200 | AI 文献检索管理界面（6 tab） |
| `js/graph/` | 13 文件 | 文献计量图谱：Three.js 3D + 2D 可视化（dataModel/layoutEngine/rendererThree/panel 等） |
| `app.js` | ~614 | 启动入口 boot()、复核门控、token 统计、事件面板、图片 lightbox 绑定 |

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

## 10. 知识库编译系统（paperkb，2026-09-20 架构）

### 10.1 两级编译链路
- **L1 `_note.md`**：精简知识卡（one_liner + 六维总结 + concepts + tags），~500 tokens
- **L2 `_wiki.md`**：深度分析（方法论批判 + 可复现性 + 应用转化），~1300 tokens
- **L3 `_relations.md`**：概念关系层（概念关系图 + 方法论连接 + 研究趋势），自动触发
- **L1+L2 合并编译**：一次 API 调用同时产出 L1+L2，节省 50% 全文输入 token

### 10.2 自动升级机制
- **L1→AI 评分**：编译时 LLM 顺手输出 `ai_value`(0-5) 和 `topic_score`(0-1)，零额外成本
- **AI 评分→L2**：value_score ≥ 2.5 自动入队 L2（7 维公式：IF/cited/ai_value/topic/year/paper_rank/lib_cited）
- **L2→L3**：L2 完成后，value_score ≥ 4.0 且 ai_value > 0 → `_maybe_auto_l3()` 自动入队
- **关键顺序**：AI 评分必须在 `_maybe_auto_l3` 之前保存，否则价值分计算缺少 ai_value 维度

### 10.3 向量索引与检索
- **KbVectorIndex**（`paperkb/vector.py`）：索引编译结果（_note.md + _wiki.md + concepts）
- **SiliconFlow bge-m3 embedding**（1024 维），复用 paperlit 的 API key
- **4 通道混合检索**：概念倒排 + 向量相似度 + 引用关系 + 共被引聚类
- **配置**：`KbSettings(vector_impl="kb")` 启用向量索引（默认 "noop" 不启用）

### 10.4 筛选状态（kb_list API）
- `done`：已编译（L1/L2/L3 任一完成）
- `none`：未编译
- `failed`：编译失败（有 error 记录）
- `l1`/`l2`/`l3`：该等级已编译
- `l2`（特殊含义）：L2 已编译但 L3 未编译，且 value_score ≥ 4.0（可升级 L3）

### 10.5 关键文件
- `packages/paperkb/paperkb/compile.py`：编译器核心（L1/L2/L3 编译逻辑、自动升级、向量索引）
- `packages/paperkb/paperkb/api.py`：知识库 API 门面（kb_list/kb_vector_search/compile 等）
- `packages/paperkb/paperkb/score.py`：7 维价值评分公式
- `packages/paperkb/paperkb/vector.py`：向量索引实现
- `backend/app/services/kbmeta_service.py`：paperkb 初始化（注入 Roots + KbSettings + LLM 适配）
- `backend/app/api/kbmeta.py`：知识库 HTTP API 路由
