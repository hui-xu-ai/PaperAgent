# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [未发布]

### Added

- **翻译批次上限可调**（`设置 → 📚 知识库 → 编译/翻译策略`）：每次请求发给模型的
  **最多正文字符数**，留空＝默认（专用翻译模型 14000 / 主模型 12000）。
  - 用**字符**而不是 token 作控制变量：分批算法吃字符，日志/告警也是字符，用户可观测可验证
    （"字符→输出 token"的换算在不同模型/内容下差 2~3 倍，用它反推批次会偏乐观）。
  - **语义硬约束不变**：只在段落边界切批；单段超过上限时整段独占一批（尽量整段发送）；
    只有超长单段才按**句子边界**切块，绝不切断句子。
  - **超限日志**（带数字与建议值）：单段超上限 `单段 N 字符 > 上限 M`；批截断
    `批截断：3/12 段未译（本批 14120 字符，上限 14000）…建议降到 ≤7060`；单段补跑失败；
    整篇结束汇总 `本次翻译有 K 批触发截断（最大批 X 字符，上限 M）…`。
  - `run_translate` 返回值新增 `batch_limit` / `max_batch_chars` / `truncated_batches`
    / `oversized_paras`。
- **翻译批次「安全上限」自动探测**（`设置 → 📚 知识库 → 🔬 测试安全上限`，2026-09-22 用户要求
  "点一下自动测、按经验给安全系数、别按测试极限填"）：
  - 语料用**知识库/解析库里真实论文的英文正文段**（`document.json` + 与翻译同源的
    `context_paragraphs` 过滤，整段取用、绝不切句；优先长段以贴近真实批次形状）。
  - **阶梯** 3000 → 6000 → 12000 → 24000 → 48000 字符逐档单次真译，**首档失败即停**
    （截断随批次单调），通常只跑 2~3 次。
  - **双判据**判失败：`finish_reason == "length"`（`DeepSeekAI` 新增 `last_finish_reason`
    暴露该字段）**或** 应译段未回全——思考型模型常把 finish_reason 报成 `stop` 却只译一半，
    只看前者会漏判。
  - **安全系数 0.6**：建议值 = 最高通过档 × 0.6（**绝不等于测试极限**）；向下取整到百位。
  - **只给建议不自动写**：结果落 `settings.translate_probe_result`（含模型/时间/阶梯明细，
    超 30 天界面提示重测），写不写由用户点`写入`（复用既有 `/api/settings/translate-batch`）。
  - 新增端点 `POST/GET /api/settings/translate-probe`（后台线程 + 进度轮询；被测模型 =
    当前激活的翻译专用模型 → 回落主模型，与线上翻译路由同序）；`paperkb.api.probe_translate_batch`
    + `paperkb/translate/probe.py`；`KbMetaService.probe_translate_batch`、
    `TranslateProbeService`、`container.get_translate_probe()`。
  - 语料粒度提示：按库里段落的**中位长度 × 12 段/批**给出"你这批语料单批最多约 N 字符"，
    说明上限调到更大不会让单批变大（真实分批还有"每批 ≤12 段"这条约束）。
  - **单档墙钟上限 + 总预算**（2026-09-23 用户报"测 glm-4.5-air 的单批上限时直接卡在第 5 批、
    没有结果返回"）：LLM 客户端的 timeout 是"每次读"级别的（httpx），服务端**持续吐 token**
    就永不触发，思考型模型在 4.8 万字符档上十几分钟不返回，界面只能一直"探测中"。
    现在每档按字符数线性给墙钟预算（`max(90s, min(300s, 6s/千字符))`，实测各档 40/76/71/113s），
    超时即判该档失败并照样给结论；整轮另有 600s 总预算，用尽则余下档位逐档标"未测"
    （`stop_reason` 新增 `timed_out` / `time_budget`，`hint` 相应指向"换输出更快的模型"）。
  - **翻译效率评价**（2026-09-23 用户："增加一个翻译效率评价，监测到模型翻译效率很低很慢后，
    则认为这就是极限"）：每档记 `sec` / `sec_per_1k`（每千**正文字符**秒数，按输入归一——
    批次上限这个旋钮就是一批装多少源文）/ `out_cps`；**译完但慢于 `SLOW_SEC_PER_1K`（25 s/千字符，
    ≈2× 实测可用档最慢节奏 13.3 s/千字符）⇒ 同样视为极限**：阶梯停在该档、不计入最高通过档
    （建议值仍取更快的那一档），`stop_reason="inefficient"`。结果新增 `efficiency` 块
    （节奏 + "一篇 4 万字符论文约需 X 分钟"）并显示在界面上。
  - **探测与生产同口径：单档墙钟 = 生产读超时 90s**（2026-09-23 用户报"上一版测试 glm-4.5-air
    出现多次重试错误"）：日志证据 `Read timed out. (read timeout=90)`，13852 字符的批连续 3 次
    超时（270s 白等 + 3 次输入白烧）才回落主模型，而同一模型拿 3777 字符的小批立刻成功 ⇒ 是
    **时间不够**，不是模型不会译。根因是 `TRANSLATE_TIMEOUT_SEC = 90` 仍停留在"compact ≤1200
    字符（2-6s 返回）"时代的标定，而批次上限已涨到 14000/14500；而**探测此前用 300s 额度**，
    只证明"输出能回全"，把"生产 90s 内回不来的批次"判成通过 ⇒ 推荐值直接踩在生产超时上。
    现在：探测每档墙钟 = 90s（`probe.TIER_TIMEOUT_SEC`，守卫 `backend/tests/
    test_translate_timeout_contract.py` 强制与 `llm_service.TRANSLATE_TIMEOUT_SEC` 一致），
    客户端读超时那类异常也判为"该档超时 = 极限"（不再误报成"调用报错、稍后重试"）；
    探测客户端不再重试（测量不做重试）。⇒ 按实测节奏，glm-4.5-air 的建议值从 14400 落到
    **7200**（12000 档 71s 通过、24000 档 113s 超时）。
  - **重试事件带上原因**（`llm/retry`）：此前只报"将重试"，用户看到多条重试却分不清**读超时**
    （该降批次上限）/ 限流（等一会儿）/ 网络抖动（重试即可）。现在消息形如
    `translate 将重试（读超时 90s（单批生成超时，多为此批过大/模型过慢））（预计额外输入约
    13852 字符 token）`。

### Changed

- **翻译（专用翻译模型）的读超时 90s → 150s**（2026-09-23 用户拍板），探测的单档墙钟同步到 150s。
  两处常量必须同值，由 `backend/tests/test_translate_timeout_contract.py` 守卫：
  `backend/app/services/llm_service.py.TRANSLATE_TIMEOUT_SEC = 150`、
  `packages/paperkb/paperkb/translate/probe.py.TIER_TIMEOUT_SEC = 150.0`。
  - 派生值：探测客户端超时 = 150 + 余量 20 = **170s**（`PROBE_TIMEOUT_SEC`，自动推导）；
    整轮总预算 5 档 × 90s+余量 → **900s**（= 5 × 150 + 余量，保证 5 档全跑满也走得完阶梯给出结论）。
  - 影响：单批可译的规模上限随之变大（此前 12000 档 71s 通过、24000 档 113s 超时 ⇒ 90s 下建议值
    7200）；**建议重启后重新点一次「🔬 自动测一个值」**，用新口径重测当下的模型。
  - 超时的原始动机（绕过硅基流动免费端点偶发静默挂起）在 150s 下仍成立——挂起的端点不会因为
    多等 60s 而恢复；提高它换来的是"更大批次仍能译完、少触发超时重试（每次白烧一份输入）"。

- **三级编译提示词升级：从"输出格式说明"改成"审稿人检查清单 + 证据硬规则"**
  （2026-09-23，A/B 实测后落地）。起因是用户问"当前提示词是否足够权威简洁、能不能发挥大模型
  扮演专家阅读/总结/批评文献的能力"——旧提示词把篇幅几乎全给了 JSON 字段名，L2 的"批判"只有
  三个节标题，模型只能自己猜"什么算批判"。
  - **改了什么**（`_prompt_l1_l2_merged` / `_prompt_l1` / `_prompt_l2` 三处同一口径）：
    ① 角色 = 「科研知识编译助手（本领域资深审稿人视角）」，**期刊/被引用于校准"审视强度"**
    （权威期刊按更高证据标准审视），但**禁止因刊物知名度抬高结论**；
    ② L2 三节改成**逐项检查清单**（对照与混杂 / 样本量与统计 / 表征证据链 / 结论外推；
    关键参数完备 / 数据与代码可得性 / 复现卡点；已有证据 vs 待补验证 / 成本工艺寿命 / 最小可行验证）；
    ③ 六维从"写一段总结"改成**必须回答什么**（如 innovation 要写"在什么体系里、替换/新增了什么、
    带来什么变化"）；
    ④ **证据硬规则 4 条**（反幻觉：只写原文有的、没写就写「原文未报告」；区分「作者主张」与
    「证据支持度」；结论必须带 `[Pxxx]`；禁止"具有重要意义/首次/新颖/显著提升"这类套话）；
    ⑤ 长度约束**一律改成下限**（wiki 每节 ≥200 字、六维每维 ≥80 字）——实测 glm 类模型对
    "不超过 N 字"的上限基本不遵守，写上只占位；⑥ **wiki 段写在 L1 段之前**（深度优先）。
  - **A/B 实测**（同模型 glm-5.3-flash、同共享前缀逐字节相同、只换任务段；两篇真论文
    adma/pnas，各跑两臂，产物在 `work/scratch/ab-compile/`）：
    六维平均字数 119→300 / 123→315；wiki 总字数 1121→2519 / 1597→2484；
    wiki 段落引用 0→42（adma）与 25→20（pnas，**该项小幅回退**）；定量数值 15→37 / 3→12；
    代价 = 输出 +78%/+55%、耗时 +26%/+100%。**最实质的改善是抓到了旧提示词漏掉的结论问题**
    （adma：摘要声称"促进 MSCs 分化增殖"但正文只有概念验证装置，新提示词明确标为超出数据支持范围）。
  - **JSON schema 与旧版完全一致** ⇒ 解析/渲染/评分链路零改动。
  - 守卫 `packages/paperkb/tests/test_compile_prompt_contract.py`（11 例）：把"检查项 / 证据规则 /
    长度下限 / wiki 优先级"钉成断言；并用 `work/scratch/guard_red_proof.py` 证明**旧提示词文本
    对这组断言 16/16 不满足**（护栏确实会红，不是恒绿）。
- **L3 上下文预算放宽 + `_note.md` 增「核心概念」段**（2026-09-23，同一轮用户提问的 2+3 项）。
  L3 用"编译结果代替全文"分析跨文献关系，但每篇只注入 note ≤1500 / wiki ≤750 字——等于
  **L3 只能看到每篇 wiki 的 ~35%**，而 wiki 恰是"方法论批判/可复现性"这类跨文献分析最需要的部分。
  - 预算改为 `_L3_NOTE_CHARS = 1200` / `_L3_WIKI_CHARS = 1500`（两档独立，不再用 `limit//2`
    让 wiki 只拿一半）；L3 实际输入字符数（本文 + 相关篇数 + 合计）写进日志，便于核对
    "用编译结果代替全文"这个方案还成不成立。
  - L3 候选加**相关度下限** `_L3_MIN_SCORE = 0.30`（含 `_L3_MIN_KEEP = 5` 防呆闸门：过滤后
    不足 5 篇就退回原列表，宁可阈值无效也不让 L3 因"筛空"报错——该阈值**未用真实向量数据标定**，
    日志里会打命中分区间供后续校准）。混合检索（零成本回退通道）**不套用**此阈值（其分值是
    人工权重 0.3/0.35/0.4，与 cosine 不同量纲）。
  - `_render_note` 新增 `## 核心概念`（概念名 + 定义），`_extract_note_summary` 同步提取 ⇒
    L3 拿到的每篇上下文里有**定义锚点**，不再只是一串裸概念词。

- **单批上限改为"每个翻译模型各一份"，探测按钮搬进翻译模型表单并自动回填**；
  翻译池的 `.env` 改为**权威来源**（手改即生效）（2026-09-23 用户反馈"设计太绕 + 安全冗余是
  模型属性 + 手填的 glm-4.5-air 没加载"）：
  - `翻译模型 → 编辑` 里新增 **单批上限**（字符）与 **`🔬 自动测一个值`**（挨着 `测试连接`）：
    按钮测的就是**这个模型**（用表单里的凭据，**不必先保存/激活**），测完**自动填进输入框**，
    点「保存」才随条目生效（仍不静默改运行时）。结果不再落盘、不再有独立的「写入」按钮。
  - **探测请求的输出预算按线上同口径给**：缺 `max_tokens` 会落到 `build_ai` 的 64000 兜底，
    而 8K 输出的小模型（Qwen2.5-7B）收到 64000 被服务端 **400 Bad Request** 直接拒
    （实测用户点探测报的错）。现在取"该条目已存值 → 按单批上限推导"，并且
    `translate_output_budget` 的兜底不再是 64000（`0/0` ⇒ 紧凑默认 14000×0.4 = 5600）。
  - 文案精简（用户："功能描述太复杂"）：按钮改叫「自动测一个值」，说明压成两句
    （"不知道填多少？点它——真实翻译几段，把结果填进上面的框，点保存生效"）。
  - 运行时上限 = **激活条目自带的 `batch_chars`** → 全局默认 → 代码默认。`知识库` 页那个框降级为
    「默认值」（只作用于"条目没单独设"与"回落主模型"两种情况）。
  - **`.env` 权威**：`TRANSLATE_<i>_*` 按序号覆盖池里第 i 条（`_BATCH_CHARS` 为新增键）。
    旧规则"DB 有记录 ⇒ `.env` 一律不生效"会让**手改 `.env` 加翻译模型看似无效**——实测用户
    `.env` 里写了 `TRANSLATE_1=glm-4.5-air` 却始终加载不出来（且因此回落主模型翻译）。
    界面保存时仍按序号回写 `.env`（并清多余旧键）⇒ 两边恒等。
  - 翻译池塘保存**不再静默丢弃**不完整条目（缺 Base URL/模型/Key）→ 改为 **400 明说**，
    "我填了模型却没加载"的另一半根因。
- **翻译专用模型的「输出上限 max_tokens」不再由用户填写，改为按批次上限自动推导**；
  紧凑默认批次上限 **6000 → 14000 字符**（2026-09-23 用户拍板）。**主模型不受影响。**
  - **实测依据**（Qwen2.5-7B-Instruct via 硅基流动，读真实 `usage` 计数）：输出 token ≈
    源英文字符 × **0.20**（旧估"1:1"偏高约 5 倍）；输入 ≈ 源字符 × 0.24；耗时 ≈ 24 ms/输出
    token（≈41 tok/s），**与批次大小无关**（总时长正比于总输出量）。三档 6000 / 12000 / 24312
    字符全数译出；49515 字符档的 `completion_tokens` 恰好 == 请求里的 `max_tokens(8192)`
    ⇒ 截断来自**请求侧输出预算**，不是早前归因的"模型/服务端硬顶"。
  - **自动推导**：翻译池实例的预算 = `max(该模型已配值, 批次上限 × 0.4)`（`llm_service.
    translate_output_budget`，只增不减）；`设置 → 知识库` 保存批次上限后立即重建翻译池实例，
    新预算当场生效（旧行为：上限调大了、预算还卡在 8192 ⇒ 表现为难懂的截断）。
  - **表单移除** `翻译模型 → 编辑 → 输出上限 max_tokens`（改为一句话说明）：它与批次上限是
    从属关系，让用户填两个互相耦合的数是困惑来源。已存条目**沿用原值**、新建条目默认 8192。
  - 探测的"瓶颈是 max_tokens"提示同步改口径（改为建议"把批次上限调大一档再重测"，
    因为预算现在跟着上限走）。
- **`.env` 成为配置的唯一权威来源（文件优先于系统环境变量）**（2026-09-22 用户拍板）：
  程序启动时把 `.env` 里出现的键**一律覆盖** `os.environ`，**空值也覆盖**（在 `.env` 里清空某行
  ⇒ 该配置真的失效）；`.env` 里没有的键仍沿用系统环境变量；两个 `.env` 文件之间
  `APP_DATA_DIR/.env` 优先于 `backend/.env`。
  - **为什么改**：dotenv 默认 `override=False`，Windows 用户环境变量里的**旧 Key 会永远压住**
    `.env` 里的新值（实测：`SILICONFLOW_API_KEY` 系统变量是死值 401 `code 30014`、`.env` 里是新值
    200，程序读到的始终是死的 ⇒ embedding 通道与「硅基流动」供应商预设全坏），而启动自检
    `_warn_env_anomalies` 只查"完全没加载"，查不出"被遮蔽"（值非空就不报警）。
  - 启动时记录被覆盖的键名（**只报键名，绝不打印值**）：`以下环境变量被 .env 覆盖…`。
  - `SettingsService._sync_env_file`（保存供应商路径）写 `.env` 后**同时同步 `os.environ`**，
    与 `_write_env_keys` 行为统一（此前只写文件 ⇒ 保存 Key 后本次运行仍用旧值）。
  - 文档：`.env.example` 头部写明优先级与"不要设 Windows 环境变量"。
- **翻译模型池改为"单选激活"**（2026-09-22 用户决定）：池里仍可保存多个备选，
  但同一时刻只有一个生效；在 `设置 → 🤖 模型 → 翻译模型` 点圆圈激活，一个都不激活
  （或点 `不用专用模型（回落主模型）`）则回落主模型。
  **删除了"启用多个 = 并行轮询提速"**：实测它并非并行（`_run_batches` 是串行循环），
  只是把同一篇译文的各批**分发**给不同模型 ⇒ 风格/术语不一致。
  兜底：`save_translation_providers` 遇到多条 enabled 只保留第一条，其余强制关闭。
- **翻译模型编辑弹窗重做**（2026-09-23 用户要求"界面更宽，方便放交互信息和日志"）：
  - **加宽到 720px**（`.pform-modal.pform-wide`，只作用于翻译弹窗；主供应商弹窗仍是原尺寸）：
    名称/模型分两栏，Base URL、Key、单批上限各占整行；底部按钮条**固定在滚动区外**，
    再长的探测明细也不会把「保存」挤出屏幕。
  - **新增 `操作记录` 日志区**：探测（开始/逐档结果/建议值）、测试连接、保存（**以后端回读值
    为准**，不再拿表单值自说自话）每一步都带时间戳记下来，跨弹窗保留，可一键清空。
  - **单位与默认值说明收进可折叠区**（`这个数怎么读？`），不再占半屏。
  - **防"值看着丢了"**：探测期间**禁用「保存」**（避免结果回填前存进空值）；弹窗被关时结果
    会在日志里明确标注"该值**还没保存**"，并在**表格新增的 `单批上限` 列**显示
    `默认（建议 N）`，重新打开该条目自动回填。
- **主模型 Key 的 `.env` 权威**（2026-09-23 用户拍板，与翻译池同规则）：同一模型同时存在
  「`.env` 里的 Key」和「界面供应商条目里的 Key」且**不一致**时，**以 `.env` 为准**（只覆盖
  `api_key`，条目的 id/名称/端点不动），并在启动时打一行 warning 说明覆盖前后（打码）。
  - **为什么**：合并逻辑按 `(base_url, model)` 去重 ⇒ DB 条目会**整条遮蔽** `.env` 预设，
    于是"在 `.env` 里换了 Key"看起来完全没生效。实测用户实例：DB 里激活的「智谱」条目 Key
    已失效（`open.bigmodel.cn` 返回 **HTTP 401**），`.env` 的 `ZHIPU_API_KEY` 是好的
    （HTTP 200）——编译因此每 5 秒 401 一次，直到撞上防护红线。
  - 反向也成立：界面保存供应商会把 Key 写回 `.env`，两边不再分叉。

- **核心数据文件名的单一来源 + 守卫**（2026-09-23，用户决策："改名代价太大，先收敛"）：
  `library/<RID>/` 与 `knowledge_base/<RID>/` 各有一份**同名** `document.json`，此前有 7 处
  代码各自手拼这个名字 —— "写的一侧"和"读的一侧"指到不同目录就是静默的错源 bug（本轮两次报障的根子）。
  - 文件名只在 `packages/paperkb/paperkb/layout.py` 定义（`LIB_DOC_NAME` / `KB_DOC_NAME` +
    `doc_basename(kb=)` / `doc_path(dir, kb=)`）；`resource.find_doc` 增加"在哪一侧找"参数；
    所有路径构造改走常量（状态键等非路径用法须标注 `# doc-name-ok`）。
  - 新增守卫 `backend/tests/test_doc_name_source.py`：产品代码（`packages/paperkb` + `backend`）
    裸写该文件名即**测试失败**；另断言前端不得自拼知识库路径。
  - "kb 目录里已有产物却读不到定版"由**静默回退**改为 `warning`（区分"文献尚未入库→回退解析库"
    这一正常情形；bib-only 空壳不算），让同类错配第一次发生就被看见。
  - 前端 `papers.js` 不再拼 `knowledge_base/<doi 下划线>/document.json`：改传 `doi`，由后端
    `shared_doc_json`（定版优先）解析 —— 同时修掉"目录名不等于 DOI 下划线形态（RID/md5 目录）
    时指向不存在路径"的隐患。
  - 为什么要收敛而不是直接改名：改名要动 ~45 处引用 + 一条**现迁移机制不支持**的文件迁移
    （`up(conn)` 拿不到路径、备份不含 kb 目录）+ `DATA_FORMAT` 升级 + 兼容登记；而收敛之后，
    将来真要改名只需"改 1 行常量 + 一次改名脚本"。清单见 `docs/DATA-LAYOUT.md` §1.1。

### Fixed

- **中文译文里偶发 `$\mathrm{Co(O_{x}/P_{x})$核心` —— 公式少/多一个 `}`，KaTeX 渲染成红字**
  （2026-09-23 用户报障，实测只出现在中文侧、英文侧干净；用户拍板"直接按结构自动修，
  LaTeX 公式不可能混进中文文字"）：
  - **机制**：紧凑翻译模式**没有 `[[MATHn]]` 占位符回填**，段落连同公式整段交给模型，
    公式由**模型自己抄写**。它会顺手改写公式（本例：句子讲的是 Co(Ox/Px) 核心，它把
    `@P-LIG}` 删掉却忘了补回 `\mathrm{` 的收尾 `}` ⇒ 少一个 `}`），属**低频随机手滑**，
    与模型能力相关（复现实验：同一段同一模型两次，0/1 处不配平）。
  - **判据是结构性的**（可自动修的依据）：译文里的公式**逐字来自英文源文** ⇒ `$...$` 内
    花括号**必须配平**，不配平只可能是抄错。少 `}` 就在**段尾补齐**、多 `}` 就**删掉多余的
    那个**；位置由真扫描确定（转义的 `\{` / `\}` 视为字面量，不参与计数），不是拍脑袋插入。
  - 新增 `paperkb.textnorm.balance_math_braces` / `unbalanced_math_spans`（纯函数、幂等），
    接入**三处**：① 翻译写回（`translate.pipeline._apply_translations`，数据层即干净）；
    ② 源头归一（`engine_service.sanitize_document` 的段落 `text_en`/`text_zh` 与图题，
    `document.json` / `en.md` / 变体 / 检索四处一致）；③ 渲染层兜底
    （`engine_service._clean_html`，**历史数据不重译**、重渲染即修好）。
  - 为什么不做更激进的"公式回填"（用源文公式覆盖译文公式）：源文与译文里公式所处的
    上下文不同（译文可能合并/拆分句式），覆盖会引入语义错位；配平只动"确定是错的"那几处。
  - **安全闸（防误改散文）**：`$...$` 是**全文**配对，两处普通 `$` 之间若夹着 `{}`（价格写法、
    代码块）也会"不配平"。所以只对**像公式**的段动手（含 LaTeX 命令 `\xxx` 或 `^{`/`_{`），
    否则一律不碰——**宁可漏修，不可改坏散文**。真公式必有其一 ⇒ 对真实缺陷零损失。
  - **取证的坑（记下来免得再踩）**：全库扫描必须**按 JSON 解析** `document.json` 再扫。
    直接读原始 JSON 文本时，`\left\{` 在文件里是 `\\left\\{`，扫描器的"跳过转义"会把
    `\\` 当一对吃掉、把后面的 `{` 当**真括号** ⇒ 合法公式被误报（实测 pnas 篇 2 处假阳性）。
    按 JSON 解析后全库真实缺陷只剩 adma 3 处，且 `en.md` 为 0 ——**正好对上用户"英文不报错、
    只有中文报错"的观察**。顺手验证：pnas 的 `\left\{ ... \right\}.` 合法段修前修后**逐字节相同**
    （0 处改动），确认不会碰好公式。
  - 测试：`test_balance_math_braces_fixes`（含用户实测的两个真实坏形态）、
    `test_balance_math_braces_noop`（配平不动／无花括号／**转义的 `\{` 不补**）、
    `test_balance_math_braces_refuses_non_math`（不像公式的 `$...$` 一律不碰）、
    `test_balance_math_braces_nested`（`\text{...}` 嵌套不误判）、
    `test_balance_math_braces_handles_latex_linebreak`（`\\{` 里 `{` 算真括号）、幂等、
    以及数据层端到端 `test_sanitize_document_balances_math_braces`（document.json + en.md 双断言）。
  - **边界（如实说明）**：修的是"结构不配平"这类可判定的错。若模型抄出**配平但内容错**的
    公式（例如把 `x` 抄成 `y`），本机制**修不了**、也不该猜——那需要源文回填，见上条取舍。
- **探测按钮 100% 报错：`KbMetaService.probe_translate_batch() got an unexpected keyword
  argument 'tier_timeout'`**（2026-09-23 用户实测，"自动测一个值"一点就失败）。上一批把
  `tier_timeout` 加到 `paperkb.api.probe_translate_batch`、也改了服务层的调用与**单测里的假
  kbapi**，却漏了中间那层真实类 `backend/app/services/kbmeta_service.py:161`
  （仍是 `(self, llm, progress_cb=None)`）——假对象比真实类宽松，测试全绿而真实链路必炸。
  现在真实类收下并透传 `tier_timeout`；**新增两条不用假 kbapi 的守卫**
  （`test_service_works_with_real_kbmeta_probe`：走真实类、只替换最外层 paperkb 函数；
  `test_service_call_shape_binds_to_real_kbmeta_signature`：用 `inspect.signature().bind`
  把服务层的调用形状钉在真实签名上）。取证：从 `HEAD` 抽旧签名 + 同一组 kwarg ⇒
  复现 `TypeError: ... got an unexpected keyword argument 'tier_timeout'`，现行签名 bind 通过。
  ⚠️ **后端 `reload=False`，需要重启进程才生效。**
- **`sanitize_document` 丢掉"删占位符"的写回**（2026-09-23 自查发现，回溯到上一条同源改动）：
  重构时把 `<!-- image -->` 占位符的清洗结果只赋给了局部变量、**忘了 `setattr`**（段落与图题两处），
  于是 `changed` 恒为 `True` 而文件其实没变 —— `test_g5_sanitize_idempotent`（二次调用应不再改动）
  与另 3 例占位符/图片行测试当场报红。现在改为**先算完再比较**（`new != t` 才写回、才算 changed），
  `changed` 语义恢复如实。取证：4 例转绿（此前仅 2 例 MinerU 环境相关失败）；13:19 之后产出的
  `library`/`knowledge_base` 的 `document.json`/`en.md` 全量扫描 **0 处占位符残留**（无数据损伤）。
- **原文上下标只在"展示层"修是不够的——改为在源头归一**（2026-09-23 用户拍板"我在测试阶段，
  你可以修改"）：上一条把裸 `^{[34]}` 在阅读器里渲染成角标，但**文件本身**仍是裸的（Obsidian
  里一样是字面）。现在在**源头**（`document.json` 的 `text_en` / `text_zh` / 图题）归一：
  裸 `^{...}` / `_{...}` → `$^{...}$` / `$_{...}$`，与变体渲染**共用同一套规则**
  （`paperkb.textnorm.wrap_bare_scripts`：先抽 `$...$`/`$$...$$` 占位保护，只包数学环境之外的，
  避免把 `$\mathrm{Co(O_{x})}$` 撑成非法嵌套）。
  - 为什么改源头而不是只改 `en.md`：`en.md` 由该文档渲染 ⇒ 改这里让 **`en.md` / 变体 /
    `verify_kb_doc`（en.md ↔ document.json 一致性闸门，其归一化只剥 `#`/空白、**不认 `$` 差异**）
    / 检索** 四处同时一致；只改 `en.md` 会让一致性闸门全线报不一致。
  - 为什么**不需要重设解析回归基线**：归一发生在既有的**后端清洗步**
    （`sanitize_document`，与"清 `<!-- image -->` 占位符"同类），而
    `tools/parse_regression.py` 的指纹对应**引擎原始产物**（段落数/section/元数据 + en.md 引擎输出）
    ⇒ `--check` 仍全绿（实测）。
  - 覆盖面：不只引用，`cm^{-1}` / `^{\circ}` / `^{+}` 这类单位与离子电价上标一并修好。
  - 测试：`test_wrap_bare_scripts`（5 例含"数学内不撑坏"与幂等）、
    `test_sanitize_document_normalizes_superscripts`（数据层 + en.md 渲染双重断言）。
- **英文原文的引用上标在阅读器里显示成字面 `^{[34]}`**（2026-09-23 用户报"原文上下标出问题"）：
  解析产物 `en.md` 里的引用上标是**裸的** `^{[34]}`（同一份文件里另有约 33 处是 `$^{[34]}$`，
  来源本身不统一），而阅读器的渲染只把 `$...$` 交给 KaTeX ⇒ 裸的只能当普通文字显示。
  **不是本轮改动引入**：历史文件同样是裸的（实测 `10.1007_s40820-023-01133-2` 31 处、
  `10.1016_j.cej.2025.167798` 30 处，最早 09-20；本轮改动只碰变体渲染与译文写回，不写 en.md）。
  修法选**展示层**（`frontend/js/reader.js`）：在公式/围栏代码保护**之后**（此时 `$...$` 已
  换成占位符，绝不会切坏公式）把公式外的裸 `^{...}` / `_{...}` 直接按行内公式送 KaTeX；
  只认"引用 `[n]` / 纯数字符号"，不碰 `^{文字}` 与 Obsidian 内联脚注 `^[注]`。**零数据改动**，
  历史文件立刻正常。
  **为什么不动 `en.md` 本身**：它是解析产物、被 `tools/parse_regression.py` 按 sha256 打指纹，
  改它必须重跑 `--update` 重设基线（= 解析契约变更），属单独决策。
  验证：Node 实跑 `renderMarkdown` 9 例（裸上标→KaTeX、`$\mathrm{BF4^{-}}$` 不被切坏、
  代码块不动、`^{文字}`/`^[注]` 不误伤、破折号区间 `^{[12–14]}`）全过。
- **译文引用上标三种写法并存：有的带 `$`、有的直接丢失**（2026-09-23 用户报障）：
  实测 `kb/10.1016_j.cej.2025.167798` 同一件事有三种形态——① `$^{[29]}$` 规范（37 处）；
  ② `^[[38]]` 模型自造形态（6 处；Markdown/Obsidian 把 `^[文字]` 当**内联脚注**，文献编号
  被渲染成脚注、正文留着裸露的 `^[[38]]`）；③ `<sup>[21,22]</sup>` 模型保留的 HTML 标签，
  而 `_strip_html_tags` **连标签一起删** ⇒ 上标语义**直接丢失**（只剩正文方括号）。
  新增 `paperkb/textnorm.py`（纯函数）：`^[[38]]` / `^[38]` → `^{[38]}`（与英文原文同约定，
  展示层统一包 `$`）、`<sup>/<sub>` → `^{} / _{}`（先转义再清标签，不再丢语义）；
  只动"内容为数字/引用分隔符"的方括号上标，真脚注（`^[见附录]`）不碰；归一幂等。
  接入两处：**翻译写回**（`_apply_translations`，数据层即干净，检索/问答/笔记都受益）与
  **渲染层兜底**（`engine_service._clean_html`，历史数据**不必重译**、重渲染即修好）。
  **监测**：两处都记归一数量；归一后仍以 `^[` 开头的片段（真脚注或模型的新写法）单独打日志。
  测试：`packages/paperkb/tests/test_textnorm.py`（14 例，含幂等与"不误伤"）、
  `test_variants_normalize_citation_superscripts`（端到端，撤掉修复即失败）。
- **"换模板"对已入库文献等于没生效**（2026-09-23，与上一条同一病根）：
  `rerender_paper` 读**传入路径**（多为 library 那份、`text_zh` 恒空 ⇒ 渲染出**英文**）、
  且写回 **library** —— 而阅读器读 kb（`kb_service.read_file` 定版优先）⇒ 换模板后看到的
  内容根本不变。现在渲染源 = **定版**（`translation_target`：kb 优先），产物写**渲染源所在
  目录**（定版在 kb 就写 kb；`_paper_dir` 兼容 intermediate 中间态），并在写 kb 时清掉
  library 的历史变体残留（R3）。回归测试 `test_rerender_paper_uses_canonical_kb_doc`
  （去掉修复即失败）。
- **重译已入库文献后 `zh.md`/`en_zh.md` 全英文**（2026-09-23 用户实测论文[45]）：
  `combined_translate` 先调 `translate_now`（内部走 `translation_target`，**定版 = kb 优先**，
  2026-09-16 方案 A），译文写进 `kb/<DOI>/document.json`；随后却仍按传入路径 `load_document(p)`
  读 **library** 那份（`text_zh` 恒空）⇒ `render_variant` 取不到中文 ⇒ 两种变体双双退回英文原文。
  实测该篇 kb `text_zh`=41 段 / library=0；按旧源渲染 37598 字符里仅 9 个汉字（0.0%），
  按定版 kb 渲染 12076 字符里 7016 个汉字（58.1%）。现在**渲染源 = 翻译写回目标**
  （`paperkb.api.translation_target`，定位失败退回传入路径），并把 LaTeX 规范化写回**同一份**。
  只在"重译已入库文献"时暴露：首次导入时 kb 尚无该篇，canonical 落回 library，两者恰好同一份。
  回归测试 `test_combined_translate_renders_from_canonical_doc`（去掉修复即失败）。
- **编译失败会无限重试**（2026-09-23 用户实例暴露）：`Compiler.process_next` 只捕
  `CompileError`，LLM 层抛的 `DeepSeekError`/`TokenBudgetExceeded`（401、防护红线等）
  一路穿到 worker 主循环 ⇒ 任务**一直留在 queued**，每 5 秒重试一次、永不放弃：实测刷出
  36 条「engine 第 N 次（上限 12，红线）」（每次重试都消耗防护计数）。现在**一律置 failed**
  并记下错误原文（瞬时故障由 LLM 客户端内部重试吸收；真失败用户在知识库页点「重试」）。
  同时该失败路径**保留入队时的价值分**（REPLACE 语义历史坑）。
- **编译借用 `engine` 的 12 次/进程红线**（同一实例暴露）：paperkb 的
  `context="compile"` 原被折进 `engine` 桶 ⇒ 解析已吃掉 12 次后，编译第 1 次调用就被拦，
  被误判成死循环。现在编译有**独立桶 `compile`（60 次 / 200 万字符）**，并在
  `compile_now` / `compile_process` **每篇前置清零**；`engine` 的解析红线保持 12 次不动，
  失控仍由"累计输入 2M 字符"兜底。顺带修好一直失败的老测试
  `test_kbmeta_passes_real_context_for_effort`（还在用旧的"包装 base client"构造）。
- **翻译前置的防护计数清零此前从未执行**（2026-09-23 排查发现）：`translate_now` 里
  `from .llm_service import get_guard` 是**错的模块路径**（`get_guard` 只存在于 `container`），
  ImportError 又被 `except Exception: pass` 静默吞掉 ⇒ `reset_context("translate")` 一次都没跑。
  后果：翻译调用次数在进程生命周期内**跨篇累计**，长文跑到第二三篇就可能撞上
  「300 次 / 300 万字符」红线被误拦。改为 `container.get_guard()`（与 `task_service` 同源），
  并把静默 `pass` 换成 `logger.warning`（下次再错会说话）。
- **删条目后旧 `.env` 槽位会"复活"成幽灵条目**（2026-09-23 排查"保存后值对不上"时发现）：
  `_sync_translate_env` 只同步它写的那几个序号，池**缩短**时（删模型）`os.environ` 里残留的
  `TRANSLATE_<i>_*` 会被读取侧（`_env_translation_presets` 走 os.environ）当成池里第 i 条复活
  ——实测删到只剩 1 条，读回仍有 2 条，第 2 条带着**已删槽位的旧值**。现在写前先清掉
  `os.environ` 里所有 `TRANSLATE_<i>_*`，再按下标重建 ⇒ 文件与进程两边都不留残渣。

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
