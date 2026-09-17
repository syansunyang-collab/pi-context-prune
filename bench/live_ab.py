#!/usr/bin/env python
"""Live A/B driver for pi-context-prune.

This is the exact harness used for the numbers in bench/README.md, published as evidence rather than as a
turnkey tool: paths (Pi binary, work root, venv) are specific to the machine it ran on, and it needs a
SWE-bench Verified task list plus a per-task test baseline that are not part of this repository.

Original header follows.

Live A/B driver for pi-context-prune: one Pi RPC session solving N SWE-bench
Verified Django instances in sequence, with the pruner loaded (arm B) or not (arm A).

Inputs : test-baselines/context-prune-bench/swebench-verified-django50.json,
         a local Django clone (--base-repo), a venv python with asgiref+sqlparse (--python),
         the user-level Pi install (pi.cmd on PATH) and provider credentials.
Outputs: <run-dir>/ with sessions/*.jsonl, rpc.jsonl, stderr.txt, per-instance
         prompt/agent.patch/test-output/score.json, and run.json (timings + scores).
Never touches ~/.pi/agent/settings.json or the pruner's settings.json.
Exit code 1 on driver failure; scoring failures are recorded, not raised.

Usage:
  python tools/context-prune-bench/live_ab.py --arm A|B --run-dir <dir>
      --instances django__django-16667,django__django-16899,django__django-17087
      [--base-repo tmp/context-prune-bench/django-base] [--python tmp/context-prune-bench/venv/Scripts/python.exe]
      [--model <provider/model-id>] [--thinking medium] [--task-timeout 2400]
"""
import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BASELINE = REPO / "test-baselines/context-prune-bench/swebench-verified-django50.json"
PLUGIN = Path(os.environ["USERPROFILE"]) / ".pi/agent/npm/node_modules/@syansunyang/pi-context-prune/dist/index.js"  # fork, installed from npm
CURSOR_HOST = REPO / "tools/pi-cursor-host/index.ts"
SOLPI = REPO / "tmp/context-prune-bench/SoL-Pi"  # NVlabs/SoL-Pi checkout with `npm ci --ignore-scripts` done
PI = Path(os.environ["APPDATA"]) / "npm/pi.cmd"
# Names absent from a run are ignored by Pi; obs_recall/update_plan exist only with SoL-Pi, context_tree_query only with the pruner.
TOOLS = "read,bash,edit,write,grep,find,ls,context_tree_query,obs_recall,update_plan"
SOLPI_ALL = {"version": 1, "actionFusion": True, "observationPack": True, "evidencePreservingReducer": True,
             "evidencePreservingReducerProvider": os.environ.get("BENCH_REDUCER_PROVIDER", ""), "evidencePreservingReducerModel": os.environ.get("BENCH_REDUCER_MODEL", ""),
             "onlineContextCompact": True, "cacheWriteReadRatio": 12.5}
BASES = REPO / "tmp/context-prune-bench/bases"  # per-instance history-free base repos (built once by prepare_bases)
CACHE_KEY = {}  # base_commit -> instance_id, filled in main()
FORK_VARIANTS = REPO / "tmp/context-prune-bench/fork-variants"  # experiment builds of the pruner fork (default cap baked in)
ARMS = {  # arm -> (extensions to load, SoL-Pi project config or None)
    "A": ([], None),                      # stock Pi
    "B": ([FORK_VARIANTS / "cap2000-v140/index.js"], None),  # frozen copy of the 1.4.0 build (2000 chars per result); PLUGIN itself now carries the fork build
    "B8": ([FORK_VARIANTS / "cap8000/index.js"], None),  # fork: summarizer sees 8000 chars per result
    "B0": ([FORK_VARIANTS / "cap0/index.js"], None),     # fork: summarizer sees full results
    "C": ([SOLPI], SOLPI_ALL),            # SoL-Pi, all four mechanisms
    "D": ([PLUGIN, SOLPI], SOLPI_ALL),    # both
    "H": ([CURSOR_HOST], None),           # pi-cursor-host provider, no pruner; run with --model cursor-host/<model>
}
STATUS_RE = re.compile(r"^(.*?) \.\.\. (ok|FAIL|ERROR|skipped.*|expected failure|unexpected success)\s*$")


def sh(args, cwd, timeout=600, check=True):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"{args} failed ({p.returncode}): {p.stderr[-2000:]}")
    return p


def reset_tree(work, commit, base_repo):
    """Rebuild the work tree as a history-free single-commit repo at `commit`.

    A plain checkout keeps the full Django history (including the upstream fix commits) in .git, and
    every earlier run found the real fix via `git log`/`git show`. The tree is cloned from a prepared
    single-commit base (BASES/<instance>, built by `--prepare-bases`) or exported with `git archive`.
    The work directory itself is never deleted: it is the cwd of the running Pi process and Windows
    refuses to remove it; only its contents are replaced (the `.pi` folder is kept).
    """
    work = Path(work)
    work.mkdir(exist_ok=True)
    for name in ("nul", "con", "prn", "aux"):  # Windows reserved names need the extended-length prefix
        try:
            os.remove("\\\\?\\" + str(work / name))
        except OSError:
            pass
    for child in work.iterdir():
        if child.name == ".pi":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, onexc=lambda f, p, e: (os.chmod(p, 0o700), f(p)))
        else:
            child.unlink()
    fresh = work.parent / (work.name + ".fresh")
    shutil.rmtree(fresh, ignore_errors=True)
    cached = BASES / CACHE_KEY[commit] if commit in CACHE_KEY else None
    if cached and (cached / ".git").exists():
        # --no-hardlinks: the work root may sit on another volume than the bases (hardlinks fail with "Improper link")
        sh(["git", "clone", "-q", "--local", "--no-hardlinks", str(cached), str(fresh)], cwd=work.parent)
    else:
        fresh.mkdir()
        with open(fresh / ".export.tar", "wb") as fh:
            subprocess.run(["git", "archive", "--format=tar", commit], cwd=base_repo, stdout=fh, check=True)
        sh(["tar", "-xf", ".export.tar"], cwd=fresh)
        (fresh / ".export.tar").unlink()
        sh(["git", "init", "-q"], cwd=fresh)
        sh(["git", "-c", "user.email=bench@local", "-c", "user.name=bench", "add", "-A"], cwd=fresh)
        sh(["git", "-c", "user.email=bench@local", "-c", "user.name=bench", "commit", "-q", "-m", "base"], cwd=fresh)
    # The local clone records the base repo (inside the control repo) as `origin` and in the reflog
    # ("clone: from E:\...ases\<instance>"); that path names the instance and points the agent at the
    # benchmark's own files (one run got there via `git reflog`). Drop both.
    sh(["git", "remote", "remove", "origin"], cwd=fresh, check=False)
    shutil.rmtree(fresh / ".git" / "logs", ignore_errors=True)
    for name in ("FETCH_HEAD", "ORIG_HEAD"):
        (fresh / ".git" / name).unlink(missing_ok=True)
    for child in fresh.iterdir():
        shutil.move(str(child), str(work / child.name))
    fresh.rmdir()


def patched_test_files(test_patch):
    return [line[len("+++ b/"):] for line in test_patch.splitlines() if line.startswith("+++ b/")]


def test_labels(test_patch):
    labels = []
    for line in test_patch.splitlines():
        if line.startswith("+++ b/tests/"):
            mod = line[len("+++ b/tests/"):].removesuffix(".py").replace("/", ".")
            labels.append(mod)
    return sorted(set(labels))


def parse_test_log(text):
    status = {}
    for line in text.splitlines():
        m = STATUS_RE.match(line.rstrip("\r"))
        if m:
            status[m.group(1).strip()] = m.group(2)
    return status


def score(inst, work, python, out_dir):
    """Apply the hidden test patch, run the touched test modules, judge F2P/P2P."""
    f2p, p2p = json.loads(inst["FAIL_TO_PASS"]), json.loads(inst["PASS_TO_PASS"])
    (out_dir / "test_patch.diff").write_text(inst["test_patch"], encoding="utf-8")
    # The agent may have edited the same test files; the hidden tests replace those edits (source fix is what is judged).
    for rel in patched_test_files(inst["test_patch"]):
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", rel], cwd=work, capture_output=True).returncode == 0
        if tracked:
            subprocess.run(["git", "checkout", "-q", "--", rel], cwd=work, capture_output=True)
        elif (work / rel).exists():
            (work / rel).unlink()  # agent-created file at the hidden test's path would make `git apply` fail
    apply = subprocess.run(["git", "apply", "--whitespace=nowarn", str(out_dir / "test_patch.diff")], cwd=work,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
    result = {"test_patch_applied": apply.returncode == 0, "apply_stderr": apply.stderr[-1000:]}
    if apply.returncode != 0:
        result.update(resolved=False, f2p_pass=0, f2p_total=len(f2p), p2p_pass=0, p2p_total=len(p2p))
        return result
    labels = test_labels(inst["test_patch"])
    cmd = [str(python), "tests/runtests.py", "--settings=test_sqlite", "-v", "2", "--parallel", "1", *labels]
    t0 = time.time()
    env = {**os.environ, "PYTHONPATH": str(work)}
    try:
        p = subprocess.run(cmd, cwd=work, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
        log = p.stdout + "\n" + p.stderr
        result["test_exit"] = p.returncode
    except subprocess.TimeoutExpired as ex:
        log = (ex.stdout or "") + "\n" + (ex.stderr or "") + "\nTIMEOUT"
        result["test_exit"] = "timeout"
    (out_dir / "test-output.txt").write_text(log, encoding="utf-8")
    st = parse_test_log(log)
    f2p_ok = [t for t in f2p if st.get(t) == "ok"]
    p2p_ok = [t for t in p2p if st.get(t) == "ok"]
    result.update(test_seconds=round(time.time() - t0, 1), labels=labels, parsed=len(st),
                  f2p_pass=len(f2p_ok), f2p_total=len(f2p), p2p_pass=len(p2p_ok), p2p_total=len(p2p),
                  f2p_missing=[t for t in f2p if t not in st], p2p_missing=[t for t in p2p if t not in st],
                  resolved=len(f2p_ok) == len(f2p) and len(p2p_ok) == len(p2p))
    return result


class PiRpc:
    def __init__(self, cwd, session_dir, args, out_dir, extra_ext, approve=False):
        cmd = [str(PI), "--mode", "rpc", "--session-dir", str(session_dir), "--provider", args.model.split("/")[0],
               "--model", args.model.split("/", 1)[1], "--thinking", args.thinking, "--tools", TOOLS]
        if approve:
            cmd.append("--approve")  # trust project-local files so <cwd>/.pi/sol-pi.json is honoured
        for e in extra_ext:
            cmd += ["--extension", str(e)]
        (out_dir / "pi-command.json").write_text(json.dumps(cmd, indent=1), encoding="utf-8")
        self.stderr = (out_dir / "stderr.txt").open("w", encoding="utf-8")
        self.events = (out_dir / "rpc.jsonl").open("w", encoding="utf-8")
        # Whitelist the environment: the inherited one carries PWD/OLDPWD and tool variables that name the
        # control repo, and agents read `env` when hunting for the benchmark's files.
        keep = ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE",
                "HOMEPATH", "HOME", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
                "USERNAME", "COMPUTERNAME", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "SYSTEMDRIVE", "OS",
                "PI_CODING_AGENT_DIR")
        env = {k: v for k, v in os.environ.items() if k.upper() in keep}
        env["PYTHONPATH"] = str(cwd)  # django importable for the agent's own test runs
        # Black-hole proxy for everything the agent runs in bash (curl/git/pip/requests honour these);
        # Pi's own provider calls are unaffected because Node ignores proxy env unless NODE_USE_ENV_PROXY=1.
        # pi-ai honours the same variables (dist/utils/node-http-proxy.js), so the model provider host is exempted.
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
            env[k] = "http://127.0.0.1:9"
        # provider hosts: opencode-go -> opencode.ai; openai-codex -> chatgpt.com (+ openai.com for OAuth refresh)
        env["NO_PROXY"] = env["no_proxy"] = "opencode.ai,chatgpt.com,openai.com,localhost,127.0.0.1"
        if args.model.startswith("cursor-host/"):
            # The Cursor SDK authenticates with the user's API key; it lives in the user environment, not in this shell.
            key = os.environ.get("CURSOR_API_KEY") or subprocess.run(
                ["powershell", "-NoProfile", "-Command", "[Environment]::GetEnvironmentVariable('CURSOR_API_KEY','User')"],
                capture_output=True, text=True).stdout.strip()
            env["CURSOR_API_KEY"] = key
            env["PI_CURSOR_HOST_DEBUG"] = "1"  # one stderr line per request: plan=fresh|resume|followup
            if os.environ.get("PI_CURSOR_HOST_IDLE_MS"):  # short value forces a release + transcript replay between tasks
                env["PI_CURSOR_HOST_IDLE_MS"] = os.environ["PI_CURSOR_HOST_IDLE_MS"]
            env["NO_PROXY"] = env["no_proxy"] = env["NO_PROXY"] + ",cursor.sh,cursor.com"
        self.proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
                                     text=True, encoding="utf-8", errors="replace", bufsize=1)
        self.q = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.proc.stdout:
            self.events.write(line)
            self.events.flush()
            try:
                self.q.put(json.loads(line))
            except ValueError:
                pass

    def wait(self, pred, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                ev = self.q.get(timeout=1)
            except queue.Empty:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"pi exited with {self.proc.returncode}")
                continue
            if pred(ev):
                return ev
        raise TimeoutError("rpc wait timeout")

    def command(self, kind, ident, **kw):
        self.proc.stdin.write(json.dumps({"type": kind, "id": ident, **kw}, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        resp = self.wait(lambda e: e.get("type") == "response" and e.get("id") == ident, 120)
        if not resp.get("success"):
            raise RuntimeError(f"{kind} failed: {resp}")
        return resp.get("data", {})

    def prompt_and_settle(self, ident, text, timeout):
        self.command("prompt", ident, message=text)
        self.wait(lambda e: e.get("type") == "agent_settled", timeout)
        for _ in range(60):
            st = self.command("get_state", ident + "-state")
            if not st.get("isStreaming") and not st.get("isCompacting") and not st.get("pendingMessageCount"):
                return st
            time.sleep(2)
        raise RuntimeError("session did not become idle after agent_settled")

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=20)
        except Exception:
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True)
        self.stderr.close()
        self.events.close()


def build_prompt(inst, base, python):
    return (
        f"当前目录是 django/django 仓库的工作副本，已 checkout 到 commit {base}。请修复下面的问题并让相关测试通过。\n"
        f"运行测试的方式：`{python} tests/runtests.py --settings=test_sqlite <test label>`，例如 `forms_tests.widget_tests`；"
        "环境变量 PYTHONPATH 已指向本仓库根目录，无需安装。\n"
        "要求：只修改 django/ 下的源码和必要的测试文件；不要 git commit；只在当前目录内读写和搜索，不要访问当前目录之外的任何路径；"
        "完成后简要说明改了什么、跑了哪些测试及结果。\n\n<issue>\n" + inst["problem_statement"].strip() + "\n</issue>"
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", required=True, choices=sorted(ARMS))
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--instances", required=True)
    ap.add_argument("--base-repo", default=str(REPO / "tmp/context-prune-bench/django-base"))
    ap.add_argument("--python", default="C:/pib/venv/Scripts/python.exe",
                    help="interpreter named in the prompt; keep it outside the control repo (a copy of tmp/context-prune-bench/venv)")
    ap.add_argument("--work-root", default="C:/pib",
                    help="where the agent's work trees live: <work-root>/<batch>/<run>/django. Must be outside the control repo: "
                         "Pi loads AGENTS.md from every ancestor directory, and agents have used sibling run dirs, "
                         "the baseline JSON and old work trees to look up the hidden tests")
    ap.add_argument("--model", default="openai-codex/gpt-5.6-luna")
    ap.add_argument("--thinking", default="medium")
    ap.add_argument("--task-timeout", type=int, default=2400)
    args = ap.parse_args()

    run_dir = Path(args.run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    by_id = {i["instance_id"]: i for i in data["instances"]}
    CACHE_KEY.update({i["base_commit"]: i["instance_id"] for i in data["instances"]})
    ids = [s.strip() for s in args.instances.split(",") if s.strip()]
    missing = [i for i in ids if i not in by_id]
    if missing:
        sys.exit(f"unknown instances: {missing}")
    work = Path(args.work_root).resolve() / run_dir.parent.name / run_dir.name / "django"
    work.parent.mkdir(parents=True, exist_ok=True)
    python = Path(args.python).resolve()
    exts, solpi_cfg = ARMS[args.arm]
    run = {"arm": args.arm, "model": args.model, "thinking": args.thinking, "instances": ids, "tools": TOOLS, "work": str(work),
           "extensions": [str(e) for e in exts], "solpi": solpi_cfg, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "tasks": []}
    (run_dir / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=1), encoding="utf-8")

    reset_tree(work, by_id[ids[0]]["base_commit"], args.base_repo)
    (work / ".pi").mkdir(exist_ok=True)
    # The pruner is installed and enabled user-wide (production). A project entry for the same package wins over the
    # global one (docs/packages.md), so an empty extensions filter here keeps every arm from loading the production
    # copy; arms that want a pruner load their own build via --extension. Needs --approve (project trust) in RPC mode.
    (work / ".pi/settings.json").write_text(json.dumps({"packages": [
        {"source": "npm:@syansunyang/pi-context-prune@1.5.1", "extensions": [], "skills": [], "prompts": [], "themes": []}]}, indent=1), encoding="utf-8")
    if solpi_cfg:
        (work / ".pi/sol-pi.json").write_text(json.dumps(solpi_cfg, indent=1), encoding="utf-8")
    # A trusted project APPEND_SYSTEM.md replaces the global one (resource-loader discoverAppendSystemPromptFile), so
    # every round runs under the same frozen style prompt even if ~/.pi/agent/APPEND_SYSTEM.md changes mid-batch.
    frozen = REPO / "tmp/context-prune-bench/APPEND_SYSTEM.frozen-20260916.md"
    (work / ".pi/APPEND_SYSTEM.md").write_text(frozen.read_text(encoding="utf-8"), encoding="utf-8")
    rpc = PiRpc(work, run_dir / "sessions", args, run_dir, exts, approve=True)
    try:
        state = rpc.command("get_state", "state0")
        (run_dir / "state0.json").write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        for n, iid in enumerate(ids, 1):
            inst = by_id[iid]
            tdir = run_dir / f"task{n}-{iid}"
            tdir.mkdir(exist_ok=True)
            reset_tree(work, inst["base_commit"], args.base_repo)
            prompt = build_prompt(inst, inst["base_commit"], python.as_posix())
            (tdir / "prompt.txt").write_text(prompt, encoding="utf-8")
            t0 = time.time()
            rec = {"instance": iid, "started": t0}
            try:
                rpc.prompt_and_settle(f"task{n}", prompt, args.task_timeout)
                rec["agent_seconds"] = round(time.time() - t0, 1)
                rec["agent_error"] = None
            except Exception as ex:  # keep the run alive for scoring of what exists
                rec["agent_seconds"] = round(time.time() - t0, 1)
                rec["agent_error"] = f"{type(ex).__name__}: {ex}"
                rpc.command("abort", f"abort{n}") if rpc.proc.poll() is None else None
            diff = sh(["git", "diff"], cwd=work, check=False).stdout
            (tdir / "agent.patch").write_text(diff, encoding="utf-8")
            rec["untracked"] = sh(["git", "ls-files", "--others", "--exclude-standard"], cwd=work, check=False).stdout.split()
            rec["diff_chars"] = len(diff)
            try:
                rec["score"] = score(inst, work, python, tdir)
            except Exception as ex:
                rec["score"] = {"error": f"{type(ex).__name__}: {ex}", "resolved": False}
            (tdir / "score.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
            run["tasks"].append(rec)
            (run_dir / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"[{args.arm}] task{n} {iid} agent={rec['agent_seconds']}s resolved={rec['score'].get('resolved')} "
                  f"f2p={rec['score'].get('f2p_pass')}/{rec['score'].get('f2p_total')} p2p={rec['score'].get('p2p_pass')}/{rec['score'].get('p2p_total')}", flush=True)
            if rpc.proc.poll() is not None:
                run["aborted"] = f"pi exited with {rpc.proc.returncode}"
                break
        try:
            stats = rpc.command("get_session_stats", "stats")
            (run_dir / "session-stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")
            state = rpc.command("get_state", "state1")
            run["sessionFile"] = state.get("sessionFile")
        except Exception as ex:
            run["stats_error"] = str(ex)
    finally:
        rpc.close()
        run["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        (run_dir / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
