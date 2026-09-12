# paperparse —— PaperAgent 统一 PDF 解析算法包

PaperAgent 的 PDF 解析算法单一来源：**本地骨架权威边界 + MinerU 文本基底 +
双通道验证/仲裁 + 领域词典校正 + AI 共识扫描**。

> 2026-08-26 架构快照：HTML 识别与自我训练已剥离归档（`archive/html-20260826/`、
> `archive/learning_workspace-20260826/`）；P12 双通道块级管线退役归档
> （`archive/paperparse-core-20260826/`）。生产基底 = `process_pdf_v2`（P14 管线）。

## 一、门面与调用约定

**所有调用只经 `paperparse.api`**（主 agent / backend / 工具禁止直接 import core 内部模块）。

| 函数 | 用途 | 状态 |
|---|---|---|
| `process_pdf_v2(pdf, md_path, out_dir, paddle, ai_review, provider, ...)` | ★ P14 生产管线（推荐） | 生产 |
| `process_pdf(...)` / `convert_pdf(...)` | 老 parse 降级链（mineru/本地） | 降级保留 |
| `load_document` / `save_document` | document.json 读写 | 生产 |
| `export_package` / `export_variants` / `render_md` | 产物导出/渲染 | 生产 |
| `measure_pdf` | token/成本评估 | 工具 |
| `summarize_pdf` / `translate_pdf` / `run_m5` / `web_roundtrip` / `qna` | **翻译/总结（规划剥离到主 agent，见下）** | 待剥离 |
| `run_batch` / `selftest` / `status` / `list_tools` | 批量/自检/状态 | 工具 |

## 二、目录结构（2026-08-26 核实）

```
paperparse/
├── api.py                 # 唯一门面
├── config.py              # load_config(.env) / asset_root()（资源根=本目录）
├── core/                  # 35 模块
│   ├── p14_pipeline.py    #   P14 生产链主模块（skeleton/md_align/repair/仲裁/词典）
│   ├── consensus_fix.py   #   领域词典校正层（共识错误兜底，运行时零外部依赖）
│   ├── consensus_scan.py  #   C 层 AI 共识扫描（学习闭环样本源）
│   ├── dual_ai_review.py  #   AI 仲裁 provider（OpenAI 兼容）+ 批量仲裁
│   ├── para_verify.py     #   双通道整段验证（段首对齐守卫）
│   ├── rule_engine.py     #   规则引擎（legacy，未接生产链）
│   └── ...                #   mineru_client/paddleocr_client/markdown_render 等
├── llm/                   # ★ 翻译/总结（summarize/translate/m5combined/web_roundtrip/
│                          #   calibrate_translate/prompts/latextap/cost/measure）
│                          #   ——规划剥离到主 agent（下轮与知识库一并设计）
├── middleware/
│   ├── schema.py          # 数据契约（ArticleDocument/Paragraph/Figure/信封）
│   └── stages.py          # 老 parse 降级链（orchestrator/audit）
├── rules/                 # 内置资源（单一来源）
│   └── builtin/
│       ├── domain.json    #   领域词典（86 化学式+72 单位+36 专名，生成器产出）
│       ├── formula.json / variable.json / layout.json / spacing.json
├── templates/             # render_variant 模板（recognized/zh/summary/...）
└── tests/                 # 449 项测试（2026-08-26）
```

## 三、配置与密钥（从外界注入，包内不硬编码）

密钥全部经环境变量 / `.env` 注入（`config.load_config()` 加载，调用方提供）：

| 变量 | 用途 |
|---|---|
| `MINERU_API_KEY` | MinerU 解析（v4 高精度） |
| `PADDLEOCR_ACCESS_TOKEN` / `PADDLEOCR_BASE_URL` / `PADDLEOCR_MODEL_VERSION` | PaddleOCR-VL 辅通道 |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` | AI 仲裁/共识扫描（主） |
| `SILICONFLOW_API_KEY` / `SILICONFLOW_BASE_URL` / `SILICONFLOW_MODEL` | AI 仲裁（备用链） |

- 仲裁 provider 经 `process_pdf_v2(provider=...)` 从外界传入（backend 组装；
  未传则按环境链 `DEEPSEEK_*` → `SILICONFLOW_*` 自动创建）
- `RULES_DIR`：外部规则根（learned/user 可写）；`PAPERPARSE_PIPELINE`：管线开关
  （p14 默认；p12 已冻结回落）

## 四、P14 管线（process_pdf_v2）流程

1. M1-M3 本地骨架（PyMuPDF 行级，权威边界）→ M4 图注驱动大图提取
2. M5 mineru md ↔ 骨架对齐 → M6 拼接修复（文本取 md，跨页/图断段合并）
3. M7 双通道整段验证（verify_pair；段首对齐守卫防误配）
4. M8 字符级仲裁：确定性规则（百度准/公式等价）→ AI 批量仲裁（conf≥0.8 自动落地，
   <0.8 进复核清单）
5. P16b 领域词典校正（共识错误：双通道同错兜底；conf≥0.95 自动+audit）
6. P16c C 层 AI 共识扫描（词典外候选 → 复核 → 确认后提升 learned 词典）
7. 组装 en.md（干净版无参考文献）+ document.json + mineru_full.md（拼接修复完整版
   含参考文献，自动备份）+ qa_report.json；复核数据写 `<out_dir>/work/`

## 五、依赖

- **运行时**：numpy/scipy/pandas（pyvalem 校验 AI 建议时）、pymupdf、requests、pydantic
- **生成期**（词典构建 `tools/build_domain_dict.py`）：pyvalem/pint/periodictable
  （纯 Python）；PubChem PUG REST（在线交叉验证，可选）
- 运行时词典匹配零外部依赖（元素表固化在 domain.json）

## 六、移植与维护

- 安装：`pip install -e packages/paperparse`（开发）或打包 wheel（发布）
- 测试：`pytest packages/paperparse/tests`（449 项，2026-08-26 全绿）
- 架构快照测试：`test_architecture_snapshot.py` 锁定生产链 17 模块 + 退役模块不存在
- 新增解析能力建议路径：确定性规则（consensus_fix/domain.json）→ AI 扫描
  （consensus_scan）→ 复核确认提升 learned（学习闭环），避免直接改管线核心
- 已知规划：翻译/总结剥离到主 agent；知识库建立（en.md 作源文件）；D 项同段聚合

## 七、输出产物约定（library/<DOI>/ 自包含）

| 文件 | 说明 |
|---|---|
| `en.md` | 双通道+AI 仲裁干净版（无 frontmatter/笔记/参考文献，与 PDF 等价，知识库源文件） |
| `mineru_full.md` | MinerU 拼接修复完整版（含参考文献表，双通道审核参照，自动备份） |
| `document.json` | 单一事实源（消费方主产物） |
| `images/` / `work/` / `qa_report.json` | 图 / 复核与审计数据（review.json 等）/ QA 报告 |
