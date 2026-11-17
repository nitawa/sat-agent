"""sat-agent command line.

    sat-agent port fedora:45                 # port SALOME-master-native to Fedora 45
    sat-agent port --prompt "Port SALOME-9.15.0-native to Ubuntu 26.10, use the native boost"
    sat-agent port --prompt-file request.txt --dry-run   # show how the request is understood
    sat-agent port ubuntu:26.10 --batch      # unattended (policy.on_failure decides)
    sat-agent port debian:14 --resume        # continue the last run of that platform
    sat-agent port fedora:45 --trace         # + full trace of steps, LLM messages and tool calls
    sat-agent guard FD45                     # re-check the non-regression guard of a port
    sat-agent -w /data/sat-agent port fedora:45   # another working directory than the config's
    sat-agent -w ~/bench/qwen3-32b port fedora:45 --batch --model qwen3:32b   # another model
    sat-agent bench ~/bench/*                # compare the token usage of those runs
    sat-agent port --local --app SALOME-master-windows   # Windows: port this machine, without docker
    sat-agent port --local                   # macOS / Linux: port this machine (brew / apt, dnf... with sudo)
    sat-agent port --msys2 C:/msys64         # Windows: port its MSYS2 environment (UCRT64 / MINGW64: the console's)
    sat-agent port                           # (in an MSYS2 UCRT64 / MINGW64 console: the same, found from it)
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from sat_agent.config import load_config
from sat_agent.logs import log, setup_logging
from sat_agent.trace import tracer


def get_config(args) -> dict:
    """The config file, then the command-line overrides (which always win)."""
    cfg = load_config(args.config)
    if getattr(args, "work_root", None):
        # relative to where the command is run, not to the config file
        cfg["paths"]["work_root"] = str(Path(args.work_root).expanduser().resolve())
    if getattr(args, "model", None):
        cfg["llm"]["model"] = args.model
    if getattr(args, "llm_base_url", None):
        cfg["llm"]["base_url"] = args.llm_base_url
    if getattr(args, "msys2", None):
        cfg["execution"]["msys2"] = args.msys2
    if getattr(args, "msystem", None):
        cfg["execution"]["msystem"] = args.msystem
    return cfg


def _checkpointer(path: Path):
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
        return SqliteSaver(sqlite3.connect(str(path), check_same_thread=False))
    except ImportError:
        from langgraph.checkpoint.memory import MemorySaver
        log.warning("langgraph-checkpoint-sqlite not installed: --resume will not be available")
        return MemorySaver()


def ask_native(payload: dict) -> dict:
    """A native product without a package on the new platform: embed it or not."""
    embed = payload.get("embed") or {}
    print("\n" + "=" * 78, file=sys.stderr)
    print(f"Native product {payload['product']}: no {payload.get('family')} package found on "
          f"{payload.get('pretty') or payload.get('dist')} for {', '.join(payload.get('packages') or [])}",
          file=sys.stderr)
    for e in payload.get("evidence") or []:
        print(f"  web: {e}", file=sys.stderr)
    if payload.get("last_error"):
        print(f"  ! {payload['last_error']}", file=sys.stderr)
    print("=" * 78, file=sys.stderr)
    if not sys.stdin.isatty():
        return {"action": "embed" if embed else "remove"}
    choices = (f"[e]mbed it (build {embed['tag']} from sources: {embed.get('why')}) / " if embed else "") + \
        "[p]ackage name / [n]ative without package / [r]emove it on this platform ? "
    while True:
        ans = input(choices).strip().lower()
        if ans.startswith("e") and embed:
            return {"action": "embed"}
        if ans.startswith("p"):
            return {"action": "package", "package": input("package name: ").strip()}
        if ans.startswith("n"):
            return {"action": "none"}
        if ans.startswith("r"):
            return {"action": "remove"}


def ask_missing_on_host(payload: dict) -> dict:
    """Local mode: a native product whose packages are not installed on this machine."""
    fam, embed = payload.get("family"), payload.get("embed") or {}
    print("\n" + "=" * 78, file=sys.stderr)
    print(f"{payload['product']}: {' '.join(payload['packages'])} not installed on this machine "
          f"({payload.get('pretty')}), available with {fam}.", file=sys.stderr)
    if payload.get("last_error"):
        print(f"  ! {payload['last_error']}", file=sys.stderr)
    print("=" * 78, file=sys.stderr)
    if not sys.stdin.isatty():
        return {"action": "source" if payload.get("buildable") else "skip"}
    src = (f"[s]ource build ({embed['tag']}"
           + (", with the Homebrew formula's patches" if fam == "brew" else "") + ") / ") \
        if payload.get("buildable") else ""
    while True:
        ans = input(f"[i]nstall with {fam} / {src}[k]eep it missing ? ").strip().lower()
        if ans.startswith("i"):
            return {"action": "install"}
        if ans.startswith("s") and src:
            return {"action": "source"}
        if ans.startswith("k"):
            return {"action": "skip"}


def make_ask_install(batch: bool, cfg: dict):
    """Approval of an install on this machine outside the per-product question (a renamed package,
    the agent's install_system_packages): a yes/no question, policy.local_install in batch."""
    def ask(pkgs: list[str], reason: str = "") -> bool:
        if batch or not sys.stdin.isatty():
            return cfg["policy"].get("local_install", "no") == "yes"
        ans = input(f"\nInstall on this machine{f' ({reason})' if reason else ''}: {' '.join(pkgs)} ? [y/N] ")
        return ans.strip().lower().startswith("y")
    return ask


def ask_gate(payload: dict) -> dict:
    if payload.get("kind") == "native_product":
        return ask_native(payload)
    if payload.get("kind") == "missing_on_host":
        return ask_missing_on_host(payload)
    print("\n" + "=" * 78, file=sys.stderr)
    print(f"Build agent stopped: {payload.get('phase')} of {payload.get('product') or 'the platform'}",
          file=sys.stderr)
    print(payload.get("message", "")[-3000:], file=sys.stderr)
    if payload.get("last"):
        print(f"\nLast agent conclusion: {payload['last']}", file=sys.stderr)
    print("=" * 78, file=sys.stderr)
    if not sys.stdin.isatty():
        return {"action": "abort"}
    while True:
        ans = input("[r]etry with a hint / [s]kip (drop it for this platform) / [a]bort ? ").strip().lower()
        if ans.startswith("r"):
            return {"action": "retry", "hint": input("hint for the agent: ").strip()}
        if ans.startswith("s"):
            return {"action": "skip"}
        if ans.startswith("a"):
            return {"action": "abort"}


def drive(app, inp, config) -> dict:
    from langgraph.types import Command

    while True:
        for ns, chunk in app.stream(inp, config=config, stream_mode="updates", subgraphs=True):
            for node, update in (chunk or {}).items():
                tracer.step(ns, node, update)
        snap = app.get_state(config, subgraphs=True)
        pending = _find_interrupt(snap)
        if pending is None:
            return dict(snap.values)
        decision = ask_gate(pending)
        tracer.note("failure gate", json.dumps({"payload": pending, "decision": decision}, indent=1, default=str))
        inp = Command(resume=decision)


def _find_interrupt(snap):
    for task in snap.tasks or ():
        if task.interrupts:
            return task.interrupts[0].value
        sub = getattr(task, "state", None)
        if sub is not None and hasattr(sub, "tasks"):
            found = _find_interrupt(sub)
            if found is not None:
                return found
    return None


def make_request(args, cfg: dict, llm):
    """The PortRequest of this run: from --prompt/--prompt-file, else (on --resume) the one saved by
    the previous run of the image, else just the command line."""
    from sat_agent import request as rq

    prompt = rq.read_prompt(args.prompt, args.prompt_file)
    work_root = Path(cfg["paths"]["work_root"])
    saved = rq.load(args.image, work_root) if args.resume and args.image else None
    if saved is not None and args.app and args.app != saved.application:
        raise rq.RequestError(f"--resume: the run of {args.image} ports {saved.application}, not {args.app}")
    if not prompt and saved is not None:
        log.info("resuming the request saved for %s", args.image)
        return saved
    if prompt:
        log.info("interpreting the request%s", " with the LLM" if llm else "")
    # on --resume, new instructions replace the saved ones but keep its application by default
    return rq.interpret(prompt, Path(cfg["paths"]["sat_salome"]), llm=llm if prompt else None,
                        default_app=saved.application if saved else cfg["application"], image=args.image,
                        application=args.app)


def apply_request(req, cfg: dict, args) -> None:
    """Run options of the request; explicit command-line flags win."""
    cfg["application"] = req.application
    if req.run_tests is not None:
        cfg["tests"]["enabled"] = req.run_tests
    if args.no_tests:
        cfg["tests"]["enabled"] = False
    if req.tests_include:
        cfg["tests"]["include"] = req.tests_include
    if req.tests_exclude:
        cfg["tests"]["exclude"] = req.tests_exclude
    if args.until:
        req.stop_after = args.until
    if args.verify_dockerfile:
        cfg["docker"]["verify_dockerfile"] = True


def confirm(req, args) -> bool:
    print("\nsat-agent understood this request:\n" + req.describe() + "\n", file=sys.stderr)
    if args.yes or args.batch or not sys.stdin.isatty() or not req.prompt:
        return True
    return input("Proceed? [Y/n] ").strip().lower() in ("", "y", "yes", "o", "oui")


def branch_decision(args):
    """What to do with the branches of an earlier port of the platform when the port directory is new."""
    def decide(found) -> str:
        if args.new_branch:
            return "new"
        if args.continue_branch:
            return "continue"
        print("\nThis port directory is new, but an earlier port of this platform left its branches:",
              file=sys.stderr)
        for e in found:
            print(f"  - {e}", file=sys.stderr)
        if args.batch or not sys.stdin.isatty():
            print("Choose with --new-branch (start again; the old branches are kept under a dated name) or "
                  "--continue-branch (keep its commits).", file=sys.stderr)
            return "error"
        while True:
            ans = input("[n]ew branch (old one kept, renamed) / [c]ontinue its commits / [a]bort ? ").strip().lower()
            if ans[:1] in ("n", "c", "a"):
                return {"n": "new", "c": "continue", "a": "error"}[ans[:1]]
    return decide


def start_trace(args, cfg: dict) -> None:
    """--trace / --trace-file (or config trace.enabled / trace.file): opens the trace file of this run."""
    if not (args.trace or args.trace_file or cfg["trace"]["enabled"]):
        return
    path = args.trace_file or cfg["trace"]["file"] or \
        Path(cfg["paths"]["work_root"]) / "traces" / f"trace-{time.strftime('%Y%m%d-%H%M%S')}.log"
    llm = {k: v for k, v in cfg["llm"].items() if k not in ("api_key", "extra_headers")}   # no secrets
    log.info("trace: %s", tracer.open(path, {"argv": sys.argv, "llm": llm}))


def cmd_port(args) -> int:
    from sat_agent import request as rq
    from langgraph.errors import GraphRecursionError

    from sat_agent.context import GuardViolation, open_local_port, open_port
    from sat_agent.git_workspace import BranchInUse
    from sat_agent.graph import build_graph
    from sat_agent.llm import LLMClient, LLMError, run_usage
    from sat_agent.state import initial_state
    from sat_agent.tools.web import Web
    from sat_agent.docker_env import docker_available

    from sat_agent.local_env import msys2_console, no_terminal_hint, under_msys_runtime

    cfg = get_config(args)
    if under_msys_runtime():
        log.error("sat-agent runs on MSYS2's own python (msys runtime), which is only for sat: start it with a "
                  "Windows python, e.g. its venv: .venv/Scripts/sat-agent port --local")
        return 2
    msys2 = str((cfg.get("execution") or {}).get("msys2") or "")
    in_console = os.name == "nt" and msys2.lower() not in ("off", "no") and bool(msys2_console())
    local = getattr(args, "local", False) or (cfg.get("execution") or {}).get("backend") == "local" \
        or (bool(msys2) and msys2.lower() not in ("off", "no")) or in_console
    if (in_console or msys2) and local and not args.batch and not sys.stdin.isatty():
        log.warning("%s", no_terminal_hint())
    if local:
        if args.image and args.image != "local":
            log.error("--local ports this machine itself: no IMAGE (%s)", args.image)
            return 2
        args.image = "local"
    elif os.name == "nt" or sys.platform == "darwin":
        log.error("on Windows and macOS, sat-agent ports this machine itself without docker: use "
                  "`sat-agent port --local` (Windows, MSYS2: `sat-agent port --msys2 <root>`)")
        return 2
    start_trace(args, cfg)
    llm = LLMClient(cfg["llm"]) if cfg["llm"].get("base_url") and cfg["llm"].get("model") else None
    try:
        req = make_request(args, cfg, llm)
    except rq.RequestError as e:
        log.error("%s", e)
        return 2
    if not req.image:
        log.error("no target image: give it as argument (e.g. fedora:45) or name it in the prompt")
        return 2
    apply_request(req, cfg, args)
    if not confirm(req, args):
        return 1
    if args.dry_run:
        print(json.dumps(req.to_dict(), indent=1, ensure_ascii=False))
        return 0
    if not local and not docker_available():
        log.error("docker is not available (docker info failed)")
        return 2
    if llm is None:
        log.warning("no LLM configured: failures will go straight to the failure gate")
    rq.save(req, Path(cfg["paths"]["work_root"]))
    try:
        if local:
            ctx = open_local_port(cfg, llm=llm, web=Web(cfg["web_search"]), on_existing_branch=branch_decision(args))
            ctx.ask_install = make_ask_install(args.batch, cfg)
        else:
            ctx = open_port(cfg, req.image, llm=llm, web=Web(cfg["web_search"]),
                            on_existing_branch=branch_decision(args))
    except BranchInUse as e:
        log.error("%s: use --new-branch or --continue-branch", e)
        return 2
    ctx.request = req

    ckpt = _checkpointer(ctx.port_dir / "checkpoints.sqlite")
    app = build_graph(cfg, ckpt)
    last = ctx.port_dir / "last_thread"
    if args.resume and last.exists():
        thread = last.read_text().strip()
        inp = None
        log.info("resuming run %s", thread)
    else:
        thread = f"{ctx.dist}-{time.strftime('%Y%m%d-%H%M%S')}"
        inp = initial_state(thread, ctx.dist, args.batch, stop_after=req.stop_after)
        last.write_text(thread)
    if llm is not None:
        llm.persist_usage(ctx.port_dir / "llm_usage.json", thread)
    config = {"configurable": {"thread_id": thread, "ctx": ctx}, "recursion_limit": 1000}
    resume = f"sat-agent port {'--local' if local else req.image} --resume"
    try:
        final = drive(app, inp, config)
    except KeyboardInterrupt:
        log.warning("interrupted; resume with: %s", resume)
        return 130
    except (LLMError, GuardViolation, GraphRecursionError) as e:
        # the checkpoint keeps every step done so far: the run can continue once the cause is fixed
        log.debug("run stopped", exc_info=True)
        log.error("run stopped: %s: %s", type(e).__name__, e)
        if isinstance(e, GraphRecursionError):
            log.error("the graph looped without progress (recursion limit %d); see the trace (--trace)",
                      config["recursion_limit"])
        log.error("once the cause is fixed, resume with: %s", resume)
        return 3
    if final.get("report_path"):
        print(Path(final["report_path"]).read_text(encoding="utf-8"))
    status = final.get("build_status")
    failed = [n for n, s in ((final.get("tests") or {}).get("results") or {}).items()
              if s not in ("passed", "skipped")]
    log.info("build %s, %d failing test(s); report %s", status, len(failed), final.get("report_path"))
    for model, u in run_usage(ctx.llm).items():
        log.info("LLM %s: %d call(s), %d input + %d output tokens (%d reasoning), %.0fs (whole run)", model,
                 u["calls"], u["prompt_tokens"], u["completion_tokens"], u["reasoning_tokens"], u["seconds"])
    return 0 if status == "ok" and not failed else 1


def cmd_guard(args) -> int:
    from sat_agent.guard import check_sat_salome

    cfg = get_config(args)
    port = Path(cfg["paths"]["work_root"]) / "ports" / args.dist / "port.json"
    if not port.exists():
        log.error("no port %s under %s", args.dist, port.parent.parent)
        return 2
    info = json.loads(port.read_text())
    errs = check_sat_salome(Path(info["sat_salome"]), info["sat_salome_base"], info["dist"], Path(info["sat"]))
    for e in errs:
        print(e)
    print("OK: no change affects another platform" if not errs else f"{len(errs)} violation(s)")
    return 1 if errs else 0


def cmd_bench(args) -> int:
    from sat_agent import bench

    rows = [r for root in args.roots for r in bench.collect(Path(root).expanduser())]
    if not rows:
        log.error("no llm_usage.json under %s", ", ".join(args.roots))
        return 2
    print(json.dumps(rows, indent=1) if args.json else bench.render(rows, by_phase=args.by_phase))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="sat-agent", description="Port SALOME to a new Linux platform with SAT.")
    p.add_argument("--config", default=None, help="JSON config (default: built-in defaults; see config.example.json)")
    p.add_argument("-w", "--work-root", metavar="DIR",
                   help="working directory of sat-agent (worktrees, builds, logs, checkpoints); overrides "
                        "paths.work_root of the config file")
    p.add_argument("-v", "--verbose", action="count", default=0)
    sub = p.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("port", help="port the application to the platform of a docker image")
    pp.add_argument("image", nargs="?",
                    help="base docker image of the target platform, e.g. fedora:45, ubuntu:26.10 "
                         "(optional if the prompt names it)")
    pp.add_argument("--app", help="SAT application (default: the one named in the prompt, else config "
                                  "'application', SALOME-master-native)")
    pp.add_argument("-p", "--prompt", help="the request in your own words: application, target, recommendations "
                                           "for the agents (any language)")
    pp.add_argument("--prompt-file", metavar="FILE", help="read the request from FILE ('-' = stdin)")
    pp.add_argument("--dry-run", action="store_true", help="only show how the request is understood")
    pp.add_argument("-y", "--yes", action="store_true", help="do not ask to confirm the understood request")
    pp.add_argument("--batch", action="store_true", help="never ask: policy.on_failure decides")
    pp.add_argument("--resume", action="store_true", help="continue the last run of this platform")
    pp.add_argument("--no-tests", action="store_true", help="stop after a successful compilation")
    bp = pp.add_mutually_exclusive_group()
    bp.add_argument("--new-branch", action="store_true",
                    help="if an earlier port of this platform left its sat-agent/<DIST> branches, start new "
                         "ones (the old ones are kept as sat-agent/<DIST>-<date>)")
    bp.add_argument("--continue-branch", action="store_true",
                    help="if an earlier port of this platform left its branches, continue them")
    pp.add_argument("--until", choices=["sysdeps", "prepare"],
                    help="stop after this phase (staged run; continue later without --until)")
    pp.add_argument("--trace", action="store_true",
                    help="write every graph step, every message sent to / received from the LLM and every tool "
                         "call (arguments and output) to <work_root>/traces/trace-<date>.log")
    pp.add_argument("--trace-file", metavar="FILE", help="like --trace, to FILE")
    pp.add_argument("--verify-dockerfile", action="store_true",
                    help="rebuild the exported Dockerfile from scratch at the end and re-check it")
    pp.add_argument("--local", action="store_true",
                    help="port this machine itself, without docker: sat runs here. Windows: cmd / PowerShell / "
                         "Python, Visual Studio, no system packages; macOS: Homebrew; Linux: its package manager "
                         "(sudo). Nothing is installed without your approval")
    pp.add_argument("--msys2", nargs="?", const="auto", metavar="ROOT",
                    help="Windows: port the MSYS2 environment of this machine (implies --local): sat runs with "
                         "MSYS2's python in bash, native products are the environment's pacman packages. ROOT: "
                         "the MSYS2 installation (default: the console's, else C:/msys64); `off`: the Visual "
                         "Studio platform even from an MSYS2 console. Started from an MSYS2 UCRT64/MINGW64 "
                         "console, sat-agent ports it without this option")
    pp.add_argument("--msystem", choices=["UCRT64", "MINGW64"], type=str.upper,
                    help="MSYS2 environment to port (execution.msystem; default: the console's: MSYSTEM of the "
                         "console sat-agent runs in, else msys2.ini, else the installed toolchain, UCRT64 first)")
    pp.add_argument("--model", help="LLM model (overrides llm.model of the config file)")
    pp.add_argument("--llm-base-url", metavar="URL",
                    help="OpenAI-compatible endpoint (overrides llm.base_url of the config file)")
    pb = sub.add_parser("bench", help="compare the LLM token usage of runs (one work root or port directory "
                                      "per model)")
    pb.add_argument("roots", nargs="+", metavar="DIR", help="work roots (-w of the runs) or port directories")
    pb.add_argument("--by-phase", action="store_true", help="also detail each run by phase (prompt, sysdeps, "
                                                            "build:<product>, tests...)")
    pb.add_argument("--json", action="store_true", help="machine-readable output")
    pg = sub.add_parser("guard", help="check that a port does not change any other platform")
    pg.add_argument("dist", help="platform code, e.g. FD45")
    for sp in (pp, pg):      # also accepted after the command: sat-agent port fedora:45 -w DIR
        sp.add_argument("-w", "--work-root", metavar="DIR", default=argparse.SUPPRESS,
                        help="working directory (overrides paths.work_root of the config file)")
    args = p.parse_args(argv)
    try:
        cfg_dir = Path(get_config(args)["paths"]["work_root"])
    except (OSError, ValueError) as e:        # missing / invalid config file: one clean message
        print(f"sat-agent: {e}", file=sys.stderr)
        return 2
    cfg_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(args.verbose, str(cfg_dir / "sat-agent.log"))
    try:
        return {"port": cmd_port, "guard": cmd_guard, "bench": cmd_bench}[args.cmd](args)
    except Exception as e:  # noqa: BLE001 — one clean line instead of a traceback
        log.debug("fatal", exc_info=True)
        log.error("%s: %s", type(e).__name__, e)
        return 2
    finally:
        tracer.close()


if __name__ == "__main__":
    sys.exit(main())
