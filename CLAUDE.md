# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`sat-agent` ports SALOME (`SALOME-master-native` by default) to a new Linux release: from a
docker image of the target, two LangGraph agents (build, test) drive the real `sat` tool against
the real `sat_salome` project in a long-lived container, fix what fails, and leave reviewable git
branches `sat-agent/<DIST>` in sat and sat_salome — with the constraint that **no other platform's
build changes**. See README.md for the user-facing description.

The sibling checkouts `../sat`, `../sat_salome` and `../salome-agent` (LangGraph patterns this
project follows) are inputs, not part of this package. `legacy/` holds the previous hand-rolled
implementation; it is not installed nor tested.

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest                      # all tests (~2 s, no docker / LLM needed)
.venv/bin/python -m pytest tests/test_graph_flow.py::test_full_port_flow
.venv/bin/sat-agent --config config.sat.json port fedora:45 --until sysdeps --batch
.venv/bin/sat-agent --config config.sat.json guard FD45
.venv/bin/sat-agent --config config.sat.json port --prompt "Port SALOME-9.15.0-native to Fedora 45, ..." --dry-run
```

No lint config; `python -m pyflakes sat_agent` is used to keep imports clean.

## Architecture essentials

- `graph.py`: supervisor graph with two compiled subgraphs as nodes (`build_agent`, `test_agent`)
  sharing `state.PortState`. **No reducers in PortState**: LangGraph re-applies a parent reducer
  to a subgraph's full output, so `operator.add` lists would duplicate — nodes write full values.
- Nodes return `Command(update, goto)`. Live objects are in `config["configurable"]["ctx"]`
  (`context.PortContext`), never in state (checkpointed in sqlite). Big data (logs, ctest output)
  stays on disk; nodes re-read it (e.g. `nodes/tests.analyze` re-parses the cycle's log).
- `context.open_port()` runs at every start (not a graph node) so `--resume` gets fresh live
  objects; everything it does is idempotent. The platform code (`FD45`, `UB26.10`) comes from
  sat's own `src/architecture.py` run inside the bootstrap image (`detect_dist`).
- The maintainer's request (`--prompt`, `request.PortRequest`) lives in `ctx.request`, not in
  state: it is re-read at every start (saved per image in `work_root/requests/`), so
  `--resume --prompt` can change the instructions. `prompts.fmt` appends `PortRequest.guidance()`
  to every system prompt; `nodes/build.apply_guidance` applies up-front changes once per
  `PortRequest.digest()` (recorded in `done["guidance"]`). Values written literally in the prompt
  win over the LLM's extraction; everything the LLM returns is validated.
- Agent safety rails (each one comes from a real FD45 incident): tool arguments are coerced to their
  schema types (`react.coerce_args`: `"[cmake]"` once became `sat compile -p [,],a,c,...`); names
  in `finish(rebuild=...)` must be products of the application; `run_shell` undoes any change it
  makes in the sat/sat_salome worktrees (`gw.snapshot`/`undo_since`); action tools refuse local
  paths, `compil_script` goes through `add_compil_script`, `'native'` needs a declared system
  package; the guard also rejects local paths; `apply_guidance` runs only if the prompt says more
  than "port <app> to <target>"; `edit_source` only edits under `SOURCES/<product>` of a product of
  the application (it once patched sat itself with `product='../../../sat'`); tool outputs are
  cleaned of lone surrogates (`react.clean_text`) before being stored or sent.
- macOS (local only): `distro_support.macos_plan` (sat name from the distro module, new code, version
  rule "macos" = `platform.mac_ver()` major, spec family `platform.SPECS["brew"]`). Local mode asks
  per product in `system_deps` → `native_choices` (`kind: missing_on_host`, answers in
  `state.local_installs`); "source" embeds the product and, with brew, `nodes/build._brew_sources`
  registers the formula's macOS patches (`brew.py`: parse, sha256, -p1). `PortContext.install_packages`
  never installs on the machine without approval (`ask_install`, `host_approved`); package queries
  run as the user in local mode (`root=not ctx.local`), installs with sudo on Linux.
- MSYS2 (local only, Windows, `port --msys2 <root>`): `LocalEnv(system="MSYS2")` runs `bash.exe -lc`
  with `MSYSTEM` (`--msystem`, else the console's: `local_env.detect_msystem` = MSYSTEM of
  sat-agent's console, `msys2.ini`, installed `<env>/bin/gcc.exe`; UCRT64, MINGW64); sat runs
  with MSYS2's `/usr/bin/python3` (the `/ucrt64` one is a Windows python: sat would see W11).
  `distro_support.msys2_plan`: uname key (`mingw64_nt`), code `MSYS`, version rule "msystem" → `MSYS_UCRT64`, family `msys2` (`platform.msys2_spec`: pacman, names prefixed by
  `MSYS2_ENVS[...]["package_prefix"]`, `heuristic_names(prefix=)`). Paths to sat go through
  `env.posix_path` (`/c/...`), paths from sat through `env.host_path` (SatRunner does it).
  Started from an MSYS2 build console (`local_env.msys2_console`: MSYSTEM + `<root>/usr/bin` on PATH),
  `port`/`port --local` is MSYS2 without `--msys2` (`off` disables); sat-agent itself on the msys
  python is refused (`under_msys_runtime`).
- Local mode (`port --local`: Windows, macOS, Linux): `context.open_local_port` + `local_env.LocalEnv`
  (same interface as DockerEnv: exec/start/stop, plus `kind`, `windows`, `quote`, `python`; build
  command lines with `env.quote`, never `shlex.quote`, when they may run on Windows). Family
  `platform.NO_PACKAGES` (`family.system_packages` False): no check_system, no package tools, no
  Dockerfile (`ctx.spec` None). Path checks must use `Path.parents`, never `str + "/"`; patches use
  `as_posix()`; `sat_agent/__init__.py` makes `Path.write_text` keep LF on Windows.
- Tests: `salome test` needs KERNEL; without it (MEDCOUPLING) `nodes/tests.launcher` runs
  `sat environ` and the tests run with ctest in `INSTALL/*/bin/salome/test` (`state.tests.dirs`).
- Token usage: `LLMClient` counts per phase (`llm.phase_of` of the `caller` label) and, once
  `persist_usage` is called (cmd_port, after the thread is known), saves `{run_id: {model: usage}}`
  in `ports/<DIST>/llm_usage.json` after every call; finalize writes `reports/<run_id>.json`
  (`run_summary`), and `sat-agent bench` (`bench.py`) compares work roots from those two files.
- Agent memory: `agents/memory.compact_history` runs before each turn of `run_agent`
  (`memory_cfg` = config `memory`): past `history_high` it compacts old outputs in one go down to
  `history_low` (prefix caching stays valid between compactions); it only rewrites `content`,
  never removes messages (tool-call/tool-result pairing must stay valid for the API). The
  `<think>` part of native tool-call answers is stripped from the history (`strip_think`).
- The fixer/test agents are `agents/react.run_agent` loops over `tools/*` with a terminal
  `finish` tool. Small local models often emit tool calls as JSON text: `llm.text_tool_calls`
  recovers them.

## sat / sat_salome facts the code relies on (verified in the sources)

- Platform codes: `VARS.dist` = distrib code (`src/internal_config/distrib.pyconf`, substring of
  `distro.linux_distribution()[0].lower()`) + `get_distrib_version` (UB keeps `major.minor`, DB
  pads to 2 digits, others major only). `sat config -v VARS.dist` prints `dist: UB24.04`.
- Upstream sat checks packages with dpkg/apt or rpm only (`src/system.py: get_pkg_check_cmd`, by
  dist code: codes outside its rpm list use dpkg). A distribution sat does not know is planned by
  `distro_support.plan_support` (derivative → parent's code; else a new two-letter code, version
  rule, package manager found in the image or described by the LLM and validated there) and written
  by `sat_capabilities.add_distribution_support` on the dedicated branch `sat-agent/distro-<id>`
  (merged into the port branch). New managers: check_cmd = `[<system_info key>, <command>...]`.
  Verified in archlinux (pacman -T, follows provides), alpine (apk info -e), void-glibc (xbps-query).
  Their ports translate the reference's rpm names (`sysdeps.translate_declarations`).
- `src/product.py: check_system_dep` merges generic `system_info.apt/apt_dev` (or `rpm/rpm_dev`)
  with the block whose key contains the dist (`"UB26.04"`). sat-agent adds
  `<apt|rpm>_exclude` support there (`sat_capabilities.py`, anchored on the
  `runtime_dep = src.system.check_system_pkgs(...)` line).
- sat checks system packages by exact name (`src/system.py`: `rpm -q`, `dpkg-query`), not
  through provides: `dnf install python3-Cython` succeeds on FD45 but sat keeps it KO, so
  `sysdeps._virtual_installs` maps such names to the real package. In incremental mode a
  section's top-level keys replace the default ones (`get_product_config`), so a platform
  section overriding `system_info` must carry the whole mapping (`pe.section_system_info`).
- Missing native packages block `sat prepare` (`commands/source.py`) and `sat compile`
  (`src/product.py: check_installation`); `--check_system` itself always exits 0.
- `sat prepare` = clean + source + patch for the given products (re-applies the whole patch
  list on pristine sources); `--complete` skips already-fetched products. Patches are applied
  with `patch -p1` from the product source dir; patch files are looked up flat in
  `products/patches/`.
- Product sections: an explicit `section:` in the application entry wins; else
  `version_<tag>`, version ranges, then `default`. With `properties.incremental: "yes"` a section
  increments `default` (so a section's `patches` list *replaces* the default one —
  `FixSession.finalize` writes the full effective list).
- `__overwrite__` blocks are evaluated in order (`src/pyconf.py: overwriteKeys`, `eval` of
  `__condition__`), later ones win; `setup_platform` therefore inserts the cloned block right after
  the reference's.
- compile output: `Compilation of <p> .... OK|KO <STEP>`, summary `Compilation: OK|KO (n/m)`;
  per-product logs in `<APP>-<DIST>/LOGS/<product>/` (one file per step reached).

## Editing pyconf files

Always go through `pyconf_edit.py` (bracket/string/comment-aware text splices that keep comments
and layout; sat cannot write pyconf back). Every new kind of edit must stay platform-scoped and be
accepted by `guard.check_sat_salome`, which compares sat-parsed structures with the port's base
commit (recorded in the upstream repo's git config `branch.<branch>.satagentbase`). Tests parse
edited real files back with sat's parser — keep doing that for new edit functions.
