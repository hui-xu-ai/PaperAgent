# -*- coding: utf-8 -*-
"""期刊分区 / 影响因子（JCR + CAS）**开箱即用**：首次启动把随包 db 拷进去。

用户 2026-09-12 拍板（比"运行期解析 xlsx"简单得多）：
- 包里直接放**已解析好的 `journals.db`**（`share/reference/journals.db`，构建期由
  `tools/build_reference_seed.py` 从 xlsx 生成）⇒ 首启只做一次**文件拷贝**（毫秒级，不再有 20s 解析）；
- 原始 Excel 也随包（`share/reference/JCR分区.xlsx`）⇒ 用户日后按同格式更新时，
  在「设置 → 期刊」里导入这份 xlsx 即可（运行期仍保留 xlsx 导入作为**兜底**）。

为什么必须有这份数据：价值分的 IF 档占 0.35 权重，库空 ⇒ 全库文献算不到 L2
（实测 adma：空库 2.31 → L1；有库 3.48 → L2）。

路径优先级（任一存在即用；打包版命中第 1 条）：
    1) `PROJECT_ROOT/share/reference/journals.db` / `…/JCR分区.xlsx`（_MEIPASS 内随包资源）
    2) `PAPERAGENT_REFERENCE_DB` / `PAPERAGENT_REFERENCE_XLSX` 环境变量
    3) `APP_DATA_DIR/用户提供的文献/影响因子和分区/…`（用户自己拷进安装目录）
    4) `PROJECT_ROOT/用户提供的文献/影响因子和分区/…`（开发态）
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)

SEED_DB_REL = "share/reference/journals.db"
SEED_XLSX_REL = "share/reference/JCR分区.xlsx"
USER_REL = "用户提供的文献/影响因子和分区"
XLSX_NAME = "JCR分区.xlsx"


def _candidates(project_root: Path, app_data_dir: Path, rel: str, name: str,
                env_var: str) -> list[Path]:
    out = [Path(project_root) / rel]
    env = os.environ.get(env_var, "").strip()
    if env:
        out.append(Path(env))
    out.append(Path(app_data_dir) / USER_REL / name)
    out.append(Path(project_root) / USER_REL / name)
    return out


def db_seed_candidates(project_root: Path, app_data_dir: Path) -> list[Path]:
    return _candidates(project_root, app_data_dir, SEED_DB_REL, "journals.db",
                       "PAPERAGENT_REFERENCE_DB")


def xlsx_seed_candidates(project_root: Path, app_data_dir: Path) -> list[Path]:
    return _candidates(project_root, app_data_dir, SEED_XLSX_REL, XLSX_NAME,
                       "PAPERAGENT_REFERENCE_XLSX")


def find_seed(project_root: Path, app_data_dir: Path) -> Path | None:
    """首个存在的种子（优先 db，其次 xlsx）——保留旧接口名供自检/测试使用。"""
    return next((p for p in db_seed_candidates(project_root, app_data_dir) + 
                 xlsx_seed_candidates(project_root, app_data_dir) if p.exists()), None)


def reference_stats(roots) -> dict:
    """当前期刊库行数（走 paperkb 数据层，不裸连 sqlite——CI 守卫要求）。"""
    from paperkb.journals import JournalsDB

    try:
        return JournalsDB(roots).stats()
    except Exception as e:  # noqa: BLE001 - 库还不存在等
        return {"jcr_rows": 0, "cas_rows": 0, "error": str(e)[:120]}


def _copy_db(seed: Path, dest: Path) -> None:
    """原子拷贝随包 db（先写 .tmp 再 replace，避免半截文件被当成可用库）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    for stale in (dest, Path(str(dest) + "-wal"), Path(str(dest) + "-shm")):
        try:
            stale.unlink()
        except FileNotFoundError:
            pass
    tmp = Path(str(dest) + ".tmp")
    shutil.copy2(seed, tmp)
    os.replace(tmp, dest)


def ensure_reference_seeded(roots, kbapi, project_root: Path, app_data_dir: Path,
                            allow_xlsx: bool = True) -> dict:
    """库为空 → 拷随包 db（首选，毫秒级）；没 db 则退化为导入随包 xlsx（慢，~20s）。返回报告。

    `allow_xlsx=False`：只做 db 拷贝（供启动期**同步**调用——毫秒级可接受，且保证任何请求
    进来之前分区表就位）；xlsx 兜底由调用方另起后台线程（避免 20s 阻塞开窗）。
    """
    stats = reference_stats(roots)
    jcr, cas = int(stats.get("jcr_rows") or 0), int(stats.get("cas_rows") or 0)
    if jcr or cas:
        return {"ok": True, "seeded": False, "reason": "reference 库非空，跳过",
                "jcr_rows": jcr, "cas_rows": cas}

    for db_seed in db_seed_candidates(project_root, app_data_dir):
        if not db_seed.exists():
            continue
        t0 = time.time()
        try:
            _copy_db(db_seed, Path(roots.reference_db))
        except OSError as e:
            logger.warning("随包期刊库拷贝失败（%s）→ 尝试 xlsx 兜底: %s", db_seed, e)
            break
        after = reference_stats(roots)
        dt = round(time.time() - t0, 3)
        logger.info("期刊分区表已随包就绪（拷贝 %s，%.3fs）: jcr=%s cas=%s",
                    db_seed.name, dt, after.get("jcr_rows"), after.get("cas_rows"))
        return {"ok": True, "seeded": True, "mode": "copy", "seed": str(db_seed),
                "elapsed_s": dt, "jcr_rows": int(after.get("jcr_rows") or 0),
                "cas_rows": int(after.get("cas_rows") or 0)}

    for xlsx in xlsx_seed_candidates(project_root, app_data_dir):
        if not xlsx.exists():
            continue
        if not allow_xlsx:
            return {"ok": False, "seeded": False, "mode": "xlsx_pending",
                    "reason": "只有 xlsx 种子（无随包 db）→ 交由后台导入", "seed": str(xlsx),
                    "jcr_rows": 0, "cas_rows": 0}
        t0 = time.time()
        try:                       # 兜底：运行期解析（慢，~20s；仅在随包 db 缺失时）
            r = kbapi.journals_import(str(xlsx))
        except Exception as e:  # noqa: BLE001 - 导入失败不阻塞启动
            logger.warning("期刊分区表 xlsx 兜底导入失败: %s", e)
            return {"ok": False, "seeded": False, "mode": "xlsx", "seed": str(xlsx),
                    "reason": f"导入失败: {e}", "jcr_rows": 0, "cas_rows": 0}
        dt = round(time.time() - t0, 1)
        logger.info("期刊分区表由随包 xlsx 导入（%.1fs，兜底路径）: jcr=%s cas=%s",
                    dt, r.get("jcr_total"), r.get("cas_total"))
        return {"ok": True, "seeded": True, "mode": "xlsx", "seed": str(xlsx),
                "elapsed_s": dt, "jcr_rows": int(r.get("jcr_total") or 0),
                "cas_rows": int(r.get("cas_total") or 0)}

    logger.warning("期刊分区表为空且找不到随包种子（%s / %s）——价值分将缺 IF 档"
                   "（文献会停在 L1）；可在设置→期刊 里手动导入 xlsx",
                   SEED_DB_REL, SEED_XLSX_REL)
    return {"ok": False, "seeded": False, "reason": "未找到种子文件", "jcr_rows": 0, "cas_rows": 0,
            "candidates": [str(p) for p in (db_seed_candidates(project_root, app_data_dir)
                                            + xlsx_seed_candidates(project_root, app_data_dir))]}
