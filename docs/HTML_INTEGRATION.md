# HTML 解析整合方案（Web2MD + 引擎 → 元素化 → 比对学习）

> 2026-08-22 用户新方向：利用半成品 HTML 转 md 项目（Web2MD）完成文献解析，并入现有
> 软件与学习系统；通过 **PDF 解析结果 vs 网页解析结果比对** 增强学习；重点工作 =
> HTML 元素段落标识解析 → 接入学习系统 → 训练 PDF 解析。

## 1. 现状盘点

| 组件 | 状态 | 说明 |
|---|---|---|
| Web2MD（`D:\Python\DeepSeek\Web2MD`） | 半成品 | `src/core/`：extractor（正文选择器/启发式）、converter（流水线）、math_handler（占位 math_ocr=none）、markdownify_adapter；`src/adapters/`：science_direct/generic/base；GUI 缺失（main.py import gui.app 失败）；config.yaml（站点适配器映射） |
| 引擎 `core/html_to_md.py` | T19 占位 | `T19_ENABLED=False`，接口就位未实现（用户旧决策"识别精度高暂缓"——现已反转，需启用） |
| 引擎 `llm/web_roundtrip.py` | 已实现 | HTML→翻译 prompt→译文回填（LLM 用，非结构化元素解析） |
| 依赖 | - | Web2MD requirements：beautifulsoup4/markdownify/lxml（引擎 pyproject `web` extra 已预留） |

## 2. 设计

### A. HTML 元素化解析（核心，引擎侧）
新模块 `core/html_elements.py`（启用并重写 html_to_md 占位）：
- 输入：HTML 文本（ScienceDirect/Springer/通用期刊页）
- 输出：`DocumentElements`——元素段落列表，**与 PDF document.json 的 Paragraph 同构**：
  ```json
  {"type": "title|author|affiliation|abstract|highlight|heading|body|formula|table|caption|reference|note",
   "text": "...", "level": 2, "section": "...", "seq": 12}
  ```
- 解析策略（三结合）：
  1. **DOM 语义**：`<h1-h6>`（heading/title）、`.abstract`/`section-title`（摘要）、
     `.author`/`.affiliation`、`<math>`（MathML→LaTeX）、`<table>`、`figure/figcaption`、
     `#references` 等语义类名 + 标签结构
  2. **站点适配器**：复用 Web2MD `adapters/`（science_direct 选择器等），扩展 springer 等
  3. **启发式回退**：文本密度/顺序（复用 Web2MD extractor 思路）
- 公式：MathML → LaTeX（mathml2latex 或 pandoc 桥接；math_handler 升级）
- 图片/表格：占位引用（![]() / table 标记），与 PDF 的 images 对齐

#### A2. 元素识别清单（用户需求 2026-08-22，H1 验收标准）
以下元素必须**显式标识并结构化**（type 分类 + 文本/位置）：
1. **标题**（<h1>/论文标题）+ 标题前 highlight（ScienceDirect 的 Highlights 区块）
2. **作者 / 研究机构 / DOI / 关键词**
3. **摘要**（abstract 区块）
4. **正文**（body 段落流）+ **章节标题**（heading，含层级）
5. **无关信息剔除**：结论后的贡献说明（Author contributions）、结论后的实验部分（若为
   正文重复）、版权/出版信息、导航/页脚等干扰残留
6. **图注/表注**（figcaption/table caption）+ **图片位置占位符**（HTML 无本地图 →
   按位置插入 ![](placeholder) 与 PDF images 对齐）
7. **正文上下标**（<sup>/<sub> → 保留标记，与 PDF 文本对应）
8. **正文参考文献引用**（[1]/(Author et al., year)/<a class="bibref"> → 标记）
9. **正文符号变量识别**（数学变量/希腊字母 → LaTeX 形式，**与 PDF 对应**）
10. **公式及编号**（MathML/<img class="formula"> → LaTeX + (n) 编号）；
    **公式无法识别时插入占位符**（如 `[FORMULA?]`）供比对/审查
11. **表**（<table> → 结构保留或占位）
12. **参考文献区**（#references 列表）

#### A3. 技术选型（用户建议：可换更专业的项目包，或多种并用）
- **pandoc**（用户已有现成 pandoc 代码，test_pandoc.py）：HTML→MD 兜底转换（保留结构）
- **trafilatura**（专业网页正文提取/清理库，推荐引入）：正文/元数据提取 + 干扰清洗
  （导航/页脚/相关文章等），比自研启发式更稳
- **readability-lxml**（备选）：正文提取
- **MathML→LaTeX**：pandoc 数学转换 / mathml2latex
- 策略：trafilatura（清洗+正文定位）→ DOM 语义/适配器（元素标识）→ pandoc（兜底转换）
  三结合；Web2MD core（extractor/converter/math_handler）并入复用

#### A4. 网页干扰清洗（用户重点）
- 导航/页脚/侧栏/相关文章/广告/版权/出版信息/DOI 横幅/引用工具条 → 适配器 exclude
  selectors + trafilatura 清理 + 文本特征（"This article is part of..."等模板句）
- 清洗效果进入 H1 验收（样本页残留干扰 ≤ 阈值）

### B. PDF vs HTML 比对（学习信号）
新工具 `tools/compare_parse.py`：
- 输入：同文献 PDF `document.json` + HTML `elements.json`
- 段落级对齐：归一化文本匹配（复用 n-gram 思路）+ 按 type 分类对比
- 输出差异报告（每类）：`missed`（PDF 缺）/ `extra`（PDF 多/噪声）/ `mismatched`（文本不一致）
- 典型信号：PDF 缺正文段 → 融合/骨架改进；公式乱码 → formula 规则；作者摘要混 → metadata 规则；
  标题下内容错位 → 布局/骨架改进

### C. 接入学习系统
- 语料扩展：`corpus/html/<doi>/elements.json` + `corpus/compare/<doi>/report.json`
- HTML 作为**高保真真值源**（出版商结构化页面 vs OCR）：差异 → 规则候选（learned）/
  融合改进 → scorecard 回归 → promote 固化
- 学习闭环扩展：`HTML 元素化 → 比对 → 差异分类 → 规则/骨架改进 → PDF 再解析 → 回归`

## 3. 实施拆分

| # | 任务 | 验收 |
|---|------|------|
| H1 | html_elements.py：HTML→元素化（DOM 语义+适配器+回退+trafilatura 清洗） | 满足 A2 元素清单（标题/摘要/highlight/作者/机构/DOI/关键词/正文/图注表注/占位符/上下标/引用/符号变量 LaTeX/公式编号+占位/表/参考文献）+ 干扰清洗达标 |
| H2 | Web2MD 并入（core 库化迁移，弃 GUI） | Web2MD core 可被引擎/工具 import |
| H3 | compare_parse.py：PDF/HTML 比对报告 | 同文献比对出差异分类 |
| H4 | 训练信号接入（差异→规则候选） | 差异可自动转 learned 规则候选 |
| H5 | 语料 HTML 样本 + 回归 | 语料含 HTML 真值；PDF 解析按差异改进 |

## 4. 风险
- HTML 站点结构多样（ScienceDirect 有反爬/动态渲染）——静态 HTML 优先，动态页需 Selenium（暂缓）
- MathML 转换质量——pandoc 依赖（系统级）
- 比对对齐噪声（HTML 与 PDF 文本差异）——归一化/容忍度调优
