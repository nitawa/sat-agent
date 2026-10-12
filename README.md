# sat-agent

Port **SALOME** to a new Linux release with **SAT** (salomeTools) and **SAT_SALOME**,
using two cooperating **LangGraph** agents — a *build agent* and a *test agent* — inside a
Docker container of the target platform, **without changing what any already-supported
platform builds**.

```bash
sat-agent --config config.sat.json port fedora:45
sat-agent --config config.sat.json port ubuntu:26.10 --batch
sat-agent --config config.sat.json port debian:14 --resume
sat-agent --config config.sat.json guard FD45          # re-check non-regression of a port
sat-agent --config config.json -w /data/sat-agent port fedora:45   # other working directory than the config's

# or say it in your own words (any language): application, target, recommendations
sat-agent --config config.sat.json port --prompt "Port SALOME-9.15.0-native to Fedora 45. Use the \
  distribution's boost and CGAL, prefer upstream patches, skip the PARAVIS tests."
sat-agent --config config.sat.json port --prompt-file request.txt --dry-run   # show how it is understood
```

Outputs, for platform `<DIST>` (sat's code, e.g. `FD45`, `UB26.10`, `DB14`, `CO11`):

| What | Where |
|---|---|
| branch `sat-agent/<DIST>` of **sat_salome**: platform registered in the application, per-platform package names, product sections, patches, `dockers/SALOMEPlatform.Dockerfile.<dist>-<arch>` | `<work_root>/ports/<DIST>/sat_salome` (git worktree) |
| branch `sat-agent/<DIST>` of **sat**: small generic capabilities (see below) | `<work_root>/ports/<DIST>/sat` |
| SALOME build tree (`SOURCES`, `BUILD`, `INSTALL`, `LOGS/<product>`), launcher | `<work_root>/ports/<DIST>/build/<APP>-<DIST>/` |
| report (fixes with their justification, packages, tests, non-regression check) | `<work_root>/ports/<DIST>/REPORT.md` |
| every command's log | `<work_root>/ports/<DIST>/logs/` |

Review the two branches, then merge them.

One port directory = one container (`sat-agent-<dist>-<hash of the port dir>`) and one pair of branches. When a *new* port directory (e.g. another `--work-root`) finds `sat-agent/<DIST>` branches left by an earlier port of the platform, it does not reuse them silently: `--new-branch` starts again from upstream (the old branches are kept as `sat-agent/<DIST>-<date>`), `--continue-branch` keeps their commits; without either, sat-agent asks (or stops in `--batch`).

---

## The request (`--prompt`)

`--prompt TEXT` / `--prompt-file FILE` (`-` = stdin) describe the port in natural language.
`sat_agent/request.py` turns it into a `PortRequest`, shown before the run (confirm with Enter;
`--yes`/`--batch` skip the question, `--dry-run` stops there):

| From the prompt | Used for | Validation |
|---|---|---|
| application (`SALOME-9.15.0-native`, "the 9.15 native one") | `sat … <APP>` everywhere | must be one of `sat_salome/applications/*.pyconf` (close matches suggested); a name written literally wins over the LLM's reading; default: `--app`, else config `application` |
| target (`fedora:45`, `registry/x:tag`, "Ubuntu 26.10") | the docker image | `name:tag`; the positional `IMAGE` wins |
| "no tests", "only GEOM tests", "not the PARAVIS ones", "stop after prepare" | `tests.enabled`, ctest `-R`/`-E`, `--until` | regexes compiled, stages checked; command-line flags win |
| everything else | the agents | — |

The recommendations reach the agents in two ways:

- **in every system prompt** of the build agent and the test agent (the extracted list *and* the
  verbatim prompt, so nothing is lost by the extraction), with precedence over the agents' own
  preferences but never over the rule that other platforms must not change;
- **up front**: right after the platform is registered and before anything is built, the
  `apply_guidance` node runs the build agent once to apply the instructions that are changes to
  make now (product versions, product options, system packages, products to drop). Its changes go
  through the same platform-scoped tools and non-regression guard as any fix, are committed as
  `Maintainer's instructions: …`, and appear in REPORT.md together with the request.

The request is saved in `<work_root>/requests/<image>.json`: `--resume` without a prompt reuses
it; `--resume --prompt …` replaces the instructions (the application stays the saved one unless
the new prompt names another). Without an LLM the application and image are still found in the
text, and the whole prompt is given to the agents.

---

## What it does

The workflow is the documented one ([SAT wiki](https://github.com/SalomePlatform/.github/wiki/SAT),
`sat_salome/dockers/README.md`), automated and self-healing:

1. **Platform container.** The image's `/etc/os-release` gives the distribution. If sat does not
   know it (its `NAME` matches no key of sat's `distrib.pyconf`), sat-agent finds out how to
   support it (`distro_support.py`, kept in `<work_root>/distros/<id>.json`):
   - **derivative** of a distribution sat knows (`ID_LIKE`, else a web search read by the LLM:
     Zorin OS → Ubuntu): it keeps its parent's two-letter code and version rule (Pop!_OS → `UB24.04`);
   - otherwise a **new two-letter code**, unique among sat's (arch → `AR`, alpine → `AL`,
     void → `VO`), and a **version rule**: none for a rolling release (no `VERSION_ID`, or the
     image date as on Arch), else the numbering found on the web (`major.minor` for Alpine 3.24 →
     `AL3.24`), `major` by default;
   - its **package manager**: the one found in the image among apt, dnf, zypper, pacman, apk,
     xbps; for another one, the LLM describes it from the web (commands to refresh, install,
     search, tell installed/available packages, and the check sat will run), and the description
     is used only once it passes in the image (real and bogus package names, an install, a search).

   sat then learns the distribution on a dedicated branch (see [sat changes](#sat-changes)). A bootstrap image
   (git, python3 + distro, toolchain, Xvfb) is built, and **sat's own detection code**
   (`src/architecture.py`) tells the platform code. Unknown derivatives are added to
   `distrib.pyconf` from `ID_LIKE`. A long-lived container mounts the port directory at the same
   path (as in `dockers/README.md`), so sources/logs are on the host and edits to the
   worktrees are seen at once.
2. **Register the platform** (build agent, `setup_platform`). The closest supported platform is
   chosen as reference among the platforms **of the application** (its `platform` list, else its
   `__overwrite__` blocks): FD45 → FD44 for SALOME-master-native, FD45 → FD42 for
   MEDCOUPLING-master-native (no FD44 block), UB26.10 → UB26.04, DB14 → DB13, a new derivative →
   newest of its family. The new platform inherits all of its choices:
   - `APPLICATION.platform` gains `<DIST>`;
   - the reference's `__overwrite__` block (`VARS.dist in ['<REF>']`) is duplicated for
     `<DIST>`; shared blocks (`['FD42', 'FD44']`) get `<DIST>` in their condition;
   - `system_info."<REF>"` blocks (salome_system.pyconf) are copied as `system_info."<DIST>"`;
   - the reference's Dockerfile packages (`dockers/SALOMEPlatform.Dockerfile.<ref>-*`) are
     checked against the new repositories and installed (renamed ones mapped);
   - another package manager than the reference's (Arch, Alpine, Void... whose reference is the
     newest Fedora): nothing in sat_salome declares its names yet, so the names sat checks on the
     reference (rpm/rpm_dev of every native product of the application) are translated — guesses
     checked in the target repositories (`boost-devel` → `boost` / `boost-dev`, `python3-X` →
     `python-X` / `py3-X`), then the LLM for the rest, checked too — and declared for `<DIST>`
     only: `system_info."AR".pacman/pacman_dev` in salome_system.pyconf, a `default_AR` section
     selected by the application elsewhere. The mapping is kept in
     `ports/<DIST>/pkgmap-rpm-<family>.json`. A name still without equivalent is looked up on the
     web: what the reference package is (`Fedora package freeimage-devel` → the FreeImage
     library), then that project on the target (web + the target's repositories); the LLM's
     answer counts only if the package installs in the image (a conflicting one such as
     `zlib-ng-compat` gets a second turn: `zlib`), and what the web said is kept in
     `pkgmap-*.evidence.json`.
   - a **native product still without a package** (freeimage, metis: AUR-only on Arch) is left
     to you (`native_choices`), with the web evidence: **embed** it — build it from sources on
     this platform only, with the version the application builds elsewhere (freeimage 3.18.0 as
     on CO10), else the one of the non-native application (`SALOME-master`), else the newest
     `version_*` section —, give its **package** name (checked in the image), keep it **native**
     without a package, or **remove** it from the application on this platform. `--batch`:
     `policy.unmapped_native` (`embed` by default; `none`, `remove`).
3. **System prerequisites** (`system_deps`). `sat config <APP> --check_system` is run until
   nothing is missing. Existing names are installed; vanished ones (new soname, `t64`, new
   compiler major…) are mapped to the new name (package-manager search + LLM tie-break) and
   written **only** for `<DIST>`: replaced in `system_info."<DIST>"`, or — if the old name sits
   in a generic list — excluded with `system_info."<DIST>".apt_exclude`/`rpm_exclude` and the new
   one added. A name declared in a product's own `system_info` (e.g. `Cython.pyconf`) gets, like
   the existing platforms, a section `<section>_<DIST>` (`default_FD45`) with the corrected
   `system_info`, selected explicitly in the application:
   `'APPLICATION.products.Cython' : {tag: 'native', base: 'no', section: 'default_FD45'}`.
   A name that only installs through a virtual name/provides (Fedora 45: `python3-Cython` is
   provided by `python3-cython`) is treated as a rename, since sat checks the exact name
   (`rpm -q`, `dpkg-query`). Every installed package goes to the platform Dockerfile.
4. **Sources** (`prepare`): `sat -o "APPLICATION.properties.git_server='github'" prepare <APP>`
   (`--complete` on re-runs). Failures (missing native packages, unreachable tag, patch not
   applying) go to the fixer.
5. **Compilation** (`compile`): `sat … compile <APP> --stop_first_fail`. On the first failing
   product, the **fixer** (an LLM tool loop) reads `<APP>-<DIST>/LOGS/<product>/*`, the sources,
   the toolchain in the container, how the reference platform handled the product, searches the
   web, then applies one fix. Its task already contains an automatic pre-analysis
   (`diagnose.py`): the first real error of the log (compiler/linker errors before configure
   messages), the target packages providing what the log reports missing (header, library, CMake
   package, pkg-config module, Python module, program), and a web search on that error (paths,
   positions and quotes removed). The tool loop refuses repeated identical calls, asks the model to
   conclude before its budget ends and always ends with a `finish` turn; every conversation is
   saved in `ports/<DIST>/logs/agents/*.json`. Possible fixes:
   - install a system package (declared in `salome_system.pyconf` `system_info."<DIST>"`);
   - set a product option for this platform (e.g. `cmake_options`);
   - use another version of the product (`'native'` or a tag with an existing section);
   - patch the sources: edits are turned into
     `products/patches/<product>-<title>-<DIST>.patch`, registered in a new section
     `<section>_<DIST_SUFFIX>` (copy of the section the platform was using, plus the patch) and
     selected in the `<DIST>` `__overwrite__` block with
     `{tag: '…', base: 'no', section: '<section>_<DIST_SUFFIX>'}`;
   - last resort: drop an optional product on this platform (`APPLICATION.rm_products`).

   Then `sat prepare -p <product>` (proves the patch applies), `sat compile -p <product>
   --clean_all`, and the whole compilation resumes (sat skips installed products).
   Each fix is committed on the branch with its justification.
6. **Tests** (test agent): `sat launcher <APP>`, then `salome test` (ctest, under Xvfb). An
   application without KERNEL (e.g. MEDCOUPLING) has no salome launcher: sat-agent runs
   `sat environ <APP>` instead, then ctest in each `INSTALL/<product>/bin/salome/test` (the
   ones with a `CTestTestfile.cmake`) with `env_launch.sh` sourced. The test
   agent diagnoses each failure (re-running tests, inspecting the installation, searching the
   web) and classifies it: `missing_system_package`, `product_bug`, `environment` are sent back
   to the build agent, which installs/fixes/rebuilds; then only the failed tests are re-run
   (`policy.max_test_cycles` rounds). `flaky`, `test_infrastructure`, `upstream`, `unknown` are
   reported.
7. **Finalize**: the platform Dockerfile is exported to
   `sat_salome/dockers/SALOMEPlatform.Dockerfile.<dist>-<arch>` (same layout as the hand-written
   ones; `--verify-dockerfile` rebuilds it from scratch and re-runs `check_system` in it), the
   non-regression guard runs, everything is committed, the report is written.

When the fixer gives up (or exhausts `policy.compile_fix_attempts`), the **failure gate** asks
(LangGraph `interrupt`): retry with a hint, skip (drop the product on this platform), or abort.
With `--batch`, `policy.on_failure` decides.

### Not breaking the other platforms

Two layers:

- every edit tool is scoped by construction (platform blocks, `*_<DIST_SUFFIX>` sections, new
  patch files only);
- `guard.py` re-parses every changed pyconf with **sat's own parser** and compares it, semantically,
  with the port's base commit: all sections of other platforms, generic `system_info` lists,
  `APPLICATION` (except `platform` gaining `<DIST>`), every `__overwrite__` block of other
  platforms must be identical; in `patches/`, `compil_scripts/`, `env_scripts/`, `dockers/` only
  new files are allowed. A fix violating it is reverted before commit; commits are refused while
  a violation exists. `sat-agent guard <DIST>` runs it on demand.

### sat changes

On its branch, sat gets generic, backward-compatible capabilities (`sat_capabilities.py`):

- `system_info."<DIST>".<key>_exclude` (`apt_exclude`, `rpm_exclude`, `pacman_exclude`...), honoured by
  `check_system_dep` — lets a platform drop a generic package that no longer exists, without
  touching the generic list (no other platform uses the key);
- missing distribution names (new derivative) added to `src/internal_config/distrib.pyconf`;
- a distribution sat does not know, on its own branch **`sat-agent/distro-<id>`**
  (`sat-agent/distro-arch`), one commit created from the port's base commit and merged into the
  port branch, so it can be reviewed and upstreamed independently of any release:
  - its name → two-letter code in `distrib.pyconf` (a derivative: its parent's code);
  - its version rule in `get_distrib_version` (`elif distrib in ["AR"]: version=""`,
    `major.minor` for `AL`; `major` is sat's default);
  - its package manager: a dnf/zypper distribution joins the rpm list of `get_pkg_check_cmd`;
    a manager sat does not know gets its check command there (`pacman -T`, `apk info -e`,
    `xbps-query`...: check_cmd = `[<key>, <command>...]`), its branch in `check_system_pkgs`,
    and the `system_info.<key>/<key>_dev` keys in `check_system_dep`.

  No existing platform reaches the new code.

---

## Architecture (LangGraph)

```
supervisor:   START ─► build_agent ─┬─(ok)─► test_agent ─┬─(actionable feedback, cycles left)─► build_agent
                                    └─(failed/stopped)─┐  └─(else)─┐
                                                       ▼           ▼
                                                    finalize ─► END

build_agent:  entry ─► setup_platform ─► apply_guidance ─► system_deps ─► prepare ─► compile ─► END
                 │                    (prompt's up-front changes)    └────────────┴──────────┤ failure
                 └─► apply_feedback ◄──┐                               ▼
                                       │                 fix ─(fixed)─► rebuild ─► back to the phase
                                       │                  │
                                       └──────────── failure_gate (interrupt: retry+hint | skip | abort)

test_agent:   launcher ─► run_tests ─► analyze ─► END
```

As in salome-agent: nodes return `Command(update=…, goto=…)`; live objects (the container,
the sat runner, the LLM, the web client — `PortContext`) travel in
`config["configurable"]["ctx"]`, never in the checkpointed state; human decisions use
`interrupt()`; state is checkpointed in `ports/<DIST>/checkpoints.sqlite` (`--resume`).

| Module | Role |
|---|---|
| `graph.py`, `state.py` | supervisor + two subgraphs, shared `PortState` |
| `nodes/build.py`, `nodes/tests.py`, `nodes/finalize.py` | graph nodes |
| `agents/react.py`, `agents/prompts.py` | bounded tool-calling loop, system prompts |
| `tools/common.py` | investigation tools (logs, sources, grep, shell in the container, sat product info, package search, web) |
| `tools/build_tools.py` | build actions + `FixSession` (source edits → platform patch + section + `__overwrite__`) |
| `tools/test_tools.py` | test results, re-run, diagnosis reports |
| `request.py` | the `--prompt` request: application, image, run options, recommendations |
| `context.py` | `open_port`: os-release, bootstrap image, platform code, git worktrees, container, `sat init` |
| `docker_env.py` | Dockerfile model/renderer, long-lived container, streamed `docker exec` |
| `sat_runner.py`, `parsers.py` | sat commands and parsers of their output (check_system, prepare, compile, ctest) |
| `pyconf_edit.py` | formatting-preserving pyconf edits (bracket/string/comment-aware) |
| `sysdeps.py`, `platform.py` | package families, name mapping between releases, reference platform |
| `guard.py` | semantic non-regression check |
| `git_workspace.py`, `sat_capabilities.py` | branches/worktrees, sat upgrades |
| `tools/web.py` | web search (DuckDuckGo / SearxNG / Tavily) and page reading |

The previous hand-rolled implementation is kept in `legacy/` for reference.

---

## Install & configure

```bash
cd sat-agent
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp config.example.json config.sat.json     # then edit
```

Requirements: Docker (the user must be able to run `docker`), git, an OpenAI-compatible
tool-calling LLM endpoint (Ollama, vLLM, LiteLLM, a gateway…). Tool calling quality matters for
the fixer: prefer a strong model (e.g. a 30B+ coder model) over a small one.

Every LLM call logs its tokens, named after the agent and turn that made it:
`[build:compile 3] LLM call 42: 18234 in + 312 out tokens (4.1s) | total 512340 in + 9876 out`
(from the `usage` field of the response; servers that omit it are reported as such). The totals are
printed at the end of the run and written to the report (they restart from zero on `--resume`).

`--trace` (or `--trace-file FILE`, or `trace.enabled` in the config) writes a full, chronological
trace to `<work_root>/traces/trace-<date>.log`: every graph step with its state update, every
request sent to the LLM (the messages new since the previous request of the same conversation —
system prompt, task, tool outputs, nudges — and the tool schemas once), every answer as the server
returned it (including `<think>` blocks and `reasoning_content`/`reasoning` when the server sends
them, and the usage), and every tool call with its arguments (also as sent by the model, before
type coercion), its duration and the exact text sent back to the model. Nothing is truncated:
the file can grow large on a long run.

Key settings (`config.example.json`):

| Key | Meaning |
|---|---|
| `paths.sat`, `paths.sat_salome` | your checkouts (relative to the config file). Never modified in `git.mode: worktree`. |
| `paths.work_root` | worktrees, builds, logs, reports (a SALOME build needs ~50-100 GB); `-w DIR` / `--work-root DIR` overrides it for one run (before or after the command; relative to the current directory) |
| `application` | `SALOME-master-native` |
| `sat_overrides` | `-o` options of every application command (`git_server='github'`) |
| `git.mode` | `worktree` (default) or `inplace` (branch checked out in your repositories) |
| `docker.run_as_host_user` | sat runs with your uid (files stay yours); package installs run as root |
| `web_search.backend` | `duckduckgo` (no key), `searxng` (+`searxng_url`), `tavily` (+key), `none` |
| `tests.*` | ctest arguments, `include`/`exclude` regexes, Xvfb |
| `policy.unmapped_native` | `--batch`, native product without a package on a new package manager: `embed` (default), `none`, `remove` |
| `policy.*` | attempts per failure, LLM turns, test cycles, `on_failure` (`abort`/`skip`) in batch mode, timeouts |

Staged runs: `--until sysdeps` / `--until prepare` stop after that phase; `--no-tests` stops
after the compilation. Re-running the same image continues where the build tree is (sat skips
what is installed; `--resume` also restores the graph state).

## Tests

```bash
pytest
```

Unit tests cover the pyconf editor and the guard on the real sat_salome files (parsed back with
sat's parser), the parsers, package mapping, the sat capability patch (executed), and an
end-to-end run of the LangGraph supervisor with a fake container/sat/LLM on temporary git
repositories (compile failure → patch → rebuild → tests → test feedback → fix → re-test →
Dockerfile + report + commits; and the interrupt gate).
