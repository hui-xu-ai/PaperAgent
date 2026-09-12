---
name: paper-reader
description: English PDF → bilingual Markdown with AI summary and full translation, one click. Use when the user provides an English scientific PDF.
whenToUse: "用户提供英文科技 PDF 时使用。"
---

# PaperAIReader

## 默认模式（推荐）：解析 + 翻译总结
提供 PDF 路径 → `process_pdf <pdf>`（默认高精度 v4）→ 自动完成 解析 → 翻译+总结 → 导出备份包。

## 解析通道（默认 mineru-v4 高精度；询问用户后可换）
`mineru-v4` = 云端付费高精度（需 MINERU_API_KEY，**默认**）｜`mineru` = v1 免费云端**低精度**（无需密钥，仅显式选择）｜`pymupdf` = 本地（无 LaTeX）

## 解析模式
用户说"只解析" → `process_pdf <pdf> --parse-only` → 英文 markdown 全套（en.md+images+document.json），不翻译不总结。

## 高级模式
用户说"进入高级模式 / 追加提问 / 修改配置" → 读取同目录 `SKILL.advanced.md`，按其中工具操作（工具按需加载）。

## 报错与监督（立即停止，报告用户）
- 云端解析失败 → **反馈用户选择**（重试/换通道/本地），绝不静默降级；
- `mineru-v4` 未配置 MINERU_API_KEY → 报告用户配置后重试，停止执行（可显式改用低精度 `mineru` 或本地 `pymupdf`）。
