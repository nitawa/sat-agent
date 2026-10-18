# Expert AI & Application Engineering Review (`gemini-argon-review`)

## 1. Executive Summary & Architectural Assessment

`sat-agent` is a domain-specialized agentic porting system built on LangGraph that ports SALOME applications (`sat` + `sat_salome`) to new operating systems and distributions across Dockerized Linux targets (Fedora, Ubuntu, Debian, AlmaLinux, Rocky, openSUSE, Arch, Mageia, Alpine, Void, Gentoo) and local host environments (Windows MSVC, Windows MSYS2 UCRT64/MINGW64, macOS Homebrew, and local Linux).

### Architectural Strengths

1. **Deterministic First, Agentic Where Necessary**:
   - Rather than handing a raw target OS to an unconstrained LLM, `platform_setup` ([`sat_agent/nodes/build.py`](../sat_agent/nodes/build.py)) deterministically clones `.pyconf` blocks from the closest reference distribution, checks package availability in bulk, resolves virtual packages and renames via `provides`/repo search ([`sat_agent/sysdeps.py`](../sat_agent/sysdeps.py)), and translates package declarations across package manager families.
   - Similarly, before invoking the ReAct build agent on a compilation failure, [`sat_agent/diagnose.py`](../sat_agent/diagnose.py) mines the build log deterministically for compiler/linker/CMake errors, missing headers/libraries/pkg-config modules, candidate packages in the target's repositories, and cleaned web search results. This gives smaller or local open-weights models high-signal evidence on turn 1.
2. **Non-Destructive, Span-Preserving `.pyconf` Editing & Cross-Platform Guard**:
   - [`sat_agent/pyconf_edit.py`](../sat_agent/pyconf_edit.py) uses a custom character-masking scanner (`PyconfText`) that preserves comments, indentation, and formatting outside the exact modified section or platform block.
   - [`sat_agent/guard.py`](../sat_agent/guard.py) enforces a strict non-regression invariant after every action tool: no edit may alter how `sat` resolves any other platform's configuration. If a violation occurs, the git worktree is immediately restored via [`sat_agent/git_workspace.py`](../sat_agent/git_workspace.py).
3. **Bounded ReAct Loop with Small-Model Guardrails**:
   - [`sat_agent/agents/react.py`](../sat_agent/agents/react.py) includes type coercion (`coerce_args`), fallback text tool-call extraction (`text_tool_calls`), repeat-call detection, two-pass context compaction ([`sat_agent/agents/memory.py`](../sat_agent/agents/memory.py)), and a forced-conclusion final turn offering only `finish` when the turn budget expires.

---

## 2. Detailed Findings & Implemented Fixes

During a comprehensive audit of all 26 source modules in `sat_agent/` and the test suite, **10 concrete issues** spanning AI agent loop correctness, LLM tool parsing, multi-platform `.pyconf` handling, Windows/MSVC error diagnosis, LangGraph state transitions, context compaction, security, and resource management were identified, filed on GitHub, and fixed on the `gemini-argon-review` branch.

| # | GitHub Issue | Module(s) | Summary |
|---|---|---|---|
| 1 | [#2](https://github.com/nitawa/sat-agent/issues/2) | `sat_agent/agents/react.py` | ReAct repeat-call guard (`seen`) blocks read-after-write verification after state-mutating tools |
| 2 | [#3](https://github.com/nitawa/sat-agent/issues/3) | `sat_agent/llm.py` | Fallback text tool-call parser (`text_tool_calls`) fails when prose precedes JSON tool calls |
| 3 | [#4](https://github.com/nitawa/sat-agent/issues/4) | `sat_agent/sysdeps.py` | `AttributeError` crash in `sysdeps._virtual_installs` in `--local` mode & missing `ref` exclusions in `translate_declarations` |
| 4 | [#5](https://github.com/nitawa/sat-agent/issues/5) | `sat_agent/pyconf_edit.py`, `sat_agent/sysdeps.py` | Platform suffix regex fails on rolling-release (`AR`, `VO`) and multi-segment (`MSYS_UCRT64`, `MSYS_MINGW64`) distro codes |
| 5 | [#6](https://github.com/nitawa/sat-agent/issues/6) | `sat_agent/parsers.py`, `sat_agent/diagnose.py` | Compiler/linker error parsers & pre-diagnosis miss MSVC (`C1083`, `C2xxx`, `LNKxxxx`) and relative source paths |
| 6 | [#7](https://github.com/nitawa/sat-agent/issues/7) | `sat_agent/nodes/build.py` | Test-phase rebuild failure overwrites failed product identity & drops compile metadata |
| 7 | [#8](https://github.com/nitawa/sat-agent/issues/8) | `sat_agent/agents/memory.py` | History compaction (`compact_history`) second-pass threshold bug & stubbing unprefixed outputs |
| 8 | [#9](https://github.com/nitawa/sat-agent/issues/9) | `sat_agent/tools/common.py`, `test_tools.py`, `web.py` | Unconfined path resolution in `LOGS` tools & uncaught `re.error` on invalid LLM regexes in `grep` parameters |
| 9 | [#10](https://github.com/nitawa/sat-agent/issues/10) | `sat_agent/cli.py`, `sat_agent/docker_env.py` | SQLite checkpointer connection leak & subprocess cleanup on interruption/timeout |
| 10 | [#11](https://github.com/nitawa/sat-agent/issues/11) | `sat_agent/tools/web.py` | Indirect prompt-injection defense for untrusted web content |

---

### Issue 1: ReAct Repeat-Call Guard (`seen`) Blocks Read-After-Write Verification After State-Mutating Tools
- **File**: [`sat_agent/agents/react.py`](../sat_agent/agents/react.py)
- **Severity**: High (AI Agent Correctness)
- **Root Cause**:
  `_turn()` recorded every non-terminal tool call in `seen[key] = turn` (keyed by tool name and canonical JSON arguments) and refused any subsequent call with identical arguments for the remainder of the `run_agent` invocation:
  ```
  You already called read_file with exactly these arguments at turn X: the result is the same, it is above.
  ```
  When the build agent read a source file or product configuration (`read_file`, `read_product_pyconf`, `product_info`, `platform_overrides`), executed a state-mutating action tool (`edit_source`, `set_product_option`, `set_product_version`, `add_compil_script`, `exclude_product`, `install_system_packages`, `replace_system_package`), and then re-read the same file or `.pyconf` to verify its edit or prepare a second edit, `react.py` blocked the read call and falsely claimed the result was unchanged.
- **Implemented Fix**:
  Added `MUTATING_TOOLS` and a `mutates: bool = False` attribute on `Tool`. When a state-mutating tool completes without returning an `"ERROR:"` / `"refused:"` / `"bad arguments"` prefix, `seen` is cleared (retaining only the mutating call's own key for that turn) so all subsequent read/inspection calls reflect the updated workspace state.

---

### Issue 2: Fallback Text Tool-Call Parser (`text_tool_calls`) Fails When Prose Precedes JSON Tool Calls
- **File**: [`sat_agent/llm.py`](../sat_agent/llm.py)
- **Severity**: High (AI / Local LLM Reliability)
- **Root Cause**:
  `_first_json_object(text)` stopped at the very first `{` or `[` in `text` that decoded as valid JSON. When open-weights or local models wrote explanatory prose containing C++ brace initializers or JSON snippets (e.g., ``The initializer `{"a": 1}` fails...``) before emitting their JSON tool call block `{"name": "edit_source", "arguments": {...}}`, `_first_json_object` returned `{"a": 1}`. Because `"a"` is not a tool name, `text_tool_calls` returned `[]`, dropping the valid tool call and triggering an unnecessary `"no_call"` retry turn. Furthermore, models emitting multiple JSON tool calls in a single response had all calls after the first ignored.
- **Implemented Fix**:
  Implemented `_iter_json_objects(text)` using `json.JSONDecoder().raw_decode` to iterate across all top-level balanced JSON objects and arrays in `text`, allowing `text_tool_calls` to recover all valid tool calls even when preceded by brace expressions in prose.

---

### Issue 3: `AttributeError` Crash in `sysdeps._virtual_installs` in `--local` Mode & Missing Reference Exclusions in `translate_declarations`
- **File**: [`sat_agent/sysdeps.py`](../sat_agent/sysdeps.py)
- **Severity**: High (Runtime Crash in Local Mode)
- **Root Cause**:
  1. In `_virtual_installs(ctx, installed, report)`, when a newly installed virtual package was mapped to its real provider package, `ctx.spec.remove_packages([pkg])` and `ctx.spec.add_packages([real[0]])` were called unconditionally. In `--local` mode (Linux, macOS, or MSYS2), `ctx.spec` is `None`, raising `AttributeError: 'NoneType' object has no attribute 'remove_packages'`.
  2. In `translate_declarations(ctx, ref, report)`, `excluded` only queried `pe.get_system_info_list(text, ctx.dist, src + "_exclude")` and `lists` only queried `ctx.dist`, missing exclusions and package lists from the default `system_info` block or `ref` when `translate_declarations` is called on a file where `ctx.dist` does not yet have a cloned block.
- **Implemented Fix**:
  Guarded `ctx.spec` updates with `if ctx.spec is not None:` in `_virtual_installs`, and updated `translate_declarations` to union exclusions from `None` (default), `ref`, and `ctx.dist` and fall back to `ref` when `ctx.dist` has no source keys yet.

---

### Issue 4: Platform Suffix Regex (`_PLATFORM_SUFFIX_RE` & `_placement`) Fails on Rolling-Release (`AR`, `VO`) and Multi-Segment (`MSYS_UCRT64`, `MSYS_MINGW64`) Distro Codes
- **Files**: [`sat_agent/pyconf_edit.py`](../sat_agent/pyconf_edit.py), [`sat_agent/sysdeps.py`](../sat_agent/sysdeps.py)
- **Severity**: Medium-High (Multi-Platform `.pyconf` Corruption)
- **Root Cause**:
  1. `_PLATFORM_SUFFIX_RE = re.compile(r"_[A-Z]{2,}\d+(?:_\d+)?$")` in `pyconf_edit.py` required trailing digits. When cloning a section already suffixed with a rolling-release code (`_AR`, `_VO`) or an MSYS2 environment code (`_MSYS_UCRT64`, `_MSYS_MINGW64`), `platform_section_name` failed to strip the previous platform suffix and produced invalid compound names such as `default_AR_FD45` or `default_MSYS_UCRT64_MSYS_MINGW64`.
  2. `_placement` in `sysdeps.py` used a similar digit-only regex `r"_([A-Z]{2,})(\d+(?:_\d+)?)"`.
- **Implemented Fix**:
  Updated `_PLATFORM_SUFFIX_RE` in `pyconf_edit.py` and `_placement` in `sysdeps.py` to match `_(?:MSYS_[A-Z0-9]+|AR|VO|[A-Z]{2,}\d+(?:_\d+)?)` and delegate version tuple extraction to `dist_version_key`.

---

### Issue 5: Compiler/Linker Error Parsers & Pre-Diagnosis Miss MSVC (`C1083`, `C2xxx`, `LNKxxxx`) and Relative Source Paths
- **Files**: [`sat_agent/parsers.py`](../sat_agent/parsers.py), [`sat_agent/diagnose.py`](../sat_agent/diagnose.py)
- **Severity**: High (Windows/Cross-Platform Build Diagnosis)
- **Root Cause**:
  1. `_ERR_RES` in `parsers.py` required compiler error lines to start with an absolute Unix path (`^(?P<file>/\S+?):(?P<line>\d+)...`). Compiler errors on relative paths (`src/foo.cxx:42:10: error: ...`), Windows drive paths (`C:/.../foo.cxx:42: error: ...`), and MSVC compiler/linker errors (`foo.cxx(42): error C2065: ...`, `fatal error C1083: Cannot open include file: 'foo.h'`, `LINK : fatal error LNK1104: cannot open file 'foo.lib'`) were ignored by `first_errors()`.
  2. `_MISSING_HEADER_RE` and `_MISSING_LIB_RE` only matched GCC/Clang/ld syntax, missing MSVC `C1083` and `LNK1104`.
  3. `_ERROR_TIERS` and `error_query` in `diagnose.py` did not match `error Cxxxx:` / `error LNKxxxx:` or strip Windows drive paths and `(line,col)` locations.
- **Implemented Fix**:
  Extended `_ERR_RES`, `_MISSING_HEADER_RE`, and `_MISSING_LIB_RE` in `parsers.py` and `_ERROR_TIERS` / `error_query` in `diagnose.py` to support relative source paths, Windows drive-letter paths, and MSVC `Cxxxx` / `LNKxxxx` diagnostics.

---

### Issue 6: Test-Phase Rebuild Failure Overwrites Failed Product Identity & Drops Compile Metadata
- **File**: [`sat_agent/nodes/build.py`](../sat_agent/nodes/build.py)
- **Severity**: Medium-High (LangGraph State Machine / Test-to-Build Loop)
- **Root Cause**:
  In `rebuild()`, when `phase == "test"` and `sat compile` failed after a test-phase fix:
  ```python
  if phase == "test":
      f2 = {**failure, "message": "The rebuild after the fix failed:\n" + f2.get("message", "")}
  ```
  Spreading `**failure` (the test failure dict) without merging `**f2` discarded `f2["product"]` (when the test failure had no product or when a rebuilt dependent product failed), `f2["step"]`, and `f2["log_file"]`. On the next `fix` iteration, `_enrich` could not locate the failing build log or run pre-diagnosis.
- **Implemented Fix**:
  Merged `{**failure, **f2, "phase": "test", "product": f2.get("product") or failure.get("product"), "message": ...}` so all compile failure metadata (`product`, `step`, `log_file`) is preserved while keeping the node in the `"test"` lifecycle and retaining `feedback`.

---

### Issue 7: History Compaction (`compact_history`) Second-Pass Threshold Bug & Stubbing Already-Compacted Content
- **File**: [`sat_agent/agents/memory.py`](../sat_agent/agents/memory.py)
- **Severity**: Medium (Context Window Management)
- **Root Cause**:
  1. In `compact_history`, pass 2 (`shorten is stub`) lowered the message size threshold from `_SMALL` (`1200`) to `400` at the loop guard, but the assistant branch still checked `elif m.get("role") == "assistant" and len(c) > _SMALL:`, preventing assistant messages between 400 and 1200 characters from being shortened during pass 2.
  2. When pass 2 ran `stub(tool, c)` on a tool message that had already been shortened by pass 1 (`compact_output`), `c` was the compacted text rather than `changes[i]["old"]`, causing the stub header to report the compacted character count and use the compacted header's second line.
- **Implemented Fix**:
  Unified the threshold check (`len(c) > threshold`) across all message roles and passed `src = changes[i]["old"] if i in changes else c` into `shorten` so pass 2 always stubs from the original uncompacted tool output.

---

### Issue 8: Unconfined Path Resolution in `LOGS` Tools & Uncaught `re.error` on Invalid LLM Regexes in `grep` Parameters
- **Files**: [`sat_agent/tools/common.py`](../sat_agent/tools/common.py), [`sat_agent/tools/test_tools.py`](../sat_agent/tools/test_tools.py), [`sat_agent/tools/web.py`](../sat_agent/tools/web.py)
- **Severity**: Medium (Tool Safety & Fault Tolerance)
- **Root Cause**:
  1. `product_logs`, `read_log`, and `log_errors` constructed `ctx.app_workdir / "LOGS" / product / file` without path confinement, allowing `..` segments to escape `LOGS/`, and `product_logs` did not filter `f.is_file()`.
  2. `read_log(..., grep=...)`, `test_output(..., grep=...)`, and `fetch(..., grep=...)` called `re.search(grep, ln, re.I)` directly. When an LLM passed unescaped C++ syntax such as `operator[](` or `std::vector<int>*` in `grep`, `re.error` was raised instead of matching literally.
- **Implemented Fix**:
  Added `resolve_log_path(ctx, *parts)` to confine all log tool accesses to `ctx.app_workdir / "LOGS"`, filtered `product_logs` to regular files, and added `safe_line_matcher(pattern)` which falls back to case-insensitive literal substring matching when `re.compile` raises `re.error`.

---

### Issue 9: SQLite Checkpointer Connection Leak & Subprocess Cleanup on Interruption/Timeout
- **Files**: [`sat_agent/cli.py`](../sat_agent/cli.py), [`sat_agent/docker_env.py`](../sat_agent/docker_env.py)
- **Severity**: Medium (Resource Management)
- **Root Cause**:
  1. `cmd_port` in `cli.py` opened a SQLite connection via `sqlite3.connect(str(path), check_same_thread=False)` inside `_checkpointer` and never closed it upon completion or interruption.
  2. `_run` in `docker_env.py` did not ensure `proc.kill()` + `proc.wait()` and `proc.stdout.close()` inside its `finally:` block if an exception interrupted the stdout reader loop while the child process was still alive.
- **Implemented Fix**:
  Added a `finally:` block in `cmd_port` to close `ckpt.conn` on exit, and hardened `_run`'s `finally:` block in `docker_env.py` to terminate/reap any still-running child process and close `proc.stdout`.

---

### Issue 10: Indirect Prompt-Injection Defense for Untrusted Web Content (`tools/web.py`)
- **File**: [`sat_agent/tools/web.py`](../sat_agent/tools/web.py)
- **Severity**: Medium (AI Security / Indirect Prompt Injection)
- **Root Cause**:
  `Web.search` and `Web.fetch` retrieve arbitrary third-party web pages, mailing list posts, and issue trackers and inject them into the ReAct agent's conversation history. Unlike `request.py`, `web.py` did not sanitize prompt-injection phrases or model control tokens (`<|im_start|>`, `<|im_end|>`, `<|endoftext|>`, `[INST]`, `[/INST]`, `<tool_call>`, `</tool_response>`), which local models served via Ollama/vLLM may interpret as control boundaries.
- **Implemented Fix**:
  Added `_INJECTION_RE` and `_sanitize_web(text)` in `sat_agent/tools/web.py` and applied it to all fetched pages (both HTML and plain text) and search result titles/snippets.

---

## 3. Strategic Recommendations for Future Evolution

1. **DAG-Aware Parallel Product Compilation & Repair**:
   - Currently `sat compile --stop_first_fail` stops serially at the first failing product. Parsing the product dependency DAG from `sat` would allow independent subtrees to compile in parallel while the build agent diagnoses and patches a broken leaf product on an isolated worktree.
2. **Dry-Run Patch Verification Before Leaving `edit_source`**:
   - When `edit_source` modifies a file in `SOURCES/<product>`, running a fast single-translation-unit syntax check (or `ninja -C BUILD/<product>` dry-run when `BUILD/<product>` already exists) inside `edit_source` could give the agent immediate compiler feedback within a single ReAct run before spending a full `sat prepare` + `sat compile --clean_all` cycle in `rebuild`.
3. **Structured Cross-Port Knowledge Base**:
   - Persisting verified `(error_signature, upstream_product, version, platform_family) -> fix_summary` tuples across ports in `<work_root>/knowledge.json` would allow `diagnose.py` to surface proven fixes from earlier ports (e.g., Fedora 45 -> Arch Linux or Ubuntu 26.10) automatically.
