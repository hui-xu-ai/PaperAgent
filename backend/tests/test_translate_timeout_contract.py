# -*- coding: utf-8 -*-
"""守卫：**探测的单档墙钟 = 生产的读超时**（2026-09-23 用户实测"多次重试超时"后立的规矩）。

为什么必须守：生产请求是**非流式**的，`requests` 的 read timeout 就等于"整批必须在 N 秒内返回"。
探测若用比生产宽松的额度（旧版 300s），就会把"生产必然超时的批次"判为**通过**并推荐给用户 ——
实测代价：14500 字符档，13852 字符的批连续 3 次 `Read timed out (read timeout=90)`（270s 白等 +
3 次输入白烧）才回落主模型；同一模型拿 3777 字符的小批立刻成功 ⇒ 是时间不够，不是能力不够。

两个数分住两处（paperkb 不知道 backend；backend 引用 paperkb），**改一个必须改另一个**——
本文件就是那条同步线。相关：`paperkb/translate/probe.py` 的 `TIER_TIMEOUT_SEC`、
`backend/app/services/llm_service.py` 的 `TRANSLATE_TIMEOUT_SEC`。
"""
from __future__ import annotations

import requests

from app.services import llm_service
from app.services.llm_service import _retry_reason
from app.services.translate_probe_service import PROBE_TIMEOUT_SEC
from paperkb.translate.probe import TIER_TIMEOUT_SEC, TIER_WALL_GRACE_SEC


def test_probe_tier_budget_equals_production_read_timeout():
    assert TIER_TIMEOUT_SEC == llm_service.TRANSLATE_TIMEOUT_SEC, (
        "探测的单档墙钟必须等于生产读超时，否则会推荐生产必然超时的批次；"
        "改了一处就要同步另一处（见本文件 docstring）")


def test_probe_client_timeout_leaves_room_for_its_own_wall_clock():
    """探测客户端的超时要比墙钟**更大**：让探测自己的墙钟先给结论（判据与文案更明确），
    客户端超时只当传输层兜底。"""
    assert PROBE_TIMEOUT_SEC == llm_service.TRANSLATE_TIMEOUT_SEC + TIER_WALL_GRACE_SEC
    assert PROBE_TIMEOUT_SEC > TIER_TIMEOUT_SEC


def test_probe_does_not_retry():
    """探测是"测量"：重试会让同一档白等更久、把档位耗时算成两倍。"""
    from app.services.translate_probe_service import PROBE_MAX_RETRIES
    assert PROBE_MAX_RETRIES == 0


# ---------------------------------------------------------------- 重试原因可读
def test_retry_reason_labels_timeout_with_the_number():
    """用户报"多次重试"时的关键信息：要能一眼看出是**读超时 90s**（该降批次上限），
    而不是含糊的"将重试"（用户上次只能翻后端日志才知道）。"""
    err = requests.exceptions.ReadTimeout("Read timed out. (read timeout=90)")
    assert _retry_reason(err, 90).startswith("读超时 90s")


def test_retry_reason_labels_connection_and_http_code():
    assert "连接失败" in _retry_reason(requests.exceptions.ConnectionError("x"), 90)
    resp = requests.Response()
    resp.status_code = 429
    err = requests.exceptions.HTTPError("429 Too Many Requests", response=resp)
    label = _retry_reason(err, 90)
    assert "429" in label and "限流" in label


def test_retry_reason_empty_when_unknown():
    assert _retry_reason(None, 90) == ""
    assert _retry_reason(RuntimeError("随便一个业务错误"), 90) == "随便一个业务错误"
