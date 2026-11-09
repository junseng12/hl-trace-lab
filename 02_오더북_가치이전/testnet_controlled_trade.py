"""Stable entry point for the bounded Testnet controlled-fill experiment.

The validated implementation remains in
``02_오더북_가치이전/실행도구/테스트넷_T1_단건체결.py``.
This wrapper gives the two-part repository a clear, ASCII-only command path
without duplicating safety-critical signing code.
"""

from pathlib import Path
import runpy


HERE = Path(__file__).resolve().parent

# Keep the safety-critical implementation in one place.  run_path forwards the
# current command-line arguments to the already-tested Testnet runner, while
# this short ASCII path gives teammates a predictable module entry point.
runpy.run_path(
    str(HERE / "실행도구" / "테스트넷_T1_단건체결.py"),
    run_name="__main__",
)
