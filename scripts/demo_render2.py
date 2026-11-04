"""Assemble the walkthrough video from screencast segments, stills, narration and real call audio.

Output: data/NovaCRM_onboarding_demo.mp4 (1920x1080, captions in a bar under the content).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from demo_narration import CAPTIONS, NARR, dur  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
D = ROOT / "data"
FF = str(D / "ffmpeg.exe")
SEG = D / "segments"
OUT = D / "NovaCRM_onboarding_demo.mp4"
W, H, CH, FPS = 1920, 1080, 888, 25
BG = "0x0b1020"
FONT = "C\\:/Windows/Fonts/segoeuib.ttf"

EXTRA = {  # captions for stills without narration
    "x_overview": "Rocketlane overview: 04 Nov → 03 Dec · 0/15 tasks · Rachel (dedicated CSM) on the team",
    "x_escalation": "#cs-escalations: CS manager asked to decide (also emailed) · no project created",
}


def esc_path(p: Path) -> str:
    return str(p).replace("\\", "/").replace(":", "\\:")


def caption_filter(src: str, dst: str, key: str, start: float, end: float, idx: int) -> str:
    text = CAPTIONS.get(key) or EXTRA[key]
    f = D / "captions" / f"{key}.txt"
    f.parent.mkdir(exist_ok=True)
    f.write_text(text, encoding="utf-8")
    return (f"[{src}]drawtext=fontfile='{FONT}':textfile='{esc_path(f)}':fontsize=36:fontcolor=white:"
            f"x=(w-text_w)/2:y={CH}+({H - CH}-text_h)/2:enable='between(t,{start:.2f},{end:.2f})'[{dst}]")


def run(cmd: list[str]) -> None:
    subprocess.run([FF, "-y", "-hide_banner", "-loglevel", "error", *cmd], check=True)


def load_events():
    tl = [json.loads(x) for x in (D / "demo_timeline.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    sg = [json.loads(x) for x in (D / "demo_segments.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    calls = json.loads((D / "calls" / "meta.json").read_text())
    return tl, sg, calls


def render_video(name: str, tl, sg, calls, out: Path) -> float:
    t0 = next(e["t"] for e in sg if e["kind"] == "seg_start" and e["args"] == [name])
    t1 = next(e["t"] for e in sg if e["kind"] == "seg_end" and e["args"] == [name])
    inside = [e for e in tl if t0 <= e["t"] <= t1]
    cuts, open_c = [], None
    for e in inside:
        if e["kind"] == "cut_start":
            open_c = e["t"] - t0
        elif e["kind"] == "cut_end" and open_c is not None:
            cuts.append((open_c, e["t"] - t0)); open_c = None
    keep, pos, total_raw = [], 0.0, t1 - t0
    for a, b in sorted(cuts):
        if a > pos:
            keep.append((pos, a))
        pos = max(pos, b)
    keep.append((pos, total_raw))

    def ot(t: float) -> float:
        acc = 0.0
        for a, b in keep:
            if t < a:
                return acc
            if t <= b:
                return acc + t - a
            acc += b - a
        return acc
    total = sum(b - a for a, b in keep)
    caps = [(ot(e["t"] - t0), e["value"]) for e in inside if e["kind"] == "cap"]
    call_ids = [e["args"][1] for e in sg if e["kind"] == "call" and e["args"][0] == name]

    g = [f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[p{i}]" for i, (a, b) in enumerate(keep)]
    g.append("".join(f"[p{i}]" for i in range(len(keep))) + f"concat=n={len(keep)}:v=1:a=0,fps={FPS},"
             f"scale={W}:{CH}:force_original_aspect_ratio=decrease,pad={W}:{H}:0:0:color={BG}[v0]")
    cur = "v0"
    for i, (t, key) in enumerate(caps):
        end = caps[i + 1][0] if i + 1 < len(caps) else total
        g.append(caption_filter(cur, f"v{i + 1}", key, t, end, i)); cur = f"v{i + 1}"
    inputs, aud = ["-i", str(SEG / f"{name}.mp4")], []
    n = 1
    for t, key in caps:
        inputs += ["-i", str(NARR / f"{key}.wav")]
        ms = int(max(t, 0) * 1000) + 250
        g.append(f"[{n}:a]aresample=48000,adelay={ms}|{ms}[a{n}]"); aud.append(f"a{n}"); n += 1
    for cid in call_ids:
        m = calls[cid]
        inputs += ["-i", str(ROOT / m["file"])]
        ms = int(max(ot(m["start"] - t0), 0) * 1000)
        g.append(f"[{n}:a]aresample=48000,volume=1.6,adelay={ms}|{ms}[a{n}]"); aud.append(f"a{n}"); n += 1
    g.append("".join(f"[{a}]" for a in aud) + f"amix=inputs={len(aud)}:normalize=0:dropout_transition=0,"
             f"apad,atrim=0:{total:.3f}[aout]")
    script = D / f"filter_{name}.txt"
    script.write_text(";".join(g), encoding="utf-8")
    run([*inputs, "-/filter_complex", str(script), "-map", f"[{cur}]", "-map", "[aout]",
         "-c:v", "libx264", "-preset", "medium", "-crf", "22", "-pix_fmt", "yuv420p", "-r", str(FPS),
         "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", str(out)])
    print(f"{name}: {total:.1f}s (raw {total_raw:.1f}s, {len(cuts)} cuts, {len(caps)} captions, {len(call_ids)} calls)")
    return total


def render_still(img: str, key: str, out: Path, narrated: bool, hold: float | None = None) -> float:
    length = hold or (dur(key) + 1.2 if narrated else 5.0)
    g = [f"[0:v]scale={W}:{CH}:force_original_aspect_ratio=decrease,pad={W}:{CH}:(ow-iw)/2:(oh-ih)/2:color={BG},"
         f"pad={W}:{H}:0:0:color={BG},fps={FPS}[v0]", caption_filter("v0", "v1", key, 0, length, 0)]
    inputs = ["-loop", "1", "-t", f"{length:.2f}", "-i", str(SEG / img)]
    if narrated:
        inputs += ["-i", str(NARR / f"{key}.wav")]
        g.append(f"[1:a]aresample=48000,adelay=300|300,apad,atrim=0:{length:.3f}[aout]")
    else:
        inputs += ["-f", "lavfi", "-t", f"{length:.2f}", "-i", "anullsrc=r=48000:cl=stereo"]
        g.append("[1:a]anull[aout]")
    script = D / f"filter_{out.stem}.txt"
    script.write_text(";".join(g), encoding="utf-8")
    run([*inputs, "-/filter_complex", str(script), "-map", "[v1]", "-map", "[aout]", "-c:v", "libx264", "-preset",
         "medium", "-crf", "22", "-pix_fmt", "yuv420p", "-r", str(FPS), "-c:a", "aac", "-b:a", "160k",
         "-ar", "48000", "-ac", "2", "-shortest", str(out)])
    print(f"{img}: {length:.1f}s")
    return length


def main() -> None:
    tl, sg, calls = load_events()
    parts = D / "parts"
    parts.mkdir(exist_ok=True)
    plan = [
        ("video", "s1"), ("video", "s2"),
        ("still", "still_rl_plan.jpg", "c07", True), ("still", "still_rl_overview.jpg", "x_overview", False),
        ("still", "still_slack.jpg", "c08", True), ("still", "still_automation.jpg", "c09", True),
        ("video", "s4"), ("video", "s5"), ("still", "still_escalation.jpg", "x_escalation", False),
        ("video", "s7"),
    ]
    files, total = [], 0.0
    for i, item in enumerate(plan):
        out = parts / f"{i:02d}.mp4"
        total += render_video(item[1], tl, sg, calls, out) if item[0] == "video" else render_still(item[1], item[2], out, item[3])
        files.append(out)
    lst = parts / "list.txt"
    lst.write_text("".join(f"file '{f.as_posix()}'\n" for f in files), encoding="utf-8")
    run(["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(OUT)])
    print(f"\n{OUT}  {total / 60:.1f} min")


if __name__ == "__main__":
    main()
