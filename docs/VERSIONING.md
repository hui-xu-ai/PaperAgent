# 版本升级与维护规范（VERSIONING · v1，2026-09-12）

> 目标（用户 2026-09-12 拍板）：**升级不得影响上一版本的数据**；**不得为了兼容旧数据在业务代码里
> 堆冗余重构**；资产结构保持最小并**与软件架构隔离**，中间放**统一接口**。
> 本规范是硬约定：新增/修改数据、发布版本、改动存储层前必须先读本文件。

---

## 1. 四个版本概念（各管一件事，禁止混用）

| 概念 | 含义 | 单一来源 | 变更时机 |
|---|---|---|---|
| `APP_VERSION` | 应用发布版本（backend+frontend+paperkb） | `backend/app/version.py`（`pyproject.toml` 为打包副本，测试断言两者相等） | 每次发布 |
| `ENGINE_VERSION` | 解析算法包版本（paperparse） | `packages/paperparse/paperparse/__init__.py::__version__` | 算法变更 |
| `DATA_FORMAT` | **数据格式版本**（五库 schema + `data/` 目录语义） | `backend/app/version.py::DATA_FORMAT` + `data/manifest.json` | **仅当有迁移时 +1** |
| `ARTIFACT_FORMAT` | 单篇解析产物格式（`document.json.schema_version`） | `paperparse` 写出、`paperkb` 读取 | 产物结构变更时 |

- `MIN_READABLE_DATA_FORMAT`：当前代码**能自动迁移**的最旧 `DATA_FORMAT`（默认 `DATA_FORMAT - 1`）。
  低于它的数据必须走一次性离线工具，不做在线兼容。
- 健康检查暴露 `version`/`engine_version`/`data_format`（`GET /api/health`），便于工单定位。

## 2. 数据兼容契约（升级不得影响旧数据）

1. **只增不删**（Additive-only）：新版本对 `data/` 与 `knowledge_base/` 的变更只允许
   「加表 / 加列（带默认值）/ 加文件 / 加索引」。删列、改类型、改主键、改目录语义 =
   **破坏性变更**，必须升级 `DATA_FORMAT` 并提供迁移 + 回滚说明。
2. **用户资产神圣**：`用户提供的文献/`、`attachments/`、`knowledge_base/<篇>/cards|_note|_details|
   _wiki|_qa|source.pdf`、`data/*/*.db` 的任何写入都必须可回滚；清理/覆盖前必须过
   `paperkb.layout.assert_safe_to_clear` 与 `kb_edited` 检查（既有守卫，新代码照抄）。
3. **派生资产可重建**：`library/`（解析产物）、`work/`、`logs/`、FTS 索引、`compile_*` 队列——
   允许重建，但重建不得要求用户重新导入 PDF（`library/<RID>/source.pdf` 优先，`papers.pdf_path` 兜底）。
4. **读容忍未知**：JSON 产物（`document.json`/`settings` 值/`paperkb` 元数据）读取端必须容忍
   **未知字段**（已如此），不得因新增字段报错。
5. **降级拒绝**：代码打开 `DATA_FORMAT` **高于**自身的数据时，必须 fail-fast（明确提示
   "数据由更新版本写入，请升级应用"），**绝不猜测式部分读取**。启动时校验 `manifest.json`。
6. **升级前必做**：自动备份（§5）+ dry-run 迁移报告；迁移失败必须保持原库可用（先复制后改）；
   **备份失败必须中止迁移**（`run_migrations` 已抛 `RuntimeError`，不允许"无备份裸改"）。

## 3. 迁移纪律（兼容代码**只许**住在迁移层）

- 迁移步骤集中在 `paperkb/migrations/`（编号 `0001_…`、`0002_…`），每条：**幂等**、单事务、
  只做"旧格式 → 新格式"，不含业务逻辑；`store.py`/`db.py` 里的 `ALTER TABLE` 内联迁移
  **逐步搬迁**进来（现状见 `docs/COMPAT-REGISTER.md`）。
- 每条迁移必须实现 `NAME`/`VERSION`/`up(conn)`/**`verify(conn)`**（`verify` 必填，CI 守卫强制），
  并在台账 **`system` 库的 `migrations_applied(name,target_format,applied_at)`** 登记（库内自证）。
- **选步规则（2026-09-12 修复）**：`data_format` 是**格式**不是**步数**——同一个格式可以含多条迁移
  （现状 `0002`/`0003` 都是 VERSION=2）。所以选步不是 `cur < VERSION <= target`，而是：
  ① 台账已登记 → 跳过；② `VERSION <= 当前格式` 但台账没有 → 跑 `verify()` **自证**，证不实就**补跑**；
  ③ `VERSION > 当前格式` → 执行。否则"数据已是格式 N、但当时只跑了同格式的另一条"的库会被**永久跳过**。
- **备份闸门**：真要执行迁移时先 `VACUUM INTO` 备份，**备份失败即抛错中止**（绝不"没备份照样改库"）。
- **业务代码只读当前格式**：禁止在 `services/`、`api/`、`chat_service` 里写
  `if 旧字段 else 新字段`、"旧产物回退"、"旧格式封装"这类读时兼容（现有两处待清理：
  `settings_service` 旧扁平价格格式、`chat_service` 旧 `paper.md/paper.en.md` 回退）。
- **迁移只跑一次**：启动时 `migrate()` 检查 `manifest.data_format` 与 `APPLIED`，缺失则执行；
  已是最新则零开销（不做"每次启动试探 ALTER"）。
- 迁移必须能在**上一版本的真实数据**上跑通：用 §6 的黄金夹具（golden fixture）做自动化验证。

## 4. 反冗余（防止"为兼容旧数据而重构"）

1. **shim 登记制**：任何兼容分支必须登记进 `docs/COMPAT-REGISTER.md`，含
   「位置 / 为什么 / 移除条件（版本或日期）/ 验证方式」。没有登记条的 shim 视为违规。
2. **支持窗口 N-1**：只保证"上一版本数据可自动升级"，更旧的走离线工具；
   读时兼容（双格式并存）**一律不允许**。
3. **CI 守卫**（`backend/tests/test_version_contract.py`）：
   - `ALTER TABLE` 只允许出现在 `db.py`/`store.py`/`migrations/`；
   - `sqlite3.connect` 只允许出现在数据层（`store.py`/`db.py`/`journals.py`/工具）；
   - `pyproject.toml` 与 `version.py` 的版本号一致；
   - `manifest.json` 与 `DATA_FORMAT` 一致。
4. 兼容代码的**生命期**：登记表里给出移除条件，到期未删 = 技术债工单（发布检查清单会拦住）。

## 5. 资产最小结构 + 隔离（与软件架构解耦）

```
data/                 应用数据（五库 + manifest.json，**必须备份**）
  manifest.json       ★ 版本戳与契约（app_version / data_format / dbs / layout）
  system|chat|diary|biblio|reference/*.db
knowledge_base/       知识资产（每篇目录 + _index.md + _qa + .trash）   ★ 必须备份
library/              解析产物（可再生）                               ◐ 可选备份
work/                 scratch/upload/tmp_export/缓存（**随时可清**）
attachments/ 用户提供的文献/   用户原始资料（只读，**绝不清理**）
```
- **目录语义版本化**：`manifest.layout = "v1"`；目录语义变更 = `DATA_FORMAT` +1 + 迁移。
- **最小化原则**：任何新数据先问"能不能不进库/不出新文件"；能派生的不进、能重建的不备份、
  同一事实只存一处（`papers_meta` 是元数据唯一权威，产物只存路径不存副本）。
- **隔离原则**：业务模块**不得**自己拼数据路径或开连接（见 §4.3 CI 守卫），
  只能经 §6 的统一接口。

## 6. 统一接口（存储层与业务层之间唯一通道）

- **门面**：`container.get_assets()` 返回 `Assets`（`backend/app/services/assets.py`），
  暴露五类仓储的**稳定方法名**（不改名、只加方法）：
  `assets.system.*`（设置/用量/插件/任务）、`assets.chat.*`（会话/消息/缓存）、
  `assets.biblio.*`（文献元数据/导入记录/编译队列/FTS）、`assets.reference.*`（期刊 IF/分区）、
  `assets.content.*`（knowledge_base/library 文件与产物，含清理守卫）。
- **接口稳定性契约**：方法名与语义按 SemVer 管理——加方法是 minor，改/删方法是 major
  （需先加新方法 + 兼容期 + 登记表移除条件）。返回**纯 dict/模型**，禁止把 sqlite 连接、
  Row、Path 泄漏给业务层。
- **实现可替换**：SQLite 只是当前 adapter；将来换 PostgreSQL/向量库/Obsidian vault 只换 adapter，
  业务层不动（`paperkb` 的 `Roots` + `KBStore` 已是这个形状，规范只是把它**固定下来**）。
- 迁移路径：新增/重构业务代码时，逐步把 `store.*`/`kbapi.*` 直调替换为 `assets.*`；
  **不做一次性大重构**（见 §7 发布节奏）。

## 7. 发布流程（每次升级照做）

> **打包版（用户实际使用的形态）** = 只替换程序文件、资产原地保留 ⇒ 用户视角的操作手册见
> `docs/UPGRADE.md`（备份 → 换 exe/_internal → 首启自动迁移 → 核对 `health.data_format` → 冒烟）。
> 开发者按下表执行。

**升级前**
1. `pytest -q` 全绿（基线见 HANDOFF §5）；`python tools/release_check.py` 通过（一条命令跑完
   版本一致性 + 数据兼容闸门 + 夹具迁移演练 + 打包自检 + 文档齐备；`--full` 连 pytest 一起跑）。
2. 若动了数据：写迁移 + `DATA_FORMAT` +1 + 在**上一版本真实库的副本**上 dry-run；
   更新 `CHANGELOG.md` 与"升级说明"（用户可见：会不会丢数据、要不要重解析）。
3. 备份演练：`data/` 与 `knowledge_base/` 打包到 `data/_backups/<APP_VERSION>-<date>/`（或用户指定位置）。
4. 登记表检查：到期的 shim 必须删掉或续期（写明原因）。

**升级中**
5. 启动时自动：备份 → 校验 `manifest` → 跑迁移（事务）→ `verify()` → 写回 `manifest.data_format`。
6. 任一步失败：**回滚到备份并保持旧版本可启动**，日志给出明确原因与人工恢复步骤。

**升级后**
7. `GET /api/health` 核对 `version`/`data_format`；核心链路冒烟：文献库列表、打开一篇、提问一轮、保存 _qa。
8. 打 tag（`vX.Y.Z`）+ 归档黄金夹具到 `tests/fixtures/data-vN/`（下个版本升级测试要用）。

## 8. 额外建议（我的意见，待你拍板）

1. **黄金夹具是"不丢数据 + 不堆 shim"的关键机制**：每发布一次就冻结一份**极小**的
   上一版本数据夹具（几 KB 的 DB + 一个篇目录），升级测试永远拿它跑。没有它，
   就只能靠读时代码兜底 —— 那正是冗余的来源。
2. **`data/` 可搬迁**：加 `PAPERAGENT_DATA_DIR` 环境变量（默认项目根 `data/`），
   备份/多档案（工作/私人两套数据）立刻变简单，也让"升级前备份"可指到别的盘。
3. **备份自动化**：启动迁移前若 `DATA_FORMAT` 将变化，自动写
   `data/_backups/pre-<old>-to-<new>-<ts>.zip`（DB 用 `VACUUM INTO` 导出，保证一致性快照）。
4. **离线升级工具**：`tools/upgrade_data.py --from-format N --dry-run`，支持从更旧格式
   一次性升级（超出 N-1 窗口时用），复用同一套迁移步骤。
5. **产物契约测试**：`ARTIFACT_FORMAT` 变更时，用 `tests/fixtures/artifact-vN/document.json`
   验证新代码能读旧产物（解析产物比 DB 更常被用户手动搬运）。
6. **文档即接口**：`docs/DATA-LAYOUT.md`（放哪） + 本文件（怎么升级） + `ARCH-ENTRY`（改哪里）
   三份保持同步；改任一份必须同一提交内改另两份。
