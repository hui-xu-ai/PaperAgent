# -*- coding: utf-8 -*-
"""应用配置：从 .env / 环境变量加载。

T01 骨架：仅核心配置；随子任务扩展（如 T04 增加 DB 路径）。
P2-A：.env 统一按 UTF-8（含 BOM 兼容）加载，并对「key 被注释吞掉 / 换行损坏」
这类静默配置失效做启动自检（曾因此导致 MINERU_API_KEY 缺失、精准解析永不触发）。
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# P2-7 / 2026-09-12：版本**单一来源** = backend/app/version.py（设置中心「关于」、
# /api/health、pyproject.toml 由测试断言一致）；此处 re-export 以兼容既有 import。
from .version import APP_VERSION, DATA_FORMAT, LAYOUT_VERSION  # noqa: E402  (放在常量区之前)

# PyInstaller 打包后：资源在 _MEIPASS；数据（db/output/input/.env）在 exe 同目录
IS_FROZEN = bool(getattr(sys, "frozen", False))
if IS_FROZEN:
    PROJECT_ROOT = Path(sys._MEIPASS)  # 只读资源（前端静态页等）
    APP_DATA_DIR = Path(sys.executable).resolve().parent  # 可写数据目录
else:
    PROJECT_ROOT = Path(__file__).resolve().parents[2]  # 项目根
    APP_DATA_DIR = PROJECT_ROOT

BACKEND_DIR = Path(__file__).resolve().parents[1]
FRONTEND_DIR = PROJECT_ROOT / "frontend"

# 加载 .env（APP_DATA_DIR 优先，其次 backend/）
# P2-A：utf-8-sig 兼容 UTF-8 BOM；原实现默认编码遇 BOM/异编码会静默吞 key
def _load_dotenv_utf8(path: Path) -> None:
    if path.exists():
        load_dotenv(path, encoding="utf-8-sig")

_load_dotenv_utf8(APP_DATA_DIR / ".env")
_load_dotenv_utf8(BACKEND_DIR / ".env")


def _warn_env_anomalies() -> None:
    """P2-A 启动自检：.env 中写了值但未加载成功的 key → 立即大声报警。

    典型根因：注释行与 key 行的换行被损坏合并（如 `# 注释…？MINERU_API_KEY=sk-…`
    整行以 # 开头 → 值被当作注释吞掉），或文件编码损坏。
    """
    for p in (APP_DATA_DIR / ".env", BACKEND_DIR / ".env"):
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for var in ("MINERU_API_KEY", "MINERU_PARSER", "DEEPSEEK_API_KEY"):
            val = os.getenv(var, "").strip()
            for ln in text.splitlines():
                if ln.lstrip().startswith(var + "="):
                    inline = ln.split("=", 1)[1].strip()
                    if inline and not val:
                        logger.error(
                            "配置异常：%s 在 .env 中已写值但未被加载"
                            "（换行损坏被并入注释行，或编码异常）。当前行：%s",
                            var, ln.strip()[:100])
                    break


_warn_env_anomalies()


@dataclass(frozen=True)
class Settings:
    # --- Web 服务 ---
    host: str = os.getenv("PAPERAGENT_HOST", "127.0.0.1")
    port: int = int(os.getenv("PAPERAGENT_PORT", "8900"))

    # --- DeepSeek LLM ---
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "").strip()
    deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()
    llm_timeout_sec: int = int(os.getenv("LLM_TIMEOUT_SEC", "600"))
    llm_max_retries: int = int(os.getenv("LLM_MAX_RETRIES", "1"))

    # --- 引擎（paper-reader-skill v2.3.0）---
    mineru_api_key: str = os.getenv("MINERU_API_KEY", "").strip()
    # auto: 有 key → mineru-v4 精准；无 key → mineru v1 免费通道（无需密钥，限流）
    mineru_parser: str = os.getenv("MINERU_PARSER", "auto").strip()
    # P12 双 PDF 解析：parse_mode=dual（默认，mineru-v4 主 + PaddleOCR-VL-1.6 辅 +
    # AI 批量仲裁）/ single（原单通道）；ai_review=是否 AI 仲裁（默认开，按调用扣费）
    parse_mode: str = os.getenv("PARSE_MODE", "dual").strip()
    ai_review: bool = os.getenv("PARSE_AI_REVIEW", "1").strip() in ("1", "true", "yes", "on")
    # 2026-09-16（用户要求"尽量降低人的参与或人不参与"）：**第三信号（PDF 自带文本层）
    # 直接裁决**。决定性第三票直接定 verdict（判 P 落地 P / 判 M 保留 M 并自动撤销规则错改），
    # 这些项不再进人工复核清单；置 0 退回"只当证据提示"。0 成本（本地文本层，不联网不花 token）。
    third_signal_decide: bool = os.getenv("PARSE_THIRD_DECIDE", "1").strip() in (
        "1", "true", "yes", "on")
    # 2026-09-16（用户："让 AI 综合这些解析结果进行综合判断，给出自己的修改建议"）：
    # **AI 综合建议**——把 M/P 片段 + 句内上下文 + PDF 文本层原文旁证交给 AI，让它给出
    # 自己的最终片段（可与两侧都不同），落地后仍过五道护栏。置 0 退回旧的"只选边"。
    ai_synthesis: bool = os.getenv("PARSE_AI_SYNTHESIS", "1").strip() in (
        "1", "true", "yes", "on")
    # P15 Step5：解析管线开关（PAPERPARSE_PIPELINE）——p14（默认，P14 文本管线：
    # 本地骨架权威边界 + mineru full.md 文本基底 + 拼接修复 + 双通道验证 + 字符仲裁，
    # 替换 P12 成为生产基底）/ p12（回退 P12 双通道块级管线）
    pipeline: str = os.getenv("PAPERPARSE_PIPELINE", "p14").strip()
    # 双通道中间产物根（work/dual/<pdf_stem>/；缓存复用，重试不重复扣配额）
    # 2026-09-12（用户真实工作流＝打包版：**升级只替换程序、保留资产**）：
    # 所有**可写**资产路径一律走 `APP_DATA_DIR`（打包后 = exe 同目录），
    # 绝不用 `PROJECT_ROOT`（打包后 = _MEIPASS 只读临时目录 → 资产会丢在临时目录里）。
    dual_work_root: str = os.getenv("DUAL_WORK_ROOT",
                                    str(APP_DATA_DIR / "work" / "dual")).strip()
    # 统一规范库（T01）：所有解析/翻译/导出产物就地存放 library/<DOI>/，
    # 不再有独立 backup 目录（导出=就地渲染变体，避免内容重复与 copytree 源=目标报错）
    engine_work_root: str = os.getenv("ENGINE_WORK_ROOT",
                                      str(APP_DATA_DIR / "library")).strip()
    # 遗留引擎暂存清理（qna_writeback._clean_staging 清理 engine_out_root/<DOI>）：
    # 调用后立即清理，只作中间缓冲，不产生持久重复副本。
    # 2026-09-12（用户拍板 · 工业级数据布局）：暂存迁到 work/tmp_export（work 是可随时清理区），
    # 不再落在知识库里；engine_input_root 也从 input/ 改到 work/upload（不再留存 PDF 副本）。
    engine_out_root: str = os.getenv("ENGINE_OUT_ROOT",
                                     str(APP_DATA_DIR / "work" / "tmp_export")).strip()
    engine_input_root: str = os.getenv("ENGINE_INPUT_ROOT",
                                       str(APP_DATA_DIR / "work" / "upload")).strip()
    # P-ENHANCE R03/R09：外部学习规则根（learned/user 可写）——builtin 随引擎内嵌
    # （asset_root()/rules，打包后 _MEIPASS/share/paperparse_skill/rules 只读）；
    # frozen（打包）时外部规则默认 exe 同目录 rules/（用户可导入学习规则）
    rules_dir: str = os.getenv(
        "RULES_DIR",
        str(APP_DATA_DIR / "rules" if IS_FROZEN else PROJECT_ROOT / "rules")).strip()

    def resolve_mineru_parser(self) -> str:
        """生产解析通道：有 Key → `mineru-v4`；无 Key → `""`（解析不可用）。

        2026-09-12 批2（用户拍板"至少要有 MinerU 密钥才能保证解析质量"）：**不再回落**
        v1 免费通道 / 本地 pymupdf。实时取值版见模块函数 `resolve_live_mineru_parser`。
        """
        if not self.mineru_api_key:
            return ""
        return "mineru-v4" if self.mineru_parser in ("auto", "mineru-v4") else self.mineru_parser

    # --- 存储 ---
    # 打包版：exe 同目录 `data/system/app.db`（资产与程序分离 ⇒ 升级只换程序、资产原地保留）
    db_path: str = os.getenv("PAPERAGENT_DB",
                             str(APP_DATA_DIR / "data" / "system" / "app.db")).strip()

    # --- 会话/缓存 ---
    chat_history_limit: int = int(os.getenv("CHAT_HISTORY_LIMIT", "12"))  # 兼容保留（LLM 上下文改按 token 预算裁剪）
    answer_cache_size: int = int(os.getenv("ANSWER_CACHE_SIZE", "256"))   # 回答缓存条数
    query_limit_chars: int = int(os.getenv("QUERY_LIMIT_CHARS", "1500"))  # 局部检索每段字符上限
    # 单篇文献会话：把 `library/<资源>/en.md` 全文放进 **system 稳定前缀**的字符上限。
    # 用户设计（2026-09-12）：同一篇的多轮提问只付一次全价，之后按前缀缓存价计费；
    # 且模型真的"看过全文"，不再依赖每轮检索是否命中（中文问句实测 0 命中）。
    # 取值语义（批3 修正）：
    #   -1（默认）= **不截断**（与编译/翻译侧发送的 `shared_ctx` 全文逐字节一致 ⇒ 问答能
    #                继承编译建立的整段前缀缓存；此前默认 60000 而全文 69181 ⇒ 命中上限
    #                被白白砍掉约 2,312 token，两侧前缀长度不一致）；
    #    0        = 关闭该机制（退回"每轮检索片段"模式）；
    #   >0        = 截断到该字符数（**只在明确要压 token 时用**：会让问答前缀短于编译前缀，
    #                从而无法共享被截掉的那一段）。
    paper_fulltext_prefix_chars: int = int(
        os.getenv("PAPER_FULLTEXT_PREFIX_CHARS", "-1"))
    # P0/P1：对话历史按 token 预算裁剪（长答案不再全量回放撑破 prompt）。
    # 先取最近 history_max_messages 条，再按估算 token 从最新往回保留，
    # 超过 history_token_budget 即丢最旧（至少保留最后 1 条）。
    history_token_budget: int = int(os.getenv("HISTORY_TOKEN_BUDGET", "4000"))
    history_max_messages: int = int(os.getenv("HISTORY_MAX_MESSAGES", "60"))

    # --- 知识库 agent 管理模式（manage_tools）---
    # 工具调用循环轮次上限：默认 6；综述/写作类任务（reports）允许更大（如 12）。
    manage_tools_max_rounds: int = int(os.getenv("MANAGE_TOOLS_MAX_ROUNDS", "6"))
    manage_review_max_rounds: int = int(os.getenv("MANAGE_REVIEW_MAX_ROUNDS", "12"))
    # kb_recall 检索结果回填预算（字符）：综述/写作类放大，防单条截断丢失证据。
    manage_retrieval_budget_chars: int = int(os.getenv("MANAGE_RETRIEVAL_BUDGET_CHARS", "20000"))

    # 目录（运行时确保存在）
    dirs: tuple = field(default_factory=tuple)


def live_mineru_key(fallback: str = "") -> str:
    """MinerU API Key 的**运行期实时值**（.env/os.environ = 单一来源）。

    2026-09-12 批1：`Settings` 是 `@dataclass(frozen=True)`（启动期快照），设置中心保存
    MinerU 配置后写的是 `.env` + `os.environ`（settings_service.save_mineru）——
    快照里读不到新值。凡"保存后要立即生效"的消费方（解析前置门禁、/api/health）一律走这里。
    `os.environ` **无该键**（从未配置过）时回退 `fallback`（启动快照）；**有键但为空**
    表示用户显式清空 ⇒ 返回空串（不得回退，否则"删掉 Key"永远不生效）。
    """
    raw = os.environ.get("MINERU_API_KEY")
    return fallback if raw is None else raw.strip()


def mineru_ready(fallback: str = "") -> bool:
    """**解析硬门禁判据**（2026-09-12 批2，用户拍板）：是否已配置 MinerU Key。

    没有 Key 就没有可靠解析（免费 v1 通道与本地 pymupdf 已按用户决策从生产链移除），
    调用方（EngineService.parse_pdf 前置门禁 / 上传预检 / /api/health）一律用本函数，
    不要各自读 settings.mineru_api_key（那是启动快照，会与设置中心刚保存的值不一致）。
    """
    return bool(live_mineru_key(fallback))


def resolve_live_mineru_parser(key_fallback: str = "",
                               parser_fallback: str = "auto") -> str:
    """按**实时** Key 解析生产解析通道：有 Key → `mineru-v4`；无 Key → `""`（不可用）。

    2026-09-12 批2（用户拍板"解析必须有 MinerU 密钥"）：**不再回落** `mineru` v1 免费
    通道（限流 + 精度低）。`parser_fallback` 仅为兼容旧调用保留：显式配置 `mineru`/`mineru-v4`
    时仍按配置返回（CLI/离线工具走 paperparse 自身多通道，与本门禁无关）。
    """
    key = live_mineru_key(key_fallback)
    parser = (parser_fallback or "auto").strip()
    if parser in ("mineru", "mineru-v4") and key:
        return parser
    return "mineru-v4" if key else ""


def get_settings() -> Settings:
    s = Settings()
    # 相对路径统一相对 APP_DATA_DIR 绝对化（避免依赖 cwd 漂移）
    def _abs(p: str) -> str:
        path = Path(p)
        return str(path if path.is_absolute() else APP_DATA_DIR / path)
    s = replace(s, db_path=_abs(s.db_path),
                engine_work_root=_abs(s.engine_work_root),
                engine_out_root=_abs(s.engine_out_root),
                engine_input_root=_abs(s.engine_input_root),
                rules_dir=_abs(s.rules_dir),
                dual_work_root=_abs(s.dual_work_root))
    ensure_dirs(s)
    return s


def ensure_dirs(s: Settings) -> None:
    for d in (Path(s.engine_work_root), Path(s.engine_out_root),
              Path(s.engine_input_root),
              Path(s.db_path).parent, FRONTEND_DIR):
        d.mkdir(parents=True, exist_ok=True)
