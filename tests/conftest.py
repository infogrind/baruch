import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from support import Env  # noqa: E402


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.drives.undock_all()
    # Restore permissions so pytest can clean up the temp folder.
    for dirpath, dirnames, _ in os.walk(tmp_path):
        for d in dirnames:
            p = os.path.join(dirpath, d)
            if not os.path.islink(p):
                os.chmod(p, 0o755)
