"""One-shot audit v2: methods too, plus function-local import slop."""

import ast
from pathlib import Path

ROOT = Path(__file__).parent / "app"


def is_public(name: str) -> bool:
    return not name.startswith("_")


def problems(node) -> list[str]:
    out = []
    for a in (
        node.args.posonlyargs + node.args.args + node.args.kwonlyargs
    ):
        if a.arg in ("self", "cls"):
            continue
        if a.annotation is None:
            out.append(f"param:{a.arg}")
    if node.returns is None:
        out.append("return:MISSING")
    targets = [node.returns] if node.returns else []
    targets += [a.annotation for a in node.args.args if a.annotation]
    for t in targets:
        if isinstance(t, ast.Name) and t.id in ("dict", "list", "tuple", "Any"):
            out.append(f"vague:{t.id}")
    return out


for path in sorted(ROOT.rglob("*.py")):
    src = path.read_text()
    tree = ast.parse(src)
    rel = path.relative_to(ROOT)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname and not a.name.startswith("typing"):
                    print(f"{rel}:{node.lineno} ALIASED-IMPORT {a.name} as {a.asname}")
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if not node.name.startswith("__"):
                p = problems(node)
                scope = "method" if any(
                    isinstance(d, ast.arg) and d.arg == "self" for d in node.args.args
                ) else "func"
                if p:
                    print(f"{rel}:{node.lineno} {scope} {node.name}: {p}")
