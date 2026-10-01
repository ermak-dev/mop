"""LLM profiles: mop llm [--probe] [--tiers] — what a puppet can run on and whether the key is there

A profile changes exactly one thing — where a puppet goes for tokens.
Everything else (tmux, state, stuck detection) is the same for every profile.
--probe asks each provider that can answer (a profile with a probe hook and
a key at hand) whether the key is alive and how much quota is left.
--tiers prints the installation's tier order (MOP_LLM_TIERS): strongest
first, the order the policy walks when a credential runs out.
"""
import time

from mop.cli import lib
from mop.common import config, credreg, fsutil, llm, puppets, tiers


def main(argv):
    probe = "--probe" in argv
    if "--tiers" in argv:
        # Разбор со сверкой по реестру: незнакомый профиль в настройке --
        # громкий отказ здесь, а не молчаливый пропуск яруса в политике.
        try:
            listed = tiers.default(llm.profiles())
        except ValueError as e:
            lib.fail(str(e))
            return 1
        for i, t in enumerate(listed, 1):
            print(f"  {i}. {t.profile}" + (f":{t.model}" if t.model else ""))
        return
    if [a for a in argv if a != "--probe"]:
        lib.usage(__doc__)
    # Ключ профиля читаем из локального .env: раздачей узлам занимается
    # сервис (#391), а здесь ключ нужен самому мастеру для его сессий.
    env = {k: v for k, v in config.read_env(puppets.LOCAL_KEYS_FILE).items() if v}
    have = set(env)
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
            state = status_line(prof["probe"](env[key]))
        print(f"  {name:8}  {base:38}  {state}")



def status_line(st):
    """CredStatus -> строка колонки: вид, загрузка, окна и время сброса."""
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.resets_at)) if st.resets_at else ""
    tail = f", resets {when}" if when else ""
    return f"{credreg.WORDS.get(st.kind, st.kind)}: {st.detail}{tail}"


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
