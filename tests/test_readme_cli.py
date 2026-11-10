"""The README's CLI examples use flags the CLI actually has (vault #6 item).

The documented `--source` / `--type` did not exist (`--source-code` /
`--doc-type` do), so the commands errored out as written. Every option in the
README's CLI block is checked against the typer command it names.
"""
import re
import shlex
from pathlib import Path

import typer.main

import vaultctl

README = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")


def _cli_examples():
    block = README.split("## CLI", 1)[1].split("```bash", 1)[1].split("```", 1)[0]
    return [line.split("#", 1)[0].strip() for line in block.splitlines()
            if line.strip().startswith("vaultctl")]


def _options(command_name):
    group = typer.main.get_command(vaultctl.app)
    command = group.commands[command_name]
    return {opt for param in command.params for opt in param.opts + param.secondary_opts}


def test_the_readme_has_cli_examples():
    assert len(_cli_examples()) >= 4


def test_every_readme_cli_flag_exists():
    for example in _cli_examples():
        argv = shlex.split(example.replace("<", "").replace(">", ""))
        command, flags = argv[1], [a for a in argv[2:] if a.startswith("-")]
        unknown = set(flags) - _options(command)
        assert not unknown, f"README example {example!r}: no such option {sorted(unknown)}"


def test_the_list_example_filters_by_source_code_and_doc_type():
    assert any(re.search(r"--source-code \S+ --doc-type \S+", e) for e in _cli_examples())
