"""mop dev web preview [--port N]: serve the built dashboard on localhost

Serves web/dist on 127.0.0.1:4173 by default, without a pool API.
Build first with `mop dev web build`. Stop with Ctrl+C.
"""
import argparse
import subprocess

from mop.cli.dev.web.build import WEB, tool


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=4173)
    args = parser.parse_args(argv)
    return subprocess.call([tool("npm"), "exec", "--", "vite", "preview",
                            "--host", "127.0.0.1", "--port", str(args.port),
                            "--strictPort"], cwd=WEB)
