# -*- coding: utf-8 -*-
"""版本契约与「反冗余兼容」守卫（见 `docs/VERSIONING.md` §1/§4）。

这是**发布门槛**：它保证
  1) 版本号单一来源（`version.py` 与 `pyproject.toml` 不许漂移）；
  2) `data/manifest.json` 契约成立（数据更新 → fail-fast；数据更旧 → 标记需迁移）；
  3) 兼容代码不许扩散到业务层：`ALTER TABLE` 只许在数据层/迁移层，
     `sqlite3.connect` 只许在数据层（业务模块必须走统一接口）；
  4) 冻结的黄金数据夹具（`tests/fixtures/data-v*`）能被当前代码打开。
"""
from __future__ import annotations

import re
import shutil
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# —— 允许出现 `ALTER TABLE` 的模块（**只有迁移层**）——
# 2026-09-12：`db.py` 的内联 ALTER 已清零（登记表 A5 关闭）→ 白名单清空，不留"永久豁免"
ALTER_ALLOW: set[str] = set()
ALTER_ALLOW_PREFIX = ("packages/paperkb/paperkb/migrations/",)
# —— 允许出现 `sqlite3.connect` 的模块（数据层唯一入口）——
CONNECT_ALLOW = {
    "backend/app/services/store.py",
    "packages/paperkb/paperkb/db.py",
    "packages/paperkb/paperkb/journals.py",
    "packages/paperkb/paperkb/manifest.py",
    "packages/paperkb/paperkb/backup.py",
    "packages/paperlit/paperlit/db.py",
    # paperlit 的 journals.db 访问点（JournalMapper=journal_mappings 表；cleaning=读 jcr）
    "packages/paperlit/paperlit/ingest/journal_mapper.py",
    "packages/paperlit/paperlit/ingest/cleaning.py",
}
CONNECT_ALLOW_PREFIX = ("packages/paperkb/paperkb/migrations/", "packages/paperlit/paperlit/migrations/")
SCAN_EXCLUDE = ("/tests/", "/archive/", "/work/", ".venv")


def _iter_sources(bases=("backend", "packages")):
    for base in bases:
        for p in (ROOT / base).rglob("*.py"):
            rel = p.relative_to(ROOT).as_posix()
            if any(x in p.as_posix() for x in SCAN_EXCLUDE):
                continue
            yield rel, p


class TestVersionSingleSource:
    def test_app_version_matches_pyproject(self):
        from app.version import APP_VERSION

        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        assert data["project"]["version"] == APP_VERSION, \
            "pyproject.toml 与 backend/app/version.py 版本号必须一致（发布门槛）"

    def test_config_reexports_same_version(self):
        from app import config
        from app import version

        assert config.APP_VERSION == version.APP_VERSION
        assert config.DATA_FORMAT == version.DATA_FORMAT

    def test_data_format_is_positive_int(self):
        from app.version import DATA_FORMAT, MIN_READABLE_DATA_FORMAT

        assert isinstance(DATA_FORMAT, int) and DATA_FORMAT >= 1
        assert 1 <= MIN_READABLE_DATA_FORMAT <= DATA_FORMAT


class TestManifestContract:
    @staticmethod
    def _roots(tmp_path):
        from paperkb.config import Roots

        return Roots(data_dir=tmp_path / "data", library_dir=tmp_path / "library",
                     kb_dir=tmp_path / "kb").ensure()

    def test_creates_and_reads(self, tmp_path):
        from paperkb.manifest import ensure_manifest, manifest_path, read_manifest

        roots = self._roots(tmp_path)
        m = ensure_manifest(roots, app_version="9.9.9", data_format=2, layout="v1")
        assert manifest_path(roots).exists()
        assert m["data_format"] == 2 and m["needs_migration"] is False
        again = read_manifest(roots)
        assert again["app_version"] == "9.9.9"

    def test_newer_data_fails_fast(self, tmp_path):
        """数据由更新版本写过 → 必须拒绝启动（不许猜测式部分读取）"""
        import json

        from paperkb.manifest import DataFormatError, ensure_manifest

        roots = self._roots(tmp_path)
        (roots.data_dir / "manifest.json").write_text(
            json.dumps({"data_format": 99, "layout": "v9"}), encoding="utf-8")
        with pytest.raises(DataFormatError) as e:
            ensure_manifest(roots, app_version="1.0.0", data_format=1)
        assert "更新版本" in str(e.value)

    def test_older_data_flagged_for_migration(self, tmp_path):
        import json

        from paperkb.manifest import ensure_manifest

        roots = self._roots(tmp_path)
        (roots.data_dir / "manifest.json").write_text(
            json.dumps({"data_format": 1, "layout": "v1"}), encoding="utf-8")
        m = ensure_manifest(roots, app_version="1.0.0", data_format=2, min_readable=1)
        assert m["needs_migration"] is True

    def test_below_floor_is_rejected(self, tmp_path):
        import json

        from paperkb.manifest import DataFormatError, ensure_manifest

        roots = self._roots(tmp_path)
        (roots.data_dir / "manifest.json").write_text(
            json.dumps({"data_format": 1, "layout": "v1"}), encoding="utf-8")
        with pytest.raises(DataFormatError):
            ensure_manifest(roots, app_version="1.0.0", data_format=3, min_readable=3)


class TestCompatDiscipline:
    """兼容代码只许住在数据层/迁移层：业务层出现 ALTER/裸连接即视为违规。"""

    def test_alter_table_only_in_migration_layer(self):
        """扫 backend/packages **和 tools/**，且大小写/空白宽松匹配（2026-09-12 加固）。"""
        offenders = []
        for rel, p in _iter_sources(("backend", "packages", "tools")):
            if rel in ALTER_ALLOW or rel.startswith(ALTER_ALLOW_PREFIX):
                continue
            if re.search(r"(?i)\bALTER\s+TABLE\b", p.read_text(encoding="utf-8", errors="replace")):
                offenders.append(rel)
        assert not offenders, (
            f"ALTER TABLE 只允许在 {sorted(ALTER_ALLOW) or '（空）'} 或 migrations/ 内"
            f"（见 docs/VERSIONING.md §3）；违规：{offenders}")

    def test_sqlite_connect_only_in_data_layer(self):
        offenders = []
        for rel, p in _iter_sources():
            if rel in CONNECT_ALLOW or rel.startswith(CONNECT_ALLOW_PREFIX):
                continue
            if re.search(r"sqlite3\.connect\s*\(", p.read_text(encoding="utf-8", errors="replace")):
                offenders.append(rel)
        assert not offenders, (
            f"sqlite3.connect 只允许在数据层 {sorted(CONNECT_ALLOW)}（业务模块走统一接口）；"
            f"违规：{offenders}")

    def test_compat_register_exists(self):
        reg = ROOT / "docs" / "COMPAT-REGISTER.md"
        assert reg.exists(), "兼容代码必须登记（docs/VERSIONING.md §4.1）"
        text = reg.read_text(encoding="utf-8")
        for section in ("## A.", "## B.", "## C.", "## D."):
            assert section in text, f"登记表缺少 {section} 段"

    def test_removed_shims_stay_removed(self):
        """**清理回归钉**：登记表 §C 已删除的兼容分支不得再出现在代码里。

        比"含'旧'字就必须登记"这种模糊判据可靠：这里逐条钉住真实删除过的代码片段，
        谁把它加回来，CI 立刻失败（并提示去 COMPAT-REGISTER 走登记流程）。
        """
        pins = [
            ("backend/app/services/store.py", "ALTER TABLE"),
            # 钉**代码构造**而不是散文（注释里解释历史是允许的）：
            ("backend/app/services/chat_service.py", 'folder / "paper.md"'),
            # 2026-09-12 复核：缓存指纹里的旧候选名也被清掉了（旧钉只钉住 `folder / "paper.md"`，
            # 钉不住 `for cand in (...)` 这种元组写法 → 补钉字符串字面量）
            ("backend/app/services/chat_service.py", '"paper.en.md"'),
            ("backend/app/services/chat_service.py", '"paper.md"'),
            ("backend/app/services/settings_service.py", "旧扁平格式（只有"),
            ("packages/paperkb/paperkb/db.py", "_migrate_meta_pk_to_rid"),
            ("backend/app/config.py", 'PROJECT_ROOT / "data"'),
            ("backend/app/config.py", 'PROJECT_ROOT / "work"'),
        ]
        offenders = []
        for rel, needle in pins:
            p = ROOT / rel
            if not p.exists():
                continue
            if needle in p.read_text(encoding="utf-8", errors="replace"):
                offenders.append(f"{rel} 又出现了 {needle!r}")
        assert not offenders, (
            "已删除的兼容分支被加回来了（若确有必要，先在 docs/COMPAT-REGISTER.md 登记"
            f"并写明移除条件）：{offenders}")


class TestAssetsContract:
    """统一接口契约（`docs/VERSIONING.md` §6）：方法名是**版本契约**，改名即破坏兼容。"""

    FROZEN_METHODS = {
        "system": ("get_setting", "set_setting", "record_usage", "usage_recent",
                   "usage_summary", "create_task", "latest_task"),
        "chat": ("create_session", "get_session", "list_sessions", "add_message",
                 "recent_messages", "cached_answer", "cache_answer"),
        "biblio": ("list_papers", "get_paper", "find_paper_by_md5", "get_meta",
                   "upsert_meta", "shared_doc_json", "compile_queue"),
        "reference": ("lookup", "stats"),
        "content": ("kb_dir", "library_dir", "assert_safe_to_clear"),
    }

    def test_facade_exposes_stable_methods(self):
        from app.services.assets import Assets

        assert hasattr(Assets, "data_format") and hasattr(Assets, "describe")
        for group, names in self.FROZEN_METHODS.items():
            cls = {"system": "_SystemAssets", "chat": "_ChatAssets",
                   "biblio": "_BiblioAssets", "reference": "_ReferenceAssets",
                   "content": "_ContentAssets"}[group]
            mod = __import__("app.services.assets", fromlist=[cls])
            target = getattr(mod, cls)
            for n in names:
                assert hasattr(target, n) or hasattr(target, n.lstrip("_")), \
                    f"assets.{group}.{n} 缺失（方法名是契约，改名需走 major + 登记表）"

    def test_container_exposes_facade(self):
        from app.services import container

        assert hasattr(container, "get_assets"), "业务层唯一入口 container.get_assets() 必须存在"


class TestAssetPathIsolation:
    """打包版契约（用户真实工作流）：升级只换程序、资产原地保留。

    因此**所有可写资产路径**必须走 `APP_DATA_DIR`（打包后 = exe 同目录）；
    用 `PROJECT_ROOT`（打包后 = _MEIPASS 只读临时目录）会让资产落在临时目录里丢失。
    """

    def test_settings_asset_paths_use_app_data_dir(self):
        text = (ROOT / "backend" / "app" / "config.py").read_text(encoding="utf-8")
        lines = text[text.index("class Settings"):].splitlines()

        def _stmt(key: str) -> str:
            """取该字段的完整声明（跨行拼接：括号未闭合就继续接）"""
            for i, ln in enumerate(lines):
                if not ln.strip().startswith(key + ":"):
                    continue
                buf: list[str] = []
                depth = 0
                for cur in lines[i:]:
                    buf.append(cur.strip())
                    depth += cur.count("(") - cur.count(")")
                    if depth <= 0:
                        break
                return " ".join(buf)
            return ""

        for key in ("db_path", "engine_work_root", "engine_out_root", "engine_input_root",
                    "dual_work_root"):
            stmt = _stmt(key)
            assert stmt, f"Settings 里找不到 {key}"
            assert "APP_DATA_DIR" in stmt, \
                f"{key} 必须基于 APP_DATA_DIR（打包后可写资产根）；当前：{stmt}"
            assert "PROJECT_ROOT" not in stmt, \
                f"{key} 不得基于 PROJECT_ROOT（打包后是只读临时目录）；当前：{stmt}"

    def test_dev_paths_equal_app_data_dir(self):
        """开发模式下 APP_DATA_DIR == PROJECT_ROOT，两条路径语义一致（不改变现状）"""
        from app import config

        assert config.APP_DATA_DIR == config.PROJECT_ROOT


class TestMigrationLayer:
    def test_only_migrations_dir_has_alter(self):
        """内联 ALTER 已收编进迁移层（登记表 A3/A4 完成）；store.py 必须保持干净"""
        store = (ROOT / "backend" / "app" / "services" / "store.py").read_text(encoding="utf-8")
        assert "ALTER TABLE" not in store, "store.py 不得再有内联 ALTER（应由迁移层处理）"

    def test_baseline_migration_is_idempotent_and_adds_missing_columns(self, tmp_path):
        import sqlite3

        from paperkb.migrations import available, run_migrations

        mods = available()
        assert [m.VERSION for m in mods] == sorted(m.VERSION for m in mods)
        base = next(m for m in mods if m.VERSION == 1)

        db = tmp_path / "old.db"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE sessions (id INTEGER PRIMARY KEY, paper_id INTEGER NOT NULL);
            INSERT INTO sessions(id, paper_id) VALUES(1, 7);
        """)
        conn.commit()
        base.up(conn)
        cols = {r[1] for r in conn.execute("PRAGMA main.table_info(sessions)")}
        assert {"kind", "mode", "title"} <= cols
        assert base.verify(conn) == []
        base.up(conn)                      # 幂等：再来一次不报错
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        conn.close()

    def test_run_migrations_noop_when_current(self, tmp_path):
        from paperkb.config import Roots
        from paperkb.manifest import ensure_manifest
        from paperkb.migrations import run_migrations

        roots = Roots(data_dir=tmp_path / "data", library_dir=tmp_path / "library",
                      kb_dir=tmp_path / "kb").ensure()
        ensure_manifest(roots, app_version="1.0.0", data_format=1, layout="v1")
        rep = run_migrations(roots, target_format=1)
        assert rep["needs_migration"] is False and rep["applied"] == []

    def test_backup_creates_snapshot(self, tmp_path):
        import sqlite3

        from paperkb.backup import backup_databases
        from paperkb.config import Roots

        roots = Roots(data_dir=tmp_path / "data", library_dir=tmp_path / "library",
                      kb_dir=tmp_path / "kb").ensure()
        conn = sqlite3.connect(roots.system_db)
        conn.execute("CREATE TABLE t(x INTEGER)")
        conn.execute("INSERT INTO t VALUES(1)")
        conn.commit()
        conn.close()
        out = backup_databases(roots, tag="pre-test")
        assert out and any((tmp_path / "data" / "_backups").rglob("system-app.db"))


class TestGoldenFixture:
    def test_frozen_fixtures_open_and_migrate_with_current_code(self, tmp_path):
        """黄金夹具（历史格式快照）必须能被当前代码**打开并升级到当前格式**。

        这是"升级不影响旧数据 + 不靠读时兼容"的核心回归：夹具是 data_format=1 的真实快照，
        当前代码 data_format=2 ⇒ 必须能跑完迁移链并通过 verify()。
        """
        import sqlite3

        from app.version import APP_VERSION, DATA_FORMAT, LAYOUT_VERSION
        from paperkb.config import Roots
        from paperkb.manifest import ensure_manifest, read_manifest
        from paperkb.migrations import available, run_migrations

        dirs = sorted((ROOT / "tests" / "fixtures").glob("data-v*"))
        if not dirs:
            pytest.skip("尚未冻结黄金夹具：跑 tools/freeze_data_fixture.py")
        for d in dirs:
            work = tmp_path / d.name
            shutil.copytree(d / "data", work)
            roots = Roots(data_dir=work, library_dir=tmp_path / "library",
                          kb_dir=tmp_path / "kb").ensure()
            before = read_manifest(roots) or {}
            man = ensure_manifest(roots, app_version=APP_VERSION,
                                  data_format=DATA_FORMAT, layout=LAYOUT_VERSION)
            assert man["data_format"] <= DATA_FORMAT
            if man.get("needs_migration"):
                rep = run_migrations(roots, target_format=DATA_FORMAT,
                                     dry_run=(int(before.get("data_format") or 0) > DATA_FORMAT))
                assert rep["applied"], "夹具落后于当前格式时必须有迁移被执行"
                assert rep["backup"], "迁移前必须产生备份（VACUUM INTO 快照）"
                # 迁移后逐条 verify（迁移自己会做，这里再对成品库抽查关键结构）
                conn = sqlite3.connect(str(roots.biblio_db))
                try:
                    cols = {r[1] for r in conn.execute("PRAGMA main.table_info(papers_meta)")}
                    assert "rid" in cols and "corresponding_json" in cols
                finally:
                    conn.close()
                assert read_manifest(roots)["data_format"] == DATA_FORMAT
            else:
                assert man["data_format"] == DATA_FORMAT
        # 迁移链本身：每条迁移的 VERSION 不得高于 DATA_FORMAT
        assert all(m.VERSION <= DATA_FORMAT for m in available())


class TestMigrationLedger:
    """同格式漏跑自愈 + 台账 + 备份闸门（2026-09-12 发布轮修复）。

    背景：`data_format` 是**格式**不是**步数**（0002/0003 同为 VERSION=2）。
    旧选步 `cur < VERSION <= target` 会永久跳过"数据已是格式 2、但当时只跑了 0002"的步。
    """

    def test_same_format_missed_migration_is_self_healed(self, tmp_path):
        import sqlite3

        from paperkb.config import Roots
        from paperkb.manifest import ensure_manifest
        from paperkb.migrations import run_migrations

        roots = Roots(data_dir=tmp_path / "data", library_dir=tmp_path / "library",
                      kb_dir=tmp_path / "kb").ensure()
        # 造：数据自称已是格式 2，但 papers_meta 仍是 doi 主键（= 0003 从未生效）
        con = sqlite3.connect(roots.biblio_db)
        con.execute("CREATE TABLE papers_meta (doi TEXT PRIMARY KEY, title TEXT DEFAULT '', "
                    "year TEXT DEFAULT '', wos_id TEXT DEFAULT '')")
        con.execute("INSERT INTO papers_meta(doi,title) VALUES(?,?)",
                    ("10.1000/self-heal.1", "老行"))
        con.commit()
        con.close()
        ensure_manifest(roots, app_version="1.0.0", data_format=2, layout="v1")

        rep = run_migrations(roots, target_format=2)
        assert "0003_meta_pk_rid" in rep["applied"], f"同格式漏跑的步必须补跑：{rep}"
        assert rep["backup"], "补跑前同样必须先备份"
        con = sqlite3.connect(roots.biblio_db)
        try:
            cols = {r[1] for r in con.execute("PRAGMA main.table_info(papers_meta)")}
            assert "rid" in cols
        finally:
            con.close()
        # 台账住 **system 库**（迁移 runner 的主连接；库内自证）
        con = sqlite3.connect(roots.system_db)
        try:
            names = {r[0] for r in con.execute("SELECT name FROM main.migrations_applied")}
            assert "0003_meta_pk_rid" in names, "执行过的迁移必须写进台账（库内自证）"
        finally:
            con.close()
        # 幂等：再跑一次无事发生（台账 + verify 双重兜底）
        rep2 = run_migrations(roots, target_format=2)
        assert rep2["applied"] == [] and rep2["needs_migration"] is False, rep2

    def test_every_migration_declares_name_and_verify(self):
        """迁移必须能**自证**（NAME + verify）：这是"同格式漏跑"能被发现的前提。"""
        from paperkb.migrations import available

        for m in available():
            assert getattr(m, "NAME", ""), f"{m.__name__} 缺 NAME（台账按名字登记）"
            assert callable(getattr(m, "verify", None)), \
                f"{m.__name__} 缺 verify()（无法自证是否已生效）"

    def test_backup_failure_aborts_migration(self, tmp_path, monkeypatch):
        """备份失败必须**中止**迁移（VERSIONING §2.6）——绝不能"没备份照样改库"。"""
        import sqlite3

        from paperkb import backup as backup_mod
        from paperkb.config import Roots
        from paperkb.manifest import ensure_manifest
        from paperkb.migrations import run_migrations

        roots = Roots(data_dir=tmp_path / "data", library_dir=tmp_path / "library",
                      kb_dir=tmp_path / "kb").ensure()
        con = sqlite3.connect(roots.biblio_db)
        con.execute("CREATE TABLE papers_meta (doi TEXT PRIMARY KEY, title TEXT DEFAULT '')")
        con.commit()
        con.close()
        ensure_manifest(roots, app_version="1.0.0", data_format=2, layout="v1")
        monkeypatch.setattr(backup_mod, "backup_databases", lambda *a, **k: "")

        with pytest.raises(RuntimeError):
            run_migrations(roots, target_format=2)
        con = sqlite3.connect(roots.biblio_db)
        try:
            cols = {r[1] for r in con.execute("PRAGMA main.table_info(papers_meta)")}
        finally:
            con.close()
        assert "rid" not in cols, "备份失败时库必须保持原样"


class TestReleaseVersionContract:
    """发布轮版本契约（2026-09-12）：前端 / 后端 / 算法包 / 知识库库四处不许漂移。"""

    def test_frontend_version_matches_app_version(self):
        from app.version import APP_VERSION, read_frontend_version

        fe = read_frontend_version(ROOT / "frontend")
        assert fe, "frontend/version.js 缺失或不可解析（前端版本单一来源）"
        assert fe == APP_VERSION, \
            f"前端 {fe} ≠ 后端 {APP_VERSION}（发布时两处必须同版）"

    def test_version_js_loaded_before_app_js(self):
        html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        assert "/version.js" in html, "index.html 必须加载 version.js（否则关于页无界面版本）"
        assert html.index("/version.js") < html.index("/app.js"), "version.js 必须在 app.js 之前"

    def test_about_page_shows_frontend_and_backend_versions(self):
        """用户要求：GUI 里能看到**前端版本**与**后端库版本**。

        2026-09-21：前端已拆为 app.js + js/*.js 多模块，关于页的 `/api/version` 随
        「设置中心」搬进了 js/settings.js —— 原先只读 frontend/app.js 会误报缺版本。
        改为扫**全部前端脚本**（app.js + js/ 递归），这才是"关于页能取到版本"的真实判据。
        """
        html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        for el in ("about-version", "about-frontend", "about-engine", "about-kb",
                   "about-dataformat", "about-mismatch"):
            assert f'id="{el}"' in html, f"关于页缺少版本元素 #{el}"
        scripts = [ROOT / "frontend" / "app.js",
                   *sorted((ROOT / "frontend" / "js").rglob("*.js"))]
        blob = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                         for p in scripts if p.is_file())
        assert "/api/version" in blob, "关于页必须从 /api/version 取全组件版本"

    def test_library_pyproject_matches_module_version(self):
        """库元数据可信：pyproject 版本 == 模块 __version__（打包/诊断都读它）。"""
        for pkg in ("paperparse", "paperkb"):
            data = tomllib.loads(
                (ROOT / "packages" / pkg / "pyproject.toml").read_text(encoding="utf-8"))
            mod = __import__(pkg)
            assert data["project"]["version"] == getattr(mod, "__version__", None), \
                f"{pkg}: pyproject={data['project']['version']} ≠ __version__={getattr(mod, '__version__', None)}"

    def test_version_endpoint_exposes_all_components(self):
        from app.main import _component_versions

        c = _component_versions()
        for key in ("app", "backend", "frontend", "paperkb", "paperparse", "data_format",
                    "layout"):
            assert key in c, f"/api/version 缺 {key}"
        assert c["app"] == c["backend"] == c["frontend"], c
        assert c["frontend_matches_backend"] is True
