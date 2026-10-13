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

## Local mode (`--local`, no docker): Windows, macOS, Linux

`sat-agent port --local` ports **the machine itself**: sat runs here, not in a container, and
what the machine is decides the rest (`platform.system()`). It is the only mode on Windows and
macOS. **Nothing is installed on the machine without your approval.**

### macOS

```bash
python3 -m venv .venv && .venv/bin/pip install -e . distro
.venv/bin/sat-agent --config config.json port --local
```

- **Platform**: sat does not know macOS. As for any new distribution, sat-agent teaches it on the
  dedicated branch `sat-agent/distro-macos` of sat (merged into the port branch): the name sat sees
  (`Darwin`, from the distro module) → a new code (`MA`), the version from `platform.mac_ver()`
  (the macOS major version: `MA15`, not the Darwin kernel's 24), and Homebrew as package manager
  (sat checks a native product with `brew list --formula --versions <formula>`).
- **Native products are Homebrew formulae**: the reference platform's rpm names are translated
  (`boost-devel` → `boost`, `python3-numpy` → `numpy`, `qt5-qtbase-devel` → `qt@5`, then the LLM),
  every guess checked with `brew info`, and declared for the platform only
  (`system_info."MA15".brew/brew_dev`).
- **A native product not installed on the Mac**: sat-agent asks, per product —
  `[i]nstall with brew / [s]ource build / [k]eep it missing`:
  - *install*: `brew install --formula …` (as you: brew refuses root);
  - *source*: the product is built from sources on this platform only (as with `embed`), and
    sat-agent reads its **Homebrew formula** (`brew cat`, else homebrew-core on GitHub): the
    patches Homebrew applies on macOS (`patch do url/sha256/directory`, `patch :p0`, `patch :DATA`;
    not those of `on_linux`, `head` or `resource` blocks) are downloaded, checked against their
    sha256, rewritten for sat's `patch -p1` and **registered in sat_salome**
    (`products/patches/<product>-brew-<formula>-<n>-MA15.patch`, in the product's `<section>_MA15`).
    The formula's version is built when the product knows it (`version_<x>` section), so the
    patches apply as Homebrew applies them; patches written for another version are kept in
    `ports/MA15/brew/` and named in the report. Homebrew's build arguments (`-D…`, `--…`) and
    `inreplace` edits are given to the build agent if the product then fails (`ports/MA15/brew/<product>.json`).
  - `--batch`: `policy.missing_on_host` (`source` by default, or `install`).
- No Xvfb: tests use the display.

### Linux

`--local` on Linux works the same way on the distribution of `/etc/os-release` (sat must know it:
a docker port teaches sat a new one). Package queries run as you; installs ask, then use `sudo`
(`sudo -n` without a terminal). The reference platform's Dockerfile is not installed on your
machine: only what `sat config --check_system` reports missing, product by product, with the same
install / source build / keep question. Other installs (a renamed package, the agent's
`install_system_packages`) ask yes/no (`--batch`: `policy.local_install`, `no` by default).

### Windows

On Windows, sat-agent ports **the machine itself**, without docker:

```bat
python -m venv .venv && .venv\Scripts\pip install -e . distro
.venv\Scripts\sat-agent --config config.json port --local --app SALOME-master-windows --batch
```

- **Platform**: the code sat computes on this machine (`src/architecture.py`: `W` + `platform.release()`,
  e.g. `W11`; Python < 3.12 reports `10` on Windows 11, a warning says so). The reference is
  another Windows version of the application, never a Linux platform.
- **No system packages**: the `*-windows` applications build every product with sat, so there is no
  `check_system`, no package tool for the agents, no Dockerfile. The `sysdeps` step is skipped.
- **Commands** are cmd (DOS), PowerShell or Python, never bash: `execution.shell` is the default
  (`cmd`); the agents' `run_shell` takes `shell='cmd'|'powershell'|'python'`. Multi-line Python and
  batch go through a temporary file, PowerShell through `-EncodedCommand`. Every command runs in the
  **Visual Studio environment**: `execution.vcvars` (`auto` = the newest VS with the x64 C++ tools,
  found with vswhere; `""` when sat-agent is started from a "x64 Native Tools" prompt).
- **Build scripts** written by the agent are batch files `compil_scripts/<product>_W11.bat`
  (sat selects `<name>$VARS.scriptExtension`); changes stay platform-scoped as on Linux
  (`__overwrite__` block `VARS.dist in ['W11']`, sections `<section>_W11`); the application's
  workdir is asked to sat (`$LOCAL.workdir/$APPLICATION.name` for the Windows applications).
- **Files** sat-agent writes keep LF line ends (as in the repositories); keep
  `git config core.autocrlf input` (or `false`) in sat and sat_salome — sat-agent warns otherwise.
- Requirements: Git for Windows, Python 3.10+ with `distro` (sat imports it on Windows too),
  Visual Studio with the C++ x64 tools, CMake. Tests: `salome.bat test` or ctest after
  `env_launch.bat` (no Xvfb).

### Windows with MSYS2 (UCRT64, MINGW64)

The other Windows toolchain: an [MSYS2](https://www.msys2.org) environment, **UCRT64** (gcc, UCRT,
libstdc++) or **MINGW64** (gcc, msvcrt), with its pacman packages as native products, like a Linux
distribution:

```bat
.venv\Scripts\sat-agent --config config.json port --msys2 C:/msys64 --batch
```

or, **from an MSYS2 UCRT64 / MINGW64 console** (the installation and the environment are the
console's; no option needed):

```bash
winpty /c/path/to/sat-agent/.venv/Scripts/sat-agent --config config.json port
```

- sat-agent must run on a **Windows python** (python.org's, or the environment's
  `/ucrt64/bin/python`), never on MSYS2's own `/usr/bin/python3`: that one (msys runtime) is for
  sat only, and sat-agent stops with an explanation if it is started on it.
- In the console window (mintty) a Windows program sees no terminal: without `winpty` (`pacman -S
  winpty`), every question gets its `--batch` answer (missing native products built from sources,
  nothing installed), and sat-agent warns. `msys2_shell.cmd -defterm` or Windows Terminal have a
  real terminal.
- From an MSYS2 console, `--msys2 off` (`execution.msys2: "off"`) ports the Visual Studio platform
  (`W11`) instead.

```bash
# once, in an MSYS2 shell: what sat and the builds need (MINGW64: mingw-w64-x86_64-...)
pacman -S --needed git python python-pip patch make diffutils rsync wget unzip \
    mingw-w64-ucrt-x86_64-{gcc,gcc-fortran,cmake,ninja,pkgconf}
/usr/bin/python3 -m pip install --user distro   # add --break-system-packages if pip refuses
```

- sat-agent stays a Windows program (its venv, git for Windows); **sat runs in MSYS2**, with
  MSYS2's own python `/usr/bin/python3` (not `/ucrt64/bin/python`: that one is a Windows python,
  sat would see `W11`, the MSVC platform). Every command is `<root>/usr/bin/bash.exe -lc` with
  `MSYSTEM=UCRT64` and `CHERE_INVOKING=1`; paths are given to sat as `/c/...` and read back as `C:/...`.
- **Platform**: sat does not know MSYS2. sat-agent teaches it on the branch `sat-agent/distro-msys2`
  of sat: the name the `distro` module reports there (uname, `MINGW64_NT-10.0-<build>` → key
  `mingw64_nt`), the code `MSYS` and, as version, the environment (`MSYSTEM`): platform
  **`MSYS_UCRT64`**. sat checks the packages with `pacman -T` and reads `system_info.msys2` /
  `msys2_dev`.
- **Native products** are the environment's packages, `mingw-w64-ucrt-x86_64-<name>` with Arch-like
  names, translated from the reference platform's (Fedora) names; unprefixed MSYS packages are only
  accepted for tools (bison, make), never for libraries. Missing ones are asked per product
  (install with pacman / build from sources / keep missing) as on macOS. sat-agent never runs
  `pacman -Sy` (MSYS2 only supports full upgrades: `pacman -Syu` is yours).
- Build scripts are bash (`.sh`); tests use the Windows desktop (no Xvfb).
- **The environment is the console's**, unless `--msystem` / `execution.msystem` names it:
  1. `MSYSTEM` of the console sat-agent is started from (the `ucrt64.exe` / `mingw64.exe` launchers,
     `msys2_shell.cmd -ucrt64`, a Windows Terminal profile all set it);
  2. else `MSYSTEM` in `<root>/msys2.ini`, the default console's configuration, when it names a build
     environment (the `MSYS` environment is not one);
  3. else the environment whose toolchain is installed (`<root>/<env>/bin/gcc.exe`), UCRT64 first.
  The log says which one and why. Each environment is its own platform (`MSYS_UCRT64`,
  `MSYS_MINGW64`) of the same sat support (`sat-agent/distro-msys2`).
- Supported: UCRT64, MINGW64 (`platform.MSYS2_ENVS`: package prefix, directory, toolchain; CLANG64
  would only need its entry there).

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
(from the `usage` field of the response; servers that omit it are reported as such). The totals —
calls, input, output and reasoning tokens, LLM time, overall and per phase (`prompt`, `distro`,
`sysdeps`, `build:guidance`, `build:<product>`, `tests`) — are kept per run and per model in
`ports/<DIST>/llm_usage.json`, updated after every call (a `--resume` adds to them), printed at the
end of the run and written to the report; `reports/<run_id>.json` adds the run's outcome.

### Comparing models

`--model` and `--llm-base-url` override the config's `llm` section. Run the same port with each
model from the same starting point — its own work root, `--batch`, same image, prompt and policy
(a re-run in the same work root reuses the fixes already made and costs far less) — then compare:

```bash
for m in qwen3:8b qwen3:32b gpt-oss:20b; do
  sat-agent --config config.sat.json -w ~/bench/${m//:/-} port fedora:45 --batch --new-branch --model $m
done
sat-agent bench ~/bench/*              # one line per run, most input tokens first
sat-agent bench ~/bench/* --by-phase   # + where the tokens went; --json for scripts
```

`bench` shows calls, tokens, LLM time, build status, fixes, gave-ups, skipped products, failing
tests and tokens per applied fix: a model that spends few tokens because it gives up early is not
the cheaper one. `--until sysdeps` / `--until prepare` give shorter benchmarks. The `cached`
column counts the input tokens the server served from its prefix cache
(`usage.prompt_tokens_details.cached_tokens`; vLLM: `--enable-prefix-caching
--enable-prompt-tokens-details`; Ollama does not report it).

### Agent memory (history compaction)

Each agent turn sends the whole conversation again, so an old tool output is paid at every
following turn. `sat_agent/agents/memory.py` keeps it bounded (config section `memory`):

| Key | Default | Meaning |
|---|---|---|
| `history_high` | 60000 | conversation size (characters) that triggers a compaction; `0` = never |
| `history_low` | 25000 | size a compaction aims at, in one go — between two compactions the conversation only grows, so the server's cached prefix stays valid |
| `keep_turns` | 3 | last turns kept verbatim (with the system prompt and the task) |
| `strip_think` | true | the `<think>` blocks of earlier turns are not sent again |

Old outputs keep what the investigation needs: error lines, first line and tail of logs and
commands, titles and URLs of web results, a mention of files and pages already read; outputs
still too big become one-line stubs. Every compaction is reported: `MEMORY COMPACTED: <before> ->
<after> characters` and the size of each shortened message in the log; with `--trace`, a
`[memory]` entry holds the whole conversation **before** and **after** the compaction (also in the
log with `-v`). Compare settings with `sat-agent bench` (tokens *and*
fixes: a too aggressive compaction makes the agent re-read or give up).

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
