# PaperAgent v1.0.0

> 本地单用户文献 AI 阅读 / 翻译 / 知识库管理应用（Windows 免装包 + 源码两种用法）。
> 本版本为**首个公开发布**。

## 下载即用（推荐）

1. 下载本页 Assets 里的 **`PaperAgent-v1.0.0-win64.zip`**（约 86MB，免装 Python）；
2. 解压到**空目录**（路径别放 C 盘只读位置）；
3. 双击 `PaperAgent.exe` → 首次启动后到「设置中心」填两个 Key：
   - `MINERU_API_KEY`（解析必需，<https://mineru.net/apiManage> 免费申请）
   - `DEEPSEEK_API_KEY`（翻译 / 编译 / 问答必需）
4. 顶栏「＋ 导入」→ `📄 PDF` → 选模式（默认「解析 + 编译」，不翻译，最省 token）。

数据、密钥、文献都在**解压目录内**（`data/ library/ knowledge_base/ .env`），升级只覆盖 `PaperAgent.exe` + `_internal/`。

## 本版亮点

- **解析**：MinerU v4 主通道 + 双通道字符仲裁（PaddleOCR 辅通道，可选），零待复核项也会显式标注；
- **识别复核**：左右对照（PDF 页图 + 高亮 / 差异点 A·B·C·D 四选一），只影响对应段落，可随时改选回滚；
- **知识库编译**：L1 笔记 → L2 章节要点 → L3 深度 wiki，按价值分（含期刊分区/IF）择优；
- **翻译**：全文双语对照 + 总结，三条路径（翻译/编译/问答）共享同一份全文前缀 ⇒ 缓存命中率高、更省 token；
- **检索问答**：FTS5 召回（笔记 / 元数据 / 引用邻域 / 卡片），非全文进上下文。

## 校验

SHA256（`PaperAgent-v1.0.0-win64.zip`）见本页 Assets 旁的说明或 Release 正文末尾。

## 已知限制

- 仅 Windows；单用户本机应用（只监听 127.0.0.1，**无鉴权**，勿暴露公网）；
- 解析依赖 MinerU 网络，无 Key 直接拒绝解析；翻译/编译/问答依赖 OpenAI 兼容 LLM；
- 双通道 AI 仲裁按调用计费，可在设置中关闭。

完整变更见仓库 `CHANGELOG.md`；使用与打包说明见 `README.md`。
