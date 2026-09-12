# HTML 识别精度评估报告（P1）

> 2026-08 ｜ 样本：`用户提供的文献/HTML网页文献/`（10 个 HTML，含本次新增）+ 对应 PDF。
> 管线：`tools/html_pipeline.py`；9 个非 ScienceDirect 走通用 `parse_html`（DOM 语义遍历），
> cej 走 SD 双通道。评估维度：结构字段识别、正文/图/参考文献、干扰清洗、公式/引用。

## 一、逐篇结果（元素统计 = 识别质量粗信号）

| 出版社 | 文件 | 元素 | 正文段 | 图 | 参考文献 | 精度判断 |
|---|---|---|---|---|---|---|
| Elsevier(SD) | cej.2025.167798 | 51 | 51 | 6 | 41 | ✅ 正常（基准，全字段+关键词） |
| ACS | nanolett.4c05430 | 423 | 423 | 20 | 41 | ⚠️ 正文噪声大（段数虚高） |
| ACS | acsnano.3c07694 | 345 | 345 | 20 | 39 | ⚠️ 正文噪声大 |
| Wiley | adma.202407106 | 114 | 114 | 13 | **1** | ❌ 参考文献严重漏提；图虚高 |
| Springer | s40820-023-01133-2 | 5 | 5 | 2 | **0** | ❌ 几乎全空（仅导航噪声） |
| Nature | ncomms8258 | 86 | 86 | 6 | **2** | ❌ 参考文献漏提 |
| AIP | 1063_1.5004573 | 804 | 804 | 7 | 59 | ❌ 噪声极大（段数 804） |
| PNAS | pnas.2210651120 | 178 | 178 | 3 | **4** | ❌ 参考文献漏提；PDF 图 0 |
| AAAS/Science | sciadv.adh3350.html | — | — | — | — | ⚠️ **文件名与内容不符**：实为 Elsevier snb 论文（管线判定/提取正确） |
| AAAS/Science | scirobotics.abo6463 | 125 | 125 | 10 | **4** | ⚠️ 参考文献漏提 |

## 二、核心问题（通用规则失效点）

1. **metadata 全缺**：9 个非 SD 页 frontmatter 全是 `title="" doi="" keywords=[]`（通用路径
   不提取元数据）。`title`/`DOI`/`year`/`journal`/`type`/`keywords`/`authors` 均缺失。
2. **参考文献区普遍漏提**：Springers=0 / Wiley=1 / Nature=2 / PNAS=4 / scirobotics=4。
   通用 `_CLASS_TYPE_MAP` 只按 class 名 `reference/bibliography/citations` 识别，各出版社
   `<ol class="Bibliography">`、`<ul data-test="citations">` 等结构多样 → 需专用选择器。
3. **正文噪声大**：AIP(804)/ACS(400+) 段数虚高，大量非正文内容（页头/期刊信息/侧栏残留）
   未排除 → 需专用 exclude + 文本特征。
4. **Springer 近空**：正文容器被通用 exclude 选择器误删（如 `div[class*=content]` 变体），
   或结构未进入遍历 → 需专用正文定位。
5. **SD 检测已收紧**：`is_sciencedirect` 原先用宽松 `__PRELOADED_STATE__ + div#body` 兜底，
   理论上可能误判 AAAS 页；已改为仅认 `citation_publisher=elsevier`（正确性加固）。
   ⚠️ 注意：`sciadv.adh3350.html` 文件本身内容实为 snb 论文（用户命名与内容不符），非检测问题。
6. **DOI 提取**：SD 页按 `citation_doi` 主源提取（正确）；非 SD 页尚无元数据提取 → DOI
   回退文件名主干。待建通用 meta 扫描器。

## 三、通用 vs 专用规则边界（结论）

- **可通用（单规则可覆盖）**：正文段落遍历（DOM 语义 `p/div/hN` 顺序）、上下标→Unicode、
  化学式纠正、行首序号空格、公式 `$$..\tag{n}$$`。这些与出版社无关。
- **必须专用（每出版社一套）**：
  a) **metadata 提取**（title/DOI/year/journal/type/keywords/authors —— 各页 meta 标签
     结构不同）；
  b) **参考文献区定位**（各出版社容器/选择器不同）；
  c) **干扰排除**（各出版社页头/侧栏/推荐残留不同）；
  d) **站点识别 + DOI 主源**（防误判/防抓错 DOI）。
- **建议架构**：`paperparse/html/adapters/<publisher>.py`（sd 已有；新增 springer/acs/wiley/
  aip/nature/pnas/aaas），实现 `extract_metadata` + `parse_references` + `exclude`，由
  站点识别器路由；通用 `parse_html` 作兜底。

## 四、遗留格式问题（Q1-Q4，本轮落地）

- **Q1 引用（AI 约定）**：正文保留 `[n]`，文末 References 表给详情。**AI 读取约定**：
  遇正文 `[n]` → 查表 `n` 行得到该文献字符串 → 如需详情，用该字符串作为查询参数调用
  外部检索工具（Crossref/DOI 解析等）补全题录，不喂全文。正文不嵌入超链接，保持纯净。
- **Q2 编码**：正文化学式/单位 Unicode；独立公式 `$$..\tag{n}$$`；elements.json 公式双存
  （`text_en` + `latex` 字段，供 PDF 比对）。
- **Q3 frontmatter**：统一模板加 title/doi/year/journal/type/keywords；tags 仅合法单 token
  （含空格关键词移入 keywords 字段 → 根治 Obsidian tag 非法）。
- **Q4 空格**：行首序号 `(1)(a)(i)` 后补空格（`fix_list_markers`，已入 `fix_chem`）。

## 五、下一步建议

1. 建 `adapters/` 多出版社专用规则（先 Springer/AAAS/Wiley 三个最差/误判样本）。
2. 通用 `parse_html` 补 metadata 提取（meta 标签扫描，出版社无关部分）。
3. 收紧 `is_sciencedirect` + 主 DOI 提取（正确性修复）。
4. 回归 SD/cej 基准不降分。
