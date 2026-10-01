"""Run with uv run python -m jenkins_config <action>."""
import argparse
from pathlib import Path
import subprocess
import urllib.error
import xml.etree.ElementTree as ET

from jinja2 import TemplateError
import yaml

from .client import JenkinsClient
from .operations import ComposeClient, DeploymentManager, SecretStore
from .render import ROOT, TemplateRenderer
from .settings import Settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['init', 'render', 'up', 'down', 'status', 'reload', 'seed'])
    parser.add_argument('--settings', type=Path, default=ROOT / 'settings.local.yaml')
    parser.add_argument('--state', type=Path, default=ROOT / '.state')
    parser.add_argument('--secrets', type=Path, default=ROOT / '.secrets')
    args = parser.parse_args()
    state = args.state.resolve()
    secret_store = SecretStore(args.secrets.resolve())
    compose = ComposeClient(state)
    if args.action == 'init':
        settings = Settings.load(args.settings) if args.settings.exists() else None
        secret_store.initialize(settings.jenkins.admin.password if settings else None)
        print(f'Admin password retained in {secret_store.directory}/admin_password (value not printed).')
        return
    if args.action in ('down', 'status'):
        compose.execute('down' if args.action == 'down' else 'ps')
        return
    settings = Settings.load(args.settings)
    renderer = TemplateRenderer()
    if args.action == 'render':
        renderer.render(settings, state, secret_store.directory)
        print(f'Rendered Compose and JCasC to {state}')
        return
    password = (settings.jenkins.admin.password if args.action == 'up' and settings.jenkins.admin.password is not None
                else secret_store.read('admin_password'))
    client = JenkinsClient(f'http://127.0.0.1:{settings.jenkins.http_port}', settings.jenkins.admin.user, password)
    manager = DeploymentManager(settings, state, renderer, compose, secret_store, client)
    getattr(manager, args.action)()
    print(f'{args.action} completed; seed queued. Check its build result in Jenkins.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, TimeoutError, subprocess.CalledProcessError,
            urllib.error.URLError, ET.ParseError, TemplateError, yaml.YAMLError) as error:
        raise SystemExit(str(error)) from error
