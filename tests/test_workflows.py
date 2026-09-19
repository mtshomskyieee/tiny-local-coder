"""/review workflow must write manifest.txt then review.md even if the plan is vague."""

from __future__ import annotations

from pathlib import Path

from tinylocalcoder.agents.workflows import plan_requirement_gaps, run_workflow
from tinylocalcoder.config import Settings
from tinylocalcoder.memory.files import WorkspaceMemory
from tinylocalcoder.tools.review import REVIEW_NAME


class _FakePipeline:
    def __init__(self, memory: WorkspaceMemory, plan_text: str) -> None:
        self.memory = memory
        self.plan_text = plan_text
        self.modes: list[str] = []

    def _notify(self, _msg: str) -> None:
        return None

    def invoke(self, mode: str, prompt: str = "", **_extra: object) -> dict:
        self.modes.append(mode)
        if mode == "plan":
            self.memory.write_plan(self.plan_text)
            return {"output": "planned", "last_file": "plan.md"}
        return {"output": "executed"}


def _memory(tmp: Path) -> WorkspaceMemory:
    return WorkspaceMemory(Settings(workspace_dir=str(tmp)))


def test_review_writes_manifest_then_review_even_if_plan_omits_review_md(
    tmp_path: Path,
) -> None:
    mem = _memory(tmp_path)
    (tmp_path / "app.py").write_text("print('hi')\n", encoding="utf-8")
    plan = (
        "Goal: review the workspace\n"
        "1. [ ] create `manifest.txt` — inventory\n"
        "2. [ ] write comments on the code\n"
    )
    pipe = _FakePipeline(mem, plan)
    result = run_workflow(pipe, "review")  # type: ignore[arg-type]
    assert pipe.modes == ["plan", "execute"]
    assert mem.prototype_exists("manifest.txt")
    assert "app.py" in mem.read_prototype("manifest.txt")
    body = mem.read_prototype(REVIEW_NAME)
    assert body.strip()
    assert "### `app.py`" in body
    assert result["review_markdown"].strip()
    assert result["last_file"] == REVIEW_NAME
    assert "app.py" in result["manifest_text"]
    assert any("1/4 writing manifest.txt" in line for line in result["log_lines"])
    assert any("3/4 reviewing each manifest path" in line for line in result["log_lines"])
    assert any("review »   • app.py" in line for line in result["log_lines"])
    assert "Debug `print`" in result["review_markdown"]


def test_review_restores_review_md_if_execute_wipes_it(tmp_path: Path) -> None:
    mem = _memory(tmp_path)
    (tmp_path / "mod.py").write_text("x = 1\n", encoding="utf-8")

    class WipeReview(_FakePipeline):
        def invoke(self, mode: str, prompt: str = "", **_extra: object) -> dict:
            result = super().invoke(mode, prompt, **_extra)
            if mode == "execute":
                self.memory.write_prototype(REVIEW_NAME, "\n")
            return result

    pipe = WipeReview(
        mem,
        "Goal: review\n1. [ ] create `review.md` — notes\n",
    )
    result = run_workflow(pipe, "review")  # type: ignore[arg-type]
    assert "### `mod.py`" in result["review_markdown"]
    assert mem.read_prototype(REVIEW_NAME).strip()


def test_review_plan_prompt_includes_manifest_paths(tmp_path: Path) -> None:
    mem = _memory(tmp_path)
    (tmp_path / "svc.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    seen: list[str] = []

    class Capture(_FakePipeline):
        def invoke(self, mode: str, prompt: str = "", **_extra: object) -> dict:
            if mode == "plan":
                seen.append(prompt)
            return super().invoke(mode, prompt, **_extra)

    run_workflow(
        Capture(mem, "Goal: review\n1. [ ] create `review.md`\n"),  # type: ignore[arg-type]
        "review",
    )
    assert seen
    assert "svc.py" in seen[0]
    assert "manifest.txt" in seen[0]


def test_review_writes_review_md_when_plan_checks_todos_off_without_the_file(
    tmp_path: Path,
) -> None:
    """Regression from the TUI: plan.md had [x] create review.md but no file.

    That is what produced the yellow 'review.md is empty' line.
    """
    mem = _memory(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "db.py").write_text(
        "import json\n"
        "db = {}\n"
        "def add(item):\n"
        "    global db\n"
        "    with open('db.json') as f:\n"
        "        db = json.load(f)\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "tests" / "test_db.py").write_text(
        "def test_ok():\n    assert True\n",
        encoding="utf-8",
    )
    (tmp_path / "db.json").write_text("{}\n", encoding="utf-8")

    # Exact shape written by the small model in the failing session.
    done_plan = (
        "# Plan\n"
        "Goal: Inventory project files into manifest.txt, "
        "then write review comments into review.md.\n"
        "\n"
        "## Todos\n"
        "1. [x] create `manifest.txt` — list all Python files excluding "
        "archive/, .index/, plan.md, ask.md, exec.log, session.md\n"
        "2. [x] create `review.md` — add opinionated notes for each path "
        "listed in manifest.txt\n"
    )
    pipe = _FakePipeline(mem, done_plan)
    result = run_workflow(pipe, "review")  # type: ignore[arg-type]

    assert pipe.modes == ["plan", "execute"]
    assert mem.prototype_exists("manifest.txt")
    manifest = mem.read_prototype("manifest.txt")
    assert "src/db.py" in manifest
    assert "tests/test_db.py" in manifest

    body = mem.read_prototype(REVIEW_NAME).strip()
    assert body, "review.md must not be empty when source files exist"
    assert "# Code review" in body
    assert "### `src/db.py`" in body
    assert "### `tests/test_db.py`" in body
    assert result["review_markdown"].strip() == body
    assert "is still empty" not in str(result.get("output") or "")
    assert result["workflow"] == "review"
    assert result["last_file"] == REVIEW_NAME
    assert "src/db.py" in result["manifest_text"]
    assert "Mutable module `global`" in body
    assert "Smoke assertion is a no-op" in body
    steps = [line for line in result["log_lines"] if "review »" in line]
    assert any("1/4" in line for line in steps)
    assert any("2/4" in line for line in steps)
    assert any("3/4" in line for line in steps)
    assert any("4/4" in line for line in steps)


def test_review_fix_plans_from_review_md_then_executes(tmp_path: Path) -> None:
    mem = _memory(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "db.py").write_text("x = 1\n", encoding="utf-8")
    mem.write_prototype(
        REVIEW_NAME,
        "# Code review\n\n"
        "### `src/db.py`\n\n"
        "- **high** (L3): Bare `except:` swallows all errors.\n"
        "- **info**: No heuristic issues flagged — still skim for API/contract fit.\n",
    )
    seen: list[str] = []

    class Capture(_FakePipeline):
        def invoke(self, mode: str, prompt: str = "", **_extra: object) -> dict:
            if mode == "plan":
                seen.append(prompt)
            return super().invoke(mode, prompt, **_extra)

    plan = (
        "# Plan\n"
        "Goal: fix review findings\n"
        "\n"
        "## Todos\n"
        "1. [ ] refine `src/db.py` — catch a specific exception\n"
        "2. [ ] run `python3 -m py_compile src/db.py` — expect success\n"
    )
    result = run_workflow(Capture(mem, plan), "review-fix")  # type: ignore[arg-type]
    assert seen
    assert "src/db.py" in seen[0]
    assert "Bare `except:`" in seen[0]
    assert "No heuristic issues flagged" not in seen[0]
    assert result["workflow"] == "review-fix"
    assert result["issue_count"] == 1
    assert result["skipped_execute"] is False
    assert result["plan_text"]
    assert "refine `src/db.py`" in result["plan_text"]
    steps = result["log_lines"]
    assert any("1/3 loading review.md" in line for line in steps)
    assert any("2/3 planning fixes" in line for line in steps)
    assert any("3/3 executing fix plan" in line for line in steps)
    assert any("src/db.py" in line and "high" in line for line in steps)


def test_review_fix_skips_execute_when_review_is_clean(tmp_path: Path) -> None:
    mem = _memory(tmp_path)
    (tmp_path / "ok.py").write_text("x = 1\n", encoding="utf-8")
    mem.write_prototype(
        REVIEW_NAME,
        "# Code review\n\n"
        "### `ok.py`\n\n"
        "- **info**: No heuristic issues flagged — still skim for API/contract fit.\n",
    )
    pipe = _FakePipeline(mem, "Goal: leftover\n1. [ ] create `ok.py` — ignore\n")
    result = run_workflow(pipe, "review-fix")  # type: ignore[arg-type]
    assert pipe.modes == []
    assert result["skipped_execute"] is True
    assert result["issue_count"] == 0
    assert "nothing to fix" in " ".join(result["log_lines"])


def test_review_fix_writes_review_md_when_missing(tmp_path: Path) -> None:
    mem = _memory(tmp_path)
    (tmp_path / "app.py").write_text("print('hi')\n", encoding="utf-8")
    seen: list[str] = []

    class Capture(_FakePipeline):
        def invoke(self, mode: str, prompt: str = "", **_extra: object) -> dict:
            if mode == "plan":
                seen.append(prompt)
            return super().invoke(mode, prompt, **_extra)

    result = run_workflow(
        Capture(mem, "Goal: fix\n1. [ ] refine `app.py` — remove print\n"),  # type: ignore[arg-type]
        "review-fix",
    )
    assert mem.prototype_exists(REVIEW_NAME)
    assert "### `app.py`" in mem.read_prototype(REVIEW_NAME)
    assert seen
    assert "app.py" in seen[0]
    assert result["issue_count"] >= 1


def test_test_workflow_lists_files_and_plan_steps(tmp_path: Path) -> None:
    mem = _memory(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_db.py").write_text(
        "def test_ok():\n    assert True\n",
        encoding="utf-8",
    )
    seen: list[str] = []

    class Capture(_FakePipeline):
        def invoke(self, mode: str, prompt: str = "", **_extra: object) -> dict:
            if mode == "plan":
                seen.append(prompt)
            return super().invoke(mode, prompt, **_extra)

    plan = (
        "# Plan\n"
        "Goal: run existing tests\n"
        "\n"
        "## Todos\n"
        "1. [ ] run `python3 -m pytest tests/test_db.py -v` — expect success\n"
    )
    pipe = Capture(mem, plan)
    result = run_workflow(pipe, "test")  # type: ignore[arg-type]
    assert pipe.modes == ["plan", "execute"]
    assert result["workflow"] == "test"
    assert "tests/test_db.py" in result["test_files"]
    assert "tests/test_db.py" in seen[0]
    assert "python3 -m pytest tests/test_db.py" in result["plan_text"]
    steps = result["log_lines"]
    assert any("1/3 looking for existing tests" in line for line in steps)
    assert any("2/3 planning how tests will run" in line for line in steps)
    assert any("3/3 executing test plan" in line for line in steps)
    assert any("tests/test_db.py" in line for line in steps)
    assert any("run `python3 -m pytest tests/test_db.py -v`" in line for line in steps)
    assert any("plan.md ready" in line for line in steps)
    assert any("finished" in line for line in steps)


def test_test_workflow_notes_missing_tests(tmp_path: Path) -> None:
    mem = _memory(tmp_path)
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    result = run_workflow(
        _FakePipeline(
            mem,
            "# Plan\nGoal: add smoke\n\n## Todos\n"
            "1. [ ] create `tests/test_app.py` — smoke\n"
            "2. [ ] run `python3 -m pytest tests/test_app.py -v` — expect success\n",
        ),  # type: ignore[arg-type]
        "test",
    )
    assert result["test_files"] == []
    assert any("no test files yet" in line for line in result["log_lines"])
    assert any("create `tests/test_app.py`" in line for line in result["log_lines"])


def test_plan_requirement_gaps_flags_stub_start_script(tmp_path: Path) -> None:
    mem = _memory(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "db.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n",
        encoding="utf-8",
    )
    (tmp_path / "start_service.sh").write_text(
        "#!/usr/bin/env bash\nset -e\n",
        encoding="utf-8",
    )
    mem.write_plan("# Plan\nGoal: (none)\n\n## Todos\n")
    gaps = plan_requirement_gaps(
        mem,
        "fastapi src/db.py db.json select insert update delete start_service.sh",
    )
    assert any("start_service.sh" in g for g in gaps)
    assert any("update" in g for g in gaps)
    assert any("delete" in g for g in gaps)


def test_fix_plan_rewrites_plan_and_does_not_execute(tmp_path: Path) -> None:
    mem = _memory(tmp_path)
    (tmp_path / "start_service.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    mem.write_plan("# Plan\nGoal: (none)\n\n## Todos\n")
    mem.append_session(
        "user",
        "create a fastapi server at src/db.py with select insert update delete",
    )
    plan = (
        "# Plan\n"
        "Goal: FastAPI db on src/db.py plus start_service.sh\n"
        "\n"
        "## Todos\n"
        "1. [ ] refine `start_service.sh` — print app routes and exit\n"
        "2. [ ] refine `src/db.py` — add update and delete routes\n"
    )
    pipe = _FakePipeline(mem, plan)
    result = run_workflow(
        pipe,  # type: ignore[arg-type]
        "fix-plan",
        extra="start_service.sh must start src/db.py",
    )
    assert pipe.modes == ["plan"]
    assert result["skipped_execute"] is True
    assert result["workflow"] == "fix-plan"
    assert result["last_file"] == "plan.md"
    assert any("start_service.sh" in g for g in result["gaps"])
    assert "start_service.sh" in seen_or_prompt(pipe, result)
    assert any("1/3 reading requirement" in line for line in result["log_lines"])
    assert any("3/3 rewriting plan.md" in line for line in result["log_lines"])
    assert "refine `start_service.sh`" in result["plan_text"]


def seen_or_prompt(pipe: _FakePipeline, result: dict) -> str:
    return " ".join(result.get("log_lines") or []) + " " + str(result.get("requirement") or "")
