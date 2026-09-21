"""Language toolchain registry — the one place that knows about languages.

Everything language-specific in TinyLocalCoder is a table entry here: which
extensions belong to a language, which shell commands are legitimate verify
steps, how to build and smoke-run what the coding agent created, how to read
that language's compiler diagnostics, and which apt packages install it.

Adding a language is one `Toolchain(...)` entry. No other module names a
language.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable


@dataclass(frozen=True)
class Diagnostic:
    """One `file:line — message` pulled out of a build failure."""

    file: str
    line: int | None
    message: str
    tool: str = ""

    def format(self) -> str:
        where = f"{self.file}:{self.line}" if self.line else self.file
        return f"{where} — {self.message}"


@dataclass(frozen=True)
class Toolchain:
    name: str
    extensions: tuple[str, ...] = ()
    filenames: tuple[str, ...] = ()
    run_prefixes: tuple[str, ...] = ()
    compile_prefixes: tuple[str, ...] = ()
    probe: str = ""
    apt_packages: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    plan_example: str = ""
    fix_hint: str = ""
    compile_cmd: Callable[[list[str]], str | None] | None = None
    smoke_cmd: Callable[[list[str]], str | None] | None = None
    diagnostics: tuple[re.Pattern[str], ...] = ()

    def owns_path(self, path: str) -> bool:
        p = (path or "").strip().replace("\\", "/")
        if not p:
            return False
        name = Path(p).name
        if name.lower() in {f.lower() for f in self.filenames}:
            return True
        return bool(self.extensions) and name.lower().endswith(self.extensions)

    def missing_probes(self) -> list[str]:
        """Probe binaries that are not on PATH (empty when fully installed)."""
        if not self.probe:
            return []
        return [] if shutil.which(self.probe) else [self.probe]


# --------------------------------------------------------------------------
# Diagnostic patterns. Each must expose a `file` group; `line` and `msg` are
# optional. They are matched against the combined stdout/stderr of a failure.
# --------------------------------------------------------------------------

_GCC_ERROR = re.compile(
    r"^\s*(?P<file>[\w./+-]+\.(?:c|cc|cpp|cxx|h|hh|hpp|hxx))"
    r":(?P<line>\d+)(?::\d+)?:\s*(?:fatal\s+)?error:\s*(?P<msg>.+)$",
    re.MULTILINE,
)
_MAKE_ERROR = re.compile(
    r"^\s*(?P<file>[\w./-]*[Mm]akefile)"
    r":(?P<line>\d+):\s*\*{3}\s*(?P<msg>.+?)\.?\s*(?:Stop\.)?$",
    re.MULTILINE,
)
_LD_UNDEFINED = re.compile(
    r"^\s*(?P<file>[\w./+-]+\.(?:c|cc|cpp|cxx|o))(?::\([^)]*\))?"
    r"(?::\d+)?:\s*(?P<msg>undefined reference to\s+.+)$",
    re.MULTILINE,
)
_PY_TRACEBACK = re.compile(
    r'^\s*File "(?P<file>[^"]+\.py)", line (?P<line>\d+)', re.MULTILINE
)
_RUSTC_ERROR = re.compile(
    r"^\s*-->\s+(?P<file>[\w./-]+\.rs):(?P<line>\d+):\d+", re.MULTILINE
)
_GO_ERROR = re.compile(
    r"^\s*(?P<file>[\w./-]+\.go):(?P<line>\d+)(?::\d+)?:\s*(?P<msg>.+)$",
    re.MULTILINE,
)
_RUBY_ERROR = re.compile(
    r"^\s*(?P<file>[\w./-]+\.rb):(?P<line>\d+):\s*(?P<msg>.+)$", re.MULTILINE
)
_NODE_ERROR = re.compile(
    r"^\s*(?P<file>[\w./-]+\.(?:js|mjs|cjs|ts)):(?P<line>\d+)\s*$", re.MULTILINE
)
_JAVA_ERROR = re.compile(
    r"^\s*(?P<file>[\w./-]+\.java):(?P<line>\d+):\s*error:\s*(?P<msg>.+)$",
    re.MULTILINE,
)


# --------------------------------------------------------------------------
# Compile / smoke command builders. Each takes the list of created paths that
# belong to the toolchain and returns a single short shell command, or None
# when nothing sensible can be synthesized.
# --------------------------------------------------------------------------

_MAKEFILE_NAMES = {"makefile", "gnumakefile"}


def _has_makefile(paths: Iterable[str]) -> bool:
    return any(Path(p).name.lower() in _MAKEFILE_NAMES for p in paths)


def _sources(paths: Iterable[str], suffixes: tuple[str, ...]) -> list[str]:
    out = [p for p in paths if p.lower().endswith(suffixes)]
    return list(dict.fromkeys(out))


def _binary_name(sources: list[str]) -> str:
    """Pick the output binary name from the first non-header source."""
    for s in sources:
        stem = Path(s).stem
        if stem:
            return stem
    return "a.out"


def _cfamily_compile(compiler: str, std: str, suffixes: tuple[str, ...]):
    def build(paths: list[str]) -> str | None:
        if _has_makefile(paths):
            return "make"
        srcs = _sources(paths, suffixes)
        if not srcs:
            return None
        return f"{compiler} -Wall {std} -o {_binary_name(srcs)} {' '.join(srcs)}"

    return build


def _cfamily_smoke(suffixes: tuple[str, ...]):
    def build(paths: list[str]) -> str | None:
        srcs = _sources(paths, suffixes)
        if not srcs:
            return None
        return f"./{_binary_name(srcs)}"

    return build


def _python_compile(paths: list[str]) -> str | None:
    srcs = [p for p in paths if p.endswith(".py") and not p.endswith("__init__.py")]
    if not srcs:
        return None
    return "python3 -m py_compile " + " ".join(dict.fromkeys(srcs))


def _python_smoke(paths: list[str]) -> str | None:
    srcs = [p for p in paths if p.endswith(".py") and not p.endswith("__init__.py")]
    if not srcs:
        return None
    first = srcs[0]
    parent = str(Path(first).parent).replace("\\", "/")
    stem = Path(first).stem
    if parent in {".", ""}:
        return f'python3 -c "import {stem}"'
    return f'PYTHONPATH={parent} python3 -c "import {stem}"'


def _rust_compile(paths: list[str]) -> str | None:
    if any(Path(p).name == "Cargo.toml" for p in paths):
        return "cargo build"
    srcs = _sources(paths, (".rs",))
    if not srcs:
        return None
    return f"rustc -o {_binary_name(srcs)} {srcs[0]}"


def _rust_smoke(paths: list[str]) -> str | None:
    if any(Path(p).name == "Cargo.toml" for p in paths):
        return "cargo run"
    srcs = _sources(paths, (".rs",))
    return f"./{_binary_name(srcs)}" if srcs else None


def _go_compile(paths: list[str]) -> str | None:
    srcs = _sources(paths, (".go",))
    if not srcs:
        return None
    return f"go build -o {_binary_name(srcs)} {' '.join(srcs)}"


def _go_smoke(paths: list[str]) -> str | None:
    srcs = _sources(paths, (".go",))
    return f"./{_binary_name(srcs)}" if srcs else None


def _ruby_compile(paths: list[str]) -> str | None:
    srcs = _sources(paths, (".rb",))
    return f"ruby -c {srcs[0]}" if srcs else None


def _ruby_smoke(paths: list[str]) -> str | None:
    srcs = _sources(paths, (".rb",))
    return f"ruby {srcs[0]}" if srcs else None


def _node_compile(paths: list[str]) -> str | None:
    srcs = _sources(paths, (".js", ".mjs", ".cjs"))
    return f"node --check {srcs[0]}" if srcs else None


def _node_smoke(paths: list[str]) -> str | None:
    srcs = _sources(paths, (".js", ".mjs", ".cjs"))
    return f"node {srcs[0]}" if srcs else None


def _java_compile(paths: list[str]) -> str | None:
    srcs = _sources(paths, (".java",))
    return f"javac {' '.join(srcs)}" if srcs else None


def _java_smoke(paths: list[str]) -> str | None:
    srcs = _sources(paths, (".java",))
    return f"java {Path(srcs[0]).stem}" if srcs else None


_CPP_SUFFIXES = (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx")
_C_SUFFIXES = (".c", ".h")

TOOLCHAINS: dict[str, Toolchain] = {}


def _register(tc: Toolchain) -> Toolchain:
    TOOLCHAINS[tc.name] = tc
    return tc


_register(
    Toolchain(
        name="python",
        extensions=(".py",),
        filenames=("requirements.txt", "pyproject.toml"),
        run_prefixes=("python3", "python", "pytest"),
        compile_prefixes=(),
        probe="python3",
        apt_packages=("python3",),
        keywords=("python", "fastapi", "pytest", "uvicorn", "django", "flask"),
        plan_example=(
            "1. [ ] create `app.py` — what this file must do\n"
            "2. [ ] run `python3 -m py_compile app.py` — expect success\n"
            '3. [ ] run `python3 -c "import app"` — expect success'
        ),
        fix_hint="Python failure: fix the module, imports, or a missing __init__.py.",
        compile_cmd=_python_compile,
        smoke_cmd=_python_smoke,
        diagnostics=(_PY_TRACEBACK,),
    )
)

_register(
    Toolchain(
        name="cpp",
        extensions=_CPP_SUFFIXES,
        filenames=("Makefile", "GNUmakefile", "CMakeLists.txt"),
        run_prefixes=("g++", "clang++", "make", "cmake"),
        compile_prefixes=("g++", "clang++", "make", "cmake"),
        probe="g++",
        apt_packages=("g++", "make"),
        keywords=("c++", "cpp", "g++", "makefile", "cmake"),
        plan_example=(
            "1. [ ] create `square_root.cpp` — what this file must do\n"
            "2. [ ] run `g++ -Wall -std=c++17 -o square_root square_root.cpp` "
            "— expect success\n"
            "3. [ ] run `./square_root 9` — expect 3"
        ),
        fix_hint=(
            "C++/make failure: fix the source or the Makefile. "
            "Makefile recipe lines MUST start with a TAB, and every recipe "
            "needs a target line above it."
        ),
        compile_cmd=_cfamily_compile("g++", "-std=c++17", _CPP_SUFFIXES),
        smoke_cmd=_cfamily_smoke(_CPP_SUFFIXES),
        diagnostics=(_GCC_ERROR, _MAKE_ERROR, _LD_UNDEFINED),
    )
)

_register(
    Toolchain(
        name="c",
        extensions=_C_SUFFIXES,
        filenames=(),
        run_prefixes=("gcc", "cc", "clang", "make"),
        compile_prefixes=("gcc", "cc", "clang", "make"),
        probe="gcc",
        apt_packages=("gcc", "make"),
        keywords=(" c ", "gcc", "ansi c", "c99", "c11"),
        plan_example=(
            "1. [ ] create `hello.c` — what this file must do\n"
            "2. [ ] run `gcc -Wall -std=c11 -o hello hello.c` — expect success\n"
            "3. [ ] run `./hello` — expect hello"
        ),
        fix_hint=(
            "C/make failure: fix the source or the Makefile. "
            "Makefile recipe lines MUST start with a TAB."
        ),
        compile_cmd=_cfamily_compile("gcc", "-std=c11", _C_SUFFIXES),
        smoke_cmd=_cfamily_smoke(_C_SUFFIXES),
        diagnostics=(_GCC_ERROR, _MAKE_ERROR, _LD_UNDEFINED),
    )
)

_register(
    Toolchain(
        name="rust",
        extensions=(".rs",),
        filenames=("Cargo.toml",),
        run_prefixes=("cargo", "rustc"),
        compile_prefixes=("cargo", "rustc"),
        probe="rustc",
        apt_packages=("rustc", "cargo"),
        keywords=("rust", "cargo", "rustc"),
        plan_example=(
            "1. [ ] create `main.rs` — what this file must do\n"
            "2. [ ] run `rustc -o main main.rs` — expect success\n"
            "3. [ ] run `./main` — expect success"
        ),
        fix_hint="Rust failure: fix the source; rustc points at the exact span.",
        compile_cmd=_rust_compile,
        smoke_cmd=_rust_smoke,
        diagnostics=(_RUSTC_ERROR,),
    )
)

_register(
    Toolchain(
        name="go",
        extensions=(".go",),
        filenames=("go.mod",),
        run_prefixes=("go",),
        compile_prefixes=("go",),
        probe="go",
        apt_packages=("golang-go",),
        keywords=("golang", " go ", "go build"),
        plan_example=(
            "1. [ ] create `main.go` — what this file must do\n"
            "2. [ ] run `go build -o main main.go` — expect success\n"
            "3. [ ] run `./main` — expect success"
        ),
        fix_hint="Go failure: fix the source; the compiler names file:line.",
        compile_cmd=_go_compile,
        smoke_cmd=_go_smoke,
        diagnostics=(_GO_ERROR,),
    )
)

_register(
    Toolchain(
        name="ruby",
        extensions=(".rb",),
        filenames=("Gemfile",),
        run_prefixes=("ruby", "rake", "bundle"),
        compile_prefixes=(),
        probe="ruby",
        apt_packages=("ruby-full",),
        keywords=("ruby", "rake", "gemfile"),
        plan_example=(
            "1. [ ] create `main.rb` — what this file must do\n"
            "2. [ ] run `ruby -c main.rb` — expect success\n"
            "3. [ ] run `ruby main.rb` — expect success"
        ),
        fix_hint="Ruby failure: fix the script; the interpreter names file:line.",
        compile_cmd=_ruby_compile,
        smoke_cmd=_ruby_smoke,
        diagnostics=(_RUBY_ERROR,),
    )
)

_register(
    Toolchain(
        name="node",
        extensions=(".js", ".mjs", ".cjs"),
        filenames=("package.json",),
        run_prefixes=("node", "npm", "npx"),
        compile_prefixes=(),
        probe="node",
        apt_packages=("nodejs", "npm"),
        keywords=("javascript", "node.js", "nodejs", "npm"),
        plan_example=(
            "1. [ ] create `main.js` — what this file must do\n"
            "2. [ ] run `node --check main.js` — expect success\n"
            "3. [ ] run `node main.js` — expect success"
        ),
        fix_hint="Node failure: fix the script; the stack names file:line.",
        compile_cmd=_node_compile,
        smoke_cmd=_node_smoke,
        diagnostics=(_NODE_ERROR,),
    )
)

_register(
    Toolchain(
        name="java",
        extensions=(".java",),
        filenames=(),
        run_prefixes=("javac", "java"),
        compile_prefixes=("javac",),
        probe="javac",
        apt_packages=("default-jdk",),
        keywords=("java", "javac", "jvm"),
        plan_example=(
            "1. [ ] create `Main.java` — what this file must do\n"
            "2. [ ] run `javac Main.java` — expect success\n"
            "3. [ ] run `java Main` — expect success"
        ),
        fix_hint="Java failure: fix the source; javac names file:line.",
        compile_cmd=_java_compile,
        smoke_cmd=_java_smoke,
        diagnostics=(_JAVA_ERROR,),
    )
)

DEFAULT_TOOLCHAIN = TOOLCHAINS["python"]


# --------------------------------------------------------------------------
# Lookup / detection API — the surface every other module uses.
# --------------------------------------------------------------------------


# Commands that install a toolchain into the running container. These are
# legal run todos so a plan can provision what it needs before compiling.
PROVISION_PREFIXES: tuple[str, ...] = ("apt-get", "apt")


def _first_token(command: str) -> str:
    """First meaningful token, skipping `VAR=value` env prefixes."""
    c = (command or "").strip()
    if not c:
        return ""
    for tok in c.split():
        if "=" in tok and not tok.startswith("-") and tok.split("=", 1)[0].isidentifier():
            continue
        return tok
    return ""


def toolchain_for_path(path: str) -> Toolchain | None:
    """Toolchain owning a file, preferring an extension match over a filename."""
    by_filename: Toolchain | None = None
    p = (path or "").strip().replace("\\", "/")
    if not p:
        return None
    name = Path(p).name
    for tc in TOOLCHAINS.values():
        if tc.extensions and name.lower().endswith(tc.extensions):
            return tc
        if by_filename is None and name.lower() in {f.lower() for f in tc.filenames}:
            by_filename = tc
    return by_filename


def toolchain_for_command(command: str) -> Toolchain | None:
    """Toolchain owning a shell command, by its first meaningful token."""
    first = _first_token(command)
    if not first:
        return None
    base = Path(first).name
    for tc in TOOLCHAINS.values():
        if base in tc.run_prefixes:
            return tc
    return None


def all_run_prefixes() -> frozenset[str]:
    """Every first token that may legitimately start a run todo."""
    out: set[str] = set()
    for tc in TOOLCHAINS.values():
        out.update(tc.run_prefixes)
    out.update({"bash", "sh"})
    out.update(PROVISION_PREFIXES)
    return frozenset(out)


def all_source_extensions() -> tuple[str, ...]:
    """Every registered source extension, longest first (regex-safe order)."""
    out: set[str] = set()
    for tc in TOOLCHAINS.values():
        out.update(tc.extensions)
    return tuple(sorted(out, key=len, reverse=True))


def all_source_filenames() -> tuple[str, ...]:
    out: set[str] = set()
    for tc in TOOLCHAINS.values():
        out.update(tc.filenames)
    return tuple(sorted(out))


def is_compile_command(command: str) -> bool:
    """True for build steps (as opposed to behavioral smoke runs)."""
    c = (command or "").strip()
    if not c:
        return False
    if "py_compile" in c:
        return True
    first = Path(_first_token(c)).name
    for tc in TOOLCHAINS.values():
        if first in tc.compile_prefixes:
            return True
    # `node --check foo.js`, `ruby -c foo.rb` are syntax checks, not behavior
    return bool(re.search(r"\s(--check|-c)\s", c)) and first in {"node", "ruby"}


def is_provision_command(command: str) -> bool:
    """True for a toolchain-install step (`apt-get install …`)."""
    first = Path(_first_token(command)).name
    return first in PROVISION_PREFIXES


def detect_toolchains(
    created_paths: Iterable[str] = (), run_targets: Iterable[str] = ()
) -> list[Toolchain]:
    """Toolchains a plan needs, ordered by how strongly they are implied.

    A source-extension match outranks a bare build-file (`Makefile`) match, so
    a C project with a Makefile resolves to `c`, not to whichever toolchain
    happens to also claim `Makefile`.
    """
    strong: list[Toolchain] = []
    weak: list[Toolchain] = []
    for path in created_paths:
        name = Path((path or "").replace("\\", "/")).name.lower()
        for tc in TOOLCHAINS.values():
            if tc.extensions and name.endswith(tc.extensions):
                if tc not in strong:
                    strong.append(tc)
            elif name in {f.lower() for f in tc.filenames}:
                if tc not in weak:
                    weak.append(tc)
    for cmd in run_targets:
        tc = toolchain_for_command(cmd)
        if tc is not None and tc not in strong and tc not in weak:
            weak.append(tc)
    # A weak (build-file / command) match is redundant when a strong match
    # already shares that build tool — `Makefile` next to `hello.c` means C,
    # not C and C++.
    strong_prefixes = {p for tc in strong for p in tc.run_prefixes}
    kept_weak = [
        tc
        for tc in weak
        if tc not in strong and not (set(tc.run_prefixes) & strong_prefixes)
    ]
    return strong + kept_weak


def toolchain_from_prompt(prompt: str) -> Toolchain | None:
    """Best-effort language guess from free text, for prompt examples only."""
    blob = f" {(prompt or '').lower()} "
    best: tuple[int, Toolchain] | None = None
    for tc in TOOLCHAINS.values():
        for kw in tc.keywords:
            idx = blob.find(kw)
            if idx >= 0 and (best is None or len(kw) > best[0]):
                best = (len(kw), tc)
    if best:
        return best[1]
    for tc in TOOLCHAINS.values():
        for ext in tc.extensions:
            if ext in blob:
                return tc
    return None


def parse_diagnostics(error_text: str, limit: int = 6) -> list[Diagnostic]:
    """Pull `file:line — message` entries out of any registered toolchain's output."""
    text = error_text or ""
    out: list[Diagnostic] = []
    seen: set[tuple[str, int | None, str]] = set()
    for tc in TOOLCHAINS.values():
        for pattern in tc.diagnostics:
            for m in pattern.finditer(text):
                groups = m.groupdict()
                raw = (groups.get("file") or "").strip()
                if not raw:
                    continue
                if "/workspace/" in raw.replace("\\", "/"):
                    raw = raw.replace("\\", "/").split("/workspace/", 1)[1]
                line_s = groups.get("line")
                try:
                    line = int(line_s) if line_s else None
                except (TypeError, ValueError):
                    line = None
                msg = (groups.get("msg") or "").strip()
                key = (raw, line, msg)
                if key in seen:
                    continue
                seen.add(key)
                out.append(Diagnostic(file=raw, line=line, message=msg, tool=tc.name))
                if len(out) >= limit:
                    return out
    return out


_MISSING_CMD_RE = re.compile(
    r"(?:^|[\s/])(?:([\w.+-]+):\s*(?:command\s+)?not found"
    r"|command not found:\s*([\w.+-]+)"
    r"|([\w.+-]+):\s*No such file or directory)",
    re.IGNORECASE | re.MULTILINE,
)


def missing_binaries(error_text: str, command: str = "") -> list[str]:
    """Binaries a shell/exec error says are absent (e.g. `make: not found`)."""
    names: list[str] = []
    for m in _MISSING_CMD_RE.finditer(error_text or ""):
        for g in m.groups():
            if not g:
                continue
            base = Path(g.strip()).name
            if base and base not in names:
                names.append(base)
    if not names and command:
        first = _first_token(command)
        if first and not first.startswith("./") and "not found" in (error_text or "").lower():
            names.append(Path(first).name)
    return names


def toolchain_for_binary(binary: str) -> Toolchain | None:
    """Which toolchain would install this binary."""
    base = Path((binary or "").strip()).name
    if not base:
        return None
    for tc in TOOLCHAINS.values():
        if base == tc.probe or base in tc.run_prefixes or base in tc.apt_packages:
            return tc
    return None


def install_command(toolchains: Iterable[Toolchain]) -> str:
    """One apt-get line installing every package the given toolchains need."""
    packages: list[str] = []
    for tc in toolchains:
        for pkg in tc.apt_packages:
            if pkg not in packages:
                packages.append(pkg)
    if not packages:
        return ""
    return (
        "apt-get update && apt-get install -y --no-install-recommends "
        + " ".join(packages)
    )
