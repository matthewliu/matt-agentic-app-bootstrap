"""Copier `_tasks` must run inside the rendered `{{ app_name }}/` subfolder.

The template renders the project one level down. A task without
`working_directory` runs in the parent folder: `git init` creates a repo
there and `pip install -e '.[dev]'` finds no pyproject.toml.
"""

import yaml

from tests.conftest import TEMPLATE_DIR


def test_every_task_runs_in_app_subfolder():
    tasks = yaml.safe_load((TEMPLATE_DIR / "copier.yml").read_text())["_tasks"]
    assert tasks
    for task in tasks:
        assert isinstance(task, dict), task
        assert task["working_directory"] == "{{ app_name }}", task
