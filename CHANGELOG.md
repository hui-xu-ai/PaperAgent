# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [1.3.0] - 2026-09-21

> **升级影响：不会丢数据，无需重新解析。**
> `DATA_FORMAT` 保持 3（本版无迁移）。唯一需要动手的是**向量索引换过一次存储格式**
> ——见下方「升级要做的事」。

### Added

- **检索质量重构：RRF 融合 + 交叉编码器精排**
  - 六路召回（notes FTS / 向量 / meta FTS / 引用邻域 / 卡片 / 附件全文）改用
    RRF 融合（`Σ w/(k+rank)`，k=60），废弃早期硬编码分数相加（余弦/BM25/命中长度本就不可比）
  - 新增 `bge-reranker-v2-m3` 二阶段精排（库层默认关，应用层显式开；失败/无 key 静默降级）
  - **small-to-big 注入**：向量按 900 字块匹配，注入时扩到所属小节整段（≤1200 字）
  - **产物整份优先**：`_note.md` / `_wiki.md` / `_relations.md` 按整份注入（前 3 份，
    ≤3000 字/份），修「片段在 570 字处硬切、把数值和条件全切掉」导致的「数值被截断」
  - 新增 `paperkb/textseg.py`：边界对齐截断 / 标题感知分块 / 命中窗口四个原语
  - 新增查询向量 SQLite 缓存（`data/vector/query_cache.db`，LRU 5000）
- **知识库扩容 P0（10 万 / 50 万篇级）**
  - 向量存储从「整份 JSON + 整份 npy 重写」改为**段式追加写 + manifest + 原子压实**：
    `data/vector/kb_index.db`（元数据）+ `kb_vectors/seg-*.f32`（裸 float32，只追加）
  - 索引改为块级 + 内容 md5 增量刷新；删除/原地刷新后置脏、查询前整表重建
  - 进程级单例（索引 / 期刊库缓存）消除每次请求重载
  - 命名索引 `keys_for(doi, ptype)` 替代内存全表扫描（消除真实的 O(N²) 写放大）
  - 新增**索引健康四接口**：`GET /api/kb-meta/index/status`、`POST .../index/scan`、
    `POST .../index/compact`、`POST .../index/retry`（死信重试）
  - 新增规模仿真脚本 `tools/index_scale_sim.py`
- **agent 工具 `kb_paper_products`**：模型可主动取整份编译产物（此前 agent 只能拿到片段）
- **AI 写作 Agent** 与 **文献计量图谱** 增强（聚类分析 / 体积斥力防重叠 / 节点大小缩放 /
  被引来源选择与孤立节点过滤）
- **MinerU 模型版本选择器**（VLM / Pipeline / MinerU-HTML）+ 4 个 API 路径可配置
- 文档：`docs/KNOWLEDGE-BASE.md` 重写并新增 Mermaid 核心架构图（全局分层 / 检索链路 /
  编译升级链 / 向量存储形态）

### Fixed

- **打包版启动失败**：`PaperAgent.spec` 漏收集 **`paperlit`**（2026-09-19 才集成进
  `packages/` 的第三个 editable 包）——`app/services/lit_service.py` 在模块级
  `import paperlit`，于是打包版一启动就 `ModuleNotFoundError: No module named 'paperlit'`。
  已补 `pathex` + `hiddenimports`（`collect_submodules`），并给发布闸门加
  「**每个 `packages/` 包都必须被 spec 显式收集**」断言（同类事故第二次：v1.0.0 漏 paperkb）
- **打包构建修复**：`PaperAgent.spec` 仍引用已删除的 Obsidian 演示插件
  （`app/plugins/obsidian_notes/plugin.yaml|plugin.py` 与对应 hiddenimport），
  自 c819f49 删插件起 PyInstaller 直接报 "Unable to find ... when adding binary and
  data files" **构建失败**。已删净悬空引用
- **解析回归闸门打印即崩**：`tools/parse_regression.py` 在 Windows GBK 控制台下
  print 判据字符 `✗` 抛 `UnicodeEncodeError`，把「指纹不一致」的结论连同 diff 清单
  一起吞掉（`build_release.ps1` 里只能看到语焉不详的「闸门未通过」）。
  `os.environ["PYTHONIOENCODING"]` 对本进程无效，改用 `sys.stdout/stderr.reconfigure`
- **发布闸门列定位 off-by-one**：`tools/release_check.py` 把兼容登记表的
  「为什么存在」列当成「移除条件」列读——§A 只有占位行时被掩盖，登记第一条真债务
  立刻误报「已到期」。改为按表头定位列名
- **前端版本契约测试**：前端拆成多模块后，断言只读 `frontend/app.js` 导致
  「关于页从 /api/version 取版本」误报缺失；改为扫全部前端脚本
- **编译状态以产物为准**：界面此前只读 `compile_jobs.status`，产物被清理后仍显示
  「已完成 L1, L2, L3」。现在 done 行叠加「产物确实在盘上」判据，缺失则显示「待重建」，
  与入队幂等同源
- **L3 自动升级**：陈旧 `done` 行不再永久挡掉重建（实测 adma 产物永不重建）
- **期刊名匹配三级兜底**：`&` ≡ `and`；jcr↔cas 按规范化名配对；新增城市消歧后缀兜底
  （`Children` → `Children-Basel`）。真实库 120 篇只能靠刊名匹配的文献命中数 118 → 120
- **`kb_list` 期刊筛选漏筛**：磁盘上有产物的篇目被过滤器挡掉后会被无过滤的磁盘分支加回
- **编译队列价值分被冲成 0**：`_mark_done` / 失败路径走 `INSERT OR REPLACE` 时丢 `value_score`
- 单篇召回 doi 过滤被 SQL `OR/AND` 优先级击穿，导致跨文献证据泄漏
- L3 编译产物 wikilink 一律用目录名（DOI 的 `/` → `_`），修 Obsidian 死链；
  `_relations.md` 「相关文献」补编号，正文内联 `文献N（DOI）` 可溯源
- 翻译：嵌套 `$...$` 死循环致翻译全英文回退；同模型翻译全英文
- glm 编译「缺失 one_liner」判死：提示词禁 LaTeX + 容错解析共用
- FTS 兜底片段改取**命中位置**窗口（此前取文件开头），且与主路口径统一为 1200 字

### Changed

- `app.js` 拆分为 `frontend/js/` 10 个功能模块（保留末尾 `boot()` 调用）
- KB 管理弹窗重写（总览 + 文献管理）
- agent 工具结果裁剪改为**按整条条目**取舍（不再对序列化 JSON 做边界截断，避免切坏结构丢尾部）
- 编译产物与检索口径的注释/文档与代码对齐（`queue_all_by_value` 阈值口径、worker 职责边界）
- `docs/COMPAT-REGISTER.md` 新增 **A1 待清理**：`db.py` 的 `papers_meta` 内联补列 ALTER
  （2026-09-19 引入，属未登记的兼容分支）——登记为债，移除条件 v1.4.0

### Docs

- `docs/KNOWLEDGE-BASE.md` 重写：修正 12 处与代码不符的口径（预算单位「8k token」实为
  8000 **字符**、精排候选池是 `max(12, top_k*3)` 而非死配置 `rerank_pool=30`、
  向量块口径 900/120 而非 160、索引落盘已落地、目录与库清单补全、`notes_fts` 存相对路径…），
  并新增 4 张 Mermaid 架构图（全局分层 / 检索链路 / 编译升级链 / 向量存储形态）+ 关键代码索引表
- `docs/DATA-LAYOUT.md` 修正 5 处：`_details` 不存在（实为 `_relations.md`）、
  `_qa/` 目录不存在、`attachments/` 与 `cards/` 是按需创建、`data/_backups/` 未登记、
  `library/` 描述补 source.pdf / mineru_full.md / work / qa_report.json

### 升级要做的事

1. **不需要重新解析、不需要重编产物**（编译产物与知识库目录格式未变）。
2. **向量索引需要重建一次**：旧格式（`kb_vectors/kb_index_meta.json` + `kb_vectors.npy`）
   **不做读时兼容**，升级后索引为空。二选一：
   - 界面点一次「重建向量索引」（要花 embedding 钱，按篇数计）；
   - 想零成本：调用 `paperkb.db.import_legacy(get_index_store(roots))` 一次性把旧向量
     搬进新存储（不重新 embedding），导入后旧文件可删。
3. 升级前照例备份 `data/` + `knowledge_base/`（`docs/UPGRADE.md`）。

### Technical Details

- 新增：`packages/paperkb/paperkb/textseg.py`、`db.py::IndexStore`、
  `db.py::import_legacy`、`backend/app/services/compile_worker.py`
- 新增测试：`test_index_store.py`(20) / `test_vector_persistence.py`(13) /
  `test_paper_products.py`(5) / `test_l3_auto_upgrade.py`(5) /
  `test_compile_state_truth.py`(7) / `test_journal_name_norm.py`(11)
- 测试基线：814 项，805 通过（9 项失败为既有已知项，见 `.dsh-memory/HANDOFF.md`）

## [1.2.0] - 2026-09-19

### Added

- **翻译模型独立路由** (T1)
  - 设置中心新增"翻译模型"配置区，支持独立于主模型的翻译专用模型
  - 可配置不同供应商/模型（如 SiliconFlow Hunyuan-MT-7B 免费翻译）
  - 翻译时自动切换至专用模型，翻译完成后恢复主模型
  - API: `GET/POST/DELETE /api/settings/translation-provider`
  - 前端：设置 → 模型 tab → 翻译模型配置表单

- **AI 写作 Agent** (T3)
  - 新增 `kind="writing"` 会话类型，支持自动计划→检索→写作→验证→迭代→保存完整流程
  - 6 个专用工具：`writing_plan`/`writing_retrieve`/`writing_draft`/`writing_validate`/`writing_revise`/`writing_save`
  - 工具循环 15-20 轮（综述类任务自动增加轮次）
  - 报告保存至 `knowledge_base/_reports/<标题>.md`
  - 验证机制：字数/段落/连贯性评分，<8 分自动修订

### Fixed

- 修复 `chat_service.py` 中文引号损坏导致的 SyntaxError (U+201C/U+201D → Unicode 转义)
- 修复所有会话模式"（无回答）"问题 (T2, commit 6fa1c4a)

### Technical Details

- `backend/app/services/writing_tools.py`: 写作工具实现（6 工具）
- `backend/app/services/chat_service.py`: 新增 `_writing_system_prompt()` 和 `_ask_writing_tools()`
- `backend/app/services/kbmeta_service.py`: 翻译时临时切换全局 AI 单例
- `backend/app/api/chat.py`: 路由支持 `kind="writing"`
- `frontend/index.html` + `frontend/app.js`: 翻译模型配置 UI

## [1.1.0] - 2026-09-18

Previous release (before this changelog).

---

## Versioning Policy

- **MAJOR**: Incompatible data format changes requiring migration
- **MINOR**: New features (backwards compatible)
- **PATCH**: Bug fixes (backwards compatible)

See `docs/VERSIONING.md` for detailed versioning contract.
