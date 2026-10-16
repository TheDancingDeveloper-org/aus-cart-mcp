"""Layer rules: core never imports watch or ui; watch never imports ui.

A source scan rather than an import tracer, so it fails on a forbidden import
even when that import sits behind a flag and the suite never executes it.

Core code still lives at the package root (server.py, gateway.py, store.py).
Those modules are the core layer until they move under ``core/``.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2] / "src" / "aus_cart_mcp"

# Root modules that are the core layer, not a layer package. Globbed, so a new
# module at the package root is scanned without being added to a list.

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
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.level == 0:
                found.add(node.module)
            else:
                # Relative import. Resolve against the file's package.
                package = _package(path)
                parts = package.split(".")
                # level 1 is the current package; higher climbs.
                if node.level > len(parts):
                    found.add(node.module)
                    continue
                base = parts[: len(parts) - node.level + 1]
                found.add(".".join([*base, node.module]))
    return found


def _package(path: Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    parts = ["aus_cart_mcp", *rel.parts]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts[:-1])  # the package the file lives in


def _core_files() -> list[Path]:
    files = sorted(path for path in ROOT.glob("*.py"))
    files += sorted((ROOT / "core").rglob("*.py"))
    files += sorted((ROOT / "retailers").rglob("*.py"))
    files += sorted((ROOT / "mock").rglob("*.py"))
    return [path for path in files if path.is_file()]


def test_layers_do_not_import_upwards():
    layers = {"core": _core_files(), "watch": sorted((ROOT / "watch").rglob("*.py"))}
    for layer, paths in layers.items():
        banned = FORBIDDEN[layer]
        assert paths, f"{layer} has no source to check"
        for path in paths:
            used = imports_of(path)
            hit = [name for name in used if any(name == ban or name.startswith(ban + ".") for ban in banned)]
            # The only allowed upward import is the flag-gated watch.register.
            # A function-level import of ui, or of any other watch module, still fails.
            allowed = {"aus_cart_mcp.watch.register"} if path.name == "server.py" and layer == "core" else set()
            unexpected = [name for name in hit if name not in allowed]
            assert not unexpected, f"{path} imports a layer it must not: {unexpected}"
            if allowed & set(hit):
                tree = ast.parse(path.read_text())
                module_level = {
                    node.module
                    for node in tree.body
                    if isinstance(node, ast.ImportFrom) and node.module and node.level == 0
                }
                assert "aus_cart_mcp.watch.register" not in module_level


def test_root_core_modules_are_scanned():
    """The scan is not vacuous: every root module is in it, including features.py."""
    scanned = {path.name for path in _core_files()}
    for name in ("server.py", "gateway.py", "store.py", "features.py"):
        assert name in scanned
