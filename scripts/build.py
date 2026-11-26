#!/usr/bin/env python3
"""Build a single-file Python application using standard-library tools only."""

import argparse
from pathlib import Path
import shutil
import tempfile
import zipapp


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "migratarr"


def build(output):
    output = Path(output).expanduser().absolute()
    # Include only the package's top-level Python modules. Reports, credentials,
    # configuration, tests, bytecode and repository files cannot enter the build.
    modules = sorted(PACKAGE.glob("*.py"))
    if not modules or any(module.is_symlink() or not module.is_file() for module in modules):
        raise ValueError("package modules must be regular Python source files")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="migratarr-build-") as temporary:
        stage = Path(temporary)
        target_package = stage / "migratarr"
        target_package.mkdir()
        for module in modules:
            shutil.copyfile(module, target_package / module.name)
        (stage / "__main__.py").write_text(
            "from migratarr.cli import main\nraise SystemExit(main())\n",
            encoding="utf-8",
        )
        zipapp.create_archive(stage, target=output,
                              interpreter="/usr/bin/env python3", compressed=True)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(ROOT / "dist" / "migratarr.pyz"),
                        help="Output archive path (default: dist/migratarr.pyz)")
    arguments = parser.parse_args(argv)
    print(build(arguments.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
