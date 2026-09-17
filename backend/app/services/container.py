# -*- coding: utf-8 -*-
"""服务容器：全局单例（FastAPI 依赖注入用）。

启动时 init_container(settings) 创建 store/engine/tasks/chat/event_bus/guard；
LLM 有 key 时 init_llm 注入引擎（带 TokenGuard 防护），无 key 时翻译/对话返回清晰错误。
"""
from __future__ import annotations

import logging
import threading

from ..config import APP_DATA_DIR, Settings
from .assets import get_assets as _build_assets
from .chat_service import ChatService
from .engine_service import EngineService
from .event_bus import EventBus
from .kb_service import KnowledgeBaseService
from .kbmeta_service import KbMetaService
from .llm_service import TokenGuard, get_chat as llm_get_chat
from .llm_service import init_llm
from .plugin_registry import PluginRegistry
from .review_service import ReviewService
from .settings_service import SettingsService
from .store import Store
from .task_service import TaskManager
from .usage_service import UsageService
from paperkb.config import Roots
from .lit_service import LitService
from paperlit.config import Roots as LitRoots, LitSettings

logger = logging.getLogger(__name__)

_settings: Settings | None = None
_store: Store | None = None
_engine: EngineService | None = None
_tasks: TaskManager | None = None
_chat: ChatService | None = None
_event_bus: EventBus | None = None
_guard: TokenGuard | None = None
_usage: UsageService | None = None
_settings_svc: SettingsService | None = None
_kb: KnowledgeBaseService | None = None
_assets = None
_plugins: PluginRegistry | None = None
_review: ReviewService | None = None
_roots: Roots | None = None
_kbapi: KbMetaService | None = None
_compile_worker = None
_lit: LitService | None = None
_llm_ready: bool = False


def _build_roots() -> Roots:
    """paperkb/应用数据根路径（R3 上提容器注入；paperkb 不硬编码路径）。

    2026-09-12（用户拍板 · 工业级数据布局）：data_dir 改为项目根 `data/`，
    五库物理隔离：data/{system/app.db, chat/chat.db, diary/diary.db,
    biblio/biblio.db, reference/journals.db}（见 `paperkb.config.Roots`）。
    旧布局是 `knowledge_base/.system/paperagent.db` 单库（应用数据与知识资产混放）。
    """
    return Roots(
        data_dir=APP_DATA_DIR / "data",
        library_dir=APP_DATA_DIR / "library",
        kb_dir=APP_DATA_DIR / "knowledge_base",
    )


def _build_lit_roots() -> LitRoots:
    """paperlit 数据根路径（与 paperkb 共享 data_dir，独立 literature/ 子目录）。"""
    return LitRoots(
        data_dir=APP_DATA_DIR / "data",
        lit_dir=APP_DATA_DIR / "data" / "literature",
    )


def _build_lit_settings() -> LitSettings:
    """paperlit 行为设置（SiliconFlow API key 从环境变量注入）。"""
    import os
    s = LitSettings()
    sf_key = os.getenv("SILICONFLOW_API_KEY", "").strip()
    if sf_key:
        s.embedding_api_key = sf_key
        s.reranker_api_key = sf_key
    return s


def init_container(settings: Settings) -> None:
    """应用启动时调用一次（V01 起内置 Token 防护红线：event_bus + TokenGuard）。

    V03：LLM 供应商来自 settings_service（SQLite 配置，.env 兜底）。
    V04：kb_service 提供 Obsidian 知识库；启动时旧论文自动纳入（backfill）。
    V05：plugin_registry 订阅事件总线驱动插件钩子。
    """
    global _settings, _store, _engine, _tasks, _chat, _event_bus, _guard, _usage, _llm_ready
    global _settings_svc, _kb, _plugins, _review, _compile_worker, _roots, _kbapi, _assets, _lit
    _settings = settings
    # R3：paperkb 根路径与薄访问器上提容器（单一注入点；懒初始化）。
    _roots = _build_roots()
    # 数据资产版本戳（docs/VERSIONING.md §5/§7）：建/校验 data/manifest.json。
    # 数据格式高于代码 → fail-fast（绝不猜测式部分读取）；缺清单 → 按当前格式认领。
    try:
        from paperkb.manifest import DataFormatError, ensure_manifest
        from ..version import (APP_VERSION, DATA_FORMAT, LAYOUT_VERSION,
                               MIN_READABLE_DATA_FORMAT)

        _manifest = ensure_manifest(_roots, app_version=APP_VERSION,
                                    data_format=DATA_FORMAT, layout=LAYOUT_VERSION,
                                    min_readable=MIN_READABLE_DATA_FORMAT)
        if _manifest.get("needs_migration"):
            logger.warning("数据格式需要迁移：manifest.data_format=%s < 代码 %s",
                           _manifest.get("data_format"), DATA_FORMAT)
        logger.info("数据资产清单: data_format=%s layout=%s app_version=%s",
                    _manifest.get("data_format"), _manifest.get("layout"),
                    _manifest.get("app_version"))
    except Exception as e:  # noqa: BLE001
        from paperkb.manifest import DataFormatError as _DFE
        if isinstance(e, _DFE):
            raise                      # 降级/格式不符必须拦住启动
        logger.warning("写入数据资产清单失败（不阻塞启动）: %s", e)
    # 迁移层（docs/VERSIONING.md §3）：数据格式落后 → **先备份再迁移**；已最新 → 零开销；
    # 迁移失败必须拦住启动（半迁移状态比不启动更危险）。
    try:
        from paperkb.migrations import run_migrations

        _mig = run_migrations(_roots, target_format=DATA_FORMAT)
        if _mig.get("needs_migration"):
            logger.info("数据迁移：v%s → v%s 备份=%s 已应用=%s",
                        _mig.get("from"), _mig.get("to"), _mig.get("backup"),
                        _mig.get("applied"))
    except Exception as e:  # noqa: BLE001
        logger.error("数据迁移失败（迁移内部已回滚，请查看日志与 data/_backups/）: %s", e)
        raise
    _kbapi = KbMetaService(_roots)
    # paperlit 文献检索库（P1-P7）：独立 SQLite + FAISS 向量索引
    _lit = LitService(_build_lit_roots(), _build_lit_settings())
    _event_bus = EventBus()
    _store = Store(settings.db_path, chat_db=_roots.chat_db, biblio_db=_roots.biblio_db)
    try:      # 建表后刷新清单 dbs 状态（ensure_manifest 早于建库 → 那时必然全 false）
        from paperkb.manifest import refresh_dbs

        refresh_dbs(_roots)
    except Exception as e:  # noqa: BLE001
        logger.warning("刷新数据清单失败（不阻塞启动）: %s", e)
    _usage = UsageService(_store, event_bus=_event_bus)
    _guard = TokenGuard(event_bus=_event_bus, usage_service=_usage)
    _settings_svc = SettingsService(_store, app_settings=settings, env_sync=True)
    # paperlit 设置注入（API key 从 SQLite 配置读取，.env 兜底）
    _lit._settings_service = _settings_svc
    # T3：单价按 (provider_id, model) 解析（settings_service 是 DB 来源，保存后即时生效）
    _usage.set_price_resolver(
        lambda provider_id, model: _settings_svc.get_prices_for(provider_id, model))
    _engine = EngineService(settings, kb=get_kb, kbmeta=get_kbapi)
    _tasks = TaskManager(settings, _store, _engine, event_bus=_event_bus)
    _kb = KnowledgeBaseService(settings, _store, _engine)
    _plugins = PluginRegistry(_store, event_bus=_event_bus)
    _plugins.subscribe()  # 订阅 task_done → on_paper_processed
    # 资产门面（`docs/VERSIONING.md` §6）：业务层访问数据的**唯一通道**；
    # 新代码一律走 `container.get_assets().<system|chat|biblio|reference|content>.*`
    _assets = _build_assets(_store, _roots, settings_service=_settings_svc,
                         kbapi_getter=get_kbapi, kb_service=_kb)
    _review = ReviewService(settings)
    provider = _settings_svc.get_active_provider(masked=False)
    if provider and provider.get("api_key"):
        init_llm(provider, guard=_guard)
        _chat = ChatService(settings, _store, _engine, llm_get_chat(),
                            settings_service=_settings_svc,
                            kbmeta=get_kbapi, kb=_kb)
        _llm_ready = True
    else:
        _chat = None
        _llm_ready = False
        logger.warning("未配置 LLM 供应商：翻译/对话功能不可用（设置中心配置后保存即生效）")
    _event_bus.publish("info", "system", "startup",
                       "服务启动完成", {"llm_ready": _llm_ready,
                                      "provider": (provider or {}).get("id", "")})
    # A2：启动后台编译 worker（消费 compile_jobs 队列：L1 全自动 + L2/L3 按价值分；
    #  LLM 未配置时空转等待；daemon 线程不阻塞/不杀）
    try:
        from .compile_worker import CompileWorker

        def _notify_compiled(item: dict) -> None:
            """编译项收尾 → 事件总线（前端据此刷新阅读器「编译结果标签页」）。

            2026-09-12：此前编译完成**不发事件**，前端标签页靠 15s 轮询且文件集缓存不失效，
            用户实测"编译完阅读器没有 `_note.md` 标签页"。
            """
            try:
                _event_bus.publish(
                    "warning" if item.get("error") else "info", "kb", "compile_done",
                    f"编译完成: {item.get('doi', '')} {item.get('level', '')}",
                    {"doi": item.get("doi", ""), "level": item.get("level", ""),
                     "error": item.get("error", "")})
            except Exception:  # noqa: BLE001 - 通知失败不影响 worker
                logger.exception("编译完成事件发布失败")

        _compile_worker = CompileWorker(notify=_notify_compiled)
        _compile_worker.start()
    except Exception:  # noqa: BLE001 - worker 启动失败不阻塞服务
        logger.exception("编译 worker 启动失败（可后续手动触发编译）")

    # 期刊分区/影响因子**开箱即用**（用户 2026-09-12）：reference 库为空 → 拷贝随包 db。
    # ① 同步拷贝（毫秒级；保证任何请求进来之前分区表就位，零竞态）；
    # ② 若只有 xlsx（没随包 db）：那把 ~20s 的解析放后台线程，绝不阻塞开窗。
    try:
        import threading as _threading

        from ..config import APP_DATA_DIR, PROJECT_ROOT
        from .reference_seed import ensure_reference_seeded

        _kb_for_seed = get_kbapi()      # 先在主线程完成 paperkb 懒初始化（避免后台线程竞态）
        _seed = ensure_reference_seeded(_roots, _kb_for_seed, PROJECT_ROOT, APP_DATA_DIR,
                                        allow_xlsx=False)
        if _seed.get("seeded"):
            logger.info("期刊分区表就位: jcr=%s cas=%s（%ss）",
                        _seed.get("jcr_rows"), _seed.get("cas_rows"), _seed.get("elapsed_s"))
            _event_bus.publish("info", "system", "reference_seed",
                               f"期刊分区表已随包就位（jcr={_seed.get('jcr_rows')} "
                               f"cas={_seed.get('cas_rows')}）", {"mode": _seed.get("mode")})
        elif _seed.get("mode") == "xlsx_pending":
            def _seed_slow() -> None:
                try:
                    r2 = ensure_reference_seeded(_roots, _kb_for_seed, PROJECT_ROOT,
                                                 APP_DATA_DIR, allow_xlsx=True)
                    if r2.get("seeded"):
                        _event_bus.publish("info", "system", "reference_seed",
                                           f"期刊分区表已导入（xlsx 兜底，{r2.get('elapsed_s')}s）",
                                           {"mode": "xlsx"})
                except Exception:  # noqa: BLE001
                    logger.exception("期刊分区表 xlsx 兜底导入失败")

            _threading.Thread(target=_seed_slow, name="reference-seed", daemon=True).start()
            logger.info("无随包 db → 期刊分区表 xlsx 导入转后台（不阻塞开窗）")
    except Exception:  # noqa: BLE001 - 种子就位失败不阻塞启动
        logger.exception("期刊分区表就位失败（不影响启动；可在设置里手动导入）")
    logger.info("服务容器初始化完成 llm_ready=%s", _llm_ready)


def register_plugins(app) -> None:
    """应用路由挂载后调用：插件 on_register。"""
    if _plugins:
        _plugins.register_all(app)


def _safe_backfill() -> None:
    """（A1 收敛：退役旧 kb_service 启动 backfill——它是旧 _note/_index 双写者源头。
    新 kb 纳入统一走 task_service._assemble_kb → paperkb.sync_source_to_kb；
    历史文献补纳入用 /api/kb-meta/backfill（paperkb.backfill_kb）。保留空函数防引用断。）"""
    return


def get_settings() -> Settings:
    assert _settings is not None, "容器未初始化"
    return _settings


def get_store() -> Store:
    assert _store is not None, "容器未初始化"
    return _store


def get_engine() -> EngineService:
    assert _engine is not None, "容器未初始化"
    return _engine


def get_tasks() -> TaskManager:
    assert _tasks is not None, "容器未初始化"
    return _tasks


def get_chat() -> ChatService:
    assert _chat is not None, "LLM 未初始化（缺少 DEEPSEEK_API_KEY）"
    return _chat


def get_event_bus() -> EventBus:
    assert _event_bus is not None, "容器未初始化"
    return _event_bus


def get_guard() -> TokenGuard:
    assert _guard is not None, "容器未初始化"
    return _guard


def get_usage() -> UsageService:
    assert _usage is not None, "容器未初始化"
    return _usage


def get_settings_service() -> SettingsService:
    assert _settings_svc is not None, "容器未初始化"
    return _settings_svc


def get_kb() -> KnowledgeBaseService:
    assert _kb is not None, "容器未初始化"
    return _kb


def get_roots() -> Roots:
    """paperkb 根路径（R3：Roots 上提容器作为可注入依赖）。"""
    assert _roots is not None, "容器未初始化"
    return _roots



def get_assets():
    """资产门面（业务层访问数据的唯一通道；见 docs/VERSIONING.md §6）。"""
    assert _assets is not None, "容器未初始化"
    return _assets

def get_kbapi() -> KbMetaService:
    """paperkb 门面薄访问器（R3：backend 侧唯一注入点，替代逐方法透传）。"""
    assert _kbapi is not None, "容器未初始化"
    return _kbapi


def get_lit() -> LitService:
    """paperlit 门面薄访问器（backend 侧唯一注入点）。"""
    assert _lit is not None, "容器未初始化"
    return _lit


def get_plugin_registry() -> PluginRegistry:
    assert _plugins is not None, "容器未初始化"
    return _plugins


def get_review() -> ReviewService:
    assert _review is not None, "容器未初始化"
    return _review


def get_compile_worker():
    """后台编译 worker（A2：L1 全自动 + L2/L3 按价值分）。未启动返回 None。"""
    return _compile_worker


def apply_provider(provider: dict | None) -> bool:
    """应用供应商配置：重建 LLM 通道（热切换，无需重启）。返回 llm 是否就绪。"""
    global _chat, _llm_ready
    from .chat_service import ChatService
    from .llm_service import get_chat as llm_get_chat
    from .llm_service import reconfigure_llm
    if provider and provider.get("api_key"):
        reconfigure_llm(provider, _guard)
        _chat = ChatService(_settings, _store, _engine, llm_get_chat(),
                            settings_service=_settings_svc,
                            kbmeta=get_kbapi, kb=_kb)
        _llm_ready = True
        if _event_bus:
            _event_bus.publish("info", "settings", "provider_switch",
                               f"LLM 供应商生效: {provider.get('id')} "
                               f"({provider.get('model')})",
                               {"provider": provider.get("id")})
        return True
    _chat = None
    _llm_ready = False
    return False


def llm_ready() -> bool:
    return _llm_ready
