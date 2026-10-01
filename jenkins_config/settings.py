"""Single YAML input with explicit validation before any deployment side effect."""
from dataclasses import asdict, dataclass, field
from pathlib import Path
import re
from urllib.parse import urlsplit

import yaml


@dataclass(frozen=True)
class Settings:
    project: str
    controller_image: str
    agent_image: str
    docker_socket: str
    docker_socket_gid: int
    jenkins_url: str
    http_port: int
    admin_user: str
    pipeline_repo: str
    pipeline_branch: str
    seed_dsl: str
    seed_poll: str
    git_credentials: bool
    application_git_credentials: bool = False
    admin_password: str | None = field(default=None, repr=False)

    def __post_init__(self):
        for key, value in asdict(self).items():
            if key == 'admin_password':
                if value is not None and (not isinstance(value, str) or not value
                        or value != value.strip() or any(t in value for t in ('\n', '\r', '\x00'))):
                    raise ValueError('admin_password must be null or a nonempty single-line string without surrounding whitespace')
            elif key in ('http_port', 'docker_socket_gid'):
                if type(value) is not int:
                    raise ValueError(f'{key} must be an integer')
            elif key in ('git_credentials', 'application_git_credentials'):
                if type(value) is not bool:
                    raise ValueError(f'{key} must be a boolean')
            elif not isinstance(value, str) or not value or any(t in value for t in ('\n', '\r', '${', '{{', '}}')):
                raise ValueError(f'Invalid {key}')
        patterns = {
            'project': r'[a-z][a-z0-9-]{0,39}',
            'admin_user': r'[A-Za-z0-9_.-]+',
            'pipeline_branch': r'[A-Za-z0-9_][A-Za-z0-9_./-]*',
        }
        for key, pattern in patterns.items():
            if not re.fullmatch(pattern, getattr(self, key)):
                raise ValueError(f'Invalid {key}')
        if not 1024 <= self.http_port <= 65535 or self.docker_socket_gid < 0:
            raise ValueError('Invalid http_port or docker_socket_gid')
        if not self.docker_socket.startswith('/'):
            raise ValueError('docker_socket must be an absolute daemon-host path')
        for key in ('controller_image', 'agent_image'):
            image = getattr(self, key)
            if not re.fullmatch(r'[A-Za-z0-9_./:@-]+', image):
                raise ValueError(f'Invalid {key}')
            if '@' in image:
                valid = re.fullmatch(r'[^@]+@sha256:[a-f0-9]{64}', image)
            else:
                tag = image.rsplit('/', 1)[-1].partition(':')[2]
                valid = re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', tag) and tag not in ('latest', 'lts', 'main', 'v')
            if not valid:
                raise ValueError(f'{key} requires an explicit version or valid digest')
        for key in ('jenkins_url', 'pipeline_repo'):
            url = urlsplit(getattr(self, key))
            if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError(f'{key} must be an HTTP(S) URL without embedded credentials')
            if key == 'jenkins_url' and url.path not in ('', '/'):
                raise ValueError('jenkins_url must use the root path')
        if self.git_credentials and not self.pipeline_repo.startswith('https://'):
            raise ValueError('Git credentials require HTTPS')
        if self.seed_dsl.startswith('/') or '..' in self.seed_dsl.split('/'):
            raise ValueError('seed_dsl must stay inside the pipelines repository')

    @classmethod
    def from_mapping(cls, values):
        if not isinstance(values, dict):
            raise ValueError('Settings must be a YAML mapping')
        if 'kubernetes' in values:
            raise ValueError('Remove the obsolete kubernetes section; this project supports Docker only')
        try:
            return cls(**values)
        except TypeError as error:
            raise ValueError(f'Settings fields do not match settings.example.yaml: {error}') from error

    @classmethod
    def load(cls, path: Path):
        return cls.from_mapping(yaml.safe_load(path.read_text()))
