"""AST gate (audit F7): every spawn call site in the shipped package goes
through the winlaunch primitive — or appears in ALLOW with a justification.

Scans galvanize/ and plugins/ (tests/ may spawn directly). Rationale for
each entry is part of the entry: the gate fails if an allowlisted site
disappears (stale) OR if a site lacks a non-empty justification.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = [ROOT / "galvanize", ROOT / "plugins"]

SPAWN_ATTRS = {"run", "Popen", "call", "check_output", "check_call"}
PRIMITIVE = "galvanize/winlaunch.py"

# key: (relative path, enclosing function) -> justification (must be non-empty)
ALLOW = {
    (PRIMITIVE, "spawn_detached"):
        "the primitive itself: handoff Popen with the documented "
        "CREATE_NO_WINDOW|CREATE_NEW_PROCESS_GROUP|CREATE_BREAKAWAY_FROM_JOB "
        "flavor; DETACHED_PROCESS is reserved (see module docstring: under the "
        "Win11 WT default terminal DETACHED let the venv stub's console-subsystem "
        "re-exec pop a VISIBLE Windows Terminal window, while CREATE_NO_WINDOW "
        "gives the child a hidden console that all descendants inherit)",
    (PRIMITIVE, "_popen"):
        "the primitive itself: inner retry helper of spawn_detached "
        "(drop-BREAKAWAY fallback; escaped_job reported honestly)",
    (PRIMITIVE, "_posix_run"):
        "the primitive itself: POSIX twin (start_new_session + killpg tree kill)",
    (PRIMITIVE, "_win_spawn_monitored"):
        "the primitive itself (C2): supervisor worker spawn — suspended -> "
        "job (KILL_ON_JOB_CLOSE) -> resume, stdio to NUL, handles kept for "
        "poll/job-pid-enumerate/tree-kill; CREATE_NO_WINDOW hidden-console "
        "flavor per module policy",
    ("plugins/hermes/__init__.py", "_bootstrap_run"):
        "CLI-fallback branch: runs exactly when `import galvanize` failed in "
        "this process, so the primitive package is unreachable here; sets "
        "CREATE_NO_WINDOW literally (hidden-console flavor, Win11 WT evidence "
        "see winlaunch docstring)",
}


def _enclosing_lookup(tree):
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def enc(node):
        while node:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node.name
            node = parents.get(node)
        return "<module>"
    return enc


def _sites():
    found = set()
    for d in SCAN_DIRS:
        for py in sorted(d.rglob("*.py")):
            if "__pycache__" in py.parts:
                continue
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
            enc = _enclosing_lookup(tree)
            rel = py.relative_to(ROOT).as_posix()
            for n in ast.walk(tree):
                if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Attribute):
                    continue
                base = n.func.value
                if not isinstance(base, ast.Name):
                    continue
                attr = n.func.attr
                if base.id == "subprocess" and attr in SPAWN_ATTRS:
                    found.add((rel, enc(n), f"subprocess.{attr}"))
                elif base.id == "os" and attr in ("system", "popen") or \
                        base.id == "_winapi" and attr == "CreateProcess":
                    found.add((rel, enc(n), f"{base.id}.{attr}"))
    return found


def test_every_spawn_site_goes_through_the_primitive():
    offenders = []
    stale = {(f, fn) for (f, fn) in ALLOW}
    for rel, fn, what in sorted(_sites()):
        stale.discard((rel, fn))
        if rel == PRIMITIVE:
            continue  # the primitive module owns raw spawning
        if (rel, fn) in ALLOW:
            continue
        offenders.append(f"{rel} in {fn}(): {what}")
    assert not offenders, "spawn sites bypassing winlaunch:\n" + "\n".join(offenders)
    assert not stale, f"stale allowlist entries (site gone): {sorted(stale)}"


def test_allowlist_entries_carry_justifications():
    assert ALLOW, "allowlist must exist"
    for key, why in ALLOW.items():
        assert isinstance(key, tuple) and len(key) == 2
        assert why.strip(), f"{key}: allowlist entry needs a justification"


def test_no_window_flag_is_literal_in_primitive_and_plugin_fallback():
    """CREATE_NO_WINDOW (0x08000000) must appear as a constant in both the
    primitive and the plugin bootstrap fallback — no silent flag regressions."""
    for rel in (PRIMITIVE, "plugins/hermes/__init__.py"):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"), filename=rel)
        ints = {n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, int)}
        assert 0x08000000 in ints, f"{rel} lost its CREATE_NO_WINDOW literal"
