# 升级手册（用户视角 · 打包版）

> 面向**实际使用**：你用的是打包版（`PaperAgent.exe`），升级时**只替换程序，资产原地保留**。
> 规则与原理见 `docs/VERSIONING.md`；数据放哪见 `docs/DATA-LAYOUT.md`。

## 0. 一句话

**程序（exe）可以随便换，`data/` `knowledge_base/` `library/` 不要动。**
新程序首次启动会自动：校验数据格式 → 备份 → 迁移 → 继续用你的老数据。

## 1. 目录长什么样（打包版）

```
PaperAgent/
├─ PaperAgent.exe            ← 程序（升级时替换它 + 同目录 _internal/ 等程序文件）
├─ _internal/ ...            ← 程序自带资源（前端页、算法、引擎快照）——升级时一起替换
├─ icon.ico                  程序/托盘图标（可替换成你自己的图标）
├─ VERSION.txt               本产物的版本与构建时间（排障时先看它）
├─ data/                     ★ 你的数据（五库 + manifest.json）——**升级时保持不动**
├─ knowledge_base/           ★ 知识库（每篇笔记/卡片/原文层）——保持不动
├─ library/                  解析产物（可重建，但别删，重建要重新解析）
├─ work/  logs/              临时与日志（可随时清；`work/desktop_profile/` 是界面偏好）
└─ .env  .env.example        你的配置（API Key 等）——保持不动
```

## 1.1 平时怎么用（v1.0.0 起）

- 双击 `PaperAgent.exe`：弹出**原生窗口并自动最大化**（不是浏览器标签页，没有地址栏/标签栏）。
- 点窗口右上角 **X = 只是隐藏到托盘**（右下角通知区图标🎯），后台继续跑、正在解析/翻译的任务不中断。
- 想再打开：**单击托盘图标 → 显示主窗口**。
- **要真正退出**：**托盘图标右键 → 退出 PaperAgent**（会等正在跑的任务收尾再退出、释放端口 8900）。
  命令行等价：`PaperAgent.exe --quit`（也可用在快捷方式里）。
- 重复双击 exe 不会起两份：会自动唤起到已有窗口。

## 1.2 解析必须先配 MinerU Key（2026-09-12 起，**用户可见变更**）

- **没有 MinerU API Key 就不能解析**：免费通道与本地 pymupdf 已按"质量优先"移除，
  没有 Key 时导入 PDF 会直接被拦下并提示去填（不会再默默用低质量通道出一份不可靠的解析）。
- 在哪填：**设置中心 → 解析 → MinerU 精准解析 → API Key** → 点「保存解析设置」。
  Key 申请：<https://mineru.net/apiManage>（保存后写入 exe 同目录 `.env` 的 `MINERU_API_KEY`，**保存即生效**，无需重启）。
- 同一面板还能调**解析质量参数**（都写 `.env`，保存即生效）：
  - `解析语言`：默认「自动判定」（按 PDF 首页字符集选中/英文）；纯中文或纯英文文献可指定以提速；
  - `扫描件 OCR`：默认「自动」（无文本层的扫描 PDF 自动开启；正常 PDF 关闭更快）；
  - `表格识别`：默认开；
  - PaddleOCR 侧「跨页表格重整 / 合并表格 / 标题分级」：默认全开（论文常见的跨页表格不再被拆断）。
- 想恢复默认：把「解析语言」选回「自动判定」、「扫描件 OCR」选回「自动」，保存即可。

## 2. 升级步骤（5 步）

1. **退出程序**：**托盘图标右键 → 退出 PaperAgent**（点窗口 X 只是隐藏到托盘，不算退出！）。
   确认 `logs/paperagent.log` 不再增长、端口 8900 已释放（命令行 `PaperAgent.exe --quit` 亦可）。
2. **备份**（30 秒，强烈建议）：
   把 `data/`、`knowledge_base/`、`.env` 复制到别处（例如 `D:\PaperAgent-backup-20260912\`）。
   > 程序首次启动也会自动备份到 `data/_backups/pre-v<旧>-to-v<新>-<时间>/`，但**手动备份更稳**。
3. **替换程序文件**：解压新版，覆盖 `PaperAgent.exe` 与 `_internal/`（或新版目录整体替换），
   **不要**覆盖/删除 `data/`、`knowledge_base/`、`library/`、`.env`。
4. **首次启动**：程序自动
   - 读 `data/manifest.json` 得到 `data_format`；
   - 若数据比你上次用的版本新 → 直接拒绝启动并提示"请升级程序"（不会破坏数据）；
   - 若数据落后 → 自动备份 → 执行迁移 → 校验通过后继续；
   - 迁移失败 → 回滚，并用旧版程序仍可打开（数据未被改坏）。
5. **核对**：窗口里打开「设置 → 关于」，应看到 **应用版本 / 界面版本**（两者必须相同，不同会红字报警）
   以及解析引擎、知识库库、`data_format`；或访问 `http://127.0.0.1:8900/api/health`：
   ```json
   {"status":"ok","version":"1.0.0","data_format":2, ...}
   ```
   `version` = 新程序版本；`data_format` = 数据格式（升级后可能变大，正常）。
   再走一遍冒烟：文献库列表 → 打开一篇 → 提问一轮 → 保存 _qa。

## 3. 出问题怎么办（回退）

| 现象 | 处理 |
|---|---|
| 提示"数据由更新版本写过" | 你用旧程序打开了新数据 → 换回新程序；**不要**用旧程序继续写入 |
| 启动即报迁移失败 | 日志在 `logs/paperagent.log`；用 `data/_backups/pre-…/` 里的快照恢复（把库文件拷回 `data/<区>/`），再联系维护者 |
| 想完全回到升级前 | 关程序 → 用第 2 步的手动备份覆盖 `data/`、`knowledge_base/` → 换回旧版程序 |
| 迁移后功能异常 | **先别写数据**，保留现场（`data/` 原样）并反馈；`data/_backups/` 里有升级前快照 |

## 4. 兼容性承诺（你可以据此安排升级节奏）

- **数据只增不删**：升级不会删列/删表/删文件；知识库笔记、卡片、`_qa`、`source.pdf` 一律保留。
- **不需要重新导入 PDF**：`library/<RID>/source.pdf` 会继续被使用；解析产物缺失时用原文 PDF 兜底。
- **可回退**：迁移前自动快照；失败自动回滚。
- **只支持"上一版本数据"自动升级（N-1）**：跨越多个大版本升级时，用同一 exe 多升几次
  （每次都会自动迁移一层），或用维护者提供的离线升级工具。

## 5. 给维护者的一页（发布前自检）

```powershell
# 0) 一条命令的发布闸门（版本一致性 + 数据兼容 + 夹具迁移演练 + 打包自检 + 文档齐备；--full 连 pytest）
.venv\Scripts\python.exe tools\release_check.py --full
# 1) 构建 + 产物自检 + 打包版冒烟（窗口最大化/托盘/关窗隐藏/退出释放端口）
pwsh -File tools\build_release.ps1 -Clean            # 端口被占用时加 -SmokePort 8971
# 2) 单独冒烟（已有构建产物时）
.venv\Scripts\python.exe tools\smoke_desktop.py --exe dist\PaperAgent\PaperAgent.exe --port 8971
# 3) 发布新数据格式前：冻结当版夹具（给下一个版本做升级测试基准）
.venv\Scripts\python.exe tools\freeze_data_fixture.py --name data-v<新格式号> --rows 3
# 4) 版本号只改一处：backend/app/version.py（pyproject.toml / frontend/version.js 同步，闸门断言一致）
# 5) 打 tag：git tag v<版本>；更新 CHANGELOG.md 与本手册；清单见 docs/RELEASE-CHECKLIST.md
```
