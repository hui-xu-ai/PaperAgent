#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, r"D:\Python\DeepSeek\PaperAgent\packages\paperparse")
from paperparse.core.p14_pipeline import _sup_inline_refs as sup

cases = [
    ("compared with others. [11]", "compared with others. <sup>[11]</sup>", "单引用"),
    ("chemical sensing.<sup>[11]</sup>", "chemical sensing.<sup>[11]</sup>", "已上标不动"),
    ("blocking force (≈4.5 mN)[18,19]", "blocking force (≈4.5 mN)<sup>[18,19]</sup>", "多引用"),
    ("external stimuli,[1–5] for", "external stimuli,<sup>[1–5]</sup> for", "区间引用"),
    ("from the distance [34]", "from the distance <sup>[34]</sup>", "句尾引用"),
    ("x[1]y", "x<sup>[1]</sup>y", "行中"),
    ("[38] Fig. 2a", "<sup>[38]</sup> Fig. 2a", "段首引用"),
]
fails = 0
for src, want, note in cases:
    got = sup(src)
    ok = got == want
    if not ok:
        fails += 1
    print(("OK  " if ok else "FAIL"), note, "|", repr(got))
    if not ok:
        print("     want:", repr(want))
print("---- %d/%d" % (len(cases) - fails, len(cases)))
raise SystemExit(1 if fails else 0)
