"""Recording bookkeeping: seg start/end with the screencast file, stills, call marks."""
import json, sys, time
from pathlib import Path
F = Path(__file__).resolve().parents[1] / "data" / "demo_segments.jsonl"
kind = sys.argv[1]
rec = {"t": time.time(), "kind": kind, "args": sys.argv[2:]}
with F.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(rec) + "\n")
print(kind, sys.argv[2:])
