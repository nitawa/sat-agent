"""sat-agent: port SALOME (SAT / SAT_SALOME) to a new Linux platform with LangGraph agents."""

__version__ = "0.2.0"


import os as _os

if _os.name == "nt":
    # Windows: Path.write_text translates "\n" into "\r\n". Every file sat-agent writes (pyconf
    # edits, patches, scripts, reports) must keep the repositories' LF line ends, or a one-line edit
    # rewrites a whole pyconf and the patches no longer apply on Linux: write "\n" unless the
    # caller asks otherwise (Python >= 3.10 has the `newline` argument).
    from pathlib import Path as _Path

    _write_text = _Path.write_text

    def _write_text_lf(self, data, encoding=None, errors=None, newline="\n"):
        return _write_text(self, data, encoding=encoding, errors=errors, newline=newline)

    _Path.write_text = _write_text_lf
