"""Зависимости пакета mop -- из mop/common/deps.py (#351).

pyproject.toml объявляет dependencies динамическими, а список отдаёт этот
файл: deps.PIP один на узел, тело, CI и клиента. Читается exec'ом, как в
.gitlab-ci.yml: deps.py -- только данные, без импортов. setuptools --
только под __main__: tests/packaging.py берёт requirements() без сборки.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def requirements(path=os.path.join(HERE, "mop", "common", "deps.py")):
    scope = {}
    with open(path) as f:
        exec(f.read(), scope)
    return list(scope["PIP"])


if __name__ == "__main__":
    from setuptools import setup
    setup(install_requires=requirements())
