"""Compile the actual quickstart snippets with the installed released sqt command."""

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "src/content/docs/docs"
SQT = os.environ.get("SQT", "sqt")


def main() -> None:
    for document, prefix, count in [
        ("quickstart.mdx", "quickstart", 3),
        ("examples/staged-workflow.mdx", "virtual-start", 4),
    ]:
        guide = (DOCS / document).read_text()
        snippets = re.findall(rf"`{prefix}/([^`]+)`:\n\n```\w+\n(.*?)```", guide, re.S)
        assert len(snippets) == count, f"{document} must contain all {count} authored files"
        with tempfile.TemporaryDirectory(prefix="sqltide-docs-") as directory:
            project = Path(directory)
            for path, source in snippets:
                file = project / path
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(source)
            for command in ["discover", "compile"]:
                subprocess.run([SQT, command, "--project-dir", directory], check=True)
            assert (project / "target/manifest.json").is_file()
            assert (project / "target/sqltide_dag.json").is_file()
            compiled = list((project / "target/compiled/models").rglob("*.sql"))
            assert len(compiled) == count - 2
            if prefix == "virtual-start":
                model = project / "pipelines/pl__orders/order_totals.sql"
                model.write_text(model.read_text().replace(">= 0", ">= 100"))
                subprocess.run([SQT, "compile", "--project-dir", directory], check=True)
            print(f"{document}: published CLI discovered and compiled all documented files")
    with tempfile.TemporaryDirectory(prefix="sqltide-hero-") as directory:
        shutil.copytree(ROOT / "examples/hero", directory, dirs_exist_ok=True)
        subprocess.run([SQT, "compile", "--project-dir", directory], check=True)
        compiled = list((Path(directory) / "target/compiled/models").rglob("*.sql"))
        assert len(compiled) == 2
        config = Path(directory) / "sqltide_project.toml"
        config.write_text(config.read_text() + '\n[defaults]\npipeline_mode = "virtual"\n')
        subprocess.run([SQT, "compile", "--project-dir", directory], check=True)
        print("Hero: published CLI compiled both typed models in direct and virtual modes")


if __name__ == "__main__":
    main()
