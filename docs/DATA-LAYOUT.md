# 数据布局规范（DATA-LAYOUT · 2026-09-12 用户拍板 · 工业级）

> 本文件是**数据归属的唯一权威口径**：哪个目录/库放什么、哪些必须备份、哪些随时可清。
> 变更布局必须同步本文件 + `AGENTS.md` 的关键目录段 + `paperkb.config.Roots`（路径单点）。

## 1. 顶层结构

```
PaperAgent/
├─ data/                 ★ 应用数据（**必须备份**）：五库物理隔离，见 §2
│  ├─ system/app.db          设置/供应商 Key/用量/插件/任务
│  ├─ chat/chat.db           会话 + 消息 + 答案缓存（**不可再生**）
│  ├─ diary/diary.db         文献日记（当前实现写文件，库占位）
│  ├─ biblio/biblio.db       文献元数据 + 文献库导入记录（论文/别名/引用/编译队列/FTS）
│  ├─ literature/lit.db      文献计量库（PaperRank/共被引聚类/IF 补全；AI 检索侧主源）
│  ├─ reference/journals.db  JCR 分区 + 中科院分区（可重新导入 xlsx）
│  ├─ vector/                派生索引（可重建）：kb_index.db 元数据 + kb_vectors/ 段文件 + query_cache.db
│  ├─ _backups/              版本迁移前的库快照（`VACUUM INTO`，升级前自动写，见 backup.py）
│  └─ _migrated/             一次性布局迁移的旧库归档（工具按需创建，可删）
├─ knowledge_base/       ★ 知识资产（**必须备份**）：每篇一个目录
│  ├─ <RID>/                 en.md · document.json · source.pdf · images/ · zh.md · en_zh.md
│  │                            + 编译产物 _note.md(L1) / _wiki.md(L2) / _relations.md(L3)
│  │                            + cards/ 用户卡片（**按需创建**，永不被系统覆盖）
│  ├─ _concepts/             概念页（3+ 文献惰性聚合）
│  ├─ _reports/              综述/报告落盘（kb_write_report 写入，**按需创建**）
│  ├─ _global/cards/         全局问答卡片（不绑定单篇，**按需创建**）
│  ├─ _index.md              知识库索引
│  ├─ .trash/                回收站（移出知识库的篇目）
│  └─ .obsidian/             Obsidian vault 配置（外部应用；**不属于知识内容**，可删可重建）
├─ library/              ◐ 解析库（**可选备份**，可重解析再生）：document.json · en.md ·
│                          source.pdf · mineru_full.md · images/ · work/ · qa_report.json
├─ work/                 ✕ 可随时清理：scratch/（探针报告）· upload/（上传暂存，解析后即删）·
│                          tmp_export/（引擎暂存）· mineru_cache|mineru_backup/（离线复现用缓存）· pytest-tmp-*/
├─ logs/                 ✕ 可清理：paperagent.log（10MB×3 轮转）
├─ 用户提供的文献/          ★ 用户原始资料（**只读**，不入库）
├─ attachments/          ★ 零散用户资料（无父资源的 SI/审稿意见；**按需创建**，不入版本库）
└─ rules/ docs/ feedback/ tools/ backend/ frontend/ packages/ assets/ release/ tests/ .dsh-memory/
```

**「按需创建」的含义**：标了这一条的目录**不是预置结构**，只有用户真正写了对应内容才出现
（`cards/` 由 `cards.py` 在首次写卡片时 mkdir）。做迁移/清点/备份时不要假定它存在；
反过来也不能把它当垃圾清掉。**尚未实现的**：`_topics/`（主题 MOC，见 `KNOWLEDGE-BASE.md` §7）。

### 1.1 核心数据文件名的**唯一来源**（2026-09-23）

`library/<RID>/` 与 `knowledge_base/<RID>/` **各有一份同名**的核心数据文件
`document.json`（前者=解析产物，后者=**定版**，编译/翻译/复核读写的那份）。
同名曾让"写的一侧"和"读的一侧"指到不同目录 ⇒ **译文写进 kb、渲染却读 library
⇒ 中文变体退回英文**（2026-09-23 用户报障，见 CHANGELOG）。

规则（三条一起才成立，缺一条同类 bug 会复发）：

1. **文件名**只在 `packages/paperkb/paperkb/layout.py` 定义（`LIB_DOC_NAME` /
   `KB_DOC_NAME` + `doc_basename(kb=)` / `doc_path(dir, kb=)`）。产品代码**禁止裸写**该文件名；
   守卫 `backend/tests/test_doc_name_source.py` 把裸写判为失败，状态键等**非路径**用法
   必须在该行标注 `# doc-name-ok`。
2. **取哪一份**只有一个入口：`paperkb.api.shared_doc_json` → `canonical_doc_json` →
   `translation_target`（**定版 kb 优先 → 解析库兜底**）。渲染/编译/翻译/问答/复核都必须走它；
   "kb 目录里已有产物却缺定版"会打 warning（不再静默回退）。
3. **前端不得拼知识库路径**（`papers.js` 曾拼 `knowledge_base/<doi 下划线>/document.json`，
   目录名不等于该形态时会指向不存在的文件）；传 `doi` 由后端解析。

**若将来真要给某一侧改名**（本规则把改名从"~45 处手术"降级为"改常量 + 一次数据迁移"）：
① 改 `layout.py` 常量 + 写一次性改名脚本（`migrations/` 现只支持 DB，文件改名照
`tools/migrate_pdf_names.py` 的 dry-run/`--apply` 模式）；② 引擎侧
`packages/paperparse/`（`middleware/orchestrator.py` / `middleware/stages.py` /
`core/p14_pipeline.py` 写的是 library 侧中间产物，不参与两侧二选一，故不在守卫内）同步；
③ 文档同步：本文件、`docs/KNOWLEDGE-BASE.md`、`docs/USER-MANUAL.md`、`AGENTS.md`；
④ 按 `docs/VERSIONING.md` 走**破坏性变更**流程（`DATA_FORMAT` +1 + 兼容登记 + 旧库 dry-run）。

## 2. 五个数据库各放什么（表 → 库）

| 库 | 文件 | 表 |
|---|---|---|
| 系统 | `data/system/app.db` | `settings`（含供应商/Key/价格）· `llm_usage` · `plugins` · `tasks` |
| 会话 | `data/chat/chat.db` | `sessions` · `messages` · `answer_cache` |
| 元数据 | `data/biblio/biblio.db` | `papers`（导入记录）· `kb_edited` · `papers_meta` · `identifiers` · `citations` · `doi_md5_map` · `compile_jobs` · `compile_ctx` · `concepts` · `kb_trash` · `meta_fts` · `notes_fts` · `fulltext_fts` |
| 期刊 | `data/reference/journals.db` | `jcr` · `cas` |
| 日记 | `data/diary/diary.db` | （预留；当前日记以文件形式写 `knowledge_base/` 内笔记） |

**实现方式（重要）**：backend `Store` 以 `system/app.db` 为 main，并把 `chat`/`biblio`
用 `ATTACH DATABASE` 挂上；SQLite 的未限定表名按 main→附加库顺序解析，因此既有的
全部 SQL 无需改写就落到正确的库。建表语句按库限定（`chat.sessions` / `biblio.papers`…）。
**`data/vector/kb_index.db` 不属于上表五库**：它是**派生索引**（可由 `knowledge_base/` 编译产物重建），不参与 `data/manifest.json` 的版本契约；格式版本自带在 `kb_index.db` 的 `meta.schema_version`，新于代码支持的版本会 fail-fast。表：`segments`（段文件注册）· `passages`（块元数据 + 段/row 指针）· `papers_state`（篇级统计）· `dead_letter`（失败重试）。向量本体：`data/vector/kb_vectors/seg-*.f32`（裸 float32 行主序，**只追加**；空间回收走 `POST /api/kb-meta/index/compact`）。

**跨库外键不受支持**：`sessions.paper_id`、`tasks.paper_id` 只存整数、不建 FK（级联由应用层做）。

## 3. 备份与清理

| 动作 | 覆盖范围 |
|---|---|
| 必须备份 | `data/`（五库 + `manifest.json` + `_backups/`）+ `knowledge_base/`（知识资产）+ `用户提供的文献/` + `attachments/` |
| 可选备份 | `library/`（重解析可再生，但要花 API 钱）· `data/vector/`（向量索引可由 knowledge_base 编译产物重建，但要花 embedding 钱） |
| 随时可清 | `work/`（含 upload/tmp_export/pytest-tmp/mineru 缓存）· `logs/` · `data/_migrated/` · `__pycache__` |
| 不入版本库 | `data/` `library/` `knowledge_base/` `work/` `logs/` `input/` `*.db` `用户提供的文献/` `attachments/`（见 `.gitignore`） |

> 备份口径与 `docs/VERSIONING.md` §5 一致：**只 `data/` 里的一部分库 = 不够**。
> 早前本表曾写「迁移三件套 = kb + biblio + journals」，**已废弃**——`chat.db`（会话不可再生）
> 与 `system/app.db`（设置/Key）都不能丢。

清理命令示例（PowerShell）：
```powershell
Remove-Item -Recurse -Force work\pytest-tmp-*, work\upload\*, work\tmp_export\*, logs\*.log*
```

## 4. 迁移与校验

```powershell
# 旧单库 → 新五库（默认 dry-run，逐表核对行数）
& '.venv\Scripts\python.exe' tools\migrate_data_layout.py            # 计划
& '.venv\Scripts\python.exe' tools\migrate_data_layout.py --apply    # 执行（旧库自动归档到 data/_migrated/）
# 布局盘点（目录/体积/SQLite 表与行数）
& '.venv\Scripts\python.exe' tools\dbg_data_inventory.py
```

- 迁移实测（2026-09-12）：19 张表逐表行数一致（settings 7 / papers 2 / papers_meta 2 /
  compile_jobs 4 / meta_fts 2 / notes_fts 7 …），`journals.db` 直接移动（jcr 22249 / cas 21772 不变）。
- **`.env` 只放"不该进代码仓库的东西"**：API Key/供应商定义/端口/超时等运行参数。
  ⚠️ 路径类键（`PAPERAGENT_DB` / `ENGINE_WORK_ROOT` / `ENGINE_OUT_ROOT` / `ENGINE_INPUT_ROOT`）
  **已从 .env 移除**：代码默认值（基于 `APP_DATA_DIR`）就是正确路径，留着它们等于给同一个事实
  第二个权威来源 —— **2026-09-12 实测事故**：迁移脚本用了被 .env 覆盖的 `Settings().db_path`，
  数据被写进另一个库，随后清理时丢了 4 个 settings 键（已从归档恢复）。
  规则：**路径只有一个来源（`Roots` / 代码默认值）**；如确需覆盖，必须在 `docs/COMPAT-REGISTER.md` 登记并加校验。

## 5. 上传链路（不再留存 PDF 副本）

- 旧行为：上传的 PDF 复制到 `input/<run_id>/`，解析后**永久留存**（7 份冗余实测 37MB）。
- 新行为：暂存目录 = `work/upload/<run_id>/`（可清理区），解析完成即删；
  PDF 的唯一长期归属是 `library/<RID>/source.pdf`（+ 知识库纳入后的 `knowledge_base/<RID>/source.pdf`）。
- **重复导入检测**：按 `papers.pdf_md5` 命中即返回提示（不重复解析）；用户坚持导入 →
  覆盖旧结果（重解析重建 library/kb，旧记录被替换）。
