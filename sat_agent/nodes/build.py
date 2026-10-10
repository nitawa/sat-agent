"""Build agent nodes (a LangGraph subgraph, see graph.py).

    entry ──► setup_platform ──► apply_guidance ──► system_deps ──► prepare ──► compile ──► END (build ok)
      │                       (maintainer's            │              │           │
      │                        instructions, once)     └──────────────┴─────┬─────┘ failure
      │                                                   ▼
      └──► apply_feedback ◄─────────────┐               fix ──(fixed)──► rebuild ──► back to the phase
           (test agent reports)          │                 │                          (or apply_feedback)
                                         │            (give up)
                                         └─────────── failure_gate: retry with hint | skip | abort
"""

from pathlib import Path

from langgraph.graph import END
from langgraph.types import Command, interrupt

from sat_agent import git_workspace as gw
from sat_agent import pyconf_edit as pe
from sat_agent.agents import prompts
from sat_agent.agents.react import run_agent
from sat_agent.context import GuardViolation
from sat_agent.diagnose import pre_analysis
from sat_agent.logs import log
from sat_agent.parsers import first_errors, parse_check_system, parse_compile, parse_prepare, strip_ansi
from sat_agent.platform import choose_reference_platform
from sat_agent.sysdeps import SysdepsReport, apply_replacement, resolve_missing, seed_from_reference_dockerfile
from sat_agent.tools.build_tools import FixSession
from sat_agent.tools.common import make_common_tools


def _ctx(config):
    return config["configurable"]["ctx"]


def _policy(ctx) -> dict:
    return ctx.cfg["policy"]


def _transcript(ctx, what: str) -> Path:
    """logs/agents/<time>-<what>.json: the whole conversation of one agent run."""
    import time
    return ctx.logs_dir / "agents" / f"{time.strftime('%Y%m%d-%H%M%S')}-{what}.json"


def _commit(ctx, message: str) -> None:
    try:
        ctx.commit(message)
    except GuardViolation as e:
        log.error("%s", e)
        raise


# ------------------------------------------------------------------------- entry

def entry(state, config):
    if state.get("feedback"):
        return Command(goto="apply_feedback")
    return Command(update={"build_status": "running"}, goto="setup_platform")


# ------------------------------------------------------------------------- setup

def setup_platform(state, config):
    """Registers the platform in sat_salome, inheriting everything from the
    closest already-supported platform."""
    ctx = _ctx(config)
    app_file = ctx.app_file()
    app_text = app_file.read_text(encoding="utf-8")
    platforms = pe.app_platforms(app_text)
    sys_text = ctx.product_file("salome_system").read_text(encoding="utf-8")
    # the reference must be a platform the application itself supports (its `platform` list, else its
    # __overwrite__ blocks): MEDCOUPLING-master-native has blocks up to FD42 only, so FD45 starts from
    # FD42's choices, not from an FD44 the application knows nothing about
    app_known = list(dict.fromkeys(platforms + [d for b in pe.app_overwrite_blocks(app_text) for d in b.dists]))
    known = app_known or list(dict.fromkeys(pe.system_info_dists(sys_text)))
    ref = state.get("ref_dist") or choose_reference_platform(ctx.dist, [p for p in known if p != ctx.dist],
                                                            ctx.family)
    if state.get("done", {}).get("setup"):
        return Command(update={"ref_dist": ref, "platforms": platforms}, goto="apply_guidance")
    log.info("registering %s in %s (reference platform: %s)", ctx.dist, app_file.name, ref or "none")

    app_text = pe.app_add_platform(app_text, ctx.dist)
    if ref:
        app_text = pe.app_inherit_overwrites(app_text, ref, ctx.dist)
    else:
        app_text = pe.app_ensure_dedicated_block(app_text, ctx.dist)
    app_file.write_text(app_text, encoding="utf-8")

    if ref:
        for f in sorted((ctx.sat_salome_dir / "products").glob("*.pyconf")):
            text = f.read_text(encoding="utf-8", errors="replace")
            if f'"{ref}"' not in text and f"'{ref}'" not in text:
                continue
            if ref in pe.system_info_dists(text) and ctx.dist not in pe.system_info_dists(text):
                f.write_text(pe.clone_system_info_block(text, ref, ctx.dist), encoding="utf-8")
                log.info("  %s: system_info.\"%s\" copied from \"%s\"", f.name, ctx.dist, ref)

    report = SysdepsReport()
    ref_df = None
    if ref:
        cands = sorted((ctx.sat_salome_dir / "dockers").glob(f"SALOMEPlatform.Dockerfile.{ref.lower()}-*"))
        ref_df = next((c for c in cands if c.name.endswith(ctx.arch)), cands[0] if cands else None)
    seed_from_reference_dockerfile(ctx, ref_df, report)
    _commit(ctx, f"Register platform {ctx.dist} ({ctx.osr.pretty_name}), inherited from {ref or 'nothing'}")
    done = dict(state.get("done") or {}, setup=True)
    return Command(update={"ref_dist": ref, "platforms": platforms, "done": done,
                           "sysdeps": _merge_report(state.get("sysdeps"), report)}, goto="apply_guidance")


# ------------------------------------------------------------------------- maintainer's instructions

def apply_guidance(state, config):
    """Applies, before anything is built, the instructions of the port request
    that are up-front changes (product versions, options, packages...). The
    agents keep seeing the instructions in their system prompt afterwards."""
    ctx = _ctx(config)
    req = getattr(ctx, "request", None)
    done = dict(state.get("done") or {})
    if req is None or not req.has_guidance or ctx.llm is None or done.get("guidance") == req.digest():
        return Command(goto="system_deps")
    log.info("build agent: applying the maintainer's instructions")
    ref = state.get("ref_dist")
    session = FixSession(ctx, ref)
    system = prompts.fmt(prompts.BUILD_SYSTEM, ctx, ref, state.get("platforms") or [])
    task = prompts.GUIDANCE_TASK.format(dist=ctx.dist, ref=ref or "nothing")
    outcome = run_agent(ctx.llm, system, task, make_common_tools(ctx) + session.tools(upfront=True),
                        _policy(ctx)["agent_iteration_cap"], name="build:guidance",
                        transcript=_transcript(ctx, "guidance"))
    record = {"key": "guidance:None", "phase": "guidance", "product": None, "status": outcome.get("status"),
              "summary": outcome.get("summary", "")}
    _keep_in_scope(ctx, session, record)
    done["guidance"] = req.digest()
    update = {"done": done}
    if session.actions or session.packages:
        record["status"] = "applied"
        _commit(ctx, "Maintainer's instructions: " + record["summary"][:72])
        update["fixes"] = (state.get("fixes") or []) + [record]
        log.info("instructions applied: %s", record["summary"][:200])
    else:
        log.info("no up-front change for the instructions: %s", record["summary"][:200])
    return Command(update=update, goto="system_deps")


def _keep_in_scope(ctx, session, record: dict) -> bool:
    """Reverts the session's sat_salome changes that could affect another
    platform. False if something had to be reverted."""
    record.update(actions=session.actions, patches=session.patches, packages=session.packages)
    violations = ctx.guard_violations()
    if not violations:
        return True
    for v in violations:
        gw.restore_file(ctx.sat_salome_dir, v.split(":", 1)[0])
    log.error("reverted out-of-scope changes:\n  %s", "\n  ".join(violations))
    record["summary"] += " [reverted: out-of-scope changes " + "; ".join(violations) + "]"
    return False


def _merge_report(old: dict | None, rep: SysdepsReport) -> dict:
    old = dict(old or {})
    old["installed"] = sorted(set(old.get("installed", [])) | set(rep.installed))
    old["replaced"] = {**old.get("replaced", {}), **rep.replaced}
    old["excluded"] = sorted(set(old.get("excluded", [])) | set(rep.excluded))
    old["unresolved"] = sorted(set(rep.unresolved))
    return old


# ------------------------------------------------------------------------- system deps

def system_deps(state, config):
    """`sat config <APP> --check_system` until nothing is missing."""
    ctx = _ctx(config)
    report = SysdepsReport()
    unresolved: list[str] = []
    previous = None
    for i in range(_policy(ctx)["sysdeps_passes"]):
        r = ctx.sat.check_system()
        cs = parse_check_system(r.output)
        log.info("check_system pass %d: %d OK, %d missing (%d runtime, %d build)", i + 1, len(cs.ok),
                 len(cs.missing), len(cs.run_ko), len(cs.build_ko))
        if not cs.missing:
            unresolved = []
            break
        if cs.missing == previous:
            unresolved = cs.missing
            break
        previous = cs.missing
        unresolved = resolve_missing(ctx, cs.missing, report)
    report.unresolved = unresolved
    _commit(ctx, "System prerequisites (check_system)")
    update = {"sysdeps": _merge_report(state.get("sysdeps"), report)}
    if unresolved:
        update["failure"] = {"phase": "sysdeps", "product": None, "packages": unresolved,
                             "message": "Still reported missing by `sat config --check_system` after "
                                        "installing every package that exists: " + " ".join(unresolved)}
        return Command(update=update, goto="fix")
    update["done"] = dict(state.get("done") or {}, sysdeps=True)
    if state.get("stop_after") == "sysdeps":
        return Command(update={**update, "build_status": "stopped"}, goto=END)
    return Command(update=update, goto="prepare")


# ------------------------------------------------------------------------- prepare

def _excerpt(output: str, product: str | None, lines: int = 60) -> str:
    text = strip_ansi(output).splitlines()
    if product:
        idx = [i for i, ln in enumerate(text) if product in ln]
        if idx:
            s = max(0, idx[0] - 5)
            return "\n".join(text[s: s + lines])
    return "\n".join(text[-lines:])


def prepare(state, config):
    ctx = _ctx(config)
    sources = ctx.app_workdir / "SOURCES"
    complete = sources.is_dir() and any(sources.iterdir())
    log.info("sat prepare %s%s", ctx.app, " --complete" if complete else "")
    r = ctx.sat.prepare(complete=complete, timeout=_policy(ctx)["command_timeout_s"] * 4)
    pr = parse_prepare(r.output)
    if r.ok and pr.sources_ok and pr.patches_ok:
        update = {"done": dict(state.get("done") or {}, prepare=True)}
        if state.get("stop_after") == "prepare":
            return Command(update={**update, "build_status": "stopped"}, goto=END)
        return Command(update=update, goto="compile")
    if pr.missing_native:
        pkgs = sorted({p for v in pr.missing_native.values() for p in v})
        report = SysdepsReport()
        if not resolve_missing(ctx, pkgs, report):
            _commit(ctx, "System prerequisites of native products")
            return Command(update={"sysdeps": _merge_report(state.get("sysdeps"), report)}, goto="prepare")
    if pr.failed_patches:
        product, patch = pr.failed_patches[0]
        msg = f"Patch {patch} does not apply on the sources of {product}.\n" + _excerpt(r.output, patch)
    elif pr.failed_sources:
        product = pr.failed_sources[0]
        msg = (f"sat could not get the sources of {product} (failed: {' '.join(pr.failed_sources)}).\n"
               + _excerpt(r.output, product))
    else:
        product, msg = None, "sat prepare failed:\n" + _excerpt(r.output, None, 120)
    return Command(update={"failure": {"phase": "prepare", "product": product, "message": msg,
                                       "log_file": r.log_path}}, goto="fix")


# ------------------------------------------------------------------------- compile

def newest_log(ctx, product: str) -> Path | None:
    d = ctx.app_workdir / "LOGS" / product
    if not d.is_dir():
        return None
    files = [f for f in d.iterdir() if f.is_file()]
    return max(files, key=lambda f: f.stat().st_mtime) if files else None


def compile_failure(ctx, output: str, log_path: str | None) -> dict | None:
    pc = parse_compile(output)
    if pc.ok:
        return None
    if pc.missing_deps:
        return {"phase": "compile", "product": None, "packages": pc.missing_deps,
                "message": "sat refuses to compile: missing compile time dependencies " + " ".join(pc.missing_deps)}
    failed = pc.failed or ([(pc.last, "?")] if pc.last and newest_log(ctx, pc.last) else [])
    if failed:
        product, step = failed[0]
        lf = newest_log(ctx, product)
        text = lf.read_text(encoding="utf-8", errors="replace") if lf else ""
        errs = first_errors(text)
        tail = "\n".join(strip_ansi(text).splitlines()[-40:])
        msg = (f"Compilation of {product} failed at step {step}.\n"
               f"Log: LOGS/{product}/{lf.name if lf else '?'}\nFirst errors:\n" + ("\n=====\n".join(errs[:6]) or "-")
               + f"\n\nEnd of log:\n{tail}")
        analysis = pre_analysis(ctx, product, text) if text else ""
        if analysis:
            msg += "\n\nAutomatic pre-analysis:\n" + analysis
        return {"phase": "compile", "product": product, "step": step, "message": msg,
                "log_file": str(lf) if lf else log_path}
    return {"phase": "compile", "product": None, "message": "sat compile failed before compiling any product:\n"
            + _excerpt(output, None, 80), "log_file": log_path}


def compile_(state, config):
    ctx = _ctx(config)
    log.info("sat compile %s --stop_first_fail", ctx.app)
    r = ctx.sat.compile(stop_first_fail=True, timeout=_policy(ctx)["compile_timeout_s"])
    failure = compile_failure(ctx, r.output, r.log_path)
    if failure is None and r.ok:
        pc = parse_compile(r.output)
        log.info("compilation of %s complete (%d/%d)", ctx.app, pc.done, pc.total)
        return Command(update={"build_status": "ok", "failure": None}, goto=END)
    if failure is None:
        failure = {"phase": "compile", "product": None, "message": _excerpt(r.output, None, 80)}
    if failure.get("packages") and not failure.get("product"):
        report = SysdepsReport()
        key = "deps:compile"
        attempts = dict(state.get("attempts") or {})
        attempts[key] = attempts.get(key, 0) + 1
        if attempts[key] <= 2 and not resolve_missing(ctx, failure["packages"], report):
            _commit(ctx, "System prerequisites required by sat compile")
            return Command(update={"attempts": attempts, "sysdeps": _merge_report(state.get("sysdeps"), report)},
                           goto="compile")
    return Command(update={"failure": failure}, goto="fix")


# ------------------------------------------------------------------------- fix

def _task(ctx, state, failure: dict) -> str:
    phase, product = failure["phase"], failure.get("product")
    lines = [f"Platform {ctx.dist} ({ctx.osr.pretty_name}), application {ctx.app}.",
             f"Application workdir: {ctx.app_workdir}", ""]
    if phase == "sysdeps":
        lines += ["`sat config --check_system` still reports these system packages missing; they do not "
                  "exist under that name on this release and no obvious replacement was found:",
                  "  " + " ".join(failure.get("packages") or []), "",
                  "For each one: find the package that replaces it on this release (search_packages, "
                  "package_providing, web; mind case changes such as python3-Cython -> python3-cython) and "
                  "call replace_system_package(old, new): it installs the new name and rewrites the "
                  "declaration for this platform only. If the platform genuinely does not need/provide it, "
                  "call replace_system_package(old, new='') to drop it here. Or switch the product needing "
                  "it to a source build (set_product_version). Use grep on sat_salome/products to see which "
                  "product declares a package."]
    elif phase == "prepare":
        lines += [f"`sat prepare` failed for product {product or '?'}.", failure.get("message", "")]
    elif phase == "compile":
        lines += [f"`sat compile` failed for product {product or '?'} (step {failure.get('step') or '?'}).",
                  failure.get("message", "")]
    else:
        fb = failure.get("feedback") or {}
        lines += ["The test agent reports failing non-regression tests that need a build-side fix:",
                  f"tests: {', '.join(fb.get('tests') or [])}", f"category: {fb.get('category')}",
                  f"product: {fb.get('product')}", f"diagnosis: {fb.get('diagnosis')}",
                  f"suggested fix: {fb.get('fix')}", "", failure.get("message", "")]
    key = f"{phase}:{product}"
    previous = [f for f in (state.get("fixes") or []) + (state.get("gave_up") or []) if f.get("key") == key]
    if previous:
        lines += ["", "Previous attempts on this same failure (do NOT repeat a fix that did not work):"]
        lines += [f"- {p.get('status')}: {p.get('summary')}" for p in previous[-4:]]
    if state.get("hint"):
        lines += ["", f"Hint from the maintainer: {state['hint']}"]
    return "\n".join(lines)


def _last_run(path: Path) -> str:
    """Output of the last command appended to a sat log (sections start with `### <date> $ <cmd>`)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    i = text.rfind("\n### ")
    return text[i + 1:] if i >= 0 else text


def _rederive(ctx, failure: dict) -> dict:
    """A compile failure without product (checkpointed by an older parser, or sat's output was not
    understood): read the last compile output again."""
    if failure.get("phase") != "compile" or failure.get("product"):
        return failure
    for name in ("compile_fix.log", "compile.log"):
        lp = Path(failure.get("log_file") or "") if name == "compile.log" and failure.get("log_file") else \
            ctx.logs_dir / name
        if lp.is_file():
            again = compile_failure(ctx, _last_run(lp), str(lp))
            if again and again.get("product"):
                log.info("failing product identified from %s: %s", lp.name, again["product"])
                return again
    return failure


def _products_only(ctx, names, what: str) -> set[str]:
    """Keeps the names that are products of the application (the model may hallucinate)."""
    keep = set()
    for n in names or []:
        n = str(n).strip()
        if not n:
            continue
        try:
            ok = ctx.sat.product_info(n).get("exists")
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            keep.add(n)
        else:
            log.warning("ignoring %s %r: not a product of %s", what, n, ctx.app)
    return keep


def fix(state, config):
    ctx = _ctx(config)
    failure = _rederive(ctx, dict(state.get("failure") or {}))
    phase, product = failure.get("phase"), failure.get("product")
    key = f"{phase}:{product}"
    attempts = dict(state.get("attempts") or {})
    attempts[key] = attempts.get(key, 0) + 1
    limit = _policy(ctx)["compile_fix_attempts"] if phase in ("compile", "test") else \
        _policy(ctx)["prepare_fix_attempts"]
    if attempts[key] > limit:
        log.warning("%s: %d fix attempts exhausted", key, limit)
        return Command(update={"failure": failure, "attempts": attempts}, goto="failure_gate")
    if ctx.llm is None:
        return Command(update={"failure": failure, "attempts": attempts}, goto="failure_gate")

    log.info("build agent: fixing %s (attempt %d/%d)", key, attempts[key], limit)
    ref = state.get("ref_dist")
    session = FixSession(ctx, ref)
    system = prompts.fmt(prompts.BUILD_SYSTEM, ctx, ref, state.get("platforms") or [])
    outcome = run_agent(ctx.llm, system, _task(ctx, state, failure),
                        make_common_tools(ctx) + session.tools(), _policy(ctx)["agent_iteration_cap"],
                        name=f"build:{product or phase}", transcript=_transcript(ctx, f"{phase}-{product}"))
    record = {"key": key, "phase": phase, "product": product, "status": outcome.get("status"),
              "summary": outcome.get("summary", ""), "actions": [], "patches": [], "packages": []}
    if outcome.get("status") == "fixed":
        try:
            session.finalize(outcome.get("patch_title", ""))
        except Exception as e:  # noqa: BLE001
            log.error("could not register the fix: %s", e)
            outcome["status"] = "give_up"
            record["summary"] += f" [registration failed: {e}]"
        # never keep a change that could affect another platform
        if not _keep_in_scope(ctx, session, record):
            outcome["status"] = "give_up"
    record.update(actions=session.actions, patches=session.patches, packages=session.packages,
                  status=outcome.get("status"))
    if outcome.get("status") != "fixed":
        return Command(update={"failure": failure, "attempts": attempts, "gave_up": (state.get("gave_up") or []) + [record]},
                       goto="failure_gate")

    _commit(ctx, f"{phase} fix for {product or 'platform'}: {record['summary'][:72]}")
    rebuild = _products_only(ctx, outcome.get("rebuild") or [], "product to rebuild") | session.rebuild
    if phase == "compile" and product:
        rebuild.add(product)
    prep = set(session.prepare)
    if phase == "prepare" and product:
        prep.add(product)
    pending = {"prepare": sorted(prep), "compile": sorted(rebuild) if phase in ("compile", "test") else []}
    log.info("fix applied: %s", record["summary"][:200])
    return Command(update={"failure": failure, "attempts": attempts, "fixes": (state.get("fixes") or []) + [record],
                           "pending": pending, "hint": None}, goto="rebuild")


# ------------------------------------------------------------------------- rebuild

def _return_target(phase: str) -> str:
    return {"sysdeps": "system_deps", "prepare": "prepare", "compile": "compile"}.get(phase, "apply_feedback")


def rebuild(state, config):
    ctx = _ctx(config)
    failure = dict(state.get("failure") or {})
    phase = failure.get("phase", "compile")
    pending = state.get("pending") or {}
    if pending.get("prepare"):
        log.info("sat prepare -p %s", ",".join(pending["prepare"]))
        r = ctx.sat.prepare(pending["prepare"])
        pr = parse_prepare(r.output)
        if not (r.ok and pr.sources_ok and pr.patches_ok):
            product = pr.failed_patches[0][0] if pr.failed_patches else pending["prepare"][0]
            msg = ("After the fix, `sat prepare` fails (does the new patch apply on pristine sources? it is "
                   "applied after the product's other patches):\n" + _excerpt(r.output, product, 80))
            new_failure = {**failure, "product": product if phase != "test" else failure.get("product"),
                           "message": msg}
            return Command(update={"failure": new_failure, "pending": {}}, goto="fix")
    if pending.get("compile"):
        log.info("sat compile -p %s --clean_all", ",".join(pending["compile"]))
        r = ctx.sat.compile(pending["compile"], clean_all=True, stop_first_fail=True,
                            timeout=_policy(ctx)["compile_timeout_s"], step="compile_fix")
        f2 = compile_failure(ctx, r.output, r.log_path)
        if f2 is not None or not r.ok:
            f2 = f2 or {"phase": "compile", "product": pending["compile"][0], "message": _excerpt(r.output, None)}
            if phase == "test":
                f2 = {**failure, "message": "The rebuild after the fix failed:\n" + f2.get("message", "")}
            return Command(update={"failure": f2, "pending": {}}, goto="fix")
    target = _return_target(phase)
    update = {"pending": {}}
    if phase != "test":
        update["failure"] = None
    return Command(update=update, goto=target)


# ------------------------------------------------------------------------- gate

def failure_gate(state, config):
    ctx = _ctx(config)
    failure = state.get("failure") or {}
    if state.get("batch"):
        action = {"action": _policy(ctx).get("on_failure", "abort")}
    else:
        action = interrupt({"kind": "build_failure", "phase": failure.get("phase"),
                            "product": failure.get("product"), "message": failure.get("message", "")[-4000:],
                            "last": ((state.get("gave_up") or [{}])[-1]).get("summary")})
    act = action.get("action", "abort")
    key = f"{failure.get('phase')}:{failure.get('product')}"
    if act == "retry":
        attempts = dict(state.get("attempts") or {})
        attempts[key] = 0
        return Command(update={"attempts": attempts, "hint": action.get("hint")}, goto="fix")
    if act == "skip":
        return _skip(state, ctx, failure)
    log.error("build aborted at %s", key)
    return Command(update={"build_status": "aborted" if not state.get("batch") else "failed",
                           "aborted": not state.get("batch")}, goto=END)


def _skip(state, ctx, failure: dict):
    phase, product = failure.get("phase"), failure.get("product")
    skipped = list(state.get("skipped") or [])
    if phase == "sysdeps":
        for pkg in failure.get("packages") or []:
            apply_replacement(ctx, pkg, None)
            skipped.append(f"system package {pkg}")
        _commit(ctx, "Exclude unavailable system packages: " + " ".join(failure.get("packages") or []))
        return Command(update={"skipped": skipped, "failure": None,
                               "done": dict(state.get("done") or {}, sysdeps=True)}, goto="prepare")
    if phase == "test":
        queue = list(state.get("feedback") or [])[1:]
        return Command(update={"skipped": skipped + [f"test fix {product}"], "failure": None, "feedback": queue},
                       goto="apply_feedback")
    if not product:
        return Command(update={"build_status": "failed"}, goto=END)
    app = ctx.app_file()
    app.write_text(pe.app_add_rm_product(app.read_text(encoding="utf-8"), ctx.dist, product), encoding="utf-8")
    _commit(ctx, f"Remove {product} from {ctx.app} on {ctx.dist} (could not be ported)")
    log.warning("%s removed from %s on %s", product, ctx.app, ctx.dist)
    return Command(update={"skipped": skipped + [product], "failure": None}, goto=_return_target(phase))


# ------------------------------------------------------------------------- test feedback

def apply_feedback(state, config):
    """Consumes the test agent's actionable reports one by one."""
    ctx = _ctx(config)
    queue = list(state.get("feedback") or [])
    if state.get("failure", {}) and (state.get("failure") or {}).get("phase") == "test":
        queue = queue[1:]           # the head was just handled by fix -> rebuild
    while queue:
        item = queue[0]
        if item.get("category") == "missing_system_package" and item.get("packages"):
            status = ctx.installable(item["packages"])
            ok = [p for p in item["packages"] if status.get(p)]
            installed, _ = ctx.install_packages(ok, "runtime dependency found by tests")
            if installed:
                ctx.record_system_dep(installed, runtime=True)
                _commit(ctx, "Runtime prerequisites found by the tests: " + " ".join(installed))
                fixes = (state.get("fixes") or []) + [{"key": "test:packages", "phase": "test",
                                                        "product": None, "status": "fixed",
                                                        "summary": item.get("diagnosis", ""),
                                                        "packages": installed, "actions": [], "patches": []}]
                state = {**state, "fixes": fixes}
                queue = queue[1:]
                continue
        failure = {"phase": "test", "product": item.get("product") or None, "feedback": item,
                   "message": item.get("output", "")[-6000:]}
        return Command(update={"feedback": queue, "failure": failure, "fixes": state.get("fixes") or []},
                       goto="fix")
    return Command(update={"feedback": [], "failure": None, "build_status": "ok",
                           "fixes": state.get("fixes") or []}, goto=END)
