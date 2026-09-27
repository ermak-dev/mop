"""LLM profiles: mop llm [--probe] — what a puppet can run on and whether the key is there

A profile changes exactly one thing — where a puppet goes for tokens.
Everything else (tmux, state, stuck detection) is the same for every profile.
--probe asks each provider that can answer (a profile with a probe hook and
a key at hand) whether the key is alive and how much quota is left.
"""
import time

from mop.cli import lib
from mop.common import fsutil, llm, puppets
from mop.client import keys


def main(argv):
    probe = "--probe" in argv
    if [a for a in argv if a != "--probe"]:
        lib.usage(__doc__)
    blob, note = keys.llm_keys_blob()
    found = fsutil.read_kv(blob or "", raw=True)
    have = set(found)
    for name, prof in llm.profiles().items():
        key = prof.get("key")
        if not key:
            state = prof["doc"] or "no key required"
        elif key in have:
            state = f"key {key}: present"
        else:
            state = f"key {key}: missing from {puppets.LOCAL_KEYS_FILE}"
        base = prof["env"].get("ANTHROPIC_BASE_URL", "api.anthropic.com (default)")
        if probe and prof.get("probe") and key in have:
            state = status_line(prof["probe"](found[key]))
        print(f"  {name:8}  {base:38}  {state}")
    if note:
        print(f"\n{note}")



def status_line(st):
    """CredStatus -> строка колонки: вид, загрузка, окна и время сброса."""
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.resets_at)) if st.resets_at else ""
    tail = f", resets {when}" if when else ""
    return f"{st.kind}: {st.detail}{tail}"


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
