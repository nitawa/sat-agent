"""sat-agent command line.

    sat-agent port fedora:45                 # port SALOME-master-native to Fedora 45
    sat-agent port --prompt "Port SALOME-9.15.0-native to Ubuntu 26.10, use the native boost"
    sat-agent port --prompt-file request.txt --dry-run   # show how the request is understood
    sat-agent port ubuntu:26.10 --batch      # unattended (policy.on_failure decides)
    sat-agent port debian:14 --resume        # continue the last run of that platform
    sat-agent guard FD45                     # re-check the non-regression guard of a port
    sat-agent -w /data/sat-agent port fedora:45   # another working directory than the config's
"""

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

from sat_agent.config import load_config
from sat_agent.logs import log, setup_logging


def get_config(args) -> dict:
    """The config file, then the command-line overrides (which always win)."""
    cfg = load_config(args.config)
    if getattr(args, "work_root", None):
        # relative to where the command is run, not to the config file
        cfg["paths"]["work_root"] = str(Path(args.work_root).expanduser().resolve())
    return cfg


def _checkpointer(path: Path):
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
        return SqliteSaver(sqlite3.connect(str(path), check_same_thread=False))
    except ImportError:
        from langgraph.checkpoint.memory import MemorySaver
        log.warning("langgraph-checkpoint-sqlite not installed: --resume will not be available")
        return MemorySaver()


def ask_gate(payload: dict) -> dict:
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
        for _ in app.stream(inp, config=config, stream_mode="updates", subgraphs=True):
            pass
        snap = app.get_state(config, subgraphs=True)
        pending = _find_interrupt(snap)
        if pending is None:
            return dict(snap.values)
        inp = Command(resume=ask_gate(pending))


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


def cmd_port(args) -> int:
    from sat_agent import request as rq
    from sat_agent.context import open_port
    from sat_agent.git_workspace import BranchInUse
    from sat_agent.graph import build_graph
    from sat_agent.llm import LLMClient
    from sat_agent.state import initial_state
    from sat_agent.tools.web import Web
    from sat_agent.docker_env import docker_available

    cfg = get_config(args)
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
    if not docker_available():
        log.error("docker is not available (docker info failed)")
        return 2
    if llm is None:
        log.warning("no LLM configured: failures will go straight to the failure gate")
    rq.save(req, Path(cfg["paths"]["work_root"]))
    try:
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
    config = {"configurable": {"thread_id": thread, "ctx": ctx}, "recursion_limit": 1000}
    try:
        final = drive(app, inp, config)
    except KeyboardInterrupt:
        log.warning("interrupted; resume with: sat-agent port %s --resume", req.image)
        return 130
    if final.get("report_path"):
        print(Path(final["report_path"]).read_text(encoding="utf-8"))
    status = final.get("build_status")
    failed = [n for n, s in ((final.get("tests") or {}).get("results") or {}).items()
              if s not in ("passed", "skipped")]
    log.info("build %s, %d failing test(s); report %s", status, len(failed), final.get("report_path"))
    if ctx.llm is not None:
        u = ctx.llm.usage
        log.info("LLM: %d call(s), %d input + %d output tokens", u["calls"], u["prompt_tokens"],
                 u["completion_tokens"])
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
    pp.add_argument("--verify-dockerfile", action="store_true",
                    help="rebuild the exported Dockerfile from scratch at the end and re-check it")
    pg = sub.add_parser("guard", help="check that a port does not change any other platform")
    pg.add_argument("dist", help="platform code, e.g. FD45")
    for sp in (pp, pg):      # also accepted after the command: sat-agent port fedora:45 -w DIR
        sp.add_argument("-w", "--work-root", metavar="DIR", default=argparse.SUPPRESS,
                        help="working directory (overrides paths.work_root of the config file)")
    args = p.parse_args(argv)
    cfg_dir = Path(get_config(args)["paths"]["work_root"])
    cfg_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(args.verbose, str(cfg_dir / "sat-agent.log"))
    try:
        return cmd_port(args) if args.cmd == "port" else cmd_guard(args)
    except Exception as e:  # noqa: BLE001 — one clean line instead of a traceback
        log.debug("fatal", exc_info=True)
        log.error("%s: %s", type(e).__name__, e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
