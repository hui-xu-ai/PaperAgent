# 变更日志（CHANGELOG）

> 版本号**单一来源** = `backend/app/version.py::APP_VERSION`（`frontend/version.js`、
> 根 `pyproject.toml`、`docs/VERSIONING.md` 的契约由 `tools/release_check.py` 与
> `backend/tests/test_version_contract.py` 强制一致）。
> 数据格式变更看 `DATA_FORMAT`（`docs/VERSIONING.md` §1）；**升级说明写清"会不会丢数据、
> 要不要重解析"**（用户可见）。

## [未发布] — 2026-09-12（设置中心批1–批5；**需重建分发版才生效**）

**批6（2026-09-12 夜，用户实测：识别复核页两处报障）**
- **修掉复核页「加载失败：Cannot set properties of null (setting 'src')」**：`openReviewModal` 在
  「零差异仲裁点」分支用 `$('review-pdf').innerHTML = …` **覆盖销毁了 `<img id="review-page-img">`**，
  紧接着的 `loadReviewPage(1)` 取到 `null` ⇒ 报错；因为 img 是**一次性**被销毁，**谁先打开了零差异项
  的那篇，之后所有复核页都报错**（实测复现：paper3 → paper2 必炸，刷新后单开 paper2 正常）。
  - 修法：新增常驻空态节点 `#rv-pdf-empty`，空态只切换显隐、**绝不覆盖容器**；`loadReviewPage`/
    `reviewZoom` 统一走 `reviewPageImg()`（缺失自愈重建）⇒ 任何顺序都不再抛 null。
- **修掉「知识库内有文件，但复核页 PDF 阅读区加载不出」**（用户实测 paper3）：
  `ReviewService._pdf_path()` 的兜底链写错目录——`papers.pdf_path` 指向解析期**暂存**
  `work/upload/<uuid>/xxx.pdf`（work 是"随时可清"区，解析完即删），旧兜底 glob 到
  `doc_json.parent.parent`（= `library/` 根，**少一层**）且**从不看知识库副本** ⇒ 暂存被清理的文献
  源 PDF 定位失败：`GET /api/papers/<id>/review/pdf-page/1` **500**
  （`no such file: …\work\upload\304d1d93…\10.1038_ncomms8258.pdf`）、`page_count=0`（一页都看不了）；
  而已解析文献暂存文件还在 ⇒ 表现为"算法区别对待"。
  - 修法：单一来源兜底链 = `pdf_path`（存在才用）→ `library/<资源>/source.pdf` →
    `knowledge_base/<资源>/source.pdf` → `library/<资源>/*.pdf`（历史 `<DOI>.pdf` 命名）→
    `doc_json` 同目录 `*.pdf`；资源目录名复用 `paperkb.resource.candidate_dirnames`（不新造规则）。
  - 零差异项时**仍**给出原文 PDF 第 1 页（同类文献"能看原文"不再依赖有无差异点）。
  - 回归测试 4 条（`backend/tests/test_review_service.py`）：暂存缺失→library 命中（含 `page_count`
    不再为 0 的断言）、library 缺→知识库命中、历史 `<DOI>.pdf` 命中、暂存存在时仍优先。

**批5（2026-09-12 夜，用户实测 UI）**
- **修掉「编译队列」价值分显示 0.00**：根因是 `Compiler.queue()` 的 `value_score` 缺省写死 `0.0`
  —— UI 用**显式 level** 入队（`POST /api/kb-meta/compile/queue {doi, level}`）时**从不计算分数**，
  于是队列表格显示 `0.00`（而 `/api/kb-meta/scores` 里该篇是 3.48），看起来像"这篇没价值"。
  - **根治**：`Compiler.queue(value_score=None)` 缺省即**现算当前价值分**（与 `/api/kb-meta/scores`、
    批量入队同一评分函数）；调用方显式给分（批量路径）仍以传入值为准。
  - **历史行兜底**：`GET /api/kb-meta/compile/jobs` 对 `value_score` 缺失/为 0 的行用当前分填充
    （按 DOI 缓存，评分失败保持 0 不阻塞）。
  - 实测（**打包产物**）：造一条历史 0 分队列行 → `/compile/jobs` 返回 `3.47` = `/score` 的 `3.47` ✓。
- **发布准备（面向 GitHub）**：`README.md` 修正与代码不符处（`MINERU_API_KEY` 由"可选/免费通道"改为
  **必填硬门禁**、`PAPERAGENT_DB` 默认改为五库 `data/system/app.db`），补全新参数（`MINERU_LANGUAGE`/
  `MINERU_IS_OCR`/`MINERU_ENABLE_TABLE`/`PADDLEOCR_OPTIONS`/`PAPER_FULLTEXT_PREFIX_CHARS`）与
  「思考档位」说明；`.gitignore` 补 `logs/`。安全扫描：已跟踪文件**无硬编码密钥、无私人路径/邮箱**，
  `.env`/`data/`/`library/`/`knowledge_base/` 均未入库。

**批4（2026-09-12 夜，用户实测输出质量）**
- **修掉译文里的「（原文未提供该段内容，无法翻译。）」污染**（用户实测该篇 `zh.md`/`en_zh.md`
  各 66 处：参考文献条目 62 + 致谢/SI/利益冲突/数据可用性各 1）。
  **根因**：翻译的"待译段落清单"与"给模型的全文上下文"用了**两套判据**——上下文按
  `context_paragraphs`（尾部杂项截断 + References 过滤）正确排除了这些段落，而待译清单用自建的
  `_reference_cut`（只看 `section`/heading 名）；该篇参考文献条目的 `section` 被解析层误标成
  `"Keywords"` ⇒ 切不到 ⇒ 这些段落**进了待译清单却没有原文**，模型只能回占位句并被当作译文写盘。
  - **修法**：待译清单改为与共享上下文**同一判据**（`context_paragraphs`）⇒ 可译段落严格 ⊆
    模型可见段落；`_reference_cut` 标记废弃（保留仅为兼容）。
  - **防御**：新增拒绝/占位文本识别（中英变体），命中一律按 rejected 丢弃、绝不写入 `text_zh`。
  - **存量清理**：新工具 `tools/clean_translation_placeholders.py`（`--dry-run` 先看）——
    清空 `document.json` 里的占位译文并**本地重渲染** `zh.md`/`en_zh.md`（0 API 成本，改动前自动备份）。
    已对用户实例执行：132 段（library + knowledge_base 各 66）清理并重渲染，占位符全库归零；
    备份 `work/scratch/placeholder-cleanup-<ts>/`。清空后这些段落**回退显示英文原文**（结构不丢）。
- **翻译的全文来源与编译统一**（用户要求"编译和翻译对应的全文必须一样，以降低输入 token"）：
  原先**编译**读 kb 快照（`Compiler._doc`）、**翻译**读调用方传入的 library 正本、**问答**读
  `shared_doc_json`（kb 优先）——三处来源不一致 ⇒ 两份 document.json 一旦有差异（复核写回只重写
  library）全文前缀就分叉，前缀缓存**无法互相继承**（整篇一次翻译 ≈17k token 前缀每次全价）。
  现在翻译也走 `shared_doc_json(key)`（kb 快照优先 → library 兜底）= **与编译/问答同一份**；
  **译文仍写回 library 正本**（翻译真相源不变），按 `para_id` 回填。
- 顺带确认（用户提问）：**送给模型的正文不含参考文献**（95 段仅 43 段进共享前缀），无需改动。

**批3（2026-09-12 晚，用户三问驱动）**
- **问答前缀不再截断**（缓存对齐修复）：此前"问答把全文截断到 60,000 字符、编译发全文"⇒ 两侧前缀
  长度不一致，命中上限被白白砍掉（实测该篇 69,181 字符 ⇒ 少共享 9,181 字符 ≈ 2,312 token）。
  新默认 `PAPER_FULLTEXT_PREFIX_CHARS=-1` = **不截断**（与编译/翻译逐字节一致）；
  `0` = 关闭该机制；`>0` = 显式截断（仅在明确要压 token 时用）。
- **新增「编译思考档位」与「翻译思考档位」**（设置中心 → 知识库 → 编译 / 翻译策略）：
  `自动`（默认）/ `none` / `minimal` / `low` / `medium` / `high`；编译档作用于 L1/L2/L3，翻译档作用于翻译任务，
  两者都不影响文献问答。
  - 背景：`deepseek-flash` 是思考型模型，**思考 token 计入输出并按输出价计费**
    （实测 L2 输出 9,673 token 而产物仅 3,019 字符 ⇒ 约 7k 是思考）；
  - **推荐默认「自动」**：= **不发送**该参数（服务端自适应）⇒ 翻译/编译质量与历史完全一致。
    ⚠️ 服务端**拒绝字面 `auto`**（实测 400 unknown variant）⇒ 自动档只能以"不传参"实现；
    `none/minimal/low/medium/high` 实测均被接受（`none` 思考 0 字符）；
  - 想省 token：翻译建议先试 `low`（改完抽查术语一致性 / `[[MATHn]]` 标签完整性 / 有无漏译）；
    编译建议用同一篇做一次 A/B（六维完整性、`[Pxxx]` 引用真实性）；
  - 顺带修掉一处**从未生效的配置**：`compile=high` / `translate=low` 的映射因 `kbmeta` 把 context
    折成 `engine`、且模型名不在 `REASONING_MODEL_HINTS` 里 ⇒ 历史上所有编译/翻译请求**都没带过该参数**
    （跑服务端默认）；现在设置中心显式选档才会下发（GLM 等原有映射行为保持不变）。
- **前缀缓存设计已实测确认**（用户提问核查）：翻译 / 编译 / 提问三条路径的请求头都是同一个字符串
  `shared_ctx = CTX_HEADER + paper_context(doc)`（`paperkb/context.py`）⇒ 受控实验三种请求形状
  命中均为 **17,408 / 17,562 = 99.1%**，即三条路径**共享同一段全文前缀缓存**；
  各自的指令、L1 摘要、编译笔记、会话历史、当前问题都排在这段前缀之后（各自历史互不干扰）。
- 参考文献核查结论（用户提问）：**送给模型的正文已排除 References**（单一判据
  `paperkb/context.py` 的 `_is_ref_section` + `tail_cut_index`）——实测该篇 95 段只 43 段进入共享前缀，
  被排除的 66 段（10,389 字符）含参考文献条目、致谢、利益冲突、数据可用性、Supporting Information。

**用户可见**
- **解析必须先配 MinerU Key（硬门禁）**：免费 v1 通道与本地 pymupdf 已从生产解析链移除；
  未配置 Key 时导入直接提示「未配置 MinerU API Key：解析功能已被禁用」并给出填写指引，
  不再静默降级到低质量通道。设置中心「解析」tab 顶部有**实时状态条**（就绪 / 未配置）。
- **解析参数可见可控**（写 `.env`，保存即生效）：MinerU `解析语言`（自动判定 / 英文 / 中文）、
  `扫描件 OCR`（自动：无文本层自动开启）、`表格识别`；PaddleOCR `跨页表格重整` / `合并表格` /
  `标题分级`（论文推荐全开 —— 此前完全没下发 ⇒ 跨页表格被拆断）。
- **设置中心重排为 6 个 tab**（模型 / 解析 / 知识库 / 界面 / 插件 / 关于）：显示与外观合并为「界面」；
  每个 tab **一个主保存按钮** + 「● 未保存」提示；删掉装饰性勾选框（纳入清单 / 检索文件清单）
  与僵尸卡片（待确认学习规则）；解析 tab 删除「单通道解析通道」下拉（通道全自动）。
- **可访问性**：设置弹窗 `role="dialog"` + Esc 关闭 + 焦点陷阱与归还；控件均有 label 关联。
- **修掉的静默失效**：解析设置里「翻译时机 / 批量跳过审核 / 篇间间隔」此前提交后被丢弃（永不落库）；
  MinerU 的 Key 与通道此前"存了不生效"（只写 SQLite，引擎读 `.env`）；化学式领域词典学习
  因 `_time` 未定义被静默吞掉、`rules/learned/domain.json` **从未写入过**（已修 + 回归钉）。

**数据与升级（零风险）**
- **不改数据格式**：`DATA_FORMAT` 保持 `2` ⇒ 老数据无需迁移；新参数写在 `.env`（缺失即用默认值）。
- 删除的旧面登记在 `docs/COMPAT-REGISTER.md` C10–C13（防重复引入）。

## [1.0.0] — 2026-09-12（首个正式发布版）

**用户可见**
- **桌面壳**：双击 exe 弹出**原生窗口（WebView2）并启动即最大化**，不再嵌在浏览器里受标签栏/地址栏遮挡；
  关窗（X）= 隐藏到**系统托盘**（后端与在跑任务继续），托盘「退出」才**完全关停并释放端口**。
  托盘图标与程序图标同源（`assets/icon.ico`）。
- **单实例**：重复双击不再抢端口，改为唤起已有窗口；`PaperAgent.exe --quit` 可让运行中的实例优雅退出。
- **关于页版本管理**：显示**界面（前端）版本**、应用（后端）版本、解析引擎 `paperparse`、
  知识库库 `paperkb`、`data_format`/`layout`；前后端版本不一致时显式报警。
- 打包版无控制台窗口；启动致命错误（如端口被占）弹窗提示而非静默失败。

**数据与升级（零风险）**
- **本版不改数据格式**：`DATA_FORMAT` 保持 `2`，`LAYOUT_VERSION` 保持 `v1` ⇒ 老数据**无需迁移**。
- 迁移层加固（`docs/VERSIONING.md` §3）：新增**台账** `system.migrations_applied` +
  `verify()` **自证补跑**——修掉"数据已是格式 N、但同格式内另一条迁移从未生效却被永久跳过"的风险；
  **备份失败即中止迁移**（不再"没备份照样改库"）。
- 回归钉加固：`ALTER TABLE` 守卫纳入 `tools/` 且大小写/空白宽松匹配，白名单清空（原 `db.py` 死豁免删除）；
  清理缓存指纹里的 `paper.md`/`paper.en.md` 旧候选名（`COMPAT-REGISTER` C8 补正）。

**验收轮修复（2026-09-12 用户实测反馈，已并入本版）**
- **期刊分区/影响因子开箱即用（随包 db）**：包里直接放**已解析好的 `journals.db`**
  （`_internal/share/reference/journals.db`，构建期由 `tools/build_reference_seed.py` 从 xlsx 生成），
  首次启动**只做一次文件拷贝**（实测 **0.014s**，开窗仍 6.7s，不再有 20s 解析）；
  **原始 Excel 也随包**（`share/reference/JCR分区.xlsx`）⇒ 用户日后按同格式更新后在「设置 → 期刊」导入即可。
  数据：jcr 22249 / cas 21772；`Advanced Materials` → JIF 26.8 · Q1 · 中科院 1 区 top
  （此前空库 ⇒ 价值分缺 IF 档 ⇒ 实测 adma 2.31→L1；有库后 3.48→L2）。
- **L1 产物「基本信息」补齐**：此前只印 作者/期刊年份/DOI/被引（且作者硬截断 6 位）——提示词侧早已补齐
  通信作者/研究单位/关键词，**产物模板没跟**。现在 `_note.md` 带 通信作者（作者行 `*` 标注）/研究单位/关键词，作者全量。
- **复核页误报修复**：零待复核项时管线不再"不写 `review.json`"，而是落一份显式空清单；
  `ReviewService` 判据放宽到"有双通道痕迹（arbitration_audit/char_conflicts/verify/review）"并区分文案
  （**已走双通道·无待复核项** ≠ **未走双通道**）。
- **双击 exe「没反应」（用户报障）**：单实例闸门只要发现端口上有实例应答就静默退出——
  若占端口的是**后台/开发模式**实例（无窗口、无 `/api/desktop/show`），用户什么也看不到。
  现在：唤不起窗口时**必然可见**（打包版弹窗 + 错误日志，含占用者 PID / 运行模式 / 数据目录
  + 三条可操作的退出指引）；`GET /api/health` 增加 `desktop` / `pid` / `app_data_dir` 供闸门判断。
- **干净分发版**：`tools/build_release.ps1` 默认额外产出 `release/PaperAgent-v<版本>-win64.zip`
  （**只含程序文件 + `使用说明.txt` + `.env.example`，不含任何 data/logs/work/.env**）；
  `tools/fresh_test.ps1` 一键解压到**空目录**（可选带上 .env）并做干净度自检 —— 满足"每次测试都从干净分发版开始"。
- **显示默认值**：会话区 15px / 阅读区 16px / 阅读区页边距「窄」24px / 字体「系统默认」；
  默认值收敛为 `app.js::DISPLAY_DEFAULTS` 单一来源，并修掉"下拉显示 12px、实际生效 13px"的不一致。
- **编译等级漂移（L2→L1）根因是数据**：价值分含 IF 档（0.35 权重），IF 来自 `data/reference/journals.db`；
  沙箱清空后该库为空 ⇒ adma 分 2.31 < 2.5 ⇒ 停在 L1。重新导入 JCR 分区表后 **3.48 → L2**（已实测重编 L2 成功）。
- **编译完成不发事件**：`CompileWorker` 新增 `notify` 回调，container 接事件总线发 `kb/compile_done`。
- **阅读器"编译结果标签页"不出现**：前端 kb 文件集缓存只写不失效 ⇒ 失效机制三处补齐
  （编译/保存/任务完成事件、打开目标文件不在缓存时强制重取、切换目录）。

**工程**
- 发布闸门：`python tools/release_check.py`（`--full` 连 pytest）一条命令给出 GO/NO-GO；
  `tools/check_data_compat.py` 修掉"恒 exit 1"（GBK 控制台 UnicodeEncodeError）并**真的跑夹具迁移**。
- 打包：`PaperAgent.spec` 收集 pywebview/WebView2/.NET 依赖、排除其它 GUI 后端、
  `console=False`、`upx=False`；`tools/build_release.ps1` 一键构建 + 产物自检。
- 版本对齐：`paperparse` 的 `pyproject` 从 0.1.0 校正为 2.3.0（与模块 `__version__` 一致）。

## [0.3.0] — 2026-09-12

- 读时兼容清零（`DATA_FORMAT` 1→2）+ 迁移层三条（`0001_baseline`/`0002_settings_prices`/`0003_meta_pk_rid`）
  + 迁移前 `VACUUM INTO` 自动备份 + CI 版本契约守卫。
- 数据布局五库物理隔离（`data/{system,chat,diary,biblio,reference}` + `manifest.json`）。
- `assets` 门面（`container.get_assets()`）+ 打包版资产路径修正（可写资产落 `APP_DATA_DIR`）。
