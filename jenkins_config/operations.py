"""Small adapters composed by the deployment use cases; no custom scheduler."""
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import xml.etree.ElementTree as ET

from .client import JenkinsClient
from .render import TemplateRenderer
from .settings import Settings


class ComposeClient:
    def __init__(self, state: Path, run=subprocess.run):
        self.path = state / 'compose.yaml'
        self.run = run

    def execute(self, *arguments):
        self.run(['docker', 'compose', '-f', str(self.path), *arguments], check=True)


class SecretStore:
    def __init__(self, directory: Path):
        self.directory = directory

    def initialize(self, password=None):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.directory / 'admin_password'
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(password if password is not None else secrets.token_urlsafe(32))

    def set_admin_password(self, password):
        self.initialize(password)
        path = self.directory / 'admin_password'
        # Preserve the bind-mounted inode when updating an existing secret.
        path.chmod(0o600)
        if self.read('admin_password') != password:
            path.write_text(password)

    def read(self, name):
        value = (self.directory / name).read_text().strip()
        if not value:
            raise ValueError(f'Empty secret file: {name}')
        return value

    def validate(self, git_credentials):
        for name in ['admin_password'] + (['git_username', 'git_token'] if git_credentials else []):
            self.read(name)


class DeploymentManager:
    def __init__(self, settings: Settings, state: Path, renderer: TemplateRenderer,
                 compose: ComposeClient, secret_store: SecretStore, client: JenkinsClient):
        self.settings = settings
        self.state = state
        self.renderer = renderer
        self.compose = compose
        self.secrets = secret_store
        self.client = client

    def render(self):
        self.renderer.render(self.settings, self.state, self.secrets.directory)

    def up(self):
        if any('your-org' in value for value in (
                self.settings.controller_image, self.settings.agent_image, self.settings.pipeline_repo)):
            raise ValueError('Replace example image/repository locations before starting')
        if self.settings.admin_password is not None:
            self.secrets.initialize(self.settings.admin_password)
        self.secrets.validate(self.settings.git_credentials)
        self.render()
        if self.settings.admin_password is not None:
            self.secrets.set_admin_password(self.settings.admin_password)
        secret_path = self.state / 'agent_secret'
        secret_path.touch(exist_ok=True)
        recreate = ('--force-recreate',) if self.settings.admin_password is not None else ()
        self.compose.execute('up', '-d', '--wait', '--wait-timeout', '300', *recreate, 'controller')
        self.client.wait()
        # Also apply changed settings when Compose reused an already-running controller.
        self.client.request('configuration-as-code/reload', b'')
        xml = ET.fromstring(self.client.wait('computer/docker-agent/jenkins-agent.jnlp'))
        value = xml.findtext('.//argument', '')
        if not re.fullmatch(r'[a-f0-9]{64}', value):
            raise ValueError('Invalid inbound agent secret response')
        secret_path.write_text(value)
        secret_path.chmod(0o644)  # Container UID 1000; parent state directory is private.
        self.compose.execute('up', '-d', '--force-recreate', 'agent')
        self.client.wait('computer/docker-agent/api/json', timeout=120,
                         predicate=lambda raw: json.loads(raw)['offline'] is False)
        self.compose.execute('exec', '-T', 'agent', 'docker', 'version')
        self.seed()

    def reload(self):
        if self.settings.admin_password is not None and self.settings.admin_password != self.secrets.read('admin_password'):
            raise ValueError('Use deploy.sh or up to apply a changed admin_password')
        self.secrets.validate(self.settings.git_credentials)
        self.render()
        self.client.request('configuration-as-code/reload', b'')
        self.seed()

    def seed(self):
        self.client.request('job/seed/build', b'')
