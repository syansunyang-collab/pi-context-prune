"""List tool calls that reach the benchmark's own artifacts (possible answer leakage).

usage: python tools/context-prune-bench/scan_escapes.py <run-dir> [...]

A hit is a tool-call argument that names something the task must not see: hidden-test
artifacts (test_patch, task dirs, prompt.txt, test-output), the SWE-bench baseline files,
another run's directory, the control repo's AGENTS.md, or a `cd` to the repo root.
Absolute paths into the run's own work tree are not hits.
"""
import glob
import json
import re
import sys
from pathlib import Path

ARTIFACT = re.compile(
    r"test_patch|task\d+-django__|prompt\.txt|test-output|swebench|SWE-bench|test-baselines|AGENTS\.md",
    re.I,
)
REPO_ROOT_CD = re.compile(r'cd\s+"?(?:E:|/e)/Pi_Coding_Agent"?\s*(?:;|&&|$)', re.I)
OTHER_RUN = re.compile(r"live-2026\d{4}(?:-[a-z]+)?/r\d+-[A-Z0-9]+", re.I)


def norm(s: str) -> str:
    return s.replace("\\\\", "/").replace("\\", "/").replace("/e/Pi_Coding_Agent", "E:/Pi_Coding_Agent")


def scan(run_dir: Path):
    files = glob.glob(str(run_dir / "sessions" / "*.jsonl"))
    if not files:
        return None
    own_run = norm(str(run_dir.resolve()))
    own_tag = "/".join(own_run.split("/")[-2:])  # e.g. live-20260916/r2-B
    hits = []
    task = 0
    for line in open(files[0], encoding="utf-8"):
        if not line.strip():
            continue
        d = json.loads(line)
        if d.get("type") != "message":
            continue
        m = d["message"]
        if m.get("role") == "user":
            c = m.get("content")
            s = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c if isinstance(x, dict))
            if "仓库的工作副本" in s:
                task += 1
            continue
        if m.get("role") != "assistant":
            continue
        for x in m.get("content", []):
            if not (isinstance(x, dict) and x.get("type") == "toolCall"):
                continue
            s = norm(json.dumps(x.get("arguments", {}), ensure_ascii=False))
            why = None
            if ARTIFACT.search(s):
                why = "artifact"
            elif REPO_ROOT_CD.search(s):
                why = "repo-root"
            else:
                others = [o for o in OTHER_RUN.findall(s) if o.lower() != own_tag.lower()]
                if others:
                    why = "other-run:" + others[0]
            if why:
                hits.append((task, why, x["name"], s[:150]))
    return hits


def main(argv):
    for arg in argv:
        run_dir = Path(arg)
        hits = scan(run_dir)
        if hits is None:
            print(f"{arg}: no session")
            continue
        tasks = sorted({h[0] for h in hits})
        kinds = {}
        for h in hits:
            kinds[h[1].split(":")[0]] = kinds.get(h[1].split(":")[0], 0) + 1
        print(f"{arg}: hits={len(hits)} tasks={tasks} kinds={kinds}")
        for h in hits[:4]:
            print(f"    task{h[0]} [{h[1]}] {h[2]} {h[3]}")


if __name__ == "__main__":
    main(sys.argv[1:])
