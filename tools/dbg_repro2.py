#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys, re
sys.path.insert(0, r"D:\Python\DeepSeek\PaperAgent\packages\paperparse")
from paperparse.core.p14_pipeline import char_conflicts, _mask_formulas_keep_len
from paperparse.core.dual_pipeline import _norm_format

m = r"of $\mathrm { B F } _ { 4 }$ and"
p = r"of $\mathrm { B F } _ { 4 }$ and"

m_mask = _mask_formulas_keep_len(m, "\u0001")
p_mask = _mask_formulas_keep_len(p, "\u0002")
print("m_mask:", repr(m_mask))
print("p_mask:", repr(p_mask))

import difflib
sm = difflib.SequenceMatcher(None, m_mask, p_mask)
for tag, i1, i2, j1, j2 in sm.get_opcodes():
    if tag != "equal":
        m_raw = m[i1:i2]
        p_raw = p[j1:j2]
        print(tag, (i1, i2), (j1, j2), "m_raw:", repr(m_raw), "p_raw:", repr(p_raw))
        m_core = m_raw.strip()
        p_core = p_raw.strip()
        print("  _eq:", repr(_norm_format(m_core)), "==", repr(_norm_format(p_core)),
              _norm_format(m_core) == _norm_format(p_core) and bool(_norm_format(m_core)))
        _mm = re.search(r"\$([^$]*)\$", m_core)
        _frag = False
        for _x in (m_core, p_core):
            _mm2 = re.search(r"\$([^$]*)\$", _x)
            if _mm2:
                _inner = re.sub(r"\\[A-Za-z]+|[{}\^_~]", "", _mm2.group(1))
                n = sum(1 for _t in _inner.split() if len(_t) == 1)
                print("  frag inner:", repr(_inner), "single-tokens:", n)
                if n >= 2:
                    _frag = True
                    break
        print("  _frag:", _frag)
