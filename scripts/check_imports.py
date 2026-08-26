#!/usr/bin/env python3
"""
Enforce the two structural rules from GUIDELINE.md section 1:

  1. No circular imports anywhere in the cctv package.
  2. Dependencies point downward. A layer may only import from layers
     below it -- in particular, nothing outside ui/ may import from ui/.

Both are cheap to check and expensive to discover later, so CI runs
this on every push.

Exit codes:
    0  clean
    1  a cycle or a layering violation was found

Usage:
    python scripts/check_imports.py [--root .]
"""

import argparse
import ast
import os
import sys
from collections import defaultdict

# Lower number = lower layer. A module may import from its own layer or
# any layer with a strictly lower number.
#: Name of the application package, relative to the repository root.
#: If this is ever renamed again, change it here -- and note that the
#: script now fails loudly when it finds no modules, rather than
#: reporting "structure OK" for a package it never located.
PACKAGE = "core"

#: Lower number = lower layer. A module may import from its own layer or
#: any layer with a strictly lower number. Keys are relative to PACKAGE.
LAYERS = {
    "paths": 0,
    "diagnostics": 0,
    "worker": 0,
    "storage": 1,
    "capture": 2,
    "detection": 3,
    "recording": 4,
    "alerts": 4,
    "ui": 5,
}


def layer_of(module):
    """Longest matching prefix wins, so core.ui.settings resolves to the
    core.ui layer rather than falling through to the default."""
    best, best_len = None, -1
    for suffix, level in LAYERS.items():
        prefix = f"{PACKAGE}.{suffix}"
        if (module == prefix or module.startswith(prefix + ".")) and len(prefix) > best_len:
            best, best_len = level, len(prefix)
    return best


def module_name(path, root):
    rel = os.path.relpath(path, root)
    mod = rel[:-3].replace(os.sep, ".")
    return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod


def local_imports(path):
    """Every `cctv.*` module this file imports.

    Uses the AST rather than a regex so that imports mentioned inside
    strings, docstrings or comments are not counted -- we shipped a bug
    once where imports had been swallowed into a docstring and a
    regex-based check happily reported them as real.
    """
    with open(path, encoding="utf-8") as f:
        try:
            tree = ast.parse(f.read(), filename=path)
        except SyntaxError as exc:
            print(f"  SYNTAX ERROR {path}: {exc}")
            raise SystemExit(1) from exc

    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(PACKAGE):
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            # level > 0 would be a relative import; we don't use them.
            if node.module and node.module.startswith(PACKAGE):
                found.add(node.module)
    return found


def build_graph(root):
    graph = {}
    pkg_dir = os.path.join(root, PACKAGE)
    for dirpath, _dirnames, filenames in os.walk(pkg_dir):
        if "__pycache__" in dirpath:
            continue
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            graph[module_name(path, root)] = local_imports(path)
    return graph


def find_cycles(graph):
    """Iterative DFS with an explicit stack, so a deep graph can't blow
    the recursion limit."""
    WHITE, GREY, BLACK = 0, 1, 2
    colour = defaultdict(int)
    cycles = []

    for start in sorted(graph):
        if colour[start] != WHITE:
            continue
        stack = [(start, iter(sorted(graph.get(start, ()))))]
        path = [start]
        colour[start] = GREY
        while stack:
            _node, children = stack[-1]
            advanced = False
            for child in children:
                if child not in graph:
                    continue  # third-party or a module we don't own
                if colour[child] == GREY:
                    cycles.append(path[path.index(child):] + [child])
                elif colour[child] == WHITE:
                    colour[child] = GREY
                    path.append(child)
                    stack.append((child, iter(sorted(graph.get(child, ())))))
                    advanced = True
                    break
            if not advanced:
                colour[path[-1]] = BLACK
                path.pop()
                stack.pop()
    return cycles


def find_layer_violations(graph):
    bad = []
    for module, deps in sorted(graph.items()):
        src = layer_of(module)
        if src is None:
            continue
        for dep in sorted(deps):
            dst = layer_of(dep)
            if dst is None:
                continue
            if dst > src:
                bad.append((module, dep, src, dst))
    return bad


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    root = os.path.abspath(args.root)

    graph = build_graph(root)
    print(f"checked {len(graph)} modules under {PACKAGE}/")

    # A rename that this script wasn't updated for used to make it
    # report "structure OK" having inspected nothing -- a green CI
    # check that enforced absolutely nothing. Fail loudly instead.
    if not graph:
        print(
            f"\nFAIL: no modules found under {os.path.join(root, PACKAGE)}/\n"
            f"  Either --root is wrong, or the package was renamed and\n"
            f"  PACKAGE at the top of this script needs updating."
        )
        return 1

    failed = False

    cycles = find_cycles(graph)
    if cycles:
        failed = True
        print(f"\nFAIL: {len(cycles)} import cycle(s):")
        for cycle in cycles:
            print("  " + " -> ".join(cycle))
    else:
        print("  no import cycles")

    violations = find_layer_violations(graph)
    if violations:
        failed = True
        print(f"\nFAIL: {len(violations)} layering violation(s):")
        print("  (dependencies must point downward -- see GUIDELINE.md section 1)")
        for module, dep, src, dst in violations:
            print(f"  {module} (layer {src}) imports {dep} (layer {dst})")
    else:
        print("  no layering violations")

    if failed:
        return 1
    print("\nstructure OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
