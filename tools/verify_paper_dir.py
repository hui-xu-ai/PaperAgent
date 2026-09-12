#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
from pathlib import Path
sys.path.insert(0, r"D:\Python\DeepSeek\PaperAgent")
sys.path.insert(0, r"D:\Python\DeepSeek\PaperAgent\backend")
sys.path.insert(0, r"D:\Python\DeepSeek\PaperAgent\packages\paperparse")
from app.services.engine_service import _paper_dir

p1 = Path(r"D:\Python\DeepSeek\PaperAgent\library\10.1002_adma.202407106\document.json")
print("生产:", _paper_dir(p1).name, "| 期望 10.1002_adma.202407106:",
      _paper_dir(p1).name == "10.1002_adma.202407106")
p2 = Path(r"D:\Python\DeepSeek\PaperAgent\library\10.1002_adma.202407106\intermediate\document.json")
print("中间态:", _paper_dir(p2).name, "| 期望 10.1002_adma.202407106:",
      _paper_dir(p2).name == "10.1002_adma.202407106")
