import logging
import sys

log = logging.getLogger("sat_agent")


def setup_logging(verbose: int = 0, log_file: str | None = None) -> None:
    level = logging.DEBUG if verbose >= 1 else logging.INFO
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(level)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname).1s %(message)s", "%H:%M:%S"))
    log.addHandler(console)
    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(fh)
