"""LLM profiles: what a puppet can run on and whether the key is there

A profile changes exactly one thing — where a puppet goes for tokens.
Everything else (tmux, state, stuck detection) is the same for every profile.
"""
from mop.cli import lib
from mop import keys, llm, puppets


def main(argv):
    if argv:
        lib.usage(__doc__)
    blob, note = keys.llm_keys_blob()
    have = {l.split("=", 1)[0] for l in (blob or "").splitlines() if "=" in l}
    for name, prof in llm.profiles().items():
        key = prof.get("key")
        if not key:
            state = prof["doc"] or "no key required"
        elif key in have:
            state = f"key {key}: present"
        else:
            state = f"key {key}: MISSING from {puppets.LOCAL_KEYS_FILE}"
        base = prof["env"].get("ANTHROPIC_BASE_URL", "api.anthropic.com (default)")
        print(f"  {name:8}  {base:38}  {state}")
    if note:
        print(f"\n{note}")
    print("\nmop add --llm <profile> [origin]   |   mop login pushes keys to nodes")



# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
