"""Check the guides' projects against the installed released sqt command, offline.

The quickstart and the staged-workflow example both start from `sqt playground`. This creates the
playground without starting it, adds the files the staged-workflow page tells the reader to save,
and compiles each version the guides build. It does not connect to a warehouse.
"""

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "src/content/docs/docs"
SQT = os.environ.get("SQT", "sqt")


def sqt(*arguments: str) -> None:
    subprocess.run([SQT, *arguments], check=True)


def compiled_models(project: Path) -> list[Path]:
    return sorted((project / "target/compiled/models").rglob("*.sql"))


def main() -> None:
    guide = (DOCS / "examples/staged-workflow.mdx").read_text()
    saved = re.findall(r"as `([^`]+)`[^\n]*:\n\n```\w+\n(.*?)```", guide, re.S)
    assert [path for path, _ in saved] == ["pipelines/pl__orders/large_orders.sql", "large.sql"], (
        "staged-workflow.mdx must tell the reader to save large_orders.sql and large.sql"
    )
    with tempfile.TemporaryDirectory(prefix="sqltide-docs-") as directory:
        project = Path(directory) / "playground"
        sqt("playground", str(project), "--no-start")
        sqt("compile", "--project-dir", str(project))
        assert len(compiled_models(project)) == 1
        shutil.copy(
            project / "revisions/completed_orders.sql",
            project / "pipelines/pl__orders/order_report.sql",
        )
        sqt("compile", "--project-dir", str(project))
        print("quickstart.mdx: the playground compiles before and after its change")
        for path, source in saved:
            (project / path).write_text(source)
        sqt("compile", "--project-dir", str(project))
        assert len(compiled_models(project)) == 2
        print("examples/staged-workflow.mdx: the added model compiles with the playground")
    with tempfile.TemporaryDirectory(prefix="sqltide-hero-") as directory:
        shutil.copytree(ROOT / "examples/hero", directory, dirs_exist_ok=True)
        sqt("compile", "--project-dir", directory)
        assert len(compiled_models(Path(directory))) == 2
        config = Path(directory) / "sqltide_project.toml"
        config.write_text(config.read_text() + '\n[defaults]\npipeline_mode = "virtual"\n')
        sqt("compile", "--project-dir", directory)
        print("Hero: published CLI compiled both typed models in direct and virtual modes")


if __name__ == "__main__":
    main()
