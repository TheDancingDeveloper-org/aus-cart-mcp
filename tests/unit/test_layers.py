"""Layer rules: core never imports watch or ui; watch never imports ui.

A source scan rather than an import tracer, so it fails on a forbidden import
even when that import sits behind a flag and the suite never executes it.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2] / "src" / "aus_cart_mcp"

FORBIDDEN = {
    "core": ("aus_cart_mcp.watch", "aus_cart_mcp.ui"),
    "watch": ("aus_cart_mcp.ui",),
}


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def test_layers_do_not_import_upwards():
    for layer, banned in FORBIDDEN.items():
        for path in (ROOT / layer).rglob("*.py"):
            used = imports_of(path)
            assert not any(name == ban or name.startswith(ban + ".") for name in used for ban in banned), (
                f"{path} imports a layer it must not"
            )
