"""Jinja2 owns configuration structure; Python supplies validated values and paths."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from jinja2 import Environment, FileSystemLoader, StrictUndefined
import yaml

from .settings import Settings

ROOT = Path(__file__).resolve().parents[1]


def groovy_string(value: str) -> str:
    return "'" + value.replace('\\', '\\\\').replace("'", "\\'").replace('\n', '\\n').replace('\r', '\\r') + "'"


class TemplateRenderer:
    def __init__(self, templates: Path = ROOT / 'templates'):
        self.environment = Environment(
            loader=FileSystemLoader(templates), undefined=StrictUndefined,
            autoescape=False, keep_trailing_newline=True, trim_blocks=True, lstrip_blocks=True,
        )
        self.environment.filters.update(
            scalar=lambda value: json.dumps(value, ensure_ascii=False),
            compose_scalar=lambda value: json.dumps(str(value).replace('$', '$$'), ensure_ascii=False),
            groovy=groovy_string,
        )

    def render(self, settings: Settings, state: Path, secrets: Path):
        context = {'s': settings, 'state': str(state.resolve()), 'secrets': str(secrets.resolve())}
        outputs = {
            'compose.yaml': self.environment.get_template('compose.yaml.j2').render(**context),
            'casc/jenkins.yaml': self.environment.get_template('casc.yaml.j2').render(**context),
        }
        # Validate every output before changing any deployed configuration.
        for name, content in outputs.items():
            if not isinstance(yaml.safe_load(content), dict):
                raise ValueError(f'Invalid generated YAML: {name}')
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        state.chmod(0o700)
        with TemporaryDirectory(prefix='.render-', dir=state) as directory:
            temporary = Path(directory)
            for name, content in outputs.items():
                source, target = temporary / name, state / name
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text(content)
                source.chmod(0o644)
                target.parent.mkdir(parents=True, exist_ok=True)
            for name in outputs:
                os.replace(temporary / name, state / name)
