# -*- mode: python ; coding: utf-8 -*-
"""PaperAgent PyInstaller spec（onedir 模式，稳定优先）。

用法（项目根目录；spec 自动以自身位置为项目根，不再硬编码路径）：
    .\.venv\Scripts\pyinstaller.exe PaperAgent.spec --noconfirm
    推荐走 `pwsh -File tools\\build_release.ps1`（会做版本一致性检查 + 产物自检 + 打包后复制 icon）

产物：dist/PaperAgent/PaperAgent.exe
  - 双击即启动：**原生 WebView2 窗口（最大化）+ 系统托盘**（关窗=隐藏到托盘，托盘「退出」=完全关停）
  - 资源在 `_internal/`（_MEIPASS）；可写资产（data/ logs/ work/ .env）在 **exe 同目录**

资源布局（打包后，2026-08 架构统一：算法包在 packages/paperparse）：
    _MEIPASS/frontend/                      → 前端静态页（config.FRONTEND_DIR，含 version.js）
    _MEIPASS/icon.ico                       → 程序/托盘图标（**与托盘同一份文件**）
    _MEIPASS/share/paperparse_skill/*       → 算法包资源（asset_root() 的 wheel 回退路径
                                               sys.prefix(= _MEIPASS) 正好命中）
    _MEIPASS/app/plugins/obsidian_notes/*   → 内置插件（从文件系统加载，必须显式收集）
    exe同目录/.env, data/, logs/, work/     → 可写数据（APP_DATA_DIR）
禁止进包：data/ knowledge_base/ library/ work/ logs/ 用户提供的文献/（运行期生成或用户资产）
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

# 项目根 = spec 文件所在目录（**不硬编码盘符/用户名**，换机/换路径可直接打包）。
PROJECT_ROOT = Path(SPECPATH).resolve()
PACKAGE_ROOT = PROJECT_ROOT / "packages" / "paperparse"
PAPERKB_ROOT = PROJECT_ROOT / "packages" / "paperkb"
BACKEND = PROJECT_ROOT / "backend"
assert PACKAGE_ROOT.exists(), f"算法包目录不存在: {PACKAGE_ROOT}（请在项目根运行 pyinstaller）"
assert PAPERKB_ROOT.exists(), f"知识库算法包目录不存在: {PAPERKB_ROOT}"
assert (PROJECT_ROOT / "frontend" / "version.js").exists(), \
    "缺 frontend/version.js（前端版本单一来源，见 docs/VERSIONING.md §1）"

datas = [
    (str(PROJECT_ROOT / "frontend"), "frontend"),
    # 图标（_MEIPASS/icon.ico：程序图标 + 托盘图标**同一来源**；用户可直接替换 exe 同目录副本）
    (str(PROJECT_ROOT / "assets" / "icon.ico"), "."),
    # 算法包资源：包内单一来源（自包含，不依赖外部目录）
    (str(PACKAGE_ROOT / "rules"), "share/paperparse_skill/rules"),
    (str(PACKAGE_ROOT / "templates"), "share/paperparse_skill/templates"),
    (str(PACKAGE_ROOT / "prompts"), "share/paperparse_skill/prompts"),
    (str(PACKAGE_ROOT / "tools"), "share/paperparse_skill/tools"),
    (str(PACKAGE_ROOT / "memory"), "share/paperparse_skill/memory"),
    (str(PACKAGE_ROOT / "SKILL.md"), "share/paperparse_skill"),
    (str(PACKAGE_ROOT / "SKILL.advanced.md"), "share/paperparse_skill"),
    # 内置插件资源（V08：动态加载需显式收集；plugin.py 从文件系统加载，不能只靠 hiddenimports）
    (str(BACKEND / "app" / "plugins" / "obsidian_notes" / "plugin.yaml"),
     "app/plugins/obsidian_notes"),
    (str(BACKEND / "app" / "plugins" / "obsidian_notes" / "plugin.py"),
     "app/plugins/obsidian_notes"),
]

# 期刊分区/影响因子（用户 2026-09-12 拍板）：**包里直接放已解析好的 db**（首启毫秒级拷贝，
# 不再运行期解析 20s），**原始 Excel 也随包**（用户日后按同格式更新时导入即可）。
# db 由 `tools/build_reference_seed.py` 在构建期从 xlsx 生成（build/reference_seed/journals.db）。
REFERENCE_SEED_DB = PROJECT_ROOT / "build" / "reference_seed" / "journals.db"
REFERENCE_XLSX = PROJECT_ROOT / "用户提供的文献" / "影响因子和分区" / "JCR分区.xlsx"
if REFERENCE_SEED_DB.exists():
    datas.append((str(REFERENCE_SEED_DB), "share/reference"))
    print(f"INFO: 期刊分区表随包（已解析 db，{REFERENCE_SEED_DB.stat().st_size // 1024} KB）")
else:
    print("WARNING: 缺 build/reference_seed/journals.db —— 先跑 "
          "`python tools/build_reference_seed.py`（否则打包版没有 IF/分区数据，文献会停在 L1）")
if REFERENCE_XLSX.exists():
    datas.append((str(REFERENCE_XLSX), "share/reference"))   # 供用户日后更新
else:
    print(f"WARNING: 未找到原始 Excel {REFERENCE_XLSX}（不影响运行，但用户无法自行更新分区表）")

hiddenimports = [
    # 桌面壳：原生窗口（pywebview + WebView2/.NET）
    "webview",
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "clr",
    "clr_loader",
    "pythonnet",
    "proxy_tools",
    # 系统托盘（main.py/desktop.py 函数内延迟 import，静态分析漏）
    "pystray",
    "pystray._win32",
    "PIL",
    "PIL.Image",
    "PIL.ImageDraw",
    # 内置插件（动态导入，静态分析漏）
    "app.plugins.obsidian_notes.plugin",
    # 算法包（api.py 内部延迟 import 的模块，静态分析易漏）
    "paperparse",
    "paperparse.api",
    "paperparse.config",
    "paperparse.llm.client",
    "paperparse.llm.m5combined",
    "paperparse.llm.web_roundtrip",
    "paperparse.llm.summarize",
    "paperparse.llm.prompts",
    "paperparse.core.document_builder",
    "paperparse.core.markdown_render",
    "paperparse.core.latex_normalize",
    "paperparse.core.mineru_client",
    "paperparse.core.pymupdf_fallback",
    "paperparse.middleware.orchestrator",
    "paperparse.middleware.stages",
    "paperparse.middleware.schema",
    # paperkb（知识库算法包）：**editable 安装不会被 PyInstaller 自动跟随**，
    # 必须显式收集整包 + migrations 子模块（迁移 runner 用 pkgutil 动态 import）
    # —— 实测事故：漏了它，打包版启动即 `ModuleNotFoundError: No module named 'paperkb'`。
    "paperkb",
    *collect_submodules("paperkb"),
    # uvicorn 动态加载器
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]

a = Analysis(
    [str(BACKEND / "launcher.py")],  # 独立入口（绝对导入，防相对导入报错）
    pathex=[str(BACKEND), str(PACKAGE_ROOT), str(PAPERKB_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # 排除其它 GUI 后端与本机不用的重物（pywebview 支持多后端，只留 edgechromium/winforms）。
    # ⚠️ 2026-09-12 实测：venv 里装了 OCR/深度学习实验用的全家桶（torch/cv2/transformers/scipy…，
    # 共 ~600MB）——**我们的代码一行都没 import**（`work/scratch/dbg_who_imports_torch.py` 已证），
    # 却被 PyInstaller 的收集器带进产物（首次构建 842MB）。这里显式排除，产物回落到百兆级。
    # 若将来真要引入其中某个依赖：先从本列表移除，再在 `tools/release_check.py` 加断言。
    excludes=["pytest", "tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "gi",
              "cefpython3", "qtpy", "matplotlib", "notebook", "IPython",
              # —— 未使用的深度学习/OCR/科学计算栈（体积主因）——
              "torch", "torchvision", "torchaudio", "functorch", "transformers",
              "accelerate", "huggingface_hub", "safetensors", "tokenizers", "hf_xet",
              "networkx", "scipy", "cv2", "tiktoken", "sympy", "sklearn", "onnx",
              "onnxruntime", "rapidocr", "docling", "docling_core", "docling_ibm_models",
              "paddle", "paddleocr", "sentence_transformers"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PaperAgent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX 会破坏 pymupdf/.NET 二进制（实测 UPX 对 WebBrowserInterop.dll 报错），关闭
    console=False,      # 发布：无控制台窗口（日志落 logs/paperagent.log；致命错误弹 MessageBox）
    icon=str(PROJECT_ROOT / "assets" / "icon.ico"),  # exe 文件图标
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="PaperAgent",
)
