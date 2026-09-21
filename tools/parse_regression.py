# -*- coding: utf-8 -*-
"""P14 解析回归闸门（**0 API 成本**）：缓存件复算 + 产物指纹比对。

为什么（2026-09-17 T13）：
  本会话两类**破坏性** bug（替换吃到 `</sup>` 产出 `</supregnated…`、AI 建议把
  `$E_{\\mathsf F}$ stands for` 改成 `E_` 丢掉 "stands for"）都是靠手工脚本
  `work/scratch/p14_offline_rerun.py` + 逐块 diff 才抓到的。手工流程不可持续 ⇒
  固化成本工具并纳入发布闸门（`tools/build_release.ps1` [5/5] 前）。

判据（**产物指纹**，不含时间/路径等易变字段）：
  · `en.md` / `mineru_full.md` 的 sha256 与行数；
  · `document.json`：段落数、section 直方图、metadata(title/keywords/doi)；
  · `qa_report.json`：仲裁来源分布（rule/third/fallback/ai）、applied_p、third_signal 票型、
    verify verdict 计数、图数、公式自检 issue 数；
  · `review.json`：count/total + item_kind 直方图。
  ⇒ 任何"静默改文/丢内容/丢标签/决策阶梯失衡"都会改变指纹。

用法：
  & .venv\\Scripts\\python.exe tools\\parse_regression.py --check            # 全部用例
  & .venv\\Scripts\\python.exe tools\\parse_regression.py --check --case adma
  & .venv\\Scripts\\python.exe tools\\parse_regression.py --list
  & .venv\\Scripts\\python.exe tools\\parse_regression.py --update           # 重写基线（须人工 review）
退出码：0 = 通过（或缺缓存件→SKIP）；1 = 指纹不一致（回归）；2 = 环境/用例错误。

注意：本工具**默认关闭 AI**（`ai_review=False`）——判据必须与花费解耦、可重复；
AI 路径的验证走 `PARSE_AI_*` 开关下的单独实测（见 `docs/RELEASE-CHECKLIST.md`）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "paperparse"))

TOOL_DIR = Path(__file__).resolve().parent / "parse_regression"
CASES_FILE = TOOL_DIR / "cases.json"
BASE_DIR = TOOL_DIR / "baselines"
OUT_ROOT = ROOT / "work" / "parse-regression"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def fingerprint(src: Path) -> dict:
    """[全局] 产物目录 → 指纹 dict（稳定、可比、可读）。"""
    doc = json.loads((src / "document.json").read_text(encoding="utf-8"))
    qa = json.loads((src / "qa_report.json").read_text(encoding="utf-8"))
    paras = doc.get("paragraphs") or []
    secs = Counter((p.get("section") or "") for p in paras)
    meta = doc.get("metadata") or {}
    arb = qa.get("arbitration") or {}
    ts = qa.get("third_signal") or {}
    verify = qa.get("verify") or {}
    fp: dict = {
        "en_md": {"sha256": _sha(src / "en.md"),
                  "lines": len((src / "en.md").read_text(encoding="utf-8").splitlines())},
        "document": {
            "paragraphs": len(paras),
            "sections": dict(sorted((k, v) for k, v in secs.items() if k)),
            "title": meta.get("title") or "",
            "keywords": list(meta.get("keywords") or []),
            "doi": meta.get("doi"),
        },
        "arbitration": {
            "arbitrated": arb.get("arbitrated"),
            "applied_p": arb.get("applied_p"),
            "third_decided": arb.get("third_decided"),
            "machine_fallback": arb.get("machine_fallback"),
            "ref_paras": arb.get("ref_paras"),
            "ref_skipped": arb.get("ref_skipped"),
            "by_source": dict(sorted((arb.get("by_source") or {}).items())),
        },
        "third_signal": {
            "pages_usable": ts.get("pages_usable"),
            "no_paddle_pair": ts.get("no_paddle_pair"),
            "votes": dict(sorted((ts.get("votes") or {}).items())),
        },
        "verify": {k: verify.get(k) for k in ("items", "ok", "review", "suspicious")},
        "figures": len(qa.get("figures") or []),
        "formula_issues": len(((qa.get("formula_self_check") or {}).get("issues")) or []),
    }
    mf = src / "mineru_full.md"
    if mf.exists():
        fp["mineru_full_md"] = {"sha256": _sha(mf)}
    rev_path = src / "work" / "review.json"
    if rev_path.exists():
        rev = json.loads(rev_path.read_text(encoding="utf-8"))
        fp["review"] = {
            "count": rev.get("count"), "total": rev.get("total"),
            "kinds": dict(sorted(Counter(
                i.get("item_kind") for i in (rev.get("items") or [])).items())),
        }
    cc = src / "work" / "char_conflicts.json"
    if cc.exists():
        fp["conflicts"] = len((json.loads(cc.read_text(encoding="utf-8")) or {}).get("items") or [])
    return fp


def _diff(expected, actual, path: str = "") -> list:
    """[局部] 递归比对，返回人读的差异行。"""
    out: list = []
    if isinstance(expected, dict) and isinstance(actual, dict):
        for k in sorted(set(expected) | set(actual)):
            out += _diff(expected.get(k), actual.get(k), f"{path}.{k}" if path else k)
        return out
    if expected != actual:
        out.append("  %-46s 基线=%r 实际=%r" % (path, expected, actual))
    return out


def run_case(name: str, cfg: dict) -> tuple:
    """[局部] 单用例：缓存件复算 → (指纹 | None, 说明)。缺缓存件返回 (None, 'SKIP ...')。"""
    need = {k: Path(cfg[k]) for k in ("mineru_md", "paddle_blocks", "pdf")}
    missing = [str(p) for p in need.values() if not p.exists()]
    if missing:
        return None, "SKIP（缺缓存件）: " + ", ".join(missing)
    from paperparse.core.p14_pipeline import process_pdf_v2

    out = OUT_ROOT / name
    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    res = process_pdf_v2(str(need["pdf"]), md_path=str(need["mineru_md"]),
                         out_dir=str(out), paddle=True, ai_review=False,
                         paddle_blocks_path=str(need["paddle_blocks"]))
    dt = time.time() - t0
    if res.get("status") != "success":
        return None, "FAIL 解析未成功: %s" % json.dumps(res, ensure_ascii=False)[:200]
    src = Path(res["document_json"]).parent
    return fingerprint(src), "ok 用时=%.1fs 产物=%s" % (dt, src)


def main() -> int:
    ap = argparse.ArgumentParser(description="P14 解析回归闸门（0 API）")
    ap.add_argument("--check", action="store_true", help="复算并与基线比对")
    ap.add_argument("--update", action="store_true", help="复算并写基线")
    ap.add_argument("--case", default="", help="只跑某个用例（默认全部）")
    ap.add_argument("--list", action="store_true", help="列出用例与基线状态")
    args = ap.parse_args()
    if not (args.check or args.update or args.list):
        ap.print_help()
        return 2

    raw = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    cases = {k: v for k, v in raw.items() if not k.startswith("_")}   # `_note` 等注释键
    names = [args.case] if args.case else sorted(cases)
    for n in names:
        if n not in cases:
            print("未知用例:", n)
            return 2

    if args.list:
        for n in names:
            b = BASE_DIR / f"{n}.json"
            print("  %-8s 基线=%s  输入=%s" % (
                n, "有" if b.exists() else "无",
                "齐" if all(Path(cases[n][k]).exists()
                            for k in ("mineru_md", "paddle_blocks", "pdf")) else "缺"))
        return 0

    BASE_DIR.mkdir(parents=True, exist_ok=True)
    failed = 0
    for n in names:
        fp, note = run_case(n, cases[n])
        if fp is None:
            print("[%s] %s" % (n, note))
            continue
        bpath = BASE_DIR / f"{n}.json"
        if args.update or not bpath.exists():
            bpath.write_text(json.dumps(fp, ensure_ascii=False, indent=1, sort_keys=True),
                             encoding="utf-8")
            print("[%s] %s → 基线已写入 %s" % (n, note, bpath.relative_to(ROOT)))
            continue
        exp = json.loads(bpath.read_text(encoding="utf-8"))
        diffs = _diff(exp, fp)
        if diffs:
            failed += 1
            print("[%s] ✗ **回归**：%d 处指纹不一致" % (n, len(diffs)))
            for d in diffs[:40]:
                print(d)
            print("    若确为有意变更：人工 review 后 `--update` 重写基线并提交。")
        else:
            print("[%s] ✓ 通过（%s）" % (n, note))
    if failed:
        print("\n解析回归闸门未通过：%d 个用例指纹变化" % failed)
        return 1
    return 0


if __name__ == "__main__":
    # Windows 控制台默认 GBK：判据里的 ✗/✓ 会让 print 直接抛 UnicodeEncodeError，
    # 把「指纹不一致」的结论连同 diff 清单一起吞掉 —— 实测在 build_release.ps1 [5/5] 里
    # 只看到一个语焉不详的「解析回归闸门未通过」，diff 一条都没打出来。
    # 注意 `os.environ["PYTHONIOENCODING"]` 对本进程无效（stdout writer 早已建好），
    # 必须 reconfigure 才真正生效。
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 非 TTY/被重定向时忽略
            pass
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    raise SystemExit(main())
