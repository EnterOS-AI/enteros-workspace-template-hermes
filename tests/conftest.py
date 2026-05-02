"""Make the template files importable from tests/.

The repo layout puts adapter.py and executor.py at the root rather
than under a package directory. Inject the parent dir on sys.path so
pytest can import them as top-level modules.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
