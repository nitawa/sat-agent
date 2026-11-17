"""What makes the build agent find a compile error by itself: the failing product is identified
from sat's real (\\r-rewritten) output, the first real error is extracted and searched on the web,
and the tool loop cannot burn its budget repeating one call."""

from sat_agent.agents.react import Tool, obj, run_agent, S
from sat_agent.diagnose import error_query, first_error_line, missing_items
from sat_agent.parsers import parse_compile

# exactly what sat writes (commands/compile.py: log_step rewrites the line with \r)
SAT_OUTPUT = ("Compilation of boost ......................... \rCompilation of boost .........................     "
              "     \rCompilation of boost ......................... native\n"
              "Compilation of cmake ......................... \rCompilation of cmake .........................     "
              "     \rCompilation of cmake ......................... SCRIPT /x/sat_salome/products/compil_scripts/"
              "cmake.sh\rCompilation of cmake .........................                    \r"
              "Compilation of cmake ......................... \x1b[31mKO\x1b[0m \n\nCompilation: KO (43/44)\n")

CMAKE_LOG = """-- Looking for strerror - found
-- Could NOT find OpenSSL, try to set the path to OpenSSL root folder (missing: OPENSSL_CRYPTO_LIBRARY)
[ 42%] Building C object Utilities/cmcurl/lib/CMakeFiles/cmcurl.dir/vtls/openssl.c.o
/b/SOURCES/cmake/Utilities/cmcurl/lib/vtls/openssl.c:477:11: error: invalid use of incomplete typedef ‘ASN1_INTEGER’ {aka ‘struct asn1_string_st’}
make[2]: *** [Utilities/cmcurl/lib/CMakeFiles/cmcurl.dir/build.make:2207: openssl.c.o] Error 1
"""


def test_failing_product_found_in_rewritten_lines():
    r = parse_compile(SAT_OUTPUT)
    assert not r.ok and (r.done, r.total) == (43, 44)
    assert r.failed == [("cmake", "SCRIPT")]       # the step shown before KO
    assert "boost" in r.succeeded and r.last == "cmake"


def test_first_real_error_and_query():
    err = first_error_line(CMAKE_LOG)
    assert "invalid use of incomplete typedef" in err          # not "-- Looking for strerror - found"
    assert error_query(err, "cmake") == "cmake openssl.c invalid use of incomplete typedef ASN1_INTEGER"
    assert missing_items(CMAKE_LOG)["cmake packages"] == ["OpenSSL"]
    assert missing_items("fatal error: hdf5.h: No such file or directory\n")["headers"] == ["hdf5.h"]


class LoopingLLM:
    """Repeats one grep forever, concludes only when nothing but `finish` is offered."""
    def __init__(self):
        self.calls = 0
        self.offered = []

    def chat(self, messages, tools=None):
        self.calls += 1
        names = [t["name"] for t in tools]
        self.offered.append(names)
        if names == ["finish"]:
            call = ("finish", {"status": "give_up", "summary": "cmcurl does not build with OpenSSL 4"})
        else:
            call = ("grep", {"pattern": "Cython", "path": "products"})
        tc = [{"id": f"c{self.calls}", "name": call[0], "arguments": call[1]}]
        return {"content": "", "tool_calls": tc, "raw": {"tool_calls": tc, "content": ""}}


def test_repeated_calls_are_not_rerun_and_a_conclusion_is_forced(tmp_path):
    ran = []
    tools = [Tool("grep", "g", obj({"pattern": S, "path": S}), lambda **a: ran.append(a) or "no match"),
             Tool("finish", "f", obj({"status": S, "summary": S}), lambda **a: dict(a), terminal=True)]
    llm = LoopingLLM()
    out = run_agent(llm, "sys", "task", tools, cap=30, name="t", transcript=tmp_path / "t.json")
    assert len(ran) == 1                              # executed once, then refused
    assert llm.calls < 12                             # stopped early instead of 30 identical turns
    assert llm.offered[-1] == ["finish"] and out["summary"] == "cmcurl does not build with OpenSSL 4"
    assert (tmp_path / "t.json").exists()


def test_budget_warning_then_final_finish(tmp_path):
    n = {"i": 0}

    def grep(**a):
        return "result"
    tools = [Tool("grep", "g", obj({"pattern": S}), grep),
             Tool("finish", "f", obj({"status": S, "summary": S}), lambda **a: dict(a), terminal=True)]

    class Varying(LoopingLLM):
        def chat(self, messages, tools=None):
            n["i"] += 1
            if [t["name"] for t in tools] == ["finish"]:
                tc = [{"id": "f", "name": "finish", "arguments": {"status": "give_up", "summary": "diag"}}]
            else:
                tc = [{"id": f"c{n['i']}", "name": "grep", "arguments": {"pattern": f"p{n['i']}"}}]
            self.last = messages
            return {"content": "", "tool_calls": tc, "raw": {"tool_calls": tc, "content": ""}}
    llm = Varying()
    out = run_agent(llm, "sys", "task", tools, cap=8)
    assert out["summary"] == "diag"
    assert any("turns left" in (m.get("content") or "") for m in llm.last if m["role"] == "user")


class Script(LoopingLLM):
    """Plays a fixed list of calls (one per turn), then finishes."""
    def __init__(self, calls):
        super().__init__()
        self.script = list(calls)

    def chat(self, messages, tools=None):
        self.calls += 1
        call = self.script.pop(0) if self.script else ("finish", {"status": "fixed", "summary": "done"})
        tc = [{"id": f"c{self.calls}", "name": call[0], "arguments": call[1]}]
        return {"content": "", "tool_calls": tc, "raw": {"tool_calls": tc, "content": ""}}


def test_reads_run_again_after_an_action():
    """Issue #2: re-reading a file to check an edit is not a repeat; the same edit twice still is."""
    files = {"a.c": "old"}
    ran = []

    def read_file(path):
        ran.append(("read", files[path]))
        return files[path]

    def edit_source(path, new_text):
        ran.append(("edit", new_text))
        files[path] = new_text
        return "edited"
    tools = [Tool("read_file", "r", obj({"path": S}), read_file),
             Tool("edit_source", "e", obj({"path": S, "new_text": S}), edit_source, mutates=True),
             Tool("finish", "f", obj({"status": S, "summary": S}), lambda **a: dict(a), terminal=True)]
    read, edit = ("read_file", {"path": "a.c"}), ("edit_source", {"path": "a.c", "new_text": "new"})
    out = run_agent(Script([read, read, edit, read, edit, read]), "sys", "task", tools, cap=10)
    assert out["status"] == "fixed"
    # 2nd read refused (nothing changed), read after the edit runs, the same edit again is refused,
    # and so is the read after that refused edit
    assert ran == [("read", "old"), ("edit", "new"), ("read", "new")]


def test_build_action_tools_are_marked_as_mutating():
    from types import SimpleNamespace
    from sat_agent.tools.build_tools import FixSession

    ctx = SimpleNamespace(env=SimpleNamespace(windows=False), family=SimpleNamespace(system_packages=True))
    tools = FixSession(ctx, None).tools()
    assert {t.name for t in tools if t.mutates} == {
        "edit_source", "enable_repository", "install_system_packages", "replace_system_package",
        "set_product_option", "add_compil_script", "set_product_version", "exclude_product"}
