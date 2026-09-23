# AGENTS.md

本项目为 **PaperAgent**：文献 AI 阅读/翻译/知识库管理应用（前端静态页 + FastAPI 后端 + 统一算法包 paperparse）。**所有代码与环境均在 PaperAgent 沙盒内**（2026-08 架构统一，外部引擎已冻结为存档）。

## 工作协议

- 遵循 `pydev-protocol` 总纲：先方案后执行、子任务化、小步 git 提交、及时汇报、bug/工具重试纪律。
- **数据与升级规范（2026-09-12 起，硬约束）**：数据放哪看 `docs/DATA-LAYOUT.md`（`data/` 五库隔离 ·
  `knowledge_base/` 只放知识资产 · `work/` 随时可清）；升级/迁移/兼容看 `docs/VERSIONING.md`
  （版本单一来源 `backend/app/version.py`、`data/manifest.json` 版本戳、**兼容代码只住迁移层**、
  发布前跑 `tools/check_data_compat.py`）；兼容分支必须登记 `docs/COMPAT-REGISTER.md`。
  守卫由 `backend/tests/test_version_contract.py` 强制（ALTER/裸 sqlite 连接越界即失败）。
  **核心数据文件名单一来源**：`document.json` 只在 `paperkb.layout` 定义（`LIB_DOC_NAME`/`KB_DOC_NAME`），
  产品代码禁裸写、"取哪一份"只走 `shared_doc_json`/`translation_target`——守卫 `backend/tests/test_doc_name_source.py`（见 `docs/DATA-LAYOUT.md` §1.1）。
- 记忆系统：`.dsh-memory/`（独立 git，不随代码提交）。**会话开始只读 `HANDOFF.md`（≤80 行，唯一必读）**；
  `INDEX.md` / `SHORT-TERM.md` / `PROJECT.md` 等**按需 grep 定位后再读片段**——禁止开局整读（流水文档已数百行，
  旧口径"读 HANDOFF + INDEX + PROJECT"会让模型两边都读，开局白付数千 token）。检查用 `tools/memory-gate.ps1`。
- 子任务执行：`subtask-runner`（先写 NOTES → 实现 → 测试 → git 提交）。
- 技能匹配时加载：`paper-reader`（PDF→双语 Markdown）、`debug-discipline`、`progress-report`、`skill-authoring`、`git-protocol`、`memory-system`。
- 批量文本替换一律用 Python 脚本或逐文件 edit，**禁用 PowerShell 嵌套数组 foreach 解包**（L-004）。
- **提交纪律（硬）**：**禁止 `git add -A` / `git add .`**——本工作区可能存在**并行会话**（实测发生过），会把对方未完成
  的改动一起入库。一律**显式列路径**（`git add <file> ...`），提交前先 `git status --short` 核对白名单；发现非自己改动的
  文件出现在 `git status` 里 → 先停下来确认归属，不要覆盖、不要顺手提交。

## 当前目标（见 .dsh-memory/project/PROJECT.md）

统一架构重构 M0-M6（**2026-08-26 调整**：HTML 整合（M6）与学习工作区已**剥离归档** `archive/html-20260826/`、`archive/learning_workspace-20260826/`，未来扩展记录在 `.dsh-memory/project/ARCH-REVIEW-20260826.md`；当前主线=P14 解析质量 + library 规范化 + 知识库重建（待用户启动））。

## 关键目录（真实布局，2026-08-26 核实）

- `packages/paperparse/` ★ 统一算法包（pip install -e；import paperparse）
  - `paperparse/api.py` 唯一门面（convert_pdf/process_pdf_v2/...）；`paperparse/core/`（PDF 解析 **35 模块**：P14 生产链 ~20 + 老 parse 降级链（stages/stitch/calibration/rule_engine 等）+ 挖掘 rule_mining）、`paperparse/llm/`、`paperparse/middleware/`
  - `assets`：rules（builtin 单一来源）/templates/prompts/tools/memory/SKILL*
  - **已退役归档**（archive/paperparse-core-20260826/）：dual_pipeline/dual_fuse/dual_align/self_learn/sf_ocr_client（P12 双通道与规则学习）；backend 解析仅走 process_pdf_v2（P14），PAPERPARSE_PIPELINE=p12 已冻结回落 p14
- `backend/` FastAPI 应用：`app/services/`（container 依赖注入：engine/task/kb/chat/llm/settings/usage/event_bus/plugin_registry）、`app/api/`（10 路由统一 /api/*）、`app/plugins/`、`app/engine_assets/`（打包快照）、`tests/`
- `frontend/` 静态界面（单页，前后端经 fetch /api/* 分离；2026-09-19 拆分为模块化多文件）
  - `index.html` + `style.css` + `css/graph.css`（界面骨架与样式）
  - `app.js`（~614 行：boot 启动、复核门控、token 统计、事件面板、图片 lightbox）
  - `js/utils.js`（通用工具：api fetch 封装、DOM 辅助、格式化）
  - `js/ui.js`（UI 组件：模态/菜单/拖拽/面板停靠弹出）
  - `js/sessions.js`（会话管理：列表/新建/归档/批量操作）
  - `js/papers.js`（文献库：列表渲染/搜索/类型芯片/日期分组/导入）
  - `js/diary.js`（阅读日记：日历/日志/心得笔记）
  - `js/settings.js`（设置中心：模型/解析/知识库/界面/插件/关于 + 翻译模型/外观）
  - `js/reader.js`（阅读器：附件/知识库面板/标签管理/Markdown 渲染/PDF 预览/frontmatter）
  - `js/kb.js`（知识库管理：列表/目录树/编译进度/磁盘同步/回收站/元数据管理）
  - `js/chat.js`（对话：消息/发送/等待反馈/回收站/输入上下文）
  - `js/lit-admin.js`（AI 文献检索管理界面）
  - `js/graph/`（文献计量图谱：Three.js 3D + 2D 可视化）
  - 加载顺序在 `index.html` 底部 `<script>` 标签定义，跨文件调用均为运行时引用
- `tools/` 运维/脚本（scorecard/parse_offline/rules_cli/**parse_regression**（P14 产物指纹回归闸门：`python tools/parse_regression.py --check`，0 API 缓存件复算，已接入 `build_release.ps1` [5/5] 与 `docs/RELEASE-CHECKLIST.md`；用例+基线在 `tools/parse_regression/`）等，import paperparse）
- `archive/` 剥离归档（html-20260826/ learning_workspace-20260826/ paperparse-core-20260826/——用户可单独拷贝备份）
- `rules/` 外部规则根（learned/user 可写；builtin 在包内；挖掘规则已归档 archive，规则引擎不应用）
- `library/`（**PDF 解析库**，每篇文件夹 `library/<RID>/` 只留解析产物：mineru_full.md / en.md（干净版，供 AI 翻译/总结/提问源）/ document.json / images / work / qa_report.json；**不含 source.pdf（已移 kb）**、不含带 DOI 前缀的变体文件与 variants/；**依附资料挂父目录** `library/<RID>/attachments/{si,review,data}/`，无父资源的零散资料放项目根 `attachments/<RID>/`）、`knowledge_base/`（知识库：每篇 `kb/<DOI>/` = en.md / document.json / images / **source.pdf** / **zh.md / en_zh.md / summary.md**（翻译/总结变体）+ 知识库笔记 _note/_details/_wiki + _index.md 索引）、`input/`（导入暂存，备份 PDF 已迁 kb）、`data/`、`用户提供的文献/`

## 资源身份与检索（P0-B step3，2026-09-12）

- **单一物理根 + 类型进名字**：资源类型由 RID 前缀表达（`book__`/`thesis__`/`std__`/`patent__`/`chapter__`/`nd-<指纹>`；论文无前缀），
  并记在 `papers_meta.kind`。`paperkb.attachments.library_candidates` 把 **RID / 裸 DOI / 目录名** 三种键写法归一到同一目录。
- **依附资料（SI/审稿意见/数据）不是独立文献**：挂父资源目录、不进知识库列表；导入走**轻量管线**（纯本地抽文本，**0 API 成本**），
  文本进 `notes_fts`（key=父 RID，filename=`attachments/<kind>/<名>`）。**清理任何目录前必须过 `layout.assert_safe_to_clear`（用户附件不可删）。**
- **文献库 agent 工具**：`kb_search_papers`（含 `kind`/`has_attachment`）、`kb_attachments(rid)`、`kb_attachment_text(rid, path)`
  （白名单路径校验，越权一律 404）；系统提示含「存储与检索约定」**稳定前缀**段（改它 = 提示词缓存失效一次）。
- **前端**：文献库/知识库顶部**类型芯片**（计数取 `/api/kb-meta/kind/options`）、卡片类型徽标 + `📎 附件 N` 入口、
  搜索语法糖（`书 电化学` / `kind:thesis 磁性`）、导入弹窗第 4 tab「📎 支撑信息·审稿意见」。
