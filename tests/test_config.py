import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import yaml
from jinja2 import UndefinedError
import helpers
from jenkins_config.operations import DeploymentManager, SecretStore, ComposeClient
from jenkins_config.render import TemplateRenderer
from jenkins_config.settings import Settings


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.values = helpers.load_settings(helpers.ROOT / 'settings.example.yaml')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.secrets = SecretStore(self.root / 'secrets')
        self.renderer = TemplateRenderer()

    def render(self):
        self.renderer.render(Settings.from_mapping(self.values), self.state, self.secrets.directory)
        return (yaml.safe_load((self.state/'compose.yaml').read_text()),
                yaml.safe_load((self.state/'casc/jenkins.yaml').read_text()))

    def test_single_settings_source_and_seed_only(self):
        self.values.update(admin_user='owner',jenkins_url='https://ci.example.org/',controller_image='registry.example.org:5000/org/jenkins:v2.3.4')
        compose,casc=self.render()
        self.assertEqual(compose['services']['controller']['image'],self.values['controller_image'])
        self.assertEqual(casc['unclassified']['location']['url'],self.values['jenkins_url'])
        self.assertEqual(casc['jenkins']['securityRealm']['local']['users'][0]['id'],'owner')
        self.assertEqual(set(compose['services']),{'controller','agent'})
        self.assertEqual(casc['jenkins']['numExecutors'],0)
        self.assertNotIn('clouds',casc['jenkins'])
        script=casc['jobs'][0]['script']
        self.assertIn("pipelineJob('seed')",script)
        self.assertNotIn('integration-test',script)
        self.assertNotIn('PDD_EXECUTION_MODE',script)
        self.assertNotIn('podTemplate',script)

    def test_secrets_remain_references_and_groovy_values_escaped(self):
        self.values.update(git_credentials=True,pipeline_repo="https://git.example.org/it's.git")
        compose,casc=self.render()
        credential=casc['credentials']['system']['domainCredentials'][0]['credentials'][0]['usernamePassword']
        self.assertEqual(credential['password'],'${git_token}')
        self.assertEqual(compose['services']['agent']['secrets'],['agent_secret'])
        self.assertIn('git_token',compose['services']['controller']['secrets'])
        self.assertIn("it\\\\\\'s.git",casc['jobs'][0]['script'])

    def test_compose_escapes_dollar_in_host_paths(self):
        self.state=self.root/'state$literal'
        self.values['docker_socket']='/tmp/$literal.sock'
        compose,_=self.render()
        self.assertIn('$$literal',compose['services']['controller']['volumes'][1]['source'])
        self.assertEqual(compose['services']['agent']['volumes'][1]['source'],'/tmp/$$literal.sock')

    def test_invalid_settings_are_rejected_before_render(self):
        self.render();before=(self.state/'compose.yaml').read_bytes()
        cases=[('http_port',True),('docker_socket_gid',-1),('docker_socket','relative'),('controller_image','org/image:latest'),('controller_image','org/image@sha256:bad'),('pipeline_repo','https://user:secret@example.org/r'),('seed_dsl','../jobs'),('pipeline_branch','${SECRET}'),('kubernetes',{}),('unknown','x')]
        for key,value in cases:
            with self.subTest(key=key):
                settings=copy.deepcopy(self.values);settings[key]=value
                with self.assertRaises(ValueError): Settings.from_mapping(settings)
        self.assertEqual((self.state/'compose.yaml').read_bytes(),before)

    def test_failed_template_does_not_replace_outputs(self):
        self.render();before=(self.state/'compose.yaml').read_bytes()
        templates=self.root/'templates';templates.mkdir()
        (templates/'compose.yaml.j2').write_text('services: {}\n')
        (templates/'casc.yaml.j2').write_text('{{ missing_value }}')
        with self.assertRaises(UndefinedError):
            TemplateRenderer(templates).render(Settings.from_mapping(self.values),self.state,self.secrets.directory)
        self.assertEqual((self.state/'compose.yaml').read_bytes(),before)

    def test_secret_initialization_preserves_existing_value(self):
        self.secrets.initialize();first=self.secrets.read('admin_password')
        self.secrets.initialize();self.assertEqual(self.secrets.read('admin_password'),first)
        self.assertEqual((self.secrets.directory/'admin_password').stat().st_mode & 0o777,0o600)

    def test_invalid_agent_secret_never_starts_agent_or_seed(self):
        self.values.update(controller_image='local/controller:v1',agent_image='local/agent:v1',pipeline_repo='https://example.org/pipelines.git')
        self.secrets.initialize()
        api=Mock();api.wait.side_effect=[b'{}',b'<jnlp><argument>invalid</argument></jnlp>']
        compose=Mock()
        manager=DeploymentManager(Settings.from_mapping(self.values),self.state,self.renderer,compose,self.secrets,api)
        with self.assertRaises(ValueError):manager.up()
        compose.execute.assert_called_once_with('up','-d','--wait','--wait-timeout','300','controller')
        self.assertNotIn(unittest.mock.call('job/seed/build',b''),api.request.call_args_list)

    def test_down_preserves_volumes_and_uses_rendered_compose(self):
        run=Mock();ComposeClient(self.state,run=run).execute('down')
        run.assert_called_once_with(['docker','compose','-f',str(self.state/'compose.yaml'),'down'],check=True)

    def test_yaml_password_is_not_rendered_or_in_repr(self):
        self.values['admin_password'] = 'private-$value:with-quotes!'
        settings = Settings.from_mapping(self.values)
        self.render()
        self.assertNotIn(settings.admin_password, repr(settings))
        for path in self.state.rglob('*.yaml'):
            self.assertNotIn(settings.admin_password, path.read_text())
        for invalid in ('', ' padded ', 123, True, 'two\nlines', '\x00'):
            with self.subTest(value=type(invalid).__name__), self.assertRaises(ValueError):
                Settings.from_mapping({**self.values, 'admin_password': invalid})

    def test_explicit_password_initialization_and_update(self):
        self.secrets.initialize('first-password')
        self.assertEqual(self.secrets.read('admin_password'), 'first-password')
        self.secrets.initialize('second-password')
        self.assertEqual(self.secrets.read('admin_password'), 'first-password')
        self.secrets.set_admin_password('second-password')
        self.assertEqual(self.secrets.read('admin_password'), 'second-password')
        self.assertEqual((self.secrets.directory/'admin_password').stat().st_mode & 0o777, 0o600)

    def test_up_applies_password_before_recreating_controller(self):
        self.values.update(controller_image='local/controller:v1',agent_image='local/agent:v1',pipeline_repo='https://example.org/pipelines.git',admin_password='new-password')
        self.secrets.initialize('old-password')
        api=Mock();api.wait.side_effect=[b'{}',b'<jnlp><argument>'+b'a'*64+b'</argument></jnlp>',b'{"offline":false}']
        compose=Mock()
        def verify(*args):
            self.assertEqual(self.secrets.read('admin_password'),'new-password')
        compose.execute.side_effect=verify
        manager=DeploymentManager(Settings.from_mapping(self.values),self.state,self.renderer,compose,self.secrets,api)
        manager.up()
        self.assertEqual(compose.execute.call_args_list[0],unittest.mock.call('up','-d','--wait','--wait-timeout','300','--force-recreate','controller'))

    def test_reload_rejects_password_change_without_mutation(self):
        self.values['admin_password']='new-password'
        self.secrets.initialize('old-password')
        api=Mock()
        manager=DeploymentManager(Settings.from_mapping(self.values),self.state,self.renderer,Mock(),self.secrets,api)
        with self.assertRaisesRegex(ValueError,'deploy.sh'):manager.reload()
        self.assertEqual(self.secrets.read('admin_password'),'old-password')
        api.request.assert_not_called()
