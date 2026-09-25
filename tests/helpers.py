"""Integration fixture utilities; production HTTP/renderer adapters are reused."""
import subprocess
import sys
import urllib.error
import urllib.parse
from pathlib import Path
import yaml
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from jenkins_config.client import JenkinsClient
from jenkins_config.render import TemplateRenderer
from jenkins_config.settings import Settings
from jenkins_config.operations import ComposeClient


def load_settings(path):
    return yaml.safe_load(Path(path).read_text())


def write_yaml(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False))


def render(settings, state, secret_dir):
    TemplateRenderer().render(Settings.from_mapping(settings), state, secret_dir)


def run_compose(state, *args):
    ComposeClient(state).execute(*args)
