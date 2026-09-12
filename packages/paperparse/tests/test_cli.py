#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tests/test_cli.py
功能: T0 CLI 单元测试（version / selftest，网络检查打桩）
对外接口: 无（测试）
版本: v1.0.0 (2025-xx-xx)
版本历史:
  v1.0.0 初始版本
"""
import paperparse.cli as cli


def test_version(capsys):
    assert cli.main(["version"]) == 0
    out = capsys.readouterr().out
    assert "paper-reader-skill" in out


def test_selftest_stubbed(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_check_mineru_connectivity", lambda cfg: (True, "ok"))
    assert cli.main(["selftest"]) == 0
    out = capsys.readouterr().out
    assert "Python" in out
