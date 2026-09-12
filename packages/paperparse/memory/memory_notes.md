# PaperAIReader 记忆层（跨会话经验摘要）

> 本文件是记忆层跨会话摘要，供新会话快速加载；细分经验见 code_lessons.json 与 prompt_lessons.md。

## 项目核心链路（2026-08-18）
convert_pdf（本地 stitch 结构）→ build_mineru_doc（full.md 高精度 LaTeX 文本段落级替换，段落守恒）
→ apply_known_fixes（异常修复库）→ render。校验：latex_check（语法层）+ anomaly_detect（语义层→AI 子代理）。

## 关键经验速查
- **样本文献禁止全文读入对话**（D9 纪律）；测试用大小守卫（conftest read_sample_head）。
- **pytest**：用 conftest 的 tmp_work fixture，禁止 tmp_path（rmtree 被拒）。
- **PowerShell**：python 输出中文前设 PYTHONIOENCODING=utf-8；> 重定向是 UTF-16/BOM，一律用 python 写 UTF-8 文件。
- **git**：C:\Program Files\Git\cmd\git.exe；每轮改动先 add+commit，大改动先汇报方案。
- **失败 3 次即汇报用户，禁止盲目尝试；有疑问询问，禁止擅自做主。**
- **MinerU 通道**：v4batch（24h 上传链接）可用；v1 免费通道兜底；full.md 是文本主体（LaTeX），content_list 无图注无行级 bbox。

## 待办续接（最新会话）
1. #2 自我学习框架 → 完成
2. #5 M5 翻译+总结管线（llm/ 模块，自定义提示词 + 专业默认版，中英对照）
3. #4 期刊名称缺失 → 子进程 AI 按 DOI 补全
4. #3 单篇 PDF token/花费输出（子代理隔离，deepseek-chat 3.0/0.10/9.0 元/M，高峰/空闲半价）
5. #6 反复 debug 汇报纪律（贯彻全程）

## 用户关键决策（2026-08-18）
- AI 调用方式：通过 DeepSeek Harness Web GUI 交互（子代理）。
- 成本价格表：deepseek-chat 输入 3.0 元/M、缓存命中 0.10 元/M、输出 9.0 元/M；
  空闲时段（北京 9-12、14-18 之外）= 高峰价一半；用户说"更新定价策略"时联网搜索自动更新。
- M5 默认输出：中英对照（obsidian_bilingual <details> 折叠英文）。
- 实现顺序：#1→#2→#5→#4→#3。
