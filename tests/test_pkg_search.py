from unittest.mock import patch

from sat_agent.pkg_search import (
    AUR_PREFIX,
    _best_by_description,
    _parse_pacman_search_results,
    derive_name_from_debian_and_rpm,
    fetch_debian_package_description,
    search_aur_package,
    verify_system_package,
)


class _FakeExecutorClient:
    """Records calls and returns canned responses keyed by exact command."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls = []

    def run_in_container(self, cmd, work_dir, image_tag, timeout=30, **kwargs):
        self.calls.append(cmd)
        return self.responses.get(cmd, {"ok": False, "stdout": "", "stderr": "not mocked"})


def test_verify_system_package_confirms_first_matching_guess():
    client = _FakeExecutorClient({
        "pacman -Si eigen": {"ok": True, "stdout": "Name : eigen\n"},
    })
    result = verify_system_package(
        "eigen3", ["eigen", "eigen3"], "arch", client, "/work", "img",
    )
    assert result == "eigen"
    # the second guess should never even be queried once the first hits
    assert "pacman -Si eigen3" not in client.calls


def test_verify_system_package_falls_back_to_search_when_guesses_fail():
    client = _FakeExecutorClient({
        "pacman -Si freeimage3": {"ok": False, "stdout": "", "stderr": "error: package 'freeimage3' was not found"},
        "pacman -Ss freeimage": {"ok": True, "stdout": "extra/freeimage 3.19.0-1 (graphics)\n    Library for image formats\n"},
    })
    with patch("sat_agent.pkg_search.fetch_debian_package_description", return_value=None):
        result = verify_system_package(
            "libfreeimage3", ["freeimage3"], "arch", client, "/work", "img",
        )
    assert result == "freeimage"


def test_verify_system_package_prefers_description_match_over_first_search_hit():
    # Real-world case: a bare name search for "jinja2" surfaces
    # python-j2cli (a CLI tool built on Jinja2) before python-jinja (the
    # actual Jinja2 library Debian's python3-jinja2 package is).
    client = _FakeExecutorClient({
        "pacman -Si python-jinja2": {"ok": False, "stdout": "", "stderr": "not found"},
        # Real-world split: the narrower term ("jinja2") only turns up the
        # wrong package; the broader one ("jinja", also tried since
        # _candidate_search_terms strips trailing digits) is what actually
        # has the real library -- confirmed live, this exact case is why
        # candidates must be collected across ALL terms, not just the
        # first term that returns anything.
        "pacman -Ss jinja2": {
            "ok": True,
            "stdout": "extra/python-j2cli 0.3.12-4\n    Command-line interface to Jinja2 for templating in shell scripts.\n",
        },
        "pacman -Ss jinja": {
            "ok": True,
            "stdout": (
                "extra/python-j2cli 0.3.12-4\n"
                "    Command-line interface to Jinja2 for templating in shell scripts.\n"
                "extra/python-jinja 3.1.6-1\n"
                "    A small but fast and easy to use stand-alone template engine\n"
            ),
        },
    })
    with patch(
        "sat_agent.pkg_search.fetch_debian_package_description",
        return_value="small but fast and easy to use stand-alone template engine",
    ):
        result = verify_system_package(
            "python3-jinja2", ["python-jinja2"], "arch", client, "/work", "img",
        )
    assert result == "python-jinja"


def test_verify_system_package_merges_candidates_across_multiple_search_terms():
    # Regression test for the bug above, isolated to the merging logic:
    # a term with only ONE (wrong) hit must not short-circuit before a
    # later term's better hit is even considered.
    client = _FakeExecutorClient({
        "pacman -Si eigen3": {"ok": False, "stdout": "", "stderr": "not found"},
        "pacman -Ss eigen3": {"ok": True, "stdout": "extra/wrong-eigen3-thing 1.0-1\n    Unrelated widget\n"},
        "pacman -Ss eigen": {"ok": True, "stdout": "extra/eigen 5.0.1-2\n    Lightweight C++ template library for linear algebra\n"},
    })
    with patch(
        "sat_agent.pkg_search.fetch_debian_package_description",
        return_value="lightweight C++ template library for linear algebra",
    ):
        result = verify_system_package("eigen3", ["eigen3"], "arch", client, "/work", "img")
    assert result == "eigen"


def test_verify_system_package_name_match_beats_higher_description_overlap():
    # Real, live-observed false positive: python-kajiki's description
    # ("...template engine with...Jinja-style blocks") scores HIGHER on
    # raw keyword overlap against Debian's python3-jinja2 description
    # ("...template engine") than python-jinja's own description
    # ("...template language", no literal "engine") -- but python-jinja is
    # the actually-correct package, and its NAME contains the search term
    # ("jinja") while python-kajiki's doesn't. Name match must win.
    client = _FakeExecutorClient({
        "pacman -Si python-jinja2": {"ok": False, "stdout": "", "stderr": "not found"},
        "pacman -Ss jinja2": {
            "ok": True,
            "stdout": "extra/python-j2cli 0.3.12-4\n    Command-line interface to Jinja2 for templating in shell scripts.\n",
        },
        "pacman -Ss jinja": {
            "ok": True,
            "stdout": (
                "extra/python-j2cli 0.3.12-4\n"
                "    Command-line interface to Jinja2 for templating in shell scripts.\n"
                "extra/python-jinja 3.1.6-1\n"
                "    A simple pythonic template language written in Python\n"
                "extra/python-kajiki 1.0.2-2\n"
                "    Python XML-based template engine with Genshi-like syntax and Jinja-style blocks\n"
            ),
        },
    })
    with patch(
        "sat_agent.pkg_search.fetch_debian_package_description",
        return_value="small but fast and easy to use stand-alone template engine",
    ):
        result = verify_system_package(
            "python3-jinja2", ["python-jinja2"], "arch", client, "/work", "img",
        )
    assert result == "python-jinja"


def test_verify_system_package_falls_back_to_first_hit_when_no_reference_description():
    client = _FakeExecutorClient({
        "pacman -Si freeimage3": {"ok": False, "stdout": "", "stderr": "not found"},
        "pacman -Ss freeimage": {
            "ok": True,
            "stdout": "extra/freeimage 3.19.0-1 (graphics)\n    Library for image formats\n",
        },
    })
    with patch("sat_agent.pkg_search.fetch_debian_package_description", return_value=None):
        result = verify_system_package(
            "libfreeimage3", ["freeimage3"], "arch", client, "/work", "img",
        )
    assert result == "freeimage"


def test_verify_system_package_falls_back_to_aur_when_official_repos_have_nothing():
    # Real-world case: freeimage genuinely isn't in Arch's official repos
    # (confirmed live via pacman -Ss) but is on the AUR.
    client = _FakeExecutorClient({})  # every official pacman call misses
    with patch("sat_agent.pkg_search.fetch_debian_package_description", return_value=None), \
         patch("sat_agent.pkg_search.search_aur_package", return_value="freeimage") as mock_aur:
        result = verify_system_package(
            "libfreeimage3", ["freeimage3", "freeimage"], "arch", client, "/work", "img",
        )
    assert result == f"{AUR_PREFIX}freeimage"
    mock_aur.assert_called_once()


def test_verify_system_package_returns_none_when_nothing_found_anywhere():
    client = _FakeExecutorClient({})  # everything falls through to the default not-ok response
    with patch("sat_agent.pkg_search.fetch_debian_package_description", return_value=None), \
         patch("sat_agent.pkg_search.search_aur_package", return_value=None):
        result = verify_system_package(
            "python-totally-made-up-package", ["totally-made-up-package"], "arch", client, "/work", "img",
        )
    assert result is None


def test_verify_system_package_noop_for_non_arch_distros():
    client = _FakeExecutorClient({})
    with patch("sat_agent.pkg_search.search_aur_package") as mock_aur:
        result = verify_system_package("libproj-dev", ["proj"], "debian", client, "/work", "img")
    assert result is None
    assert client.calls == []
    mock_aur.assert_not_called()


def test_verify_system_package_rejects_unsafe_candidate_names():
    client = _FakeExecutorClient({})
    with patch("sat_agent.pkg_search.fetch_debian_package_description", return_value=None), \
         patch("sat_agent.pkg_search.search_aur_package", return_value=None):
        verify_system_package("pkg", ["eigen; rm -rf /"], "arch", client, "/work", "img")
    assert all("rm -rf" not in c for c in client.calls)


def test_search_aur_package_confirms_exact_candidate_via_info():
    def fake_rpc_get(path, timeout=10):
        assert path.startswith("info?arg[]=")
        return {"resultcount": 1, "results": [{"Name": "freeimage"}]}

    with patch("sat_agent.pkg_search._aur_rpc_get", side_effect=fake_rpc_get):
        result = search_aur_package(["freeimage"], [])
    assert result == "freeimage"


def test_search_aur_package_falls_back_to_search_by_name():
    def fake_rpc_get(path, timeout=10):
        if path.startswith("info?"):
            return {"resultcount": 0, "results": []}
        assert path.startswith("search/")
        return {"resultcount": 1, "results": [{"Name": "python-pytest-cython", "Description": "pytest plugin"}]}

    with patch("sat_agent.pkg_search._aur_rpc_get", side_effect=fake_rpc_get):
        result = search_aur_package(["pytest-cython"], ["pytest-cython"])
    assert result == "python-pytest-cython"


def test_search_aur_package_picks_best_description_match_among_results():
    def fake_rpc_get(path, timeout=10):
        if path.startswith("info?"):
            return {"resultcount": 0, "results": []}
        return {
            "resultcount": 2,
            "results": [
                {"Name": "unrelated-thing", "Description": "totally unrelated widget manager"},
                {"Name": "freeimage-git", "Description": "library for image formats, git version"},
            ],
        }

    with patch("sat_agent.pkg_search._aur_rpc_get", side_effect=fake_rpc_get):
        result = search_aur_package(["freeimage"], ["freeimage"], reference_desc="support library for graphics image formats")
    assert result == "freeimage-git"


def test_search_aur_package_returns_none_when_nothing_found():
    with patch("sat_agent.pkg_search._aur_rpc_get", return_value={"resultcount": 0, "results": []}):
        result = search_aur_package(["totally-made-up"], ["totally-made-up"])
    assert result is None


def test_search_aur_package_returns_none_on_network_error():
    with patch("sat_agent.pkg_search._aur_rpc_get", return_value=None):
        result = search_aur_package(["freeimage"], ["freeimage"])
    assert result is None


def test_parse_pacman_search_results_real_shape():
    stdout = (
        "extra/python-j2cli 0.3.10-4\n"
        "    Command-line interface for Jinja2\n"
        "extra/python-jinja 3.1.6-1\n"
        "    A small but fast and easy to use stand-alone template engine\n"
    )
    assert _parse_pacman_search_results(stdout) == [
        ("python-j2cli", "Command-line interface for Jinja2"),
        ("python-jinja", "A small but fast and easy to use stand-alone template engine"),
    ]


def test_parse_pacman_search_results_empty():
    assert _parse_pacman_search_results("") == []


def test_best_by_description_no_reference_returns_first():
    candidates = [("a", "foo"), ("b", "bar")]
    assert _best_by_description(candidates, None) == "a"


def test_best_by_description_scores_keyword_overlap():
    candidates = [
        ("python-j2cli", "command-line interface for Jinja2"),
        ("python-jinja", "small but fast and easy to use stand-alone template engine"),
    ]
    result = _best_by_description(candidates, "small but fast and easy to use stand-alone template engine")
    assert result == "python-jinja"


def test_fetch_debian_package_description_parses_meta_tag():
    html = '<html><head><meta name="Description" content="lightweight C++ template library for linear algebra"></head></html>'

    class _Resp:
        def read(self):
            return html.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with patch("urllib.request.urlopen", return_value=_Resp()):
        result = fetch_debian_package_description("libeigen3-dev")
    assert result == "lightweight C++ template library for linear algebra"


def test_fetch_debian_package_description_returns_none_for_missing_package():
    html = '<title>Debian -- Error</title><p>No such package.</p>'

    class _Resp:
        def read(self):
            return html.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with patch("urllib.request.urlopen", return_value=_Resp()):
        result = fetch_debian_package_description("nonexistent-package-xyz")
    assert result is None


def test_fetch_debian_package_description_rejects_unsafe_names():
    assert fetch_debian_package_description("pkg; rm -rf /") is None


def test_derive_name_from_debian_and_rpm_real_boost_case():
    # Real data seen live: SAT_SALOME's products/boost.pyconf declares
    # apt_dev = ['libboost-all-dev'], rpm_dev = ['boost-devel'] -- the
    # shared token "boost" is exactly Arch's real package name.
    assert derive_name_from_debian_and_rpm("libboost-all-dev", ["boost-devel"]) == "boost"


def test_derive_name_from_debian_and_rpm_conservative_pass_wins_when_it_matches():
    # Debian and RPM both spell it "eigen3" (they don't disagree with each
    # other), so the conservative pass matches on "eigen3" -- there's
    # nothing here for the heuristic to detect that Arch happens to drop
    # the "3". "eigen3" is still a reasonable candidate to try via an exact
    # `pacman -Si` first (cheap, fails harmlessly), before falling through
    # to the network-description search that finds the real "eigen".
    assert derive_name_from_debian_and_rpm("libeigen3-dev", ["eigen3-devel"]) == "eigen3"


def test_derive_name_from_debian_and_rpm_falls_back_to_digit_stripped_pass():
    # Debian keeps the "3" (libfreeimage3) but its own -dev package and
    # RPM's package don't -- only the digit-stripped comparison finds the
    # shared "freeimage" token.
    assert derive_name_from_debian_and_rpm("libfreeimage3", ["freeimage-devel"]) == "freeimage"


def test_derive_name_from_debian_and_rpm_prefers_conservative_hdf5_case():
    # Regression test: HDF5's "5" is part of the real name on every distro
    # (Debian, RPM, and Arch all call it hdf5) -- digit-stripping must NOT
    # turn this into "hdf".
    assert derive_name_from_debian_and_rpm("libhdf5-dev", ["hdf5-devel"]) == "hdf5"


def test_derive_name_from_debian_and_rpm_tries_multiple_rpm_names():
    assert derive_name_from_debian_and_rpm("libhdf5-dev", ["nothing-alike", "hdf5-devel"]) == "hdf5"


def test_derive_name_from_debian_and_rpm_no_overlap_returns_none():
    assert derive_name_from_debian_and_rpm("libfoo-dev", ["bar-devel"]) is None


def test_derive_name_from_debian_and_rpm_empty_rpm_list_returns_none():
    assert derive_name_from_debian_and_rpm("libboost-all-dev", []) is None
