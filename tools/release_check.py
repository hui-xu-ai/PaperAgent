#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布闸门（GO / NO-GO）：一条命令跑完"发布前必查项"（`docs/VERSIONING.md` §7）。

查什么：
  [1] 版本一致性：后端 `APP_VERSION` · 根 `pyproject.toml` · `frontend/version.js` ·
      `paperparse`/`paperkb` 的 `pyproject` 与模块 `__version__`；`data/manifest.json` 的 `data_format`。
  [2] 数据兼容闸门：实跑 `tools/check_data_compat.py`（清单契约 + 五库结构 + 黄金夹具**真迁移** + 迁移链自检）。
  [3] 兼容登记表：`docs/COMPAT-REGISTER.md` 结构齐备；§A 待清理行的"移除条件"到期即拦。
  [4] 打包自检：`PaperAgent.spec` 的 datas **不得**含用户资产/运行期目录；关键开关（console/upx/icon）。
  [5] 文档齐备：`CHANGELOG.md` 有当前版本条目；`RELEASE-CHECKLIST.md`/`UPGRADE.md`/`DATA-LAYOUT.md` 存在。
  [6] dist 产物（若已构建）：不含 data/ knowledge_base/ library/ logs/ work/；
      含 `frontend/version.js`（且版本=APP_VERSION）与 `icon.ico`。
  [7] `--full`：再跑一次全量 `pytest`。

用法：
  python tools/release_check.py              # 快检（秒级，不含 pytest）
  python tools/release_check.py --full       # 发布前完整跑（含 pytest）
  python tools/release_check.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "packages" / "paperkb"))
sys.path.insert(0, str(ROOT / "packages" / "paperparse"))

FORBIDDEN_IN_DIST = ("data", "knowledge_base", "library", "logs", "work",
                     "用户提供的文献", "attachments")


def _pkg_version(pkg: str) -> str:
    data = tomllib.loads(
        (ROOT / "packages" / pkg / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


# ------------------------------------------------------------------ [1] 版本一致性

def check_versions() -> list[str]:
    from app.version import APP_VERSION, DATA_FORMAT, read_frontend_version

    print("[1] 版本一致性")
    bad: list[str] = []
    py = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    fe = read_frontend_version(ROOT / "frontend")
    pairs = [
        ("backend/app/version.py", APP_VERSION),
        ("pyproject.toml", str(py["project"]["version"])),
        ("frontend/version.js", fe),
        ("packages/paperparse/pyproject.toml", _pkg_version("paperparse")),
        ("packages/paperkb/pyproject.toml", _pkg_version("paperkb")),
    ]
    import paperkb
    import paperparse

    libs = [("paperparse.__version__", getattr(paperparse, "__version__", "")),
            ("paperkb.__version__", getattr(paperkb, "__version__", ""))]
    print(f"    应用版本 APP_VERSION={APP_VERSION}  DATA_FORMAT={DATA_FORMAT}")
    for name, val in pairs:
        print(f"    {'OK ' if val else '!! '}{name} = {val or '<缺>'}")
        if not val:
            bad.append(f"{name} 版本缺失")
    # 应用族四处必须同版（paperparse/paperkb 是独立库版本，只要求 pyproject == __version__）
    app_family = {"backend/app/version.py": APP_VERSION, "pyproject.toml": py["project"]["version"],
                  "frontend/version.js": fe}
    if len(set(app_family.values())) != 1:
        bad.append(f"应用族版本不一致：{app_family}")
    for name, val in libs:
        print(f"    库 {name} = {val or '<缺>'}")
    for pkg in ("paperparse", "paperkb"):
        if _pkg_version(pkg) != getattr(sys.modules[pkg], "__version__", None):
            bad.append(f"{pkg}: pyproject={_pkg_version(pkg)} ≠ __version__="
                       f"{getattr(sys.modules[pkg], '__version__', None)}")
    # 数据格式：manifest（若存在）必须与代码一致
    man_path = ROOT / "data" / "manifest.json"
    if man_path.exists():
        man = json.loads(man_path.read_text(encoding="utf-8"))
        df = int(man.get("data_format") or 0)
        print(f"    {'OK ' if df == DATA_FORMAT else '!! '}data/manifest.json "
              f"data_format={df}（代码 {DATA_FORMAT}）")
        if df != DATA_FORMAT:
            bad.append(f"data/manifest.json data_format={df} ≠ 代码 {DATA_FORMAT}")
    else:
        print("    -  data/manifest.json 不存在（未初始化数据目录）")
    return bad


# ------------------------------------------------------------------ [2] 数据兼容闸门

def check_data_compat() -> list[str]:
    print("[2] 数据兼容闸门（tools/check_data_compat.py）")
    proc = subprocess.run([sys.executable, str(ROOT / "tools" / "check_data_compat.py")],
                          cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    tail = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    for ln in tail[-8:]:
        print(f"    {ln}")
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        return [f"数据兼容闸门未通过（exit={proc.returncode}）"
                + (f"：{err[-1]}" if err else "")]
    return []


# ------------------------------------------------------------------ [3] 兼容登记表

def check_compat_register() -> list[str]:
    print("[3] 兼容登记表（docs/COMPAT-REGISTER.md）")
    path = ROOT / "docs" / "COMPAT-REGISTER.md"
    if not path.exists():
        return ["docs/COMPAT-REGISTER.md 不存在"]
    text = path.read_text(encoding="utf-8")
    bad: list[str] = []
    for section in ("## A.", "## B.", "## C.", "## D."):
        if section not in text:
            bad.append(f"登记表缺 {section} 段")
    today = date.today().isoformat()
    pending = 0
    for ln in text.splitlines():
        if not ln.startswith("|") or ln.startswith("|---") or "| # |" in ln:
            continue
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if len(cells) < 5 or cells[0] in ("—", "-", ""):
            continue
        cond = cells[3]
        if re.fullmatch(r"\D*\d{4}-\d{2}-\d{2}", cond) or "20" in cond:
            pending += 1
            m = re.search(r"(\d{4}-\d{2}-\d{2})", cond)
            if m and m.group(1) < today:
                bad.append(f"§A 待清理项「{cells[1][:60]}」移除条件已到期（{m.group(1)}）")
    print(f"    OK 结构齐备（§A 待清理行 {pending} 条）")
    return bad


# ------------------------------------------------------------------ [4] 打包自检

def check_packaging_spec() -> list[str]:
    print("[4] 打包自检（PaperAgent.spec）")
    spec_path = ROOT / "PaperAgent.spec"
    if not spec_path.exists():
        return ["PaperAgent.spec 不存在"]
    spec = spec_path.read_text(encoding="utf-8")
    bad: list[str] = []
    block = spec.split("datas = [", 1)[-1].split("\n]", 1)[0]
    for token in FORBIDDEN_IN_DIST:
        if f'"{token}"' in block or f'"{token}/"' in block:
            bad.append(f"datas 里出现禁止内容：{token}（运行期/用户资产不得进包）")
    for needle, why in (("console=False", "发布版必须无控制台窗口"),
                        ("upx=False", "UPX 会破坏 pymupdf/.NET 二进制"),
                        ("frontend", "必须打包前端静态资源"),
                        ("icon.ico", "必须打包程序/托盘图标")):
        ok = needle in spec
        print(f"    {'OK ' if ok else '!! '}spec 含 {needle}（{why}）")
        if not ok:
            bad.append(f"spec 缺少 {needle}")
    if not (ROOT / "frontend" / "version.js").exists():
        bad.append("frontend/version.js 不存在（前端版本单一来源）")
    if not (ROOT / "assets" / "icon.ico").exists():
        bad.append("assets/icon.ico 不存在（托盘图标来源）")
    print(f"    OK datas 未含 data/knowledge_base/library/logs/work/用户资产")
    return bad


# ------------------------------------------------------------------ [5] 文档齐备

def check_docs() -> list[str]:
    from app.version import APP_VERSION

    print("[5] 文档齐备")
    bad: list[str] = []
    for rel in ("CHANGELOG.md", "docs/RELEASE-CHECKLIST.md", "docs/UPGRADE.md",
                "docs/DATA-LAYOUT.md", "docs/VERSIONING.md", "docs/COMPAT-REGISTER.md"):
        ok = (ROOT / rel).exists()
        print(f"    {'OK ' if ok else '!! '}{rel}")
        if not ok:
            bad.append(f"缺文档 {rel}")
    chg = ROOT / "CHANGELOG.md"
    if chg.exists():
        text = chg.read_text(encoding="utf-8")
        has = (f"[{APP_VERSION}]" in text) or (f"v{APP_VERSION}" in text)
        print(f"    {'OK ' if has else '!! '}CHANGELOG 有 v{APP_VERSION} 条目")
        if not has:
            bad.append(f"CHANGELOG.md 缺 v{APP_VERSION} 条目")
    return bad


# ------------------------------------------------------------------ [6] dist 产物

def check_dist() -> list[str]:
    from app.version import APP_VERSION, read_frontend_version

    dist = ROOT / "dist" / "PaperAgent"
    print("[6] dist 产物自检")
    if not dist.exists():
        print("    -  dist/PaperAgent 尚未构建（构建后本项才有意义）")
        return []
    bad: list[str] = []
    top = {p.name for p in dist.iterdir() if p.is_dir()}
    leak = top & set(FORBIDDEN_IN_DIST)
    print(f"    {'OK ' if not leak else '!! '}顶层目录：{sorted(top)}")
    if leak:
        bad.append(f"产物里混入运行期/用户目录：{sorted(leak)}")
    vjs = list(dist.rglob("frontend/version.js"))
    if not vjs:
        bad.append("产物缺 frontend/version.js")
    else:
        fe = read_frontend_version(vjs[0].parent)
        print(f"    {'OK ' if fe == APP_VERSION else '!! '}产物前端版本 {fe or '<缺>'}（应用 {APP_VERSION}）")
        if fe != APP_VERSION:
            bad.append(f"产物前端版本 {fe} ≠ {APP_VERSION}")
    icos = list(dist.rglob("icon.ico"))
    print(f"    {'OK ' if icos else '!! '}icon.ico（{len(icos)} 份）")
    if not icos:
        bad.append("产物缺 icon.ico")
    # 期刊分区表：已解析 db（首启拷贝）+ 原始 Excel（用户日后更新用）——两者都必须随包
    src_db = ROOT / "build" / "reference_seed" / "journals.db"
    src_xlsx = ROOT / "用户提供的文献" / "影响因子和分区" / "JCR分区.xlsx"
    seeds_db = list(dist.rglob("share/reference/*.db"))
    seeds_xlsx = list(dist.rglob("share/reference/*.xlsx"))
    if src_db.exists():
        print(f"    {'OK ' if seeds_db else '!! '}期刊分区表 db（{len(seeds_db)} 份，首启拷进 data/reference/）")
        if not seeds_db:
            bad.append("产物缺期刊分区表 db（share/reference/journals.db）"
                       "——价值分缺 IF 档，文献会停在 L1")
    else:
        print("    -  build/reference_seed/journals.db 不存在（跑 tools/build_reference_seed.py）")
    if src_xlsx.exists():
        print(f"    {'OK ' if seeds_xlsx else '!! '}期刊分区表原始 Excel（{len(seeds_xlsx)} 份，供用户更新）")
        if not seeds_xlsx:
            bad.append("产物缺原始 Excel（share/reference/JCR分区.xlsx）——用户无法自行更新分区表")
    else:
        print("    -  源 xlsx 不存在，跳过检查")
    exes = list(dist.glob("PaperAgent.exe"))
    print(f"    {'OK ' if exes else '!! '}PaperAgent.exe")
    if not exes:
        bad.append("产物缺 PaperAgent.exe")
    return bad


# ------------------------------------------------------------------ [7] 全量测试

def check_tests() -> list[str]:
    print("[7] 全量测试（pytest -q）")
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=str(ROOT),
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    tail = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    for ln in tail[-3:]:
        print(f"    {ln}")
    if proc.returncode != 0:
        return [f"pytest 未通过（exit={proc.returncode}）"]
    return []


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="连 pytest 一起跑")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    from app.version import APP_VERSION

    print(f"=== PaperAgent 发布闸门  v{APP_VERSION} ===")
    bad: list[str] = []
    bad += check_versions()
    bad += check_data_compat()
    bad += check_compat_register()
    bad += check_packaging_spec()
    bad += check_docs()
    bad += check_dist()
    if args.full:
        bad += check_tests()

    print("\n" + "=" * 56)
    if bad:
        print(f"[FAIL] {len(bad)} 项需处理：")
        for b in bad:
            print(f"  - {b}")
    else:
        print("[PASS] 发布闸门全绿（GO）")
    if args.json:
        Path(args.json).write_text(json.dumps({"version": APP_VERSION, "problems": bad,
                                               "pass": not bad}, ensure_ascii=False,
                                              indent=2), encoding="utf-8")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
