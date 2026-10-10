import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SAT_DIR = ROOT / "sat"
SAT_SALOME_DIR = ROOT / "sat_salome"

needs_real_repos = pytest.mark.skipif(not (SAT_DIR / "src" / "pyconf.py").exists()
                                      or not (SAT_SALOME_DIR / "salome.pyconf").exists(),
                                      reason="real sat / sat_salome checkouts not found next to sat-agent")

APP = """\
APPLICATION :
{
    name      : 'APP'
    workdir   : $LOCAL.workdir + $VARS.sep + $APPLICATION.name + '-' + $VARS.dist
    tag       : 'master'
    platform : ["FD44", "UB24.04", "UB26.04"]
    products :
    {
        boost : 'native'
        salome_system : 'native'
        cgal : 'v5.6.1'
        'KERNEL'
    }
    properties :
    {
        git_server : 'tuleap'
    }
}
__overwrite__ :
[
    {
        __condition__ : "APPLICATION.use_pyside == 'no'"
        'APPLICATION.rm_products_for_all_distributions'   : ['pyside']
    }
    {
        __condition__ : "VARS.dist in ['UB24.04']"
        'APPLICATION.rm_products' : ['hdf5_openmpi']
    }
    {
        __condition__ : "VARS.dist in ['UB26.04']"
        'APPLICATION.rm_products' : ['hdf5_openmpi']
        'APPLICATION.products.cgal' : {tag: 'v5.6.1', base: 'no', section: 'version_v5_6_1_UB26_04'}
    }
    {
        __condition__ : "VARS.dist in ['FD42', 'FD44']"
        'APPLICATION.products.boost' : 'native'
    }
]
"""

SYSTEM = """\
default :
{
    name : "salome_system"
    get_source : "native"
    system_info :
    {
        rpm : ["gcc"]
        rpm_dev : ["openssl-devel"]
        apt : ["libc6", "libtiff5"]
        apt_dev : ["libssl-dev", "g++"]
        "UB24.04" :
        {
            apt : ["libgdal34t64"]
            apt_dev : ["libtbb-dev"]
        }
        "UB26.04" :
        {
            apt : ["libgdal38"]   # comment ] with bracket
            apt_dev : ["libtbb-dev", "gfortran-13"]
        }
    }
    depend : []
    properties :
    {
      incremental : "yes"
    }
}
"""

CGAL = """\
default :
{
    name : "cgal"
    build_source : "cmake"
    cmake_options : "-DWITH_tests=ON"
    get_source : "git"
    system_info :
    {
        rpm : []
        rpm_dev : ['CGAL-devel']
        apt : []
        apt_dev : ['libcgal-dev']
    }
    patches : []
    depend : []
    properties :
    {
      incremental : "yes"
    }
}

version_v5_6_1:
{
    patches : ["cgal-p01.patch"]
}

version_v5_6_1_UB26_04:
{
    patches : ["cgal-p01.patch", "cgal-p03.patch"]
}
"""


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def mini_sat_salome(tmp_path):
    """A tiny sat_salome git repository with real pyconf syntax."""
    repo = tmp_path / "sat_salome"
    (repo / "applications").mkdir(parents=True)
    (repo / "products" / "patches").mkdir(parents=True)
    (repo / "dockers").mkdir()
    (repo / "salome.pyconf").write_text("PROJECT :\n{\n}\n")
    (repo / "applications" / "APP.pyconf").write_text(APP)
    (repo / "products" / "salome_system.pyconf").write_text(SYSTEM)
    (repo / "products" / "cgal.pyconf").write_text(CGAL)
    (repo / "products" / "patches" / "cgal-p01.patch").write_text("")
    git(repo, "init", "-q")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                          text=True).stdout.strip()
    return repo, base


@pytest.fixture
def sat_copy(tmp_path):
    if not SAT_DIR.exists():
        pytest.skip("no sat checkout")
    dst = tmp_path / "sat"
    shutil.copytree(SAT_DIR, dst, ignore=shutil.ignore_patterns(".git"))
    return dst
