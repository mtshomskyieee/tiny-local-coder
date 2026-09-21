"""The toolchain registry is the single place that knows about languages.

Everything here is pure table lookup and regex — no workspace, no LLM. These
tests pin the contract the rest of the system depends on: which commands are
legal verify steps, which file belongs to which language, how a build failure
is turned into `file:line — message`, and what installs a missing toolchain.
"""

from __future__ import annotations

import pytest

from tinylocalcoder.toolchains import (
    TOOLCHAINS,
    all_run_prefixes,
    detect_toolchains,
    install_command,
    is_compile_command,
    is_provision_command,
    missing_binaries,
    parse_diagnostics,
    toolchain_for_binary,
    toolchain_for_command,
    toolchain_for_path,
    toolchain_from_prompt,
)


@pytest.mark.parametrize(
    "path,expected",
    [
        ("src/square_root.cpp", "cpp"),
        ("square_root.hpp", "cpp"),
        ("hello.c", "c"),
        ("hello.h", "c"),
        ("app.py", "python"),
        ("main.rs", "rust"),
        ("main.go", "go"),
        ("Makefile", "cpp"),
        ("src/GNUmakefile", "cpp"),
        ("", None),
        ("README", None),
    ],
)
def test_toolchain_for_path(path: str, expected: str | None) -> None:
    tc = toolchain_for_path(path)
    assert (tc.name if tc else None) == expected


@pytest.mark.parametrize(
    "command,expected",
    [
        ("make", "cpp"),
        ("g++ -Wall -o app main.cpp", "cpp"),
        ("gcc -o hello hello.c", "c"),
        ("CC=gcc make", "cpp"),
        ("python3 -m py_compile a.py", "python"),
        ("cargo build", "rust"),
        ("Define the struct", None),
    ],
)
def test_toolchain_for_command(command: str, expected: str | None) -> None:
    tc = toolchain_for_command(command)
    assert (tc.name if tc else None) == expected


def test_run_prefixes_cover_build_tools_and_provisioning() -> None:
    prefixes = all_run_prefixes()
    for tool in ("make", "g++", "gcc", "cargo", "go", "node", "apt-get"):
        assert tool in prefixes
    # The Python originals must survive
    for tool in ("python3", "pytest", "bash", "sh"):
        assert tool in prefixes


def test_detect_prefers_source_language_over_shared_build_file() -> None:
    """`Makefile` alongside hello.c means C, not C and C++."""
    assert [t.name for t in detect_toolchains(["hello.c", "Makefile"])] == ["c"]
    assert [t.name for t in detect_toolchains(["a.cpp", "Makefile"])] == ["cpp"]
    # A bare Makefile with no sources still resolves to something buildable
    assert [t.name for t in detect_toolchains(["Makefile"])] == ["cpp"]


def test_compile_vs_behavior_vs_provision_classification() -> None:
    assert is_compile_command("make")
    assert is_compile_command("g++ -o app main.cpp")
    assert is_compile_command("python3 -m py_compile a.py")
    assert not is_compile_command("./square_root 9")
    assert not is_compile_command("python3 -c \"import app\"")
    assert is_provision_command("apt-get install -y g++")
    assert not is_provision_command("make")


def test_cpp_compile_prefers_make_when_a_makefile_was_created() -> None:
    cpp = TOOLCHAINS["cpp"]
    assert cpp.compile_cmd(["src/square_root.cpp", "Makefile"]) == "make"
    assert cpp.compile_cmd(["src/square_root.cpp"]) == (
        "g++ -Wall -std=c++17 -o square_root src/square_root.cpp"
    )
    assert cpp.smoke_cmd(["src/square_root.cpp"]) == "./square_root"


def test_install_command_dedupes_packages() -> None:
    cmd = install_command([TOOLCHAINS["cpp"], TOOLCHAINS["c"]])
    assert cmd.startswith("apt-get update && apt-get install -y")
    assert cmd.count("make") == 1
    assert "g++" in cmd and "gcc" in cmd
    assert install_command([]) == ""


class TestDiagnostics:
    """A build failure must yield the offending file so the fix agent can see it."""

    def test_gcc_error(self) -> None:
        text = (
            "src/square_root.cpp:12:5: error: 'sqrt' was not declared in this scope\n"
            "make: *** [Makefile:7: square_root] Error 1\n"
        )
        (d,) = parse_diagnostics(text)
        assert (d.file, d.line, d.tool) == ("src/square_root.cpp", 12, "cpp")
        assert "not declared" in d.message

    def test_make_missing_separator(self) -> None:
        (d,) = parse_diagnostics("Makefile:4: *** missing separator.  Stop.\n")
        assert (d.file, d.line) == ("Makefile", 4)
        assert d.message == "missing separator"

    def test_linker_error_names_the_source_not_the_linker(self) -> None:
        text = (
            "/usr/bin/ld: /tmp/ccXYZ.o: in function `main':\n"
            "square_root.cpp:(.text+0x1f): undefined reference to `helper()'\n"
        )
        (d,) = parse_diagnostics(text)
        assert d.file == "square_root.cpp"

    def test_python_traceback_still_parsed(self) -> None:
        text = 'Traceback:\n  File "src/api.py", line 3, in <module>\n'
        (d,) = parse_diagnostics(text)
        assert (d.file, d.line, d.tool) == ("src/api.py", 3, "python")

    def test_container_absolute_paths_are_made_workspace_relative(self) -> None:
        (d,) = parse_diagnostics("/workspace/src/a.cpp:1:1: error: boom\n")
        assert d.file == "src/a.cpp"

    def test_clean_output_yields_nothing(self) -> None:
        assert parse_diagnostics("g++ -o app main.cpp\n") == []


class TestMissingBinaries:
    """Exit 127 is an environment problem, and must be told apart from a bug."""

    @pytest.fixture
    def no_make(self, monkeypatch: pytest.MonkeyPatch):
        """The app container ships without a compiler; this host has one."""
        import tinylocalcoder.toolchains as mod

        real = mod.shutil.which
        monkeypatch.setattr(
            mod.shutil, "which", lambda n: None if n in {"make", "g++"} else real(n)
        )

    def test_shell_not_found(self, no_make) -> None:
        assert missing_binaries("/bin/sh: 1: make: not found\n") == ["make"]
        assert toolchain_for_binary("make").name == "cpp"

    def test_locally_built_binary_is_not_a_toolchain(self) -> None:
        """`./square_root: not found` means the build failed, not that apt is needed."""
        assert toolchain_for_binary("square_root") is None

    def test_command_fallback_ignores_relative_binaries(self, no_make) -> None:
        assert missing_binaries("not found", command="./square_root") == []
        assert missing_binaries("not found", command="make") == ["make"]

    def test_a_tool_that_is_installed_is_never_reported_missing(self) -> None:
        """pytest prints "not found" about its own arguments, not its interpreter.

        Without the PATH check this sent an ordinary test failure to apt-get.
        """
        assert (
            missing_binaries(
                "ERROR: file or directory not found: tests\n",
                command="python3 -m pytest",
            )
            == []
        )


def test_run_subcommands_are_behavioral_not_build_steps() -> None:
    """`go run` builds *and* runs, so it is the smoke step, not the compile step."""
    assert is_compile_command("go build -o main main.go")
    assert not is_compile_command("go run main.go")
    assert is_compile_command("cargo build")
    assert not is_compile_command("cargo run")
    assert not is_compile_command("cargo test")


def test_toolchain_from_prompt() -> None:
    assert toolchain_from_prompt("write a C++ program with a Makefile").name == "cpp"
    assert toolchain_from_prompt("build a FastAPI endpoint").name == "python"
    assert toolchain_from_prompt("a rust cli").name == "rust"
    assert toolchain_from_prompt("do something vague") is None
