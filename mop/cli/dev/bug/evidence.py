"""mop dev bug evidence <iid>: remote issue branches and verified integration merges

Fetches origin first. Inspects at most 500 remote refs, 40 integration refs and
80 first-parent merges per integration ref. A branch mention or #iid in a
commit message is not evidence of integration; a merge needs a second parent
on that branch. Missing/deleted branch tips cannot be verified.
"""
from mop.cli import lib
from mop.cli.dev.bug._common import parser
from mop.common import gitlab


MCP = {"annotations": "readonly", "args": [
    {"name": "iid", "type": "string", "required": True, "help": "issue number"}]}

REF_LIMIT = 500
INTEGRATION_LIMIT = 40
MERGE_LIMIT = 80


def _ancestor(older, newer):
    """Родство коммитов: код 1 означает не-предка, и это обычный ответ."""
    import subprocess
    r = subprocess.run(["git", "merge-base", "--is-ancestor", older, newer],
                       capture_output=True, text=True)
    if r.returncode not in (0, 1):
        raise RuntimeError(f"git merge-base: {r.stderr.strip()}")
    return r.returncode == 0


def main(argv):
    p = parser("evidence")
    p.add_argument("iid")
    a = p.parse_args(argv)
    if not a.iid.isascii() or not a.iid.isdecimal() or len(a.iid) > 10 or int(a.iid) < 1:
        p.error("iid must be a positive decimal number")
    iid = str(int(a.iid))
    lib.git("fetch", "--prune", "origin")
    raw = lib.git("for-each-ref", f"--count={REF_LIMIT + 1}",
                  "--format=%(refname:short)%09%(objectname)", "refs/remotes/origin")
    refs = [line.split("\t", 1) for line in raw.splitlines()]
    clipped = len(refs) > REF_LIMIT
    refs = refs[:REF_LIMIT]
    branches = {ref: sha for ref, sha in refs if gitlab.evidence_branch(ref, iid)}
    integrations = [ref for ref, _ in refs if ref == "origin/master" or
                    ref.startswith(("origin/epic/", "origin/integration/", "origin/integrate/"))]
    limited = len(integrations) > INTEGRATION_LIMIT
    integrations = integrations[:INTEGRATION_LIMIT]
    print(f"#{iid} remote issue branches (origin, fetched):")
    for ref, sha in branches.items():
        print(f"  {ref} {sha}")
    if not branches:
        print("  none in inspected refs")
    print("integration merges (first-parent history):")
    seen = set()
    matches = 0
    for ref in integrations:
        raw = lib.git("log", f"-n{MERGE_LIMIT + 1}", "--first-parent", "--merges",
                      "--format=%H%x09%P%x09%s", ref)
        rows = raw.splitlines()
        if len(rows) > MERGE_LIMIT:
            print(f"  {ref}: history limited to {MERGE_LIMIT} merges")
        for row in rows[:MERGE_LIMIT]:
            sha, parents, subject = row.split("\t", 2)
            source = gitlab.merge_source(subject)
            if len(parents.split()) != 2 or not source or not gitlab.evidence_branch("origin/" + source, iid):
                continue
            tip = branches.get("origin/" + source)
            first, second = parents.split()
            if sha in seen:
                continue
            seen.add(sha)
            if tip and not _ancestor(second, first) and not _ancestor(tip, first) and \
                    (_ancestor(second, tip) or _ancestor(tip, second)):
                print(f"  {ref} {sha} merged {source} at {second} (verified ancestry)")
            else:
                print(f"  {ref} {sha} names {source} at {second} (branch ancestry unverified)")
            matches += 1
    if not matches:
        print("  no matching structural merge in inspected history")
    if clipped or limited:
        print(f"search limited: {REF_LIMIT} remote refs, {INTEGRATION_LIMIT} integration refs; results may be incomplete")
