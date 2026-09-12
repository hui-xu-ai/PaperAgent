# -*- coding: utf-8 -*-
r"""清理译文中的「拒绝/占位」文本（2026-09-12 用户实测：66 处「（原文未提供该段内容，无法翻译。）」）。

现象与根因（已修，见 `paperkb/translate/pipeline.py` 批4）：
  待译段落清单与"给模型的全文上下文"口径不一致 ⇒ 参考文献条目/致谢/SI 这些**模型看不到原文**
  的段落也被要求翻译 ⇒ 模型回「（原文未提供该段内容，无法翻译。）」并被当作译文写进
  `document.json` 的 `text_zh`，再渲染进 `zh.md` / `en_zh.md`（实测该篇 66 处，严重污染阅读）。

本工具做三件事（**0 API 成本**）：
  1) 扫 `<root>/{library,knowledge_base}/**/document.json`，把命中拒绝/占位模式的 `text_zh` **清空**；
  2) 用同一份 document.json **重新渲染** 同目录的 `zh.md` / `en_zh.md`（本地渲染，不重新翻译）；
  3) 改动前把原文件备份到 `work/scratch/placeholder-cleanup-<时间戳>/`，并打印逐文件统计。

用法：
  .venv\Scripts\python.exe tools\clean_translation_placeholders.py --dry-run            # 只看不改
  .venv\Scripts\python.exe tools\clean_translation_placeholders.py --root <实例目录>     # 实改
  （--root 可重复多次；默认 = 项目根）

注意：清空后该段 `text_zh` 为空 ⇒ 渲染时**回退显示英文原文**（`zh` 版保留结构不丢内容）。
若你希望这些段落**整段从译文变体里消失**，请说明，我再加 `--drop-paragraphs` 开关。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "paperkb"))
sys.path.insert(0, str(ROOT / "packages" / "paperparse"))

from paperkb.translate.pipeline import is_refusal_translation  # noqa: E402


def _docs(roots: list[Path]) -> list[Path]:
    out: list[Path] = []
    for r in roots:
        for sub in ("library", "knowledge_base"):
            base = r / sub
            if base.is_dir():
                out.extend(sorted(base.rglob("document.json")))
    return out


def _clean_one(doc_p: Path, backup_root: Path | None, dry: bool) -> dict:
    """清空该 document.json 里的拒绝式译文并重渲染变体；返回统计。"""
    stat = {"doc": str(doc_p), "cleared": 0, "rendered": [], "skipped": ""}
    try:
        data = json.loads(doc_p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        stat["skipped"] = f"读取失败: {e}"
        return stat
    paras = data.get("paragraphs") or []
    hits = [p for p in paras if is_refusal_translation(p.get("text_zh") or "")]
    if not hits:
        return stat
    stat["cleared"] = len(hits)
    if dry:
        return stat
    if backup_root is not None:
        rel = doc_p.parent.name
        dst = backup_root / rel
        dst.mkdir(parents=True, exist_ok=True)
        for name in ("document.json", "zh.md", "en_zh.md"):
            src = doc_p.parent / name
            if src.exists():
                shutil.copy2(src, dst / name)
    for p in hits:
        p["text_zh"] = ""
    doc_p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    # 重渲染变体（0 API 成本）：只对**已存在**的变体文件重渲染，不新增产物
    try:
        from paperparse.core.document_builder import load_document
        from paperparse.core.markdown_render import render_variant

        doc = load_document(str(doc_p))
        for name, variant in (("zh.md", "zh"), ("en_zh.md", "translated")):
            fp = doc_p.parent / name
            if fp.exists():
                fp.write_text(render_variant(doc, variant), encoding="utf-8")
                stat["rendered"].append(name)
    except Exception as e:  # noqa: BLE001
        stat["skipped"] = f"重渲染失败（text_zh 已清空）: {e}"
    return stat


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", action="append", default=None,
                    help="要清理的实例/项目根（可重复；默认项目根）")
    ap.add_argument("--dry-run", action="store_true", help="只统计不修改")
    args = ap.parse_args()
    roots = [Path(r).resolve() for r in (args.root or [str(ROOT)])]
    docs = _docs(roots)
    print(f"扫描根：{', '.join(str(r) for r in roots)}")
    print(f"document.json 数量：{len(docs)}（{'dry-run' if args.dry_run else '实改'}）")
    backup_root = None
    if not args.dry_run:
        backup_root = ROOT / "work" / "scratch" / f"placeholder-cleanup-{time.strftime('%Y%m%d-%H%M%S')}"
        backup_root.mkdir(parents=True, exist_ok=True)
    total_files = total_paras = 0
    for d in docs:
        st = _clean_one(d, backup_root, args.dry_run)
        if st["cleared"]:
            total_files += 1
            total_paras += st["cleared"]
            print(f"  {st['cleared']:4d} 段  {d.relative_to(Path.cwd()) if d.is_relative_to(Path.cwd()) else d}"
                  f"  重渲染={st['rendered'] or '—'}" + (f"  ⚠️{st['skipped']}" if st["skipped"] else ""))
    print(f"\n合计：{total_paras} 段占位译文在 {total_files} 个文件里"
          + ("（dry-run，未改动）" if args.dry_run else f"已清空并重渲染；备份 {backup_root}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
