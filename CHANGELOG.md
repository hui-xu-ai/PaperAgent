# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

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
