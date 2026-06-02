"""Launcher that runs phantom's orchestrator.main() with patches in effect.

Running `python orchestrator.py` directly creates a `__main__` module that is
distinct from the `orchestrator` module our sitecustomize patches target — so
calls inside __main__ would still hit the unpatched functions. Importing the
module first (which the bench-staged sitecustomize already does at startup)
and then calling main() ensures exactly one module instance with the four
monkey-patches applied.

PhantomRuntime spawns this launcher via `python <this-file> --task <text>`.
The argv shim makes argparse inside orchestrator.main() see the same flags
it would in production.
"""

from __future__ import annotations

import sys


def main() -> int:
    # orchestrator.main() reads sys.argv via argparse; mimic the production
    # `python orchestrator.py --task ...` invocation.
    args_for_orch = sys.argv[1:]
    sys.argv = ["orchestrator.py", *args_for_orch]

    # Importing orchestrator here triggers the same import the sitecustomize
    # patches use; Python caches it in sys.modules so all patches stay in
    # effect for the call below.
    import orchestrator  # type: ignore[import-not-found]

    orchestrator.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
