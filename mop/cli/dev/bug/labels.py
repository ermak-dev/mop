"""mop dev bug labels: the label vocabulary
"""
from mop.common import gitlab
from mop.common.render import table


def main(_argv):
    print("\n".join(table([(g + "::", ", ".join(v)) for g, v in gitlab.VOCAB.items()])))
    print("\nOne label from each group per issue: a second one of the same "
          "group silently replaces the first.")
