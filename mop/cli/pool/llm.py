"""LLM profiles: what a puppet can run on and whether the key is there

A profile changes exactly one thing — where a puppet goes for tokens.
Everything else (tmux, state, stuck detection) is the same for every profile.
"""
from mop.cli import lib
from mop.common import fsutil, llm, puppets
from mop.client import keys


def main(argv):
    if argv:
        lib.usage(__doc__)
    blob, note = keys.llm_keys_blob()
    have = set(fsutil.read_kv(blob or "", raw=True))
    for name, prof in llm.profiles().items():
        key = prof.get("key")
        if not key:
            state = prof["doc"] or "no key required"
        elif key in have:
            state = f"key {key}: present"
        else:
            state = f"key {key}: missing from {puppets.LOCAL_KEYS_FILE}"
        base = prof["env"].get("ANTHROPIC_BASE_URL", "api.anthropic.com (default)")
        print(f"  {name:8}  {base:38}  {state}")
    if note:
        print(f"\n{note}")



# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
