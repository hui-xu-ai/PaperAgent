# paperkb — PaperAgent 知识库算法包

卡帕西（LLM Wiki 风格）文献知识库算法包，独立于 paperparse（纯 PDF 解析）。

- 唯一门面：`paperkb.api`（backend/agent 经此调用）
- 路径不硬编码：一切根路径经 `paperkb.config.Roots` 注入
- 元数据唯一权威 = bib（papers_meta，按 DOI 查询）；document.json 的 metadata 仅兜底

## M1 功能（已实现）
- `bib_preview(path)`：WOS bib 解析 → 预览摘要（不 dump 全文）
- `bib_import(path)`：papers_meta upsert + 引用边重建 + FTS5 索引（幂等）
- `citations_for(doi)`：引用/被引列表
- `missing_dois(roots)`：扫描内容源缺元数据 DOI（library/kb/用户提供的文献）
- `wos_query(dois)`：生成 WOS 检索式（DO=(... OR ...)，>50 分批）
- `search_papers_meta(q)`：FTS5 元数据检索

## 目录
```
paperkb/
├── api.py        # ★门面
├── config.py     # Roots（路径注入）+ KbSettings
├── models.py     # PaperMeta/CitedRef/Citation/CompileJob
├── db.py         # KBStore（建表迁移/FTS5/查询）
├── bib_parser.py # WOS bib 解析（零依赖）
├── doi.py        # DOI 规范化/目录名转换
└── vector.py     # VectorIndex 抽象（初期 Noop）

tests/            # pytest（真实 savedrecs_1.bib）
```

backend 经 `app/services/kbmeta_service.py` + `app/api/kbmeta.py`（/api/kb-meta/*）暴露。
