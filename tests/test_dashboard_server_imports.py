"""Import-contract test for dashboard/server.py (OCR finding #5).

`logging` was used in server.py (browser auto-open except block) but never
imported, so the handler raised NameError instead of logging.

Why an AST/source check instead of importing the module: importing
dashboard.server has import-time side effects (argparse/uvicorn/LaunchAgent
env loading), so the module is intentionally never imported here. This is an
IMPORT CONTRACT, not a behaviour test — asserting the import exists in the
source is the correct and sufficient check.
"""

import ast
from pathlib import Path

SERVER_PY = Path(__file__).resolve().parent.parent / "dashboard" / "server.py"


def _module_imports_logging(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "logging" for alias in node.names):
                return True
        if isinstance(node, ast.ImportFrom):
            if node.module == "logging":
                return True
    return False


def test_server_module_imports_logging():
    tree = ast.parse(SERVER_PY.read_text(encoding="utf-8"))
    assert _module_imports_logging(tree), (
        "dashboard/server.py uses logging but never imports it "
        "(NameError in the browser auto-open except block)"
    )
