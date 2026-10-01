"""Validated, structured YAML settings for the Jenkins deployment."""
from dataclasses import dataclass, field, fields
from pathlib import Path
import re
from urllib.parse import urlsplit

import yaml


@dataclass(frozen=True)
class ProjectSettings:
    name: str


@dataclass(frozen=True)
class ImageSettings:
    controller: str
    agent: str


@dataclass(frozen=True)
class DockerSettings:
    socket: str
    socket_gid: int


@dataclass(frozen=True)
class AdminSettings:
    user: str
    password: str | None = field(repr=False)


@dataclass(frozen=True)
class JenkinsSettings:
    url: str
    http_port: int
    admin: AdminSettings


@dataclass(frozen=True)
class RepositorySettings:
    url: str
    branch: str


@dataclass(frozen=True)
class SeedSettings:
    dsl: str
    poll: str


@dataclass(frozen=True)
class PipelineSettings:
    repository: RepositorySettings
    seed: SeedSettings


@dataclass(frozen=True)
class GitCredentialSettings:
    enabled: bool
    username: str | None = field(repr=False)
    token: str | None = field(repr=False)


@dataclass(frozen=True)
class CredentialSettings:
    pipeline_git: GitCredentialSettings
    application_git: GitCredentialSettings


def _section(value, cls, path):
    expected = {item.name for item in fields(cls)}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f'{path} must contain exactly: {", ".join(sorted(expected))}')
    return dict(value)


def _nonempty(value, path):
    if not isinstance(value, str) or not value or any(part in value for part in ('\n', '\r', '${', '{{', '}}')):
        raise ValueError(f'Invalid {path}')


def _secret(value, path):
    if value is not None and (not isinstance(value, str) or not value
            or value != value.strip() or any(part in value for part in ('\n', '\r', '\x00'))):
        raise ValueError(f'{path} must be null or a nonempty single-line string without surrounding whitespace')


@dataclass(frozen=True)
class Settings:
    project: ProjectSettings
    images: ImageSettings
    docker: DockerSettings
    jenkins: JenkinsSettings
    pipeline: PipelineSettings
    credentials: CredentialSettings = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.project.name, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,39}', self.project.name):
            raise ValueError('Invalid project.name')
        for name in ('controller', 'agent'):
            image = getattr(self.images, name)
            _nonempty(image, f'images.{name}')
            if not re.fullmatch(r'[A-Za-z0-9_./:@-]+', image):
                raise ValueError(f'Invalid images.{name}')
            if '@' in image:
                valid = re.fullmatch(r'[^@]+@sha256:[a-f0-9]{64}', image)
            else:
                tag = image.rsplit('/', 1)[-1].partition(':')[2]
                valid = re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', tag) and tag not in ('latest', 'lts', 'main', 'v')
            if not valid:
                raise ValueError(f'images.{name} requires an explicit version or valid digest')
        _nonempty(self.docker.socket, 'docker.socket')
        if not self.docker.socket.startswith('/'):
            raise ValueError('docker.socket must be an absolute daemon-host path')
        if type(self.docker.socket_gid) is not int or self.docker.socket_gid < 0:
            raise ValueError('Invalid docker.socket_gid')
        if type(self.jenkins.http_port) is not int or not 1024 <= self.jenkins.http_port <= 65535:
            raise ValueError('Invalid jenkins.http_port')
        for path, value in (('jenkins.url', self.jenkins.url),
                            ('pipeline.repository.url', self.pipeline.repository.url)):
            _nonempty(value, path)
            url = urlsplit(value)
            if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError(f'{path} must be an HTTP(S) URL without embedded credentials')
            if path == 'jenkins.url' and url.path not in ('', '/'):
                raise ValueError('jenkins.url must use the root path')
        if not isinstance(self.jenkins.admin.user, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', self.jenkins.admin.user):
            raise ValueError('Invalid jenkins.admin.user')
        _secret(self.jenkins.admin.password, 'jenkins.admin.password')
        branch = self.pipeline.repository.branch
        if not isinstance(branch, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_./-]*', branch):
            raise ValueError('Invalid pipeline.repository.branch')
        _nonempty(self.pipeline.seed.dsl, 'pipeline.seed.dsl')
        if self.pipeline.seed.dsl.startswith('/') or '..' in self.pipeline.seed.dsl.split('/'):
            raise ValueError('pipeline.seed.dsl must stay inside the pipelines repository')
        _nonempty(self.pipeline.seed.poll, 'pipeline.seed.poll')
        for name in ('pipeline_git', 'application_git'):
            credential = getattr(self.credentials, name)
            if type(credential.enabled) is not bool:
                raise ValueError(f'credentials.{name}.enabled must be a boolean')
            for field_name in ('username', 'token'):
                value = getattr(credential, field_name)
                _secret(value, f'credentials.{name}.{field_name}')
                if credential.enabled and value is None:
                    raise ValueError(f'credentials.{name}.{field_name} is required when enabled')
        if self.credentials.pipeline_git.enabled and not self.pipeline.repository.url.startswith('https://'):
            raise ValueError('Private pipeline Git credentials require HTTPS')

    @classmethod
    def from_mapping(cls, values):
        root = _section(values, cls, 'settings')
        jenkins = _section(root['jenkins'], JenkinsSettings, 'jenkins')
        jenkins['admin'] = AdminSettings(**_section(jenkins['admin'], AdminSettings, 'jenkins.admin'))
        pipeline = _section(root['pipeline'], PipelineSettings, 'pipeline')
        pipeline['repository'] = RepositorySettings(**_section(pipeline['repository'], RepositorySettings, 'pipeline.repository'))
        pipeline['seed'] = SeedSettings(**_section(pipeline['seed'], SeedSettings, 'pipeline.seed'))
        credentials = _section(root['credentials'], CredentialSettings, 'credentials')
        for name in ('pipeline_git', 'application_git'):
            credentials[name] = GitCredentialSettings(**_section(credentials[name], GitCredentialSettings, f'credentials.{name}'))
        return cls(
            project=ProjectSettings(**_section(root['project'], ProjectSettings, 'project')),
            images=ImageSettings(**_section(root['images'], ImageSettings, 'images')),
            docker=DockerSettings(**_section(root['docker'], DockerSettings, 'docker')),
            jenkins=JenkinsSettings(**jenkins),
            pipeline=PipelineSettings(**pipeline),
            credentials=CredentialSettings(**credentials),
        )

    @classmethod
    def load(cls, path: Path):
        return cls.from_mapping(yaml.safe_load(path.read_text()))
