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
│  ├─ reference/journals.db  JCR 分区 + 中科院分区（可重新导入 xlsx）
│  ├─ vector/                派生索引（可重建）：kb_index.db 元数据 + kb_vectors/ 段文件 + query_cache.db
│  └─ _migrated/             一次性迁移的旧库归档（可删）
├─ knowledge_base/       ★ 知识资产（**必须备份**）：每篇一个目录（编译产物/卡片/_qa/_index）
│  ├─ <RID>/                 en.md · document.json · source.pdf · images/ · _note/_details/_wiki · cards/
│  ├─ _qa/  _global/         全局问答卡
│  ├─ _index.md              知识库索引
│  ├─ .trash/                回收站（移出知识库的篇目）
│  └─ .obsidian/             Obsidian vault 配置（外部应用；**不属于知识内容**，可删可重建）
├─ library/              ◐ 解析库（**可选备份**，可重解析再生）：document.json/en.md/images/译文变体
├─ work/                 ✕ 可随时清理：scratch/（探针报告）· upload/（上传暂存，解析后即删）·
│                          tmp_export/（引擎暂存）· mineru_cache|mineru_backup/（离线复现用缓存）· pytest-tmp-*/
├─ logs/                 ✕ 可清理：paperagent.log（10MB×3 轮转）
├─ 用户提供的文献/          ★ 用户原始资料（**只读**，不入库）
├─ attachments/          ★ 零散用户资料（无父资源的 SI/审稿意见；不入版本库）
└─ rules/ docs/ feedback/ tools/ backend/ frontend/ packages/ .dsh-memory/
```

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
| 必须备份 | `data/`（五库）+ `knowledge_base/`（知识资产）+ `用户提供的文献/` + `attachments/` |
| 可选备份 | `library/`（重解析可再生，但要花 API 钱）· `data/vector/`（向量索引可由 knowledge_base 编译产物重建，但要花 embedding 钱） |
| 随时可清 | `work/`（含 upload/tmp_export/pytest-tmp/mineru 缓存）· `logs/` · `data/_migrated/` · `__pycache__` |
| 不入版本库 | `data/` `library/` `knowledge_base/` `work/` `logs/` `input/` `*.db` `用户提供的文献/` `attachments/`（见 `.gitignore`） |

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
