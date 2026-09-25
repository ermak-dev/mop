"""mop dev ci lint [file]: check a .gitlab-ci.yml against the project

The file is the working copy's .gitlab-ci.yml by default. Prints
"valid — N job(s): ..." or "invalid" with the errors (exit 1), then warnings.
Includes resolve against the project, as they would in a pipeline.
"""
import os

from mop import config, gitlab
from mop.cli.dev.ci._common import parser


def main(argv):
    p = parser("lint")
    p.add_argument("file", nargs="?", default=os.path.join(config.PROJECT, ".gitlab-ci.yml"))
    a = p.parse_args(argv)
    try:
        with open(a.file, encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        raise RuntimeError(f"cannot read {a.file}: {e.strerror}")
    lines, valid = gitlab.lint_report(gitlab.lint(content))
    print("\n".join(lines))
    return 0 if valid else 1
