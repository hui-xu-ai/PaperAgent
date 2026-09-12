# PaperAIReader 高级模式（用户明确要求"进入高级模式/追加提问/修改配置"时才读取本文件）

> 本文件由同目录 `SKILL.md` 指示按需加载。进入高级模式后按以下说明操作，工具详情用 `help <tool>` 渐进加载。

## 引擎与命令

- Python 引擎：本 skill 包目录（本文件同目录 = `skill/`；在 skill/ 下执行 `uv run paper_reader …` 或 `.venv\Scripts\python.exe -m paper_reader …`；路径以本文件位置为准，勿硬编码绝对路径）
- **禁止运行** `study`（AI provider 为占位，必报错）；禁止读/grep 本 skill 源码。
- 所有命令加 `--json`；**所有路径传绝对路径**（相对路径在不同 pwsh 调用间会因 cwd 漂移导致 images/audit 找不到）。

## 主流程（解析）

0. **解析通道**（v2.2 监督）：默认 `mineru-v4`（高精度，需 MINERU_API_KEY，无 key 报错停止）；可询问用户后换 `mineru`（v1 免费**低精度**，无需密钥）或 `pymupdf`（本地，无 LaTeX）；云端解析失败 → **中止并反馈用户选择**（重试/换通道/本地），绝不静默降级。
1. `process_pdf --json <pdf> --parser <通道>` → 解析 + 导出 document.json（返回含 parse_source/latex_count 精度反馈）。
   - `--parse-only`：只解析导出 en.md+images+document.json（0 AI token）。
   - 翻译/总结由**知识库流水线 paperkb** 统一处理（combined_translate），不再经命令行手动翻译。

## 硬性约束

1. 文献全文**禁止读入对话**；document/result/paper.md 全文**各只允许读取一次**，禁止验证性重读（以工具返回统计为准）。
2. 公式保真：AI/翻译只出"人话"，公式由本地 `reassemble`/`latex_restore` 从源文逐字节回填，绝不人工改写（见 paperkb/translate/latextap）。
3. 失败 2 次即停下报表；命令报错立即停止并原样报告；重试 ≤1 次。
4. 每篇独立 run_id，逐篇处理，防止上下文污染。
5. `list_tools`/`help` 全流程只调一次；若 Harness 支持，调低模型思考强度（reasoning minimal/low）。

## 工具（高级模式全部可用；`list_tools` 默认只列 process_pdf，本模式用 `list_tools(scope=all)` 或 `help <tool>`）

| 工具 | 用途 | 说明 |
|---|---|---|
| `process_pdf` | 主流程入口 | 可改 parser/dpi/template/out_dir（默认 mineru/300/obsidian_bilingual/output） |
| `query` | 局部查询段落/章节 | 不返回全文，追加问答定位用 |
| `render_md` | 重渲染 | 改模板后重渲染 |
| `convert_pdf` | 底层单步解析 | process_pdf 内部使用（开发用） |
| `align` | 段落级匹配校准 | 自动版 vs 手动校准版对比报告 |
| `batch` | 批量执行 | JSONL 任务队列 → 独立子进程 |
| `status`/`audit` | 运行状态/审计 | 监督查询 |
| `help`/`list_tools` | 工具详情/索引 | 渐进加载 |

## 追加问答（默认不执行）

- 追加提问：agent 直接对话中回答（不重读全文），引用段落用 `query`。
- **默认不把问答写入总结**；仅用户明确要求时调后端 qna-writeback（/api/chat/{id}/qna-writeback，经知识库流水线写回 ai_summary）。
- **同篇论文坚持同一会话**：全文在上下文前缀 → DeepSeek 前缀缓存命中（0.1 vs 3.0 元/M，省约 30 倍）；新开会话需重读全文。
- 开发/配置/错误码/自我学习见 `DEV_GUIDE.md`。
