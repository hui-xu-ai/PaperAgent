#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P11 批量语料双通道解析：8 篇剩余 PDF → dual_parse（--corpus --persist-rules --ai-review）"""
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
LOG = Path("work/dual/batch_run.log")

def main():
    t0 = time.time()
    results = []
    with LOG.open("a", encoding="utf-8") as log:
        for name in PDFS:
            pdf = PDF_DIR / name
            if not pdf.exists():
                results.append((name, "SKIP 文件缺失"))
                log.write("[%s] SKIP 文件缺失\n" % name)
                log.flush()
                continue
            ts = time.strftime("%H:%M:%S")
            print("[%s] ==== 开始 %s ====" % (ts, name), flush=True)
            try:
                r = subprocess.run(
                    [sys.executable, "tools/dual_parse.py", str(pdf),
                     "--corpus", "--persist-rules"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=1800)
                tail = (r.stdout + r.stderr)[-600:]
                ok = "双通道解析完成" in r.stdout
                results.append((name, "OK" if ok else "FAIL"))
                log.write("[%s] %s %s\n%s\n" % (ts, name, "OK" if ok else "FAIL", tail))
            except subprocess.TimeoutExpired:
                results.append((name, "TIMEOUT"))
                log.write("[%s] %s TIMEOUT\n" % (ts, name))
            except Exception as e:
                results.append((name, "ERR %s" % e))
                log.write("[%s] %s ERR %s\n" % (ts, name, e))
            log.flush()
    print("\n==== 批量完成（%.1f 分钟）====" % ((time.time() - t0) / 60))
    for name, status in results:
        print("  %-40s %s" % (name, status))

if __name__ == "__main__":
    main()
