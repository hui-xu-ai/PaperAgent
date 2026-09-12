# 文献结构识别与 AI 验证方案（P10，请审阅）

> 2026-08 ｜ 目的：建立「结构识别方法 + 通用画像模板 + AI 验证闭环」，让解析质量可量化、
> 可机器验收，你只需看画像与 AI 报告，不再逐字检查低质量文本。

## 一、取证结论（针对你 9 篇反馈）

| 问题 | 根因（已定位） |
|---|---|
| AIP 公式全错、where 说明丢失 | `_walk` 只处理标签节点、**丢弃段落内文本节点**：含 `<math>` 的段落（"where the material point..."）文本被丢，每个 `<math>` 变成独立 `$$..$$` 块。你的原始基准=行内 `$...$`+编号 `(n)`+where 紧跟（Web2MD MathHandler 正确行为） |
| 引用 `[1-5]` → `[[1⁻]]` | `fix_charges` 把数字序列 `1-5` 的 `-` 误当电荷转成 `⁻`；且无引用格式校验 |
| 单位 `cm–¹` | `fix_units` 未识别 en-dash（U+2013），只认 ASCII `-`/U+2212 |
| Springer 化学式 `$$_{4}^{-}$$` | 化学式 `<sub>`/`<sup>` 内容被当公式元素独立成块 |
| 图注全没识别 | 无「Fig. N 检索 → caption 配对」机制；caption 与 figure 未关联 |
| 头部/尾部垃圾 | 无「标题→正文」「正文→参考文献」两个清洗区间的系统规则 |
| type 时有时无 | citation_article_type 缺失即空，无正文/JSON-LD 推断 |

## 二、文献结构识别方法（算法层）

### 1. 结构识别器（新模块 `paperparse/html/structure.py`）
对元素流做**段落级分类 + 区间识别**：

- **段落分类**：`title / author / affiliation / abstract / highlight / keywords /
  heading / body / formula(block|inline) / figure / caption / table / reference /
  note / garbage`
- **公式识别细分（关键）**：
  - **块级公式**：独立段落、带编号 `(n)` 或 display 模式 → `$$...$$` + 编号右对齐
  - **内联公式**：段内 `<math>` → `$...$` **保留在段落文本流中**（不独立成行、不打断正文）
  - **段落内文本恢复**：重构 `_walk`，段落含 math 时**保留文本节点**，math 原位转内联 `$...$`
  - **where 说明**：公式后的 "where a is ..." 正文段保留，不吞不丢
- **图注配对**：全文检索 `Fig. N` / `Figure N` 字样 → 定位图注文本（figcaption/
  caption 容器）→ 与 figure 元素配对，输出 `figures[{id, image, caption}]`
- **垃圾区识别**（两个清洗区间 + 规则）：
  - **区间 A：标题→正文**：只保留 作者/机构/摘要/highlight/关键词/摘要图，
    其余（期刊名横幅、导航、DOI 链接、出版日期、订阅提示、中文 cookie、Research funding 等）全删
  - **区间 B：正文→参考文献**：保留 正文/图/表/公式/参考文献；
    删除 Conflict of Interest / Acknowledgements / Supporting Information /
    Author contributions / Funding / Data availability / 结论后的公式补充与表格
- **引用格式规范化**：所有引用统一 `[n]`（`[1-5]`、`[1,2,3]`、上标簇、`(1–6)` 全归一），
  fix_charges 增加「数字序列 `-` 不转电荷」边界

### 2. 正文渲染双通道（恢复 Web2MD 公式精度）
- **正文+公式**：Web2MD `MathHandler`（annotation/x-tex + math/tex + pandoc 批量，
  显式 utf-8）→ markdownify，保持行内/块级与文本流（= 你原始基准的格式）
- **元数据+参考文献**：元素化管线（citation_meta + 参考文献容器提取，已就绪）
- 两个通道在元素流层面合并输出

## 三、通用文献结构画像模板（JSON，每篇一个）

```json
{
  "doi": "10.1063/1.5004573", "publisher": "aip",
  "frontmatter": {"title": "…", "doi": "…", "year": "2018", "journal": "…",
                  "type": "research", "keywords": ["…"], "tags": ["文献"],
                  "title_matches_body": true},
  "modules": {
    "title":    {"present": true, "fragment": "前80字", "verified": "pass"},
    "authors":  {"present": true, "count": 3, "list": ["…"], "noise_free": true},
    "affiliations": {"present": true, "count": 1},
    "abstract": {"present": true, "fragment": "前300字", "verified": "pass"},
    "highlights": {"present": true, "count": 5},
    "keywords": {"present": true, "count": 10},
    "sections": [{"heading": "I. INTRODUCTION", "para_count": 8,
                  "sample": "前150字", "verified": "pass"}],
    "figures":  [{"fig_id": "F1", "image": "images/fig1.png",
                  "caption": "…", "caption_verified": "pass"}],
    "formulas": {"block": 12, "inline": 8, "numbered": 12,
                 "bad_latex": 0, "where_kept": 12, "verified": "pass"},
    "citations": {"format": "[n]", "non_standard": 0, "sample": "…"},
    "references": {"count": 59, "numbered": true, "first": "…", "verified": "pass"}
  },
  "garbage": {
    "before_abstract": ["…(残留清单)"], "after_conclusion": ["…"], "section_interstitial": []
  },
  "missing": [],
  "issues": [{"type": "formula_bad", "fragment": "…", "severity": "high"}],
  "ai_verification": {"model": "deepseek-chat", "checked": 12, "pass": 12,
                      "report": "…"}
}
```

## 四、AI 验证闭环（workflow 扇出便宜模型 + 评分，交付前强制）

1. **规则层（零 token）先行**：公式占位符/实体/上标引用簇/图注缺失/垃圾区/摘要重复
   —— 全部可规则检测
2. **AI 抽检层（Harness workflow 扇出，不占主线 token）**：
   - 用 **workflow 工具** 写 JS 脚本：`agent(prompt, {provider, model})` 指定便宜模型
     （如 siliconflow / deepseek-chat），把各篇的**可疑片段清单**并行发出去校验
   - 校验项：标题一致性、摘要是否像摘要、公式 LaTeX 合理性（±100 字上下文）、
     图注与图对应、引用格式 `[n]`、段落与章节语义一致、垃圾残留
   - 主线程只收汇总报告，不做逐条检查
3. **评分机制（训练数据过滤）**：
   - 每片段/每模块打 **0-100 分**（规则分 + AI 分加权）
   - 分数 < 阈值（如 60）→ 该片段/模块标记 `"trust": false`
   - **训练 PDF 解析时过滤**：`elements.json` 输出 `trust` 字段，compare_parse 学习闭环
     只采纳 trust=true 的片段，低分段记为不可信不用于训练
4. **交付门槛**：`issues` 中 high 级=0、AI 校验全 pass（或低分项已标记不可信）才交付；
   输出 `profile.json`（画像）+ `verification.json`（AI 评分报告）供你快速过目

## 五、关键修复清单（对应你的每条反馈）

| 你的反馈 | 修复 |
|---|---|
| AIP 公式全错/where 丢失 | 段落内文本恢复 + 内联 `$...$` + where 保留（MathHandler 双通道） |
| 引用 `[[1⁻]]`、格式不统一 | 引用归一 `[n]` + fix_charges 数字序列边界 + 引用校验入画像 |
| `cm–¹` 单位上标错 | fix_units 加 en-dash/其它连接符 |
| Springer 化学式 `$$_{4}^{-}$$` | 化学式上下文不入公式块；块/内联判定 |
| 图注全没识别、正文缺图 | Fig. N 检索→caption 配对→入画像 |
| 头部垃圾（Advanced Hub/Research funding/First published/DOI 链接…） | 区间 A 清洗规则 + 垃圾清单入画像 |
| 尾部垃圾（Conflict/Ack/Supporting…） | 区间 B 清洗 + 入画像 |
| PNAS 摘要错/前言前垃圾（Sign up for PNAS alerts） | 摘要定位修正 + 区间 A 规则 |
| acsnano 公式无编号/where 缺失 | 编号提取 + where 保留（同公式修复） |
| ncomms 公式缺占位 | 公式缺失 → `[FORMULA?]` 占位并标记 |
| type 不统一 | citation_article_type → 正文特征/JSON-LD 推断，恒填 |
| scirobotics 关键词 0 | AAAS 关键词 DOM 回退 |

## 六、交付物与验收方式

- 每篇：`<doi>.md` + `elements.json` + `profile.json`（画像）+ `verification.json`（AI 校验）
- 你验收 = 看 profile/verification 报告 + 抽查，不逐字读
- 10 篇全部「high 问题=0 + AI 校验 pass」才交付

## 七、需你确认的 3 个决策

1. **公式格式**：恢复你原始基准格式（**行内 `$...$`、块级 `$$...$$`+编号 `(n)`、where 紧跟**）？
   还是保持 `$$..\tag{n}$$` 统一块式？（推荐前者，与你的基准一致）
2. **AI 验证模型**：需要配置 `DEEPSEEK_API_KEY` 或 `SILICONFLOW_API_KEY`（你有哪个？
   我按可用配置接入；无密钥则 AI 校验跳过、规则层照跑）
3. **正文渲染双通道**：正文+公式走 MathHandler（原始精度），元数据/文献走元素化——
   同意此架构？（涉及 render_md 与 html_pipeline 较大重构）
