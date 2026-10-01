"""Small adapters composed by the deployment use cases; no custom scheduler."""
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import xml.etree.ElementTree as ET
import yaml

from .client import JenkinsClient
from .render import TemplateRenderer
from .settings import Settings

GIT_SECRET_NAMES = {'git_username', 'git_token', 'application_git_username', 'application_git_token'}


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
        self.write('admin_password', password)

    def write(self, name, value):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.directory / name
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, 'w') as stream:
                stream.write(value)
        # Preserve the bind-mounted inode when updating an existing secret.
        path.chmod(0o600)
        if path.read_text().strip() != value:
            path.write_text(value)

    def git_values(self, settings):
        for credential, names in (
                (settings.credentials.pipeline_git, ('git_username', 'git_token')),
                (settings.credentials.application_git, ('application_git_username', 'application_git_token'))):
            if credential.enabled:
                yield names[0], credential.username
                yield names[1], credential.token

    def sync_git(self, settings):
        changed = False
        for name, value in self.git_values(settings):
            path = self.directory / name
            if not path.exists() or path.read_text().strip() != value:
                changed = True
            self.write(name, value)
        return changed

    def git_matches(self, settings):
        for name, value in self.git_values(settings):
            try:
                if self.read(name) != value:
                    return False
            except (FileNotFoundError, ValueError):
                return False
        return True

    def read(self, name):
        value = (self.directory / name).read_text().strip()
        if not value:
            raise ValueError(f'Empty secret file: {name}')
        return value

    def validate(self, git_credentials, application_git_credentials=False):
        names = ['admin_password']
        if git_credentials:
            names.extend(['git_username', 'git_token'])
        if application_git_credentials:
            names.extend(['application_git_username', 'application_git_token'])
        for name in names:
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

    def mounted_git_secrets(self):
        try:
            rendered = yaml.safe_load((self.state / 'compose.yaml').read_text())
            mounted = set(rendered['services']['controller'].get('secrets', []))
        except (FileNotFoundError, KeyError, TypeError):
            return set()
        return mounted & GIT_SECRET_NAMES

    def up(self):
        if any('your-org' in value for value in (
                self.settings.images.controller, self.settings.images.agent,
                self.settings.pipeline.repository.url)):
            raise ValueError('Replace example image/repository locations before starting')
        if self.settings.jenkins.admin.password is not None:
            self.secrets.initialize(self.settings.jenkins.admin.password)
        old_mounts = self.mounted_git_secrets()
        required_mounts = {name for name, _ in self.secrets.git_values(self.settings)}
        credentials_changed = self.secrets.sync_git(self.settings)
        self.secrets.validate(self.settings.credentials.pipeline_git.enabled,
                              self.settings.credentials.application_git.enabled)
        self.render()
        if self.settings.jenkins.admin.password is not None:
            self.secrets.set_admin_password(self.settings.jenkins.admin.password)
        secret_path = self.state / 'agent_secret'
        secret_path.touch(exist_ok=True)
        recreate = ('--force-recreate',) if self.settings.jenkins.admin.password is not None or credentials_changed or old_mounts != required_mounts else ()
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
        if (self.settings.jenkins.admin.password is not None
                and self.settings.jenkins.admin.password != self.secrets.read('admin_password')):
            raise ValueError('Use deploy.sh or up to apply a changed admin_password')
        required = {name for name, _ in self.secrets.git_values(self.settings)}
        if not self.secrets.git_matches(self.settings) or required != self.mounted_git_secrets():
            raise ValueError('Use deploy.sh or up to apply changed Git credentials')
        self.secrets.validate(self.settings.credentials.pipeline_git.enabled,
                              self.settings.credentials.application_git.enabled)
        self.render()
        self.client.request('configuration-as-code/reload', b'')
        self.seed()

    def seed(self):
        self.client.request('job/seed/build', b'')
