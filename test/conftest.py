import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path


import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--mkfontscale",
        default=None,
        help="Path to the mkfontscale binary to test",
    )

@dataclass
class MkFontScale:
    path: Path

    def run(self, directory: Path) -> subprocess.CompletedProcess:
        """
        Run ``mkfontscale -b`` on *directory* and return the CompletedProcess.

        Asserts on crashes and asan/ubsan markers in stderr
        """

        result = subprocess.run(
            [self.path, "-b", directory],
            capture_output=True,
            timeout=10,
        )

        stderr = result.stderr.decode("utf-8", errors="replace")
        # A negative return code means death by signal (e.g. -11 = SIGSEGV).
        assert result.returncode >= 0, (
            f"mkfontscale was killed by signal "
            f"{signal.Signals(-result.returncode).name}: "
            f"stderr={stderr!r}"
        )

        # ASan/MSan/UBSan exit with a small positive code (usually 1) and
        # print diagnostics to stderr rather than dying by signal.
        # Check for specific error types but ignore LeakSanitizer reports —
        # pre-existing memory leaks in mkfontscale are unrelated to this fix.
        for marker in ("heap-buffer-overflow", "heap-use-after-free",
                    "stack-buffer-overflow", "global-buffer-overflow",
                    "use-of-uninitialized-value",
                    "MemorySanitizer", "UndefinedBehaviorSanitizer"):
            assert marker not in stderr, (
                f"Sanitizer error ({marker}) detected:\n{stderr}"
            )

        return result


@pytest.fixture
def mkfontscale(request, tmp_path) -> MkFontScale:
    """Return the path to the mkfontscale binary.

    Resolution order:
      1. --mkfontscale command-line option
      2. MKFONTSCALE environment variable
      3. ../mkfontscale (in-tree build)
      4. mkfontscale on $PATH

    Skips the test if the binary is not found or fails to execute
    (e.g. missing shared libraries).
    """
    path = request.config.getoption("--mkfontscale")
    if path is None:
        path = os.environ.get("MKFONTSCALE")
    if path is None:
        intree = Path(__file__).parent.parent / "mkfontscale"
        if intree.is_file() and os.access(intree, os.X_OK):
            path = intree
    if path is None:
        path = shutil.which("mkfontscale")
    if path is None:
        pytest.skip("mkfontscale binary not found")

    path = Path(path).resolve()

    # Verify the binary actually runs (catches missing shared libraries, etc.)
    try:
        result = subprocess.run(
            [path, str(tmp_path)],
            capture_output=True,
            timeout=5,
        )
    except OSError as e:
        pytest.fail(f"mkfontscale not executable: {e}")

    if result.returncode == 127:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        pytest.fail(f"mkfontscale cannot start: {stderr}")

    return MkFontScale(Path(path))
