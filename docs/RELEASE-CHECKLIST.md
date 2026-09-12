# 发布检查清单（RELEASE-CHECKLIST · v1，2026-09-12）

> 配套规范：`docs/VERSIONING.md`（版本/升级）· `docs/DATA-LAYOUT.md`（数据放哪）·
> `docs/UPGRADE.md`（**用户视角**升级手册）· `docs/COMPAT-REGISTER.md`（兼容登记）。
> 自动化闸门：`python tools/release_check.py`（`--full` 连 pytest）。**闸门不全绿 → 不发布。**

## 0. 一句话流程

```
改版号 → 跑闸门 → 构建 → 打包版冒烟（窗口/托盘/退出/数据） → 打 tag → 归档夹具 → 写交接
```

## 1. 发布前（开发机）

- [ ] `APP_VERSION`（`backend/app/version.py`）已 +1；`frontend/version.js` 同版；
      根 `pyproject.toml` 同版；`paperparse`/`paperkb` 的 `pyproject` == 模块 `__version__`。
- [ ] `CHANGELOG.md` 有本版条目，且写明**会不会丢数据 / 要不要重解析**。
- [ ] 若改了数据（新增列/表/目录语义）：`DATA_FORMAT` +1 + 新迁移（含 `NAME`/`VERSION`/`up`/`verify`）
      + 在**上一版本真实库副本**上 dry-run；`docs/COMPAT-REGISTER.md` 同步（新增 shim 必登记 + 移除条件）。
- [ ] `python tools/release_check.py --full` → 全绿（版本一致性 / 数据兼容闸门 / 登记表 /
      打包自检 / 文档齐备 / dist 自检 / pytest）。
- [ ] **公开仓库专用的泄漏闸门**：`python tools/publish_prescan.py --history` → PASS
      （工作树 + git 历史都不得有真实 API Key / 本机绝对路径 / 违禁目录）。
      - 命中工作树：跑 `python tools/sanitize_fixtures.py`（夹具脱敏，幂等）后再扫；
      - 命中**历史**：只能重写历史（`git filter-repo`），见 `.dsh-memory/HANDOFF.md`。
        实证教训（2026-09-12）：`tests/fixtures/data-v2/.../app.db` 曾带 3 个真 Key、
        `.edge-debug/`（整个 Edge profile：Cookies/History/Cache）曾被提交。
- [ ] 到期的 shim 已删或续期（登记表 §A 为空）。

## 2. 构建（`pwsh -File tools\build_release.ps1`）

- [ ] 产物 = `dist/PaperAgent/`（onedir）；`PaperAgent.exe` 带图标；`_internal/` 内含
      `frontend/`（含 `version.js`）与 `icon.ico`。
- [ ] 产物**不含** `data/` `knowledge_base/` `library/` `logs/` `work/` `用户提供的文献/`。
- [ ] `console=False`（无黑窗）、`upx=False`（UPX 破坏 pymupdf/.NET 二进制）。

## 3. 打包版冒烟（**真实链路**，必须做）

- [ ] 双击 `PaperAgent.exe`：无控制台窗口；**原生窗口启动即最大化**（`IsZoomed=true`，
      窗口矩形 ≈ 屏幕）；**看不到浏览器标签栏/地址栏**。
- [ ] 通知区出现**托盘图标**，与 exe 图标一致；右键菜单有「显示主窗口 / 在浏览器中打开 / 退出」。
- [ ] 关窗（X）：窗口隐藏、**后端继续跑**（`GET /api/health` 仍 200）；托盘「显示主窗口」能恢复。
- [ ] 托盘「退出」：**≤30s 内**端口 8900 不再监听、进程树全部退出（`Get-Process PaperAgent` 为空）。
- [ ] 重复双击 exe：不报错、不抢端口，唤起已有窗口。
- [ ] `PaperAgent.exe --quit`：运行中的实例优雅退出（自动化/快捷方式用）。
- [ ] 关于页显示：界面版本 = 应用版本 = `frontend/version.js`；引擎/知识库库版本非空；
      `data_format` 与 `data/manifest.json` 一致。
- [ ] 数据落位：`data/`（五库 + manifest）、`logs/paperagent.log`、`work/desktop_profile/`
      都在 **exe 同目录**；`.env` 放 exe 同目录即生效。

### 3.1 设置中心 + 解析门禁（2026-09-12 批1/批2 新增，逐项打勾）

- [ ] 设置中心是 **6 个 tab**：模型 / 解析 / 知识库 / 界面 / 插件 / 关于（显示与外观已合并到「界面」）。
- [ ] 「解析」tab 顶部**状态条**：已配置 MinerU Key → 绿条「MinerU 就绪（通道 mineru-v4）」；
      未配置 → 黄条「未配置 MinerU API Key：解析功能已被禁用」。
- [ ] **硬门禁实测**：把 Key 清空并保存后导入 PDF → 前端提示需要 Key（后端 400 `mineru_required`），
      **不产生解析任务**；填回 Key 保存后导入正常。
- [ ] 解析参数面板：`解析语言`（自动判定/英文/中文）、`扫描件 OCR`（自动/强制开/关）、`表格识别`；
      PaddleOCR 侧 `跨页表格重整`/`合并表格`/`标题分级` 三项默认勾选。
      改任一值 → 按钮旁出现「● 未保存」→ 保存后消失，且 **exe 同目录 `.env`** 出现对应键
      （`MINERU_LANGUAGE` / `MINERU_IS_OCR` / `MINERU_ENABLE_TABLE` / `PADDLEOCR_OPTIONS`）。
- [ ] **一次真实解析**（新参数首次上真实 MinerU 接口）：导入 1 篇英文 PDF → 解析成功，
      日志可见参数下发；再导入 1 篇中文 PDF → `.env` 的 `MINERU_LANGUAGE=auto` 时实际按 `ch` 下发。
- [ ] 设置弹窗：`Esc` 可关闭；Tab 键焦点不逃出弹窗；关闭后焦点回到「⚙ 设置」按钮。
- [ ] 「知识库」tab 只有一个「保存」（复制方式 + 检索范围），无「纳入清单/检索文件清单」勾选框；
      「解析」tab 无「待确认学习规则」卡片、无「单通道解析通道」下拉。

## 4. 升级演练（有老数据时必做，见 `docs/UPGRADE.md`）

- [ ] 老版本跑出的 `data/` 原地保留 → 只替换程序（exe + `_internal/`）→ 首启自动：
      校验 manifest → `VACUUM INTO` 备份到 `data/_backups/pre-v<旧>-to-v<新>-<ts>/` → 迁移 → `verify()`。
- [ ] 升级后 `/api/health` 的 `data_format` = 代码值；老知识库/会话/元数据全部可用。
- [ ] 回退演练：用备份覆盖库文件，老版本仍能打开（不丢资产）。

## 5. 发布后

- [ ] 打 tag `vX.Y.Z`；`docs/UPGRADE.md` 的"版本兼容矩阵"更新。
- [ ] 冻结本版黄金夹具：`python tools/freeze_data_fixture.py --name data-v<N> --rows 3`
      （下个版本的升级测试用；`tests/fixtures/data-v*` 进版本库）。
- [ ] `.dsh-memory/`：HANDOFF（基线数字/已知坑）· SHORT-TERM（本轮流水）· SUMMARY（结论 + token 账本）
      三件套落盘。
