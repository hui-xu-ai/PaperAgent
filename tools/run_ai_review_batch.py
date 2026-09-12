#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P11 批量补跑 AI 仲裁：对已有双通道产物（跳过云端解析）执行 --ai-review"""
import subprocess, sys, time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PDFS = [
    "10.1007_s40820-023-01133-2.pdf",
    "10.1016_j.cej.2025.167798.pdf",
    "10.1021_acs.nanolett.4c05430.pdf",
    "10.1021_acsnano.3c07694.pdf",
    "10.1038_ncomms8258.pdf",
    "10.1073_pnas.2210651120.pdf",
    "10.1126_sciadv.adh3350.pdf",
    "10.1126_scirobotics.abo6463.pdf",
]
PDF_DIR = Path("learning_workspace/corpus/pdfs")
LOG = Path("work/dual/ai_review_batch.log")

def main():
    t0 = time.time()
    with LOG.open("a", encoding="utf-8") as log:
        for name in PDFS:
            pdf = PDF_DIR / name
            ts = time.strftime("%H:%M:%S")
            print("[%s] ==== %s ====" % (ts, name), flush=True)
            try:
                r = subprocess.run(
                    [sys.executable, "tools/dual_parse.py", str(pdf),
                     "--no-parse-mineru", "--no-parse-paddleocr", "--ai-review"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=600)
                line = [l for l in r.stdout.splitlines() if "AI预校验" in l]
                ok = "双通道解析完成" in r.stdout
                log.write("[%s] %s %s %s\n" % (ts, name, "OK" if ok else "FAIL",
                                               line[0].strip() if line else ""))
            except subprocess.TimeoutExpired:
                log.write("[%s] %s TIMEOUT\n" % (ts, name))
            log.flush()
    print("AI 仲裁补跑完成（%.1f 分钟）" % ((time.time() - t0) / 60))

if __name__ == "__main__":
    main()
