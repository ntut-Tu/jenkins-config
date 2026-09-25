#!/usr/bin/env python3
"""End-to-end Docker test using an isolated Git HTTP fixture and disposable volumes."""
import argparse
import json
import re
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import uuid
import urllib.error

import yaml

ROOT = Path(__file__).resolve().parents[1]
import helpers as manage


def command(*args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def build(client, scenario, number):
    path = 'job/pdd/job/integration-test/'
    client.request(path + f'buildWithParameters?SCENARIO={scenario}', b'')
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        try:
            result = json.loads(client.request(path + f'{number}/api/json'))
            if not result['building'] and result['result'] is not None:
                return result
        except manage.urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        time.sleep(2)
    raise TimeoutError(f'Build {number} did not finish')


def wait_seed(client, number):
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        try:
            result = json.loads(client.request(f'job/seed/{number}/api/json'))
            if not result['building'] and result['result'] is not None:
                if result['result'] != 'SUCCESS':
                    raise AssertionError(client.request(f'job/seed/{number}/consoleText').decode())
                return
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        time.sleep(2)
    raise TimeoutError(f'Seed {number} did not finish')

def publish_fixture(fixture, bare, scm_name):
    command('git', '-C', fixture, 'add', '.')
    command('git', '-C', fixture, '-c', 'user.name=Integration fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'Seed fixture revision', stdout=subprocess.DEVNULL)
    command('git', '--git-dir', bare, 'fetch', str(fixture), 'main:main', stdout=subprocess.DEVNULL)
    command('git', '--git-dir', bare, 'update-server-info')
    command('docker', 'cp', str(bare) + '/.', scm_name + ':/tmp/git/pipelines.git')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pipelines', type=Path, required=True)
    parser.add_argument('--controller-image', default='pdd-jenkins-controller:req009')
    parser.add_argument('--agent-image', default='pdd-jenkins-agent:req002')
    parser.add_argument('--docker-socket', default='/var/run/docker.sock')
    parser.add_argument('--docker-socket-gid', type=int, default=0)
    args = parser.parse_args()
    project = 'pdd-jenkins-it-' + uuid.uuid4().hex[:8]
    work = ROOT / '.state' / project
    work.mkdir(parents=True)
    fixture = work / 'worktree'
    shutil.copytree(args.pipelines, fixture, ignore=shutil.ignore_patterns('.git', '__pycache__', 'reports', '.idea', '.venv', '.state', '.tools'))
    command('git', 'init', '-b', 'main', fixture, stdout=subprocess.DEVNULL)
    command('git', '-C', fixture, 'add', '.')
    command('git', '-C', fixture, '-c', 'user.name=Integration fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'Test fixture', stdout=subprocess.DEVNULL)
    git_root = work / 'git'
    git_root.mkdir()
    bare = git_root / 'pipelines.git'
    command('git', 'clone', '--bare', fixture, bare, stdout=subprocess.DEVNULL)
    command('git', '--git-dir', bare, 'update-server-info')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    settings = manage.load_settings(ROOT / 'settings.example.yaml')
    settings.update(project=project, controller_image=args.controller_image, agent_image=args.agent_image, docker_socket=args.docker_socket, docker_socket_gid=args.docker_socket_gid,
        http_port=port, jenkins_url=f'http://localhost:{port}/', pipeline_repo='http://scm:8000/pipelines.git')
    settings['seed_poll'] = 'H H 1 1 *'
    settings_path = work / 'settings.yaml'
    manage.write_yaml(settings_path, settings)
    state, secrets = work / 'runtime', work / 'secrets'
    cli = [sys.executable, '-m', 'jenkins_config']
    options = ['--settings', settings_path, '--state', state, '--secrets', secrets]
    command(*cli, 'init', *options, cwd=ROOT)
    manage.render(settings, state, secrets)
    (state / 'agent_secret').touch()
    scm_name = project + '-scm'
    client = manage.JenkinsClient(f'http://127.0.0.1:{port}', 'admin', (secrets / 'admin_password').read_text().strip())
    report = {'checks': [], 'container_ids': []}
    pipeline_text = (fixture / 'pipelines/smoke.Jenkinsfile').read_text()
    python_image = re.search(r"image '(python:[^']+)'", pipeline_text).group(1)
    try:
        manage.run_compose(state, 'up', '-d', 'controller')
        command('docker', 'run', '-d', '--name', scm_name, '--network', project + '_default', '--network-alias', 'scm',
            '--entrypoint', 'python3', python_image,
            '-m', 'http.server', '8000', '--directory', '/tmp/git', stdout=subprocess.DEVNULL)
        # Explicit transfer also works when Docker Desktop host sharing snapshots bind mounts.
        command('docker', 'cp', git_root, scm_name + ':/tmp/git')
        command(*cli, 'up', *options, cwd=ROOT)
        root = json.loads(client.request('api/json'))
        assert root['numExecutors'] == 0, root
        assert not json.loads(client.request('computer/docker-agent/api/json'))['offline']
        report['checks'].append('controller isolated and agent online')
        command('docker', 'exec', project + '-agent-1', 'sh', '-ec', 'java -version; ! command -v python3')
        command('docker', 'exec', project + '-agent-1', 'docker', 'version')
        assert set(yaml.safe_load((state / 'compose.yaml').read_text())['services']) == {'controller', 'agent'}
        report['checks'].append('controller and agent only; agent Java and Docker CLI without Python')
        validated = client.request('pipeline-model-converter/validate',
            ('jenkinsfile=' + manage.urllib.parse.quote(pipeline_text)).encode(), 'application/x-www-form-urlencoded')
        assert b'Jenkinsfile successfully validated' in validated, validated.decode()
        report['checks'].append('Declarative Docker Pipeline accepted by installed plugins')
        checked = client.request('configuration-as-code/check', (state / 'casc/jenkins.yaml').read_bytes(), 'application/yaml')
        assert json.loads(checked) == [], checked.decode()
        report['checks'].append('Docker JCasC schema validated by the installed plugin')
        wait_seed(client, 1)
        job = json.loads(client.request('job/pdd/job/integration-test/api/json'))
        assert job['nextBuildNumber'] == 1
        report['checks'].append('initial seed creates sample Job from pipelines repository')
        print('BOOTSTRAP PASSED: single YAML -> Docker controller/agent -> seed -> sample Job', flush=True)
        for number, (scenario, expected, failed) in enumerate([('success', 'SUCCESS', 0), ('unstable', 'UNSTABLE', 1), ('failure', 'FAILURE', 1)], 1):
            result = build(client, scenario, number)
            console = client.request(f'job/pdd/job/integration-test/{number}/consoleText').decode()
            if result['result'] != expected:
                raise AssertionError(f'{scenario}: expected {expected}, got {result["result"]}\n{console}')
            test_report = json.loads(client.request(f'job/pdd/job/integration-test/{number}/testReport/api/json'))
            total = test_report['passCount'] + test_report['failCount'] + test_report['skipCount']
            assert (total, test_report['failCount'], test_report['skipCount']) == (3, failed, 1), test_report
            # PDD consumes the summary action on the Build API, not the TestResult endpoint.
            summary = next(action for action in result['actions'] if 'failCount' in action)
            assert (summary['totalCount'], summary['failCount'], summary['skipCount']) == (3, failed, 1), summary
            assert 'docker-agent' in console
            assert 'withDockerContainer' in console and 'Python 3.13.' in console, console
            ids = re.findall(r'docker top ([a-f0-9]{64})', console)
            assert ids, console
            for container_id in set(ids):
                remaining = subprocess.check_output(['docker', 'ps', '-aq', '--filter', 'id=' + container_id]).strip()
                assert not remaining, f'Plugin did not remove build container {container_id}'
                report['container_ids'].append(container_id)
            report['checks'].append(f'{scenario}: {expected}, JUnit correct, standard Docker container created and removed')
            print(report['checks'][-1], flush=True)
        # Fetch a changed Jenkinsfile without rebuilding any image.
        pipeline = fixture / 'pipelines/smoke.Jenkinsfile'
        pipeline.write_text(pipeline.read_text().replace("stage('Test')", "stage('SCM_REVISION_2')"))
        command('git', '-C', fixture, 'add', '.')
        command('git', '-C', fixture, '-c', 'user.name=Integration fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'Second fixture revision', stdout=subprocess.DEVNULL)
        command('git', '--git-dir', bare, 'fetch', str(fixture), 'main:main', stdout=subprocess.DEVNULL)
        command('git', '--git-dir', bare, 'update-server-info')
        command('docker', 'cp', str(bare) + '/.', scm_name + ':/tmp/git/pipelines.git')
        revision = subprocess.check_output(['git', '--git-dir', str(bare), 'rev-parse', 'main']).strip()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            advertised = subprocess.check_output(['docker', 'exec', project + '-controller-1', 'curl', '-fsS', 'http://scm:8000/pipelines.git/info/refs'])
            if revision in advertised:
                break
            time.sleep(2)
        else:
            raise TimeoutError('Git fixture has not advertised its new revision')
        assert build(client, 'success', 4)['result'] == 'SUCCESS'
        console = client.request('job/pdd/job/integration-test/4/consoleText')
        assert b'SCM_REVISION_2' in console, console.decode()
        report['checks'].append('SCM change picked up without rebuilding images')
        command(*cli, 'reload', *options, cwd=ROOT)
        assert json.loads(client.request('job/pdd/job/integration-test/api/json'))['nextBuildNumber'] == 5
        wait_seed(client, 2)
        report['checks'].append('JCasC reload and seed update retain build history')
        extra = fixture / 'jobs/extra.groovy'
        extra.write_text("pipelineJob('pdd/seed-probe') { definition { cps { script(\"echo 'probe'\"); sandbox() } } }\n")
        publish_fixture(fixture, bare, scm_name)
        client.request('job/seed/build', b'')
        wait_seed(client, 3)
        assert not json.loads(client.request('job/pdd/job/seed-probe/api/json'))['disabled']
        report['checks'].append('new Job added only in pipelines repo is created by seed')
        extra.unlink()
        publish_fixture(fixture, bare, scm_name)
        client.request('job/seed/build', b'')
        wait_seed(client, 4)
        assert json.loads(client.request('job/pdd/job/seed-probe/api/json'))['disabled']
        report['checks'].append('removed definition disables generated Job instead of deleting it')
        manage.run_compose(state, 'up', '-d', '--force-recreate', '--wait', '--wait-timeout', '300', 'controller')
        client.wait()
        assert json.loads(client.request('job/pdd/job/integration-test/1/api/json'))['result'] == 'SUCCESS'
        assert json.loads(client.request('job/pdd/job/integration-test/4/api/json'))['result'] == 'SUCCESS'
        report['checks'].append('container replacement retains credentials, Jobs and build history')
        report['status'] = 'passed'
        print(json.dumps(report, indent=2), flush=True)
        (ROOT / '.state/integration-result.json').write_text(json.dumps(report, indent=2) + '\n')
    except Exception:
        logs = subprocess.check_output(['docker', 'compose', '-f', str(state / 'compose.yaml'), 'logs', '--no-color', 'controller', 'agent'], stderr=subprocess.STDOUT).decode()
        (ROOT / '.state/integration-failure.log').write_text(logs)
        print('\n'.join(line for line in logs.splitlines() if any(word in line for word in ['Exception', 'Caused', 'ERROR', 'SEVERE'])), file=sys.stderr)
        raise
    finally:
        subprocess.run(['docker', 'rm', '-f', '-v', scm_name], stdout=subprocess.DEVNULL, check=False)
        manage.run_compose(state, 'down', '-v')
        shutil.rmtree(work)


if __name__ == '__main__':
    main()
