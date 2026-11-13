import os
import sys

if __package__ in (None, ""):
    # Started as `python path/to/codelens_agent` (a directory, not -m):
    # make the package importable from any working directory. The package
    # directory itself is removed from sys.path so its module names (cli,
    # search, ...) cannot shadow anything.
    _pkg_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _pkg_dir]
    sys.path.insert(0, os.path.dirname(_pkg_dir))
    from codelens_agent.cli import main
else:
    from .cli import main

if __name__ == "__main__":
    sys.exit(main())
