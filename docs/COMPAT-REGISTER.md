# 兼容代码登记表（COMPAT-REGISTER · 2026-09-12 建表）

> 规则（见 `docs/VERSIONING.md` §4）：**任何**兼容旧数据/旧行为的分支都必须在此登记，
> 含「位置 / 为什么 / 移除条件 / 验证方式」。未登记 = 违规；到期的 shim 未删 = 发布检查清单拦住。
> 目标：兼容代码只住在**迁移层**，业务层只读当前格式。

## A. 待清理

| # | 位置 | 是什么 | 为什么存在 | 移除条件 | 验证方式 |
|---|---|---|---|---|---|
| — | — | **当前无待清理项** ✅（A1/A2/A5 已于 2026-09-12 全部清理，见 §C C7/C8/C9） | — | — | — |

## B. 长期保留（属"接口/契约"，不设移除期限，但不得扩散）

| # | 位置 | 是什么 | 说明 |
|---|---|---|---|
| B1 | `packages/paperkb/paperkb/config.py:50` | `Roots.main_db` 仍指向 `biblio_db` | 旧名别名，供 `KBStore`/既有测试；新代码统一用 `biblio_db`，别名不新增使用点 |
| B2 | `paperkb/resource.py:find_doc` 模糊兜底扫描 | 目录名与键无字面关系的历史数据 | 历史目录命名不可逆；仅在精确命中失败时启用（O(目录数)） |
| B3 | `paperkb/context.py::_is_ref_section` / `tail_cut_index` 双判据 | Reference 判据两套（section 名 + 首段形态） | 解析产物 `section` 归属不可靠（实测），两套并存是**正确性**需要而非兼容 |
| B4 | `tools/*` 探针兼容旧路径 | 调试脚本 | 探针可随时重写，不受发布契约约束 |

## C. 已删除（留档，防重复引入）

| # | 内容 | 删除时间 | 替代 |
|---|---|---|---|
| C1 | `input/<run_id>/*.pdf` 上传副本留存 | 2026-09-12 | 暂存 `work/upload/`，解析后即删；PDF 唯一长期归属 `library/<RID>/source.pdf` |
| C2 | `knowledge_base/.system/paperagent.db` 单库 | 2026-09-12 | `data/{system,chat,diary,biblio,reference}/*.db` 五库隔离（`tools/migrate_data_layout.py`） |
| C3 | `PAPERAGENT_PIPELINE=p12` 回落分支 | 2026-08-26 | P14 单一生产链（P12 已归档） |
| C4 | `store.py::_init_db` 的 10 处内联 `ALTER TABLE`"试探式补列"（原 A3/A4） | 2026-09-12 | `paperkb/migrations/0001_baseline.py`（幂等 + `verify()` + 启动 runner + 迁移前自动备份）；**store.py 已由 CI 守卫禁止再出现 ALTER** |
| C5 | `paperkb/db.py::init_schema` 的三处 `papers_meta` 补列 ALTER | 2026-09-12 | 同上（0001_baseline） |
| C6 | `Settings` 中以 `PROJECT_ROOT` 为根的**可写**路径（`db_path`/`engine_*_root`/`dual_work_root`） | 2026-09-12 | 一律 `APP_DATA_DIR`（打包版 = exe 同目录）⇒ 升级只换程序、资产原地保留；守卫 `TestAssetPathIsolation` |
| C7 | `settings_service.get_prices` 的**读时兼容**旧扁平价格格式分支（原 A1） | 2026-09-12 | 迁移 `0002_settings_prices.py` 启动时一次性规整；业务层只读规范结构；`DATA_FORMAT` 1→2 |
| C8 | `chat_service` 的 `paper.md` / `paper.en.md` 旧产物回退（原 A2） | 2026-09-12 | 实测全库 0 命中（`library/`+`knowledge_base/` 扫描）；`_remove_legacy_paper_md` 继续清理历史残留；只读 `en.md`。**2026-09-12 复核补正**：缓存指纹里的旧候选名（`for cand in ("en.md","paper.md","paper.en.md")`）当时**仍在**，现已清成 `("en.md",)`，回归钉补钉字符串字面量（旧钉 `folder / "paper.md"` 钉不住元组写法） |
| C9 | `db.py::_migrate_meta_pk_to_rid`（原 A5，含 ALTER/重建表） | 2026-09-12 | 收编进 `migrations/0003_meta_pk_rid.py`（schema 运行时解析；不用 executescript 以保事务完整）；DDL 单一来源 `db.PAPERS_META_DDL` |
| C10 | 「待确认学习规则」面：UI 卡片 + `GET/POST /api/settings/rules`、`POST /api/settings/rules/batch`、`GET/POST /api/papers/{id}/review/rules` + `ReviewService.pending_rules/decide_rule` + `tools/dbg_rule_decide.py` | 2026-09-12（批1） | **无替代**（P12 差异挖掘已归档：P14 链不产生该类规则、`rule_engine` 仅老链调用 ⇒ 清单恒空、批准不影响解析）。⚠️ **别误删** `rules/learned/domain.json` 化学式闭环：它走 `review_service.apply_choice`，不经上述方法（有回归钉 `test_apply_choice_domain_ai_promotes`） |
| C11 | 装饰性设置键 `kb_include` / `retrieval_include`（含 `get/save_*`、`get_all` 字段、`KB_COPYABLE`/`KB_ALWAYS`/`DEFAULT_*` 常量、前端两组勾选框） | 2026-09-12（批1） | **无替代**：全仓无消费点（kb 产物由 `docs/DATA-LAYOUT.md` 布局契约固定；AI 检索只按 `retrieval_mode`）。旧库历史值成为无害残留（无读者） |
| C12 | `GET /api/settings/system-prompt`、`GET /api/settings/appearance` | 2026-09-12（批1） | 读回值已在 `GET /api/settings`（`system_prompt_extra` / `custom_css`）；前端只 POST 保存，无 GET 调用方 |
| C13 | 生产解析**降级链**的非精准通道：`mineru`（v1 免费、限流）与 `pymupdf`（本地、无 LaTeX/表格）；`_parse_chain` 多元素返回；`MINERU_PARSER=pymupdf` 配置项与设置中心「单通道解析通道」下拉 | 2026-09-12（批2，用户拍板"质量不可靠宁可不解析"） | 生产链只留 `mineru-v4`；无 Key → 前置硬门禁直接拒绝（`EngineService.parse_pdf` 抛 `PAPER-MINERU-REQUIRED`、上传预检 400 `mineru_required`）。⚠️ **`paperparse` 包内 CLI/离线工具**（`api.process_pdf(parser=…)`、`tools/parse_offline.py`）保留各自多通道能力，不受此门禁约束 |

> **迁移层现状（2026-09-12）**：`0001_baseline`（旧库补列 + sessions 重建）、`0002_settings_prices`、
> `0003_meta_pk_rid` 三条，全部幂等 + `verify()` + 事务 + 迁移前 `VACUUM INTO` 备份。
> **代码库中已无任何读时兼容分支**；`ALTER TABLE` 只允许出现在 `paperkb/migrations/`（CI 守卫强制，
> 白名单已清空——`db.py` 死豁免已删）。迁移台账 `system.migrations_applied` + `verify()` 自证，
> 保证"同格式内漏跑的迁移"会被发现并补跑（见 `docs/VERSIONING.md` §3）。

## D. 登记流程

1. 写兼容分支前：先判断能否**迁移解决**（首选）；确实只能读时兼容 → 在此表加行。
2. 行必须含**移除条件**（版本号或日期）；无条件的行不允许合入。
3. 每次发布按 §7 检查：到期行 → 删除代码 + 移到 §C 留档。
