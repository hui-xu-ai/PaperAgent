# 模板修改指南（如何调整 Markdown 输出格式）

本目录下 `*.md.j2` 是 **Jinja2 模板**，决定最终 Markdown 输出的**整体版式**（章节标题、段落、中英文排列、空行数、frontmatter、参考文献）。

渲染流程：`document.json`（结构化数据）→ `markdown_render.render(doc, template=...)` 按模板填充 → 得到 `paper.md` 及 variants。

## 当前模板

| 模板名 | 用途 |
|---|---|
| `obsidian_bilingual.md.j2` | **默认**：AI 总结 + 中英对照（中文在上、英文在下，不折叠）+ 图 + 参考文献 |
| `obsidian_bilingual_alt.md.j2` | 逐段 **EN / ZH** 交替式双语 |
| `plain.md.j2` | 纯英文 |
| `recognized.md.j2` | 英文识别原版（变体，供 `<文件名>.en.md`） |
| `summary_variant.md.j2` | AI 阅读总结版 |

## 如何选择模板 / 切换

- 默认模板用 `.env` 里的 `MD_TEMPLATE=obsidian_bilingual` 指定。
- 渲染时也可按单个文档指定：`api.render_md(document_json, template="plain")`、`CLI render-md --template plain`。
- 想让某篇用自定义样式：把自定义模板放进本目录（或 `PAPER_TEMPLATES_DIR` 指向的目录），用 `template="你命名的模板"` 引用。

## 模板语法速查（Jinja2）

- `{{ 变量 }}` —— 输出变量。如 `{{ doc.metadata.title }}`、`{{ item.text_en }}`。
- `{% 语句 %}` —— 控制结构。如 `{% if item.text_zh %}...{% endif %}`、`{% for r in doc.references %}...{% endfor %}`。
- `{%-` / `-%}` —— 去掉标签相邻的空白行（**控制行间距的关键**）。
- `items` —— 每个 `item` 有 `type`（heading/figure/para），`text`（标题文本）、`text_en`（英文）、`text_zh`（中文）、`file`（图片路径）、`section`。

## 常见调整（找对应行）

### 1) 调整段落/标题之间的行间距（空行数）
- 模板里每段输出前后用空行分隔。想**加大/减小段落间距**：
  - 减小：把 item 分支里段前的空行去掉，或用 `{%-` 收紧；例如把
    ```
    {%- elif item.text_zh %}
    <空行>
    {{ item.text_zh }}
    ```
    的段前空行删除，段落就紧挨。
  - 加大：在 `{{ item.text_en }}` / `{{ item.text_zh }}` 后多补空行。
- **中文与英文之间**默认正好一个空行（`{{ item.text_zh }}` 空行 `{{ item.text_en }}`）：改这两行之间的空行数即可。

### 2) 调整标题级别 / 标题前后空行
- 章节标题是 `## {{ item.text }}`；改成 `###` 即降一级，改 `#` 升一级。
- 标题前空行：由 item 分支开头的空行 + `{%-` 控制；想标题紧贴正文就删标题前的空行。
- 注意保持 标题 与正文 之间至少一个空行，Obsidian 才能正确解析标题层级。

### 3) 调整 frontmatter（顶部 --- 之间）
- `---` 之间的键值决定 Obsidian 属性（title/authors/year/journal/doi/keywords/tags/source/created）。增删字段在这里改。
- `tags` 由代码生成（`tags_json`），一般不动。

### 4) 调整"AI 阅读总结"块
- 文首 `> [!summary]` 区域对 `doc.ai_summary`（六段）循环输出；
- 想换成自定义版式（如用表格、或改名），改这段模板即可。

### 5) 图片与图注
- 图片 `![]({{ item.file }})`，图注在 `item.text_en`（题注也有 `text_zh`，若已翻译则中文上英文下）。
- 想图在题注后/前，调整 `figure` 与 `para` 分支的顺序（当前图先于题注）。

## 校验

修改模板后：
```
pytest tests/test_markdown_render.py          # golden 测试：模板结构变更需更新 tests/golden/obsidian_bilingual.md
```
golden 是"固定输入 → 逐字节比对"的基线；改模板后需删掉 `tests/golden/obsidian_bilingual.md` 重跑一次让它重新生成，并评审差异是否符合预期。

## 用自定义模板目录覆盖（不改本包代码）

- 在 `.env` 写 `PAPER_TEMPLATES_DIR=D:\你的自定义模板目录`；
- 把你想覆盖的模板（同名 `obsidian_bilingual.md.j2` 等）放进该目录；
- 渲染时会优先用该目录的模板（见 `config.templates_dir()`）。
