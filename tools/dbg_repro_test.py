#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, r"D:\Python\DeepSeek\PaperAgent\packages\paperparse")
from paperparse.core.p14_pipeline import char_conflicts

m = r"of $\mathrm { B F } _ { 4 }$ and"
p = r"of $\mathrm { B F } _ { 4 }$ and"
cf = char_conflicts(m, p, page=1)
print("冲突数:", len(cf))
for c in cf:
    print(" M:", repr(c["mineru"]["text"]))
    print(" P:", repr(c["paddleocr"]["text"]))
    print(" tag:", c["evidence"]["tag"])
