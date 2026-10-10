"""Action tools of the build agent, and the FixSession that turns its edits
into sat_salome changes scoped to the new platform.

Source edits are tracked (original content kept in memory) and, when the
agent finishes, turned into a unified diff saved as
sat_salome/products/patches/<product>-<title>-<DIST>.patch and registered in
a platform section `<section>_<DIST_SUFFIX>` of the product, selected for
the new platform only from the application's __overwrite__ block:

    'APPLICATION.products.<p>' : {tag: '<tag>', base: 'no', section: '<section>_<SUFFIX>'}

`sat prepare <APP> -p <p>` then re-extracts the sources and applies the
patch list, which validates the patch before the product is recompiled.
"""

import difflib
import re
from pathlib import Path

from sat_agent import pyconf_edit as pe
from sat_agent.agents.react import B, LS, S, Tool, obj
from sat_agent.logs import log

_PREPARE_KEYS = {"patches", "get_source", "git_info", "archive_info", "dir_info"}


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text or "fix").strip("-")
    return (s[:40] or "fix").strip("-")


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def host_paths(ctx) -> list[str]:
    """Paths of this machine that must never be written into sat_salome (they make a pyconf or
    script work only here): port dir, work root, upstream checkouts, home."""
    roots = {str(ctx.port_dir), str(ctx.sat_salome_dir), str(ctx.sat_dir), str(Path.home())}
    for key in ("work_root", "sat", "sat_salome"):
        v = (ctx.cfg.get("paths") or {}).get(key)
        if v:
            roots.add(str(v))
    return sorted((r for r in roots if r and r != "/"), key=len, reverse=True)


def refuse_host_path(ctx, text: str) -> str | None:
    posix = getattr(ctx.env, "posix_path", str)
    for r in host_paths(ctx):
        # Windows paths are also written with "/" (C:/Users/...), and case does not matter there;
        # MSYS2 writes them /c/Users/...
        if any(v in (text or "") for v in {r, r.replace("\\", "/"), posix(r)}) or \
                ("\\" in r and r.replace("\\", "/").lower() in (text or "").replace("\\", "/").lower()):
            return (f"refused: the value contains the local path {r}; sat_salome must stay portable. Use "
                    "sat variables ($APPLICATION.workdir, $SOURCE_DIR, $PRODUCT_INSTALL...) or, for a build "
                    "script, add_compil_script (scripts are found by name in products/compil_scripts).")
    return None


def declared_packages(text: str, keys: tuple[str, ...], dist: str | None = None) -> list[str]:
    """Every system package a product pyconf declares for a package family (all sections), and,
    with `dist`, in its platform blocks too (system_info."MA15".brew_dev)."""
    found = []
    for sec in pe.product_sections(text):
        for k in keys:
            found += pe.get_str_list(text, [sec, "system_info"], k) or []
            if dist:
                found += pe.get_str_list(text, [sec, "system_info", dist], k) or []
    return found


class FixSession:
    def __init__(self, ctx, ref_dist: str | None):
        self.ctx = ctx
        self.ref_dist = ref_dist
        self.originals: dict[tuple[str, str], str] = {}
        self.actions: list[str] = []
        self.rebuild: set[str] = set()
        self.prepare: set[str] = set()
        self.patches: list[str] = []
        self.packages: list[str] = []

    # ---------------------------------------------------------------- helpers
    def source_root(self, product: str) -> Path:
        """Source directory of a product of the application, inside its SOURCES (never sat,
        sat_salome or another directory reached with `..`: their changes are not patches)."""
        sources = (self.ctx.app_workdir / "SOURCES").resolve()
        if not product or "/" in product or "\\" in product or product in (".", ".."):
            raise ValueError(f"{product!r} is not a product name")
        info = self.ctx.sat.product_info(product)
        if not info["exists"]:
            raise ValueError(f"{product} is not a product of {self.ctx.app}")
        root = Path(info.get("source_dir") or sources / product).resolve()
        if root != sources / product and sources not in root.parents:
            raise ValueError(f"the sources of {product} ({root}) are not under {sources}")
        return root

    def platform_section(self, product: str) -> str:
        """Section of `product` dedicated to the new platform, created by
        cloning the section currently resolved for it (inherited from the
        reference platform) and selected in the platform __overwrite__."""
        ctx = self.ctx
        info = ctx.sat.product_info(product)
        if not info["exists"]:
            raise ValueError(f"{product} is not a product of {ctx.app}")
        sec = info["section"] or "default"
        pfile = Path(info["pyconf"]) if info.get("pyconf") else ctx.product_file(product)
        if pfile.resolve().parent != (ctx.sat_salome_dir / "products").resolve():
            pfile = ctx.product_file(product)
        text = pfile.read_text(encoding="utf-8")
        new = pe.platform_section_name(sec, ctx.suffix)
        if new not in pe.product_sections(text):
            text = pe.clone_section(text, sec, new)
            pfile.write_text(text, encoding="utf-8")
            self.actions.append(f"{product}: new section `{new}` (copy of `{sec}`) in {pfile.name}")
        self._select_section(product, info, new)
        return new

    def _select_section(self, product: str, info: dict, section: str) -> None:
        ctx = self.ctx
        app_text = ctx.app_file().read_text(encoding="utf-8")
        new = pe.app_select_section(app_text, ctx.dist, product, section, info.get("tag"))
        if new != app_text:
            ctx.app_file().write_text(new, encoding="utf-8")
            self.actions.append(f"{product}: {ctx.dist} now uses section `{section}` in {ctx.app}")

    # ---------------------------------------------------------------- finalize
    def finalize(self, title: str = "") -> list[str]:
        """Turns tracked source edits into registered platform patches."""
        by_product: dict[str, list[tuple[str, str]]] = {}
        for (product, rel), original in self.originals.items():
            by_product.setdefault(product, []).append((rel, original))
        created = []
        for product, files in by_product.items():
            root = self.source_root(product)
            chunks = []
            for rel, original in sorted(files):
                current = (root / rel).read_text(encoding="utf-8", errors="replace")
                if current == original:
                    continue
                a = original.splitlines(keepends=True)
                b = current.splitlines(keepends=True)
                if a and not a[-1].endswith("\n"):
                    a[-1] += "\n\\ No newline at end of file\n"
                if b and not b[-1].endswith("\n"):
                    b[-1] += "\n\\ No newline at end of file\n"
                chunks.append("".join(difflib.unified_diff(a, b, f"a/{rel}", f"b/{rel}")))
            if not chunks:
                continue
            patches_dir = self.ctx.sat_salome_dir / "products" / "patches"
            name = f"{product}-{_slug(title)}-{self.ctx.dist}.patch"
            i = 2
            while (patches_dir / name).exists():
                name = f"{product}-{_slug(title)}-{i}-{self.ctx.dist}.patch"
                i += 1
            (patches_dir / name).write_text("".join(chunks), encoding="utf-8")
            info = self.ctx.sat.product_info(product)
            current_patches = [Path(p).name for p in info.get("patches") or []]
            section = self.platform_section(product)
            pfile = self.ctx.product_file(product)
            text = pfile.read_text(encoding="utf-8")
            text = pe.set_section_list(text, section, "patches", current_patches + [name])
            pfile.write_text(text, encoding="utf-8")
            self.actions.append(f"{product}: patch `{name}` registered in section `{section}`")
            self.patches.append(name)
            self.prepare.add(product)
            self.rebuild.add(product)
            created.append(name)
            log.info("patch %s registered for %s (%s)", name, product, section)
        return created

    # ---------------------------------------------------------------- tools
    def tools(self, upfront: bool = False) -> list[Tool]:
        """upfront: applying the maintainer's instructions before the first build
        (other terminal statuses, see prompts.GUIDANCE_TASK)."""
        ctx = self.ctx
        windows = bool(getattr(ctx.env, "windows", False))
        statuses = ["applied", "nothing_to_apply"] if upfront else ["fixed", "give_up"]

        def edit_source(product: str, path: str, old_text: str, new_text: str) -> str:
            try:
                root = self.source_root(product)
            except ValueError as e:
                return (f"refused: {e}. edit_source edits the sources of a product of the application only; "
                        "sat and sat_salome are changed through the other action tools")
            p = Path(path)
            p = (p if p.is_absolute() else root / p).resolve()
            if root not in p.parents:
                return f"refused: {p} is not under the sources of {product} ({root})"
            if not p.is_file():
                return f"{p} does not exist"
            text = p.read_text(encoding="utf-8", errors="replace")
            if not old_text:
                return ("old_text is empty: to insert lines, give an existing anchor line as old_text (e.g. the "
                        "first #include) and repeat it in new_text together with the new lines")
            n = text.count(old_text)
            if n != 1:
                return f"old_text found {n} times in {p.name}; give a unique, exact excerpt (with indentation)"
            rel = p.relative_to(root).as_posix()      # patch paths use "/" (Windows too)
            self.originals.setdefault((product, rel), text)
            p.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
            self.rebuild.add(product)
            return f"edited {rel} (will become a {ctx.dist} patch of {product})"

        def install_system_packages(packages: list[str], runtime: bool = False, reason: str = "") -> str:
            status = ctx.installable(packages)
            bad = [p for p in packages if not status.get(p)]
            if bad:
                return (f"not available on {ctx.dist}: {', '.join(bad)} -- use search_packages / "
                        "package_providing to find the right names; nothing was installed")
            installed, failed = ctx.install_packages(packages, reason)
            if installed:
                ctx.record_system_dep(installed, runtime=runtime)
                self.packages += installed
                self.actions.append(f"system packages {' '.join(installed)} ({reason or 'build fix'}), "
                                    f"declared in salome_system.pyconf system_info.\"{ctx.dist}\"")
            return f"installed: {installed}; failed: {failed}"

        def replace_system_package(old: str, new: str = "") -> str:
            from sat_agent.sysdeps import apply_replacement

            if new:
                if not ctx.installable([new]).get(new):
                    return f"{new} is not available on {ctx.dist}; nothing was changed"
                installed, _ = ctx.install_packages([new], f"replaces {old}")
                if not installed:
                    return f"could not install {new}; nothing was changed"
                if ctx.not_seen_by_sat([new]):
                    return (f"{new} is only a virtual name on {ctx.dist} (sat checks exact package names): "
                            "give the real package name (package_providing)")
            actions = apply_replacement(ctx, old, new or None)
            if not actions:
                return (f"{old} is not declared in any product this platform uses (grep sat_salome/products); "
                        "nothing was changed")
            self.actions += actions
            if new:
                self.packages.append(new)
            return "\n".join(actions)

        def set_product_option(product: str, key: str, value: str, raw: bool = False) -> str:
            bad = refuse_host_path(ctx, value)
            if bad:
                return bad
            if key == "compil_script":
                return "use add_compil_script to give this platform its own build script"
            section = self.platform_section(product)
            pfile = ctx.product_file(product)
            text = pfile.read_text(encoding="utf-8")
            val = value if raw else _quote(value)
            text = pe.set_key(text, [section], key, val)
            pe.PyconfText(text)          # still balanced?
            pfile.write_text(text, encoding="utf-8")
            self.actions.append(f"{product}: `{key}` set in section `{section}`")
            self.rebuild.add(product)
            if key in _PREPARE_KEYS:
                self.prepare.add(product)
            return f"{key} set in {pfile.name}:{section} (only {ctx.dist} uses this section)"

        def set_product_version(product: str, version: str, section: str = "") -> str:
            if version == "native":
                f = ctx.product_file(product)
                pkgs = declared_packages(f.read_text(encoding="utf-8"), ctx.family.sat_keys, ctx.dist) if f.exists() else []
                if not pkgs:
                    return (f"refused: {product} declares no distribution package (system_info."
                            f"{'/'.join(ctx.family.sat_keys)}): 'native' would not install it, sat would just "
                            "skip building it. Keep building it from sources and fix the build instead.")
            app_text = ctx.app_file().read_text(encoding="utf-8")
            spec = pe.product_spec_text(version, section or None)
            app_text = pe.app_overwrite_set(app_text, ctx.dist, f"APPLICATION.products.{product}", spec)
            ctx.app_file().write_text(app_text, encoding="utf-8")
            info = ctx.sat.product_info(product)
            self.actions.append(f"{product}: {ctx.dist} uses version {version}"
                                + (f" (section {section})" if section else ""))
            self.rebuild.add(product)
            self.prepare.add(product)
            return (f"{ctx.dist} now resolves {product} to section {info.get('section')} tag {info.get('tag')} "
                    f"(get method {info.get('get_method')})")

        def add_compil_script(product: str, content: str) -> str:
            """products/compil_scripts/<product>_<DIST>.sh, selected in the product's platform section
            (sat finds compil_script by name in PRODUCTPATH/compil_scripts)."""
            bad = refuse_host_path(ctx, content)
            if bad:
                return bad
            # sat picks <name>$VARS.scriptExtension: .bat on Windows (cmd), .sh elsewhere (bash)
            ext = ".bat" if windows else ".sh"
            if windows and content.lstrip().startswith("#!"):
                return ("refused: on Windows a build script is a batch file run by cmd.exe (like "
                        "compil_scripts/boost_V1.79.0.bat: @echo off, %SOURCE_DIR%, %PRODUCT_INSTALL%), not bash")
            if not windows and not content.lstrip().startswith("#!"):
                return "the script must start with a shebang line (#!/bin/bash), like the existing ones"
            scripts = ctx.sat_salome_dir / "products" / "compil_scripts"
            name = f"{product}_{ctx.suffix}"
            path = scripts / f"{name}{ext}"
            scripts.mkdir(parents=True, exist_ok=True)
            path.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
            if not windows:
                path.chmod(0o755)
            section = self.platform_section(product)
            pfile = ctx.product_file(product)
            text = pe.set_key(pfile.read_text(encoding="utf-8"), [section], "compil_script",
                              f"'{name}' + $VARS.scriptExtension")
            pe.PyconfText(text)
            pfile.write_text(text, encoding="utf-8")
            self.actions.append(f"{product}: build script compil_scripts/{name}{ext} (section `{section}`)")
            self.rebuild.add(product)
            return (f"wrote compil_scripts/{name}{ext}; section {section} of {pfile.name} now uses it "
                    f"(only {ctx.dist} selects this section)")

        def exclude_product(product: str, reason: str) -> str:
            app_text = ctx.app_file().read_text(encoding="utf-8")
            ctx.app_file().write_text(pe.app_add_rm_product(app_text, ctx.dist, product), encoding="utf-8")
            self.actions.append(f"{product}: removed from {ctx.app} on {ctx.dist} ({reason})")
            return f"{product} added to APPLICATION.rm_products for {ctx.dist}"

        def finish(status: str, summary: str, rebuild: list[str] | None = None, patch_title: str = "") -> dict:
            return {"status": status, "summary": summary, "rebuild": rebuild or [], "patch_title": patch_title}

        script_doc = (
            "Give a product its own build script on this Windows platform: full content of "
            "products/compil_scripts/<product>_<DIST>.bat, a batch file run by cmd.exe, usually the current "
            "script (read_file sat_salome/products/compil_scripts/<script>.bat) with a change. Use sat's "
            "variables (%SOURCE_DIR%, %BUILD_DIR%, %PRODUCT_INSTALL%, %CMAKE_GENERATOR%), never local paths."
            if windows else
            "Give a product built with a script (build_source 'script') its own "
            "build script on this platform: full content of products/compil_scripts/<product>_<DIST>.sh, "
            "usually the current script (read_file sat_salome/products/compil_scripts/<script>) with a "
            "change such as an extra configure/bootstrap option. Use sat's variables ($SOURCE_DIR, "
            "$PRODUCT_INSTALL, $MAKE_OPTIONS), never local paths.")
        package_tools = {"install_system_packages", "replace_system_package"}
        tools = [
            Tool("edit_source", "Replace one exact excerpt of a source file of a product (path relative to its "
                 "SOURCES directory). The edit becomes a platform patch automatically; never edit files "
                 "any other way.", obj({"product": S, "path": S, "old_text": S, "new_text": S},
                                       ["product", "path", "old_text", "new_text"]), edit_source),
            Tool("install_system_packages", "Install target packages (exact names, see search_packages) in "
                 "the platform image and declare them for this platform. runtime=true for runtime-only "
                 "dependencies.", obj({"packages": LS, "runtime": B, "reason": S}, ["packages"]),
                 install_system_packages),
            Tool("replace_system_package", "A system package declared in sat_salome has another name on this "
                 "release (or is not needed): installs `new` and rewrites the declaration for this platform "
                 "only — in salome_system.pyconf system_info.\"<DIST>\", or, for a product's own system_info, "
                 "in a platform section <section>_<DIST> selected in the application. new='' drops `old` on "
                 "this platform.", obj({"old": S, "new": S}, ["old"]), replace_system_package),
            Tool("set_product_option", "Set a key of the product's section for this platform only (e.g. "
                 "cmake_options, compil_script, build_source, depend). The value is quoted for you unless "
                 "raw=true (then give pyconf syntax, e.g. a list or $expression).",
                 obj({"product": S, "key": S, "value": S, "raw": B}, ["product", "key", "value"]),
                 set_product_option),
            Tool("add_compil_script", script_doc,
                 obj({"product": S, "content": S}, ["product", "content"]), add_compil_script),
            Tool("set_product_version", "Use another version of a product on this platform: 'native' to take "
                 "the distribution package, or a tag/version known by its pyconf (see read_product_pyconf "
                 "sections version_*). Optional explicit section.",
                 obj({"product": S, "version": S, "section": S}, ["product", "version"]), set_product_version),
            Tool("exclude_product", "LAST RESORT: drop an optional product from the application on this "
                 "platform only (never a SALOME module others depend on).",
                 obj({"product": S, "reason": S}, ["product", "reason"]), exclude_product),
            Tool("finish", ("End the task. status='applied' if you changed something, 'nothing_to_apply' "
                            "otherwise; summary: what you did for each instruction." if upfront else
                            "End the task. status='fixed' once a fix is applied (list products to rebuild), "
                            "'give_up' if no fix was found (explain what you learned). patch_title: 2-4 words "
                            "naming the source fix, e.g. 'gcc16-includes'."),
                 obj({"status": {"type": "string", "enum": statuses}, "summary": S,
                      "rebuild": LS, "patch_title": S}, ["status", "summary"]), finish, terminal=True),
        ]
        # Windows: sat builds every product, there is no system package to install or rename
        return tools if ctx.family.system_packages else [t for t in tools if t.name not in package_tools]
