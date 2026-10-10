"""LangGraph StateGraph for SAT Compilation Agent."""

from pathlib import Path
import re
import shutil

from sat_agent.config import AppConfig
from sat_agent.docker_manager import DockerfileGenerator
from sat_agent.executor_client import SATExecutorClient
from sat_agent.llm import LLMClient
from sat_agent.nodes.assembler import assemble_build_result
from sat_agent.nodes.gates import plan_gate, retry_help_gate
from sat_agent.nodes.planner import plan_compilation
from sat_agent.nodes.report import generate_compile_report
from sat_agent import pkg_managers
from sat_agent.patch_manager import PatchManager
from sat_agent.pkg_search import AUR_PREFIX, derive_name_from_debian_and_rpm, verify_system_package
from sat_agent.prereq_resolver import DebianPackageTranslator
from sat_agent.pyconf_manager import PyconfManager, read_system_deps_cache, write_system_deps_cache
from sat_agent.sat_patcher import ensure_check_system_product_headers, ensure_pacman_support
from sat_agent.specialists.compile import CompileSpecialist, parse_show_order
from sat_agent.specialists.config import ConfigSpecialist
from sat_agent.specialists.patch import propose_source_patch
from sat_agent.specialists.prepare import PrepareSpecialist
from sat_agent.specialists.verify import VerificationSpecialist
from sat_agent.system_check import parse_check_system_output
from sat_agent.verify.log_parser import find_product_log, parse_compiler_errors_text, read_latest_session_log
from sat_agent.verify.static import verify_static_sat_command

CONTAINER_WORKDIR = DockerfileGenerator.CONTAINER_WORKDIR


def _verify_and_cache_system_packages(
    raw_debian_pkgs: list[str],
    suggested_pkgs: set[str],
    distro: str,
    sat_salome_dir: Path,
    executor_client: SATExecutorClient,
    work_dir: str,
    image_tag: str,
) -> list[str]:
    """For each raw Debian package name behind a mechanically-translated
    entry of suggested_pkgs, confirms (or corrects) the real package name
    for `distro` in real time against the live package manager in the
    current image (pkg_managers.profile_for_distro -- only distros with a
    registered profile get here, see the `has_sat_project`/profile gate at
    the call site), and persists what was learned to sat_salome/products/
    system_deps.pyconf so a later run/retry doesn't need to re-query or
    re-guess the same package. Entries of suggested_pkgs that didn't come
    from a raw Debian name (i.e. SystemPrereqResolver's already-correct,
    curated header-based matches) are left untouched.
    """
    cache_path = sat_salome_dir / "products" / "system_deps.pyconf"
    cache = read_system_deps_cache(cache_path)
    distro_cache = cache.setdefault(distro, {})
    changed = False

    for debian_pkg in raw_debian_pkgs:
        mechanical_guess = DebianPackageTranslator.translate(debian_pkg, distro)

        if debian_pkg in distro_cache:
            resolved = distro_cache[debian_pkg]
        else:
            verified = verify_system_package(
                debian_pkg, [mechanical_guess, debian_pkg], distro,
                executor_client, work_dir, image_tag,
            )
            resolved = verified or mechanical_guess
            distro_cache[debian_pkg] = resolved
            changed = True
            how = "real-time verified" if verified else "best-effort guess, unverified"
            print(f"[+] Self-Healing: '{debian_pkg}' -> '{resolved}' on {distro} ({how})")

        suggested_pkgs.discard(mechanical_guess)
        suggested_pkgs.add(resolved)

    if changed:
        write_system_deps_cache(cache_path, cache)

    return sorted(suggested_pkgs)


def _write_compile_issues_report(work_dir_path: Path, compile_issues: list[dict]) -> Path:
    """Writes a Markdown summary of every product whose compile failure
    couldn't be resolved by package self-healing or an LLM source patch --
    each with its real log excerpt, ready for a human (or an interactive
    Claude Code session with real web-search access, which this standalone
    CLI process does not have) to investigate, fix, and fill in the
    Reference column with what actually solved it.
    """
    lines = [
        "# Compile issues",
        "",
        "Products whose build failed and were not auto-resolved by package",
        "self-healing or an LLM-authored source patch. For each, the real",
        "compiler/build log excerpt is included below -- investigate (a web",
        "search on the exact error line is usually the fastest path), apply",
        "a fix, and record it here with a link to what solved it.",
        "",
    ]
    for issue in compile_issues:
        lines += [
            f"## {issue['product']}",
            "",
            f"- **Failing step:** `{issue['log_kind']}`",
            "- **Solution:** _not yet resolved_",
            "- **Reference:** _none yet_",
            "",
            "```",
            issue["log_excerpt"],
            "```",
            "",
        ]
    report_path = work_dir_path / "compile_issues.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def _write_missing_system_packages_report(work_dir_path: Path, unresolved: list[dict]) -> Path:
    """Writes a Markdown summary of every system package `run_system_check_stage`
    could not confirm anywhere -- not via an exact-name guess, not via the
    Debian/RPM cross-reference (pkg_search.py: derive_name_from_debian_and_rpm),
    not via the network description-based search, and not on the AUR.

    The last-resort guess still gets baked into the Dockerfile and attempted
    (harmless if wrong -- see DockerfileGenerator's per-package install loop),
    but it's genuinely unconfirmed, so it's also listed here for a human (or
    an interactive Claude Code session) to resolve manually.
    """
    lines = [
        "# Missing system packages",
        "",
        "System packages `sat config --check_system` reported as needed that",
        "sat-agent could not confirm on this distro through any of its",
        "resolution methods (exact-name match, Debian/RPM name",
        "cross-reference, network description search, AUR). The name shown",
        "as \"best-effort guess\" was still attempted in the Dockerfile, but",
        "is unverified and may well be wrong or nonexistent -- please",
        "confirm the real package name and update",
        "`sat_salome/products/system_deps.pyconf` (or the product's own",
        "pyconf `system_info.pacman`/`pacman_dev`) accordingly.",
        "",
        "| Product | Debian package | Best-effort guess (unverified) |",
        "|---|---|---|",
    ]
    for item in unresolved:
        lines.append(f"| {item['product']} | `{item['debian_pkg']}` | `{item['guess']}` |")

    report_path = work_dir_path / "missing-system-packages.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def run_sat_agent(user_request: str, cfg: AppConfig, run_id: str, work_dir_path: Path) -> dict:
    docker_gen = DockerfileGenerator(distro=cfg["sat"]["distro"])

    sat_root = cfg["sat"].get("root")
    project_root = cfg["sat"].get("project_root")
    has_sat_project = bool(sat_root and Path(sat_root).is_dir() and project_root and Path(project_root).is_dir())
    if has_sat_project:
        # Copy SAT and SAT_SALOME into this run's own work_dir so the Docker
        # build context is self-contained and isolated per run -- specialists
        # write/patch pyconf files into this copy, never the shared checkouts.
        shutil.copytree(sat_root, work_dir_path / "sat_tool", ignore=shutil.ignore_patterns(".git"), dirs_exist_ok=True)
        shutil.copytree(project_root, work_dir_path / "sat_salome", ignore=shutil.ignore_patterns(".git"), dirs_exist_ok=True)
        docker_gen.ensure_sat_distro_known(work_dir_path / "sat_tool")
        ensure_pacman_support(work_dir_path / "sat_tool")
        ensure_check_system_product_headers(work_dir_path / "sat_tool")
        docker_gen.set_project_paths("sat_tool", "sat_salome")

    docker_gen.save(work_dir_path / "Dockerfile")

    llm = LLMClient(
        base_url=cfg["llm"]["base_url"],
        api_key=cfg["llm"]["api_key"],
        model=cfg["llm"]["model"],
        temperature=cfg["llm"]["temperature"],
        timeout=cfg["llm"]["timeout"],
    )

    executor_client = SATExecutorClient()
    image_tag = f"sat_build_{run_id}".lower()
    exec_timeout = cfg["policy"].get("exec_timeout_s", 600)
    exec_retries = cfg["policy"].get("exec_retries", 3)

    # Step 1: Planning
    plan = plan_compilation(user_request, llm)

    state = {
        "run_id": run_id,
        "user_request": user_request,
        "config": cfg,
        "plan": plan,
        "stage_idx": 0,
        "stages": {},
        "history": [],
    }

    # Step 2: Plan Gate
    gate_res = plan_gate(state)
    if gate_res == "abort":
        return {"fatal": "Run aborted by user during plan gate."}

    # Specialists map
    specialists = {
        "config": ConfigSpecialist(llm),
        "prepare": PrepareSpecialist(llm),
        "compile": CompileSpecialist(llm),
        "environment": VerificationSpecialist(llm),
    }

    app_match = re.search(r'(SALOME-[A-Za-z0-9\.\-_]+)', user_request, re.IGNORECASE)
    # The character class allows '.', so a name at the end of a sentence
    # ("...SALOME-9.16.0-native. Pour cela...") greedily swallows the
    # sentence's trailing period -- strip stray trailing punctuation so the
    # name actually matches a real SAT_SALOME applications/*.pyconf file.
    conf_name = app_match.group(1).rstrip(".,;:!?") if app_match else "CUSTOM_APP"

    # Explicit escape hatch: a request can name an APPLICATION.properties.*
    # override directly, e.g. "...en mettant l'option
    # APPLICATION.properties.git_server='github'..." -- properties.git_server
    # is what src/__init__.py: get_git_server() reads to pick which
    # git_info.repositories mirror `sat prepare` clones from (tuleap/github/
    # gitpub), so this is how a request routes around a CEA-internal-only
    # git server. Matched loosely (with or without the "APPLICATION." prefix)
    # so both `APPLICATION.properties.X='Y'` and `properties.X: 'Y'` work.
    property_overrides = dict(re.findall(
        r"properties\.(\w+)\s*[=:]\s*['\"]([^'\"]+)['\"]", user_request, re.IGNORECASE,
    ))

    context = {
        "work_dir": work_dir_path,
        "conf_name": conf_name,
        "sat_salome_dir": work_dir_path / "sat_salome",
        "property_overrides": property_overrides,
    }

    execute_live = cfg["policy"].get("execute_live", False)
    patch_mgr = PatchManager(work_dir_path / "sat_salome") if has_sat_project else None
    compile_issues: list[dict] = []
    unresolved_system_packages: list[dict] = []

    # Where the actual SALOME sources/build trees land on the host (bind-mounted
    # to /salome_build in every container). Keyed by app+distro (not run_id) and
    # taken from sat.build_dir, so re-running the same request reuses previously
    # cloned sources/build dirs instead of starting from scratch every time.
    build_workspace = Path(cfg["sat"].get("build_dir", "~/.local/share/sat-agent/builds")).expanduser() / f"{conf_name}-{docker_gen.distro}"
    build_workspace.mkdir(parents=True, exist_ok=True)
    host_build_state = build_workspace
    print(f"Build workspace (SOURCES/BUILD/INSTALL, reused across runs): {build_workspace}")

    def bake(baked_cmd: str, comment: str) -> None:
        docker_gen.add_stage_run(baked_cmd, comment=comment)
        docker_gen.save(work_dir_path / "Dockerfile")

    executor_reachable = True
    if execute_live:
        # Materialize the base image (FROM + packages + COPY SAT/SAT_SALOME)
        # once, before any stage, so the first run_in_container has an image.
        base_build = executor_client.build_image(str(work_dir_path), image_tag, timeout=exec_timeout)
        executor_reachable = "Failed to communicate with sat_executor" not in (base_build.get("error", "") or "")
        if executor_reachable and not base_build["ok"]:
            return {"fatal": f"Failed to build base Docker image: {base_build.get('error')}"}

    def run_one_command(cmd: str, mod: str, stage_comment: str, product: str | None = None) -> dict:
        """Runs cmd through the shared exec-verify + self-heal (missing
        packages, then -- for a 'compile' failure -- an LLM source patch) +
        Dockerfile-baking retry loop.

        `product`, when given (one specific product during the compile
        stage), is used to look up ITS OWN build log
        (verify/log_parser.py: find_product_log) and record it in
        compile_issues when nothing else resolves the failure, instead of
        just an opaque "Unknown build error".

        Returns {"ok", "cmd" (the command that actually succeeded, if it
        did -- may differ from `cmd` if a retry appended "-f"), "attempts",
        "last_err", "stdout" (of the last successful run, for callers that
        need to parse it, e.g. compile's --show reconnaissance)}.
        """
        current_cmd = cmd
        attempt = 0
        last_exec_err = None

        if not execute_live or not executor_reachable:
            # Dry-run (or no live sat_executor server): record the intended
            # command in the reproducible Dockerfile without ever invoking
            # Docker, and accept it as OK.
            bake(current_cmd, stage_comment)
            return {"ok": True, "cmd": current_cmd, "attempts": 0, "last_err": None, "stdout": ""}

        # Sync the image with whatever the specialist just wrote on the
        # host -- ConfigSpecialist may have just written a brand-new
        # application/product pyconf into work_dir/sat_salome, and the
        # base image (built before this stage ran) doesn't contain it yet.
        # Cheap when nothing changed (COPY layer cache hit).
        sync_build = executor_client.build_image(str(work_dir_path), image_tag, timeout=exec_timeout)
        if not sync_build.get("ok"):
            return {"ok": False, "cmd": current_cmd, "attempts": 0, "last_err": sync_build, "stdout": ""}

        while attempt <= exec_retries:
            run_res = executor_client.run_in_container(
                current_cmd, str(work_dir_path), image_tag,
                container_workdir=CONTAINER_WORKDIR, timeout=exec_timeout,
                mount_dir=str(build_workspace),
            )

            if run_res.get("ok"):
                bake(current_cmd, stage_comment)
                bake_res = executor_client.build_image(str(work_dir_path), image_tag, timeout=exec_timeout)
                if bake_res.get("ok"):
                    return {"ok": True, "cmd": current_cmd, "attempts": attempt, "last_err": None, "stdout": run_res.get("stdout", "")}
                # The ad-hoc run succeeded but baking it permanently into
                # the image failed: undo the bake and treat it like any
                # other exec failure below.
                docker_gen.pop_stage_run()
                docker_gen.save(work_dir_path / "Dockerfile")
                run_res = bake_res

            last_exec_err = run_res
            # SAT redirects the real subprocess output (cmake/make errors,
            # git output) to its own session log (LOGS/OUT/*.txt), not
            # necessarily to the terminal -- read it too, or self-healing
            # never actually sees the interesting errors for a failed
            # compile/prepare.
            session_log = read_latest_session_log(build_workspace / "LOGS" / "OUT")
            combined_output = (
                (run_res.get("stdout", "") or "") + "\n" + (run_res.get("stderr", "") or "") + "\n" + session_log
            )
            log_analysis = parse_compiler_errors_text(combined_output, distro=cfg["sat"]["distro"])
            suggested_pkgs = log_analysis.get("suggested_packages", [])

            attempt += 1
            if attempt > exec_retries:
                break

            err_lines = (run_res.get("error", "") or "").strip().splitlines()
            first_err = err_lines[-1] if err_lines else "Unknown build error"
            print(f"[!] Stage [{mod}] failed (Attempt {attempt}/{exec_retries}): {first_err}")

            if suggested_pkgs:
                if pkg_managers.profile_for_distro(docker_gen.distro) and has_sat_project:
                    suggested_pkgs = _verify_and_cache_system_packages(
                        log_analysis.get("raw_missing_packages", []), set(suggested_pkgs), docker_gen.distro,
                        context["sat_salome_dir"], executor_client, str(work_dir_path), image_tag,
                    )
                official_pkgs = [p for p in suggested_pkgs if not p.startswith(AUR_PREFIX)]
                aur_pkgs = [p for p in suggested_pkgs if p.startswith(AUR_PREFIX)]
                print(f"[+] Self-Healing: Identified missing system package(s): {suggested_pkgs}. Installing them into the build image...")
                docker_gen.add_packages(official_pkgs)
                if aur_pkgs:
                    docker_gen.add_aur_packages(aur_pkgs)
                docker_gen.save(work_dir_path / "Dockerfile")
                base_build = executor_client.build_image(str(work_dir_path), image_tag, timeout=exec_timeout)
                if not base_build.get("ok"):
                    last_exec_err = base_build
                    break
                continue

            if mod == "compile" and patch_mgr and log_analysis.get("errors"):
                proposed = propose_source_patch(log_analysis, llm, host_build_state, container_workdir=CONTAINER_WORKDIR)
                if proposed:
                    patched_product = proposed["product"]
                    print(f"[+] Self-Healing: Real source error in {patched_product}. Asking LLM for a patch...")
                    patch_file = patch_mgr.save_patch(patched_product, f"{patched_product}_selfheal_{attempt}.patch", proposed["diff"])
                    product_file = context["sat_salome_dir"] / "products" / f"{patched_product}.pyconf"
                    if product_file.exists():
                        try:
                            patch_mgr.register_patch_in_product_file(product_file, patch_file.name)
                        except ValueError as e:
                            print(f"[!] Self-Healing: could not register patch in {product_file}: {e}")

                    # No --force_patch needed: without --complete, `sat
                    # prepare` doesn't skip already-prepared products by
                    # default, so a plain re-run already re-applies
                    # patches (--force_patch only matters for products in
                    # dev mode -- commands/prepare.py: ldev_products).
                    reapply_cmd = f"sat prepare {conf_name} -p {patched_product}"
                    reapply_res = executor_client.run_in_container(
                        reapply_cmd, str(work_dir_path), image_tag,
                        container_workdir=CONTAINER_WORKDIR, timeout=exec_timeout,
                        mount_dir=str(build_workspace),
                    )
                    if reapply_res.get("ok"):
                        bake(reapply_cmd, f"Self-heal: reapply patch for {patched_product}")
                        rebake = executor_client.build_image(str(work_dir_path), image_tag, timeout=exec_timeout)
                        if not rebake.get("ok"):
                            docker_gen.pop_stage_run()
                            docker_gen.save(work_dir_path / "Dockerfile")

                    # current_cmd already has --clean_all baked in (see
                    # CompileSpecialist.build_product_command), which is
                    # what actually forces a fresh rebuild against the
                    # freshly re-prepared/patched sources.
                    continue

            # No self-heal path applied for this failure. If we know which
            # product this is, surface its real build log instead of an
            # opaque "Unknown build error" and record it for
            # compile_issues.md -- this is genuinely a case for a human (or
            # an interactive session with real web-search access) to
            # investigate; this standalone process can't search the web.
            if product:
                found = find_product_log(build_workspace, product)
                if found:
                    kind, content = found
                    tail = "\n".join(content.splitlines()[-30:])
                    print(f"[!] {product}: real build failure in its '{kind}' log (last 30 lines):\n{tail}")
                    compile_issues.append({
                        "product": product,
                        "log_kind": kind,
                        "log_excerpt": "\n".join(content.splitlines()[-80:]),
                    })
            break

        return {"ok": False, "cmd": current_cmd, "attempts": attempt, "last_err": last_exec_err, "stdout": ""}

    def run_system_check_stage() -> dict:
        """Runs `sat config <app> --check_system` and, for every product it
        reports a missing (KO) system package for, resolves the real name
        for this distro (pkg_search.py: verify_system_package -- live
        package-manager search/AUR, via whichever profile
        pkg_managers.profile_for_distro returns for this distro) and
        persists it two ways: into sat_salome/products/system_deps.pyconf
        (sat-agent's own cache) AND directly into that PRODUCT's own
        pyconf, so `sat` itself reports the product as satisfied once the
        resolved package is installed.

        Which pyconf key the resolved name is registered under depends on
        whether `sat` already natively understands this manager
        (pkg_managers.PackageManagerProfile.natively_supported_by_sat):
        for a manager `sat` has no native concept of at all (pacman), the
        resolved name goes into `system_info.<manager>`/`<manager>_dev`,
        mirroring the real, shipped `apt`/`apt_dev` convention (see
        sat_patcher.py: ensure_pacman_support, which teaches `sat` to read
        that key). For an RPM-based manager (zypper/openSUSE) `sat`
        already natively checks installed status via `rpm -q` (src/
        system.py), so the resolved name instead goes straight into the
        `system_info.rpm`/`rpm_dev` keys `sat` already reads -- no source
        patch needed for that case at all.

        The check output doesn't distinguish which of a product's KO
        packages are runtime vs. build-time deps (check_install_system
        prints both through the identical "- pkg : status" line with no
        separating marker) -- registered under BOTH keys of the pair so
        convergence doesn't depend on guessing that right; `sat`'s check
        for either category just needs the resolved name to be present in
        ITS list.

        `sat config --check_system` always exits 0 (a report, not a hard
        failure), so this isn't run through run_one_command's retry loop
        -- it loops itself, re-running --check_system after installing
        resolved packages, until nothing is missing or retries run out.
        """
        distro = docker_gen.distro
        profile = pkg_managers.profile_for_distro(distro)
        if not execute_live or not executor_reachable or profile is None or not has_sat_project:
            return {"ok": True}

        pkg_key, pkg_key_dev = (
            ("rpm", "rpm_dev") if profile.natively_supported_by_sat else (profile.name, f"{profile.name}_dev")
        )

        check_cmd = f"sat config {conf_name} --check_system"
        products_dir = context["sat_salome_dir"] / "products"

        for attempt in range(exec_retries + 1):
            run_res = executor_client.run_in_container(
                check_cmd, str(work_dir_path), image_tag,
                container_workdir=CONTAINER_WORKDIR, timeout=exec_timeout,
                mount_dir=str(build_workspace),
            )
            missing = parse_check_system_output(run_res.get("stdout", ""))
            if not missing:
                return {"ok": True}

            total = sum(len(v) for v in missing.values())
            print(f"[+] System check: {total} missing system package(s) across {len(missing)} product(s) (pass {attempt + 1}/{exec_retries + 1})")

            cache_path = products_dir / "system_deps.pyconf"
            cache = read_system_deps_cache(cache_path)
            distro_cache = cache.setdefault(distro, {})
            cache_changed = False
            official_pkgs: set[str] = set()
            aur_pkgs: set[str] = set()

            for product, debian_pkgs in missing.items():
                product_file = products_dir / f"{product}.pyconf"
                for debian_pkg in debian_pkgs:
                    if debian_pkg in distro_cache:
                        resolved = distro_cache[debian_pkg]
                    else:
                        # Cross-reference the product's OWN already-known
                        # RPM name(s) (sat natively supports RPM distros,
                        # so most products declare system_info.rpm/rpm_dev)
                        # against the Debian name -- entirely local, no
                        # network call -- before resorting to a live
                        # network description lookup inside
                        # verify_system_package.
                        rpm_names = (
                            PyconfManager.read_nested_list(product_file, "system_info", "rpm")
                            + PyconfManager.read_nested_list(product_file, "system_info", "rpm_dev")
                        )
                        rpm_derived_guess = derive_name_from_debian_and_rpm(debian_pkg, rpm_names)
                        mechanical_guess = DebianPackageTranslator.translate(debian_pkg, distro)
                        guesses = [mechanical_guess]
                        if rpm_derived_guess and rpm_derived_guess not in guesses:
                            guesses.append(rpm_derived_guess)
                        if debian_pkg not in guesses:
                            guesses.append(debian_pkg)

                        verified = verify_system_package(
                            debian_pkg, guesses, distro,
                            executor_client, str(work_dir_path), image_tag,
                        )
                        resolved = verified or mechanical_guess
                        distro_cache[debian_pkg] = resolved
                        cache_changed = True
                        if verified:
                            how = "real-time verified"
                        else:
                            how = "best-effort guess, unverified"
                            unresolved_system_packages.append({"product": product, "debian_pkg": debian_pkg, "guess": resolved})
                        print(f"[+] System check: '{debian_pkg}' (needed by {product}) -> '{resolved}' on {distro} ({how})")

                    bare = resolved[len(AUR_PREFIX):] if resolved.startswith(AUR_PREFIX) else resolved
                    if product_file.exists():
                        try:
                            PyconfManager.add_to_nested_list(product_file, "system_info", pkg_key, bare)
                            PyconfManager.add_to_nested_list(product_file, "system_info", pkg_key_dev, bare)
                        except Exception as e:
                            print(f"[!] System check: could not update {product_file}: {e}")

                    (aur_pkgs if resolved.startswith(AUR_PREFIX) else official_pkgs).add(resolved)

            if cache_changed:
                write_system_deps_cache(cache_path, cache)

            if official_pkgs:
                docker_gen.add_packages(sorted(official_pkgs))
            if aur_pkgs:
                docker_gen.add_aur_packages(sorted(aur_pkgs))
            docker_gen.save(work_dir_path / "Dockerfile")

            rebuild = executor_client.build_image(str(work_dir_path), image_tag, timeout=exec_timeout)
            if not rebuild.get("ok"):
                return {"ok": False, "last_err": rebuild}

        return {"ok": False, "last_err": {"error": "system dependencies still missing after all retries"}}

    def run_compile_stage(stage_plan: dict, idx: int) -> dict:
        """Compile stage, restructured around real dependency order instead
        of one opaque --with_fathers run covering the whole tree at once:
        1. Reconnaissance: `sat compile ... --with_fathers --show` (no
           actual build) to get the real, ordered list of products still
           needing compilation (specialists/compile.py: parse_show_order).
        2. Compile them one at a time, in that order, each baked into the
           Dockerfile individually on success -- by the time product N is
           reached, every dependency earlier in the order is already
           built, so no --with_fathers is needed per individual call.
        Stops at the first product that can't be resolved (its failure
        blocks everything later in the order anyway).
        """
        specialist = specialists["compile"]
        show_res = specialist.run_stage(stage_plan, context)
        show_cmd = show_res.get("command_script", "")
        stage_comment = f"Stage {idx + 1} [compile] reconnaissance: {stage_plan.get('intent', '')}"

        static_v = verify_static_sat_command(show_cmd)
        if not static_v["ok"]:
            return {"ok": False, "cmd": show_cmd, "attempts": 0, "last_err": static_v}

        recon = run_one_command(show_cmd, "compile", stage_comment)
        if not recon["ok"] or not execute_live or not executor_reachable:
            return recon

        products_to_build = parse_show_order(recon.get("stdout", ""))
        print(f"[+] Compile order ({len(products_to_build)} product(s) still to build): {products_to_build}")

        result = recon
        for product in products_to_build:
            product_cmd = specialist.build_product_command(stage_plan, context, product)
            product_comment = f"Stage {idx + 1} [compile]: build {product}"
            result = run_one_command(product_cmd, "compile", product_comment, product=product)
            if not result["ok"]:
                print(f"[!] Compile stopped at '{product}' -- later products in the order depend on it.")
                break

        return result

    # Loop through stages
    while state["stage_idx"] < len(plan):
        idx = state["stage_idx"]
        stage_plan = plan[idx]
        mod = stage_plan["module"]

        if mod == "compile":
            result = run_compile_stage(stage_plan, idx)
        else:
            specialist = specialists.get(mod, ConfigSpecialist(llm))
            res = specialist.run_stage(stage_plan, context)
            cmd = res.get("command_script", "")
            stage_comment = f"Stage {idx + 1} [{mod}]: {stage_plan.get('intent', '')}"

            static_v = verify_static_sat_command(cmd)
            if not static_v["ok"]:
                state["last_error"] = static_v
                action = retry_help_gate(state)
                if action == "abort":
                    return {"fatal": f"Static verification failed: {static_v['error']}"}

            result = run_one_command(cmd, mod, stage_comment)

        stage_ok = result["ok"]
        cmd = result["cmd"]
        attempt = result["attempts"]
        last_exec_err = result["last_err"]

        if not stage_ok and last_exec_err:
            state["last_error"] = last_exec_err
            action = retry_help_gate(state)
            if action == "abort":
                return {"fatal": f"Build stage [{mod}] failed: {last_exec_err.get('error')}"}

        if mod == "config" and stage_ok:
            # Resolve every system dependency for the whole application
            # upfront (sat config <app> --check_system), before `prepare`
            # ever runs -- rather than discovering them piecemeal, one
            # opaque failure at a time, during prepare/compile.
            sys_check = run_system_check_stage()
            if not sys_check.get("ok"):
                state["last_error"] = sys_check.get("last_err") or {"error": "system dependency check did not converge"}
                action = retry_help_gate(state)
                if action == "abort":
                    return {"fatal": f"System dependency check failed: {state['last_error'].get('error')}"}

        # Record stage output
        state["stages"][idx] = {
            "module": mod,
            "status": "ok" if stage_ok else "failed",
            "command_script": cmd,
            "static_retries": 0,
            "exec_retries": attempt,
        }

        state["stage_idx"] += 1

    # Step 3: Assembler & Report
    asm_res = assemble_build_result(state, work_dir_path)
    rep_res = generate_compile_report(state, work_dir_path)
    if compile_issues:
        issues_path = _write_compile_issues_report(work_dir_path, compile_issues)
        print(f"[+] {len(compile_issues)} unresolved compile issue(s) written to {issues_path}")
    if unresolved_system_packages:
        missing_pkgs_path = _write_missing_system_packages_report(work_dir_path, unresolved_system_packages)
        print(f"[+] {len(unresolved_system_packages)} unresolved system package(s) written to {missing_pkgs_path}")

    return {
        "run_id": run_id,
        "final_script": asm_res["final_script"],
        "report": rep_res["report"],
    }
