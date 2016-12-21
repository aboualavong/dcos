import argparse
import base64
import json
import logging
import os
import random
import shutil
import stat
import subprocess
import uuid


import kazoo.exceptions
from kazoo.client import KazooClient
from kazoo.retry import KazooRetry
from kazoo.security import ACL, ANYONE_ID_UNSAFE, Permissions
from kazoo.security import make_acl, make_digest_acl


import gen
from dcos_internal_utils import ca
from dcos_internal_utils import iam
from dcos_internal_utils import utils


log = logging.getLogger(__name__)


ANYONE_CR = [ACL(Permissions.CREATE | Permissions.READ, ANYONE_ID_UNSAFE)]
ANYONE_READ = [ACL(Permissions.READ, ANYONE_ID_UNSAFE)]
ANYONE_ALL = [ACL(Permissions.ALL, ANYONE_ID_UNSAFE)]
LOCALHOST_ALL = [make_acl('ip', '127.0.0.1', all=True)]

vault_config_template = """
disable_mlock = true

backend "zookeeper" {
  address = "127.0.0.1:2181"
  advertise_addr = "%(advertise_addr)s"
  path = "dcos/vault/default"
  %(znode_owner)s
  %(auth_info)s
}

listener "tcp" {
  address = "127.0.0.1:8200"
  tls_disable = 1
}
"""


class Bootstrapper(object):
    def __init__(self, opts):
        self.opts = opts

        zk_creds = None
        if opts.zk_super_creds:
            log.info("Using super credentials for Zookeeper")
            zk_creds = opts.zk_super_creds
        elif opts.zk_agent_creds:
            log.info("Using agent credentials for Zookeeper")
            zk_creds = opts.zk_agent_creds

        conn_retry_policy = KazooRetry(max_tries=-1, delay=0.1, max_delay=0.1)
        cmd_retry_policy = KazooRetry(max_tries=3, delay=0.3, backoff=1, max_delay=1, ignore_expire=False)
        zk = KazooClient(hosts=opts.zk, connection_retry=conn_retry_policy, command_retry=cmd_retry_policy)
        zk.start()
        if zk_creds:
            zk.add_auth('digest', zk_creds)
        self.zk = zk

        self.iam_url = opts.iam_url
        self.ca_url = opts.ca_url
        self.secrets = {}

        self.CA_certificate = None
        self.CA_certificate_filename = None

        self.agent_services = [
            'dcos_3dt_agent',
            'dcos_adminrouter_agent',
            'dcos_agent',
            'dcos_log_agent',
            'dcos_mesos_agent',
            'dcos_mesos_agent_public',
            'dcos_metrics_agent',
            'dcos_minuteman_agent',
            'dcos_navstar_agent',
            'dcos_spartan_agent'
        ]

    def close(self):
        self.zk.stop()
        self.zk.close()

    def __enter__(self):
        return self

    def __exit__(self, type, value, tb):
        self.close()

    def cluster_id(self, path='/var/lib/dcos/cluster-id', readonly=False):
        dirpath = os.path.dirname(os.path.abspath(path))
        log.info('Opening {} for locking'.format(dirpath))
        with utils.Directory(dirpath) as d:
            log.info('Taking exclusive lock on {}'.format(dirpath))
            with d.lock():
                if readonly:
                    zkid = None
                else:
                    zkid = str(uuid.uuid4()).encode('ascii')
                zkid = self._consensus('/cluster-id', zkid, ANYONE_READ)
                zkid = zkid.decode('ascii')

                if os.path.exists(path):
                    fileid = utils.read_file_line(path)
                    if fileid == zkid:
                        log.info('Cluster ID in ZooKeeper and file are the same: {}'.format(zkid))
                        return zkid

                log.info('Writing cluster ID from ZK to {} via rename'.format(path))

                tmppath = path + '.tmp'
                with open(tmppath, 'w') as f:
                    f.write(zkid + '\n')
                os.rename(tmppath, path)

                log.info('Wrote cluster ID to {}'.format(path))

                return zkid

    def init_zk_acls(self):
        if not self.opts.config['zk_acls_enabled']:
            return

        paths = {
            '/': ANYONE_CR + LOCALHOST_ALL,
            '/cosmos': ANYONE_ALL,
            '/dcos': ANYONE_READ,
            '/dcos/vault': ANYONE_READ,
            '/zookeeper': ANYONE_READ,
            '/zookeeper/quotas': ANYONE_READ,
        }
        for path in sorted(paths):
            log.info('Initializing ACLs for znode {}'.format(path))
            acl = paths[path]
            self.ensure_zk_path(path, acl=acl)

    def _create_secrets(self, basepath, secrets, acl):
        for k, v in secrets.items():
            leaf = True
            for vv in v.values():
                if isinstance(vv, dict):
                    leaf = False
                    break

            if not leaf:
                path = '/'.join([basepath, k])
                self._create_secrets(path, v, acl)
                continue

            self.ensure_zk_path(basepath, acl=acl)

            path = '/'.join([basepath, k])
            js = bytes(json.dumps(v), 'ascii')
            js = self._consensus(path, js, acl)
            secrets[k] = json.loads(js.decode('ascii'))

            # set ACLs again in case znode already existed but with outdated ACLs
            if acl:
                self.zk.set_acls(path, acl)

        return secrets

    def write_CA_key(self, filename):
        key = self.secrets['CA']['RootCA']['key']
        key = key.encode('ascii')
        log.info('Writing root CA key to {}'.format(filename))
        _write_file(filename, key, 0o600)
        return key

    def write_CA_certificate(self, filename='/run/dcos/pki/CA/certs/ca.crt'):
        """"
        CA_certificate on the masters will happen after
        consensus has been reached about the master secrets,
        which include the root CA key and certificate
        """
        if 'CA' in self.secrets:
            crt = self.secrets['CA']['RootCA']['certificate']
            crt = crt.encode('ascii')
        else:
            # consensus value will only be read
            crt = None

        crt = self._consensus('/dcos/RootCA', crt, ANYONE_READ)

        log.info('Writing root CA certificate to {}'.format(filename))
        _write_file(filename, crt, 0o644)

        self.CA_certificate = crt
        self.CA_certificate_filename = filename

        return crt

    def create_master_secrets(self):
        creds = self.opts.zk_master_creds

        if creds:
            user, password = creds.split(':', 1)
            acl = [make_digest_acl(user, password, read=True)]
            log.info('Creating master secrets with user {}'.format(user))
        else:
            acl = None

        zk_creds = {}
        if self.opts.config['zk_acls_enabled']:
            service_account_zk_creds = [
                'dcos_bouncer',
                'dcos_ca',
                'dcos_cosmos',
                'dcos_marathon',
                'dcos_mesos_master',
                'dcos_metronome',
                'dcos_secrets',
                'dcos_vault_default'
            ]
            for account in service_account_zk_creds:
                zk_creds[account] = {
                    'scheme': 'digest',
                    'username': account,
                    'password': utils.random_string(64),
                }

        master_service_accounts = [
            'dcos_3dt_master',
            'dcos_adminrouter',
            'dcos_history_service',
            'dcos_log_master',
            'dcos_marathon',
            'dcos_mesos_dns',
            'dcos_metrics_master',
            'dcos_metronome',
            'dcos_minuteman_master',
            'dcos_navstar_master',
            'dcos_networking_api_master',
            'dcos_signal_service',
            'dcos_spartan_master'
        ]

        if self.opts.config['security'] == 'permissive':
            master_service_accounts.append('dcos_anonymous')

        service_account_creds = {}
        for account in master_service_accounts:
            service_account_creds[account] = {
                'scheme': 'RS256',
                'uid': account,
                'private_key': utils.generate_RSA_keypair(2048)[0],
            }

        # always generate the CA cert, regardless of whether
        # SSL is being used in the cluster
        ca_key, ca_crt = utils.generate_CA_key_certificate(3650)
        ca_certs = {
            'RootCA': {
                'key': ca_key,
                'certificate': ca_crt,
            }
        }

        private_keys = {
            'dcos_bouncer': utils.generate_RSA_keypair(2048)[0]
        }

        secrets = {
            'zk': zk_creds,
            'services': service_account_creds,
            'CA': ca_certs,
            'private_keys': private_keys
        }

        path = '/dcos/master/secrets'
        secrets = self._create_secrets(path, secrets, acl)
        utils.dict_merge(self.secrets, secrets)
        return secrets

    def create_agent_secrets(self, digest):
        if self.opts.config['zk_acls_enabled']:
            # kazoo.exceptions.MarshallingError here probably means
            # that digest is None
            acl = [make_acl('digest', digest, read=True)]
        else:
            acl = None

        service_account_creds = {}
        for account in self.agent_services:
            service_account_creds[account] = {
                'scheme': 'RS256',
                'uid': account,
                'private_key': utils.generate_RSA_keypair(2048)[0]}

        secrets = {
            'services': service_account_creds,
        }

        path = '/dcos/agent/secrets'
        secrets = self._create_secrets(path, secrets, acl)
        utils.dict_merge(self.secrets, secrets)
        return secrets

    def read_agent_secrets(self):
        self.secrets['services'] = {}

        for svc in self.agent_services:
            path = '/dcos/agent/secrets/services/' + svc
            js = self._consensus(path, None)
            self.secrets['services'][svc] = json.loads(js.decode('ascii'))

        return self.secrets

    def read_3dt_agent_secrets(self):
        path = '/dcos/agent/secrets/services/dcos_3dt_agent'
        js = self._consensus(path, None)
        self.secrets['services'] = {
            'dcos_3dt_agent': json.loads(js.decode('ascii'))
        }
        return self.secrets

    def read_dcos_log_secrets(self):
        path = '/dcos/agent/secrets/services/dcos_log_agent'
        js = self._consensus(path, None)
        self.secrets['services'] = {
            'dcos_log_agent': json.loads(js.decode('ascii'))
        }
        return self.secrets

    def write_service_account_credentials(self, uid, filename):
        creds = self.secrets['services'][uid].copy()
        creds['login_endpoint'] = self.iam_url + '/acs/api/v1/auth/login'
        creds = bytes(json.dumps(creds), 'ascii')

        log.info('Writing {} service account credentials to {}'.format(uid, filename))
        # credentials file that service can read, but not overwrite
        _write_file(filename, creds, 0o400)

    def write_private_key(self, name, filename):
        private_key = self.secrets['private_keys'][name]
        private_key = bytes(private_key, 'ascii')
        log.info('Writing {} private key to {}'.format(name, filename))
        # private key that service can read, but not overwrite
        _write_file(filename, private_key, 0o400)

    def create_service_account(self, uid, superuser, zk_secret=True):
        if zk_secret:
            account = self.secrets['services'][uid]
        else:
            account = {
                'scheme': 'RS256',
                'uid': uid,
                'private_key': utils.generate_RSA_keypair(2048)[0]
            }
        assert uid == account['uid']
        assert account['scheme'] == 'RS256'

        log.info('Creating service account {}'.format(uid))

        private_key = utils.load_pem_private_key(account['private_key'])
        pubkey_pem = utils.public_key_pem(private_key)
        account['public_key'] = pubkey_pem

        iamcli = iam.IAMClient(self.iam_url, self.CA_certificate_filename)
        iamcli.create_service_account(uid, public_key=pubkey_pem, exist_ok=True)

        # TODO fine-grained permissions for all service accounts
        if superuser:
            iamcli.add_user_to_group(uid, 'superusers')

        return account

    def create_agent_service_accounts(self):
        for svc in self.agent_services:
            self.create_service_account(svc, superuser=True)

    def _consensus(self, path, value, acl=None):
        if value is not None:
            log.info('Reaching consensus about znode {}'.format(path))
            try:
                self.zk.create(path, value, acl=acl)
                log.info('Consensus znode {} created'.format(path))
            except kazoo.exceptions.NodeExistsError:
                log.info('Consensus znode {} already exists'.format(path))
                pass

        self.zk.sync(path)
        return self.zk.get(path)[0]

    def make_service_acl(self, service, **kwargs):
        u = self.secrets['zk'][service]['username']
        p = self.secrets['zk'][service]['password']
        return make_digest_acl(u, p, **kwargs)

    def ensure_zk_path(self, path, acl=None):
        log.info('ensure_zk_path({}, {})'.format(path, acl))
        self.zk.ensure_path(path, acl=acl)
        if acl:
            self.zk.set_acls(path, acl)

    def mesos_zk_acls(self):
        acl = None
        if self.opts.config['zk_acls_enabled']:
            acl = ANYONE_READ + LOCALHOST_ALL + [self.make_service_acl('dcos_mesos_master', all=True)]
        self.ensure_zk_path('/mesos', acl=acl)

    def marathon_zk_acls(self):
        acl = None
        if self.opts.config['zk_acls_enabled']:
            acl = ANYONE_READ + LOCALHOST_ALL + [self.make_service_acl('dcos_marathon', all=True)]
        self.ensure_zk_path('/marathon', acl=acl)

    def marathon_iam_acls(self):
        if self.opts.config['security'] == 'permissive':
            permissive_acls = [
                ('dcos:mesos:master:framework', 'create'),
                ('dcos:mesos:master:reservation', 'create'),
                ('dcos:mesos:master:reservation', 'delete'),
                ('dcos:mesos:master:task', 'create'),
                ('dcos:mesos:master:volume', 'create'),
                ('dcos:mesos:master:volume', 'delete'),
            ]

            iamcli = iam.IAMClient(self.iam_url, self.CA_certificate_filename)
            iamcli.create_acls(permissive_acls, 'dcos_marathon')

        elif self.opts.config['security'] == 'strict':
            # Can only register with 'slave_public' role,
            # only create volumes/reservations in that role,
            # only destroy volumes/reservations created by 'dcos_marathon',
            # only run tasks as linux user 'nobody',
            # but can create apps in any folder/namespace.
            strict_acls = [
                ('dcos:mesos:master:framework:role:slave_public', 'create'),
                ('dcos:mesos:master:reservation:role:slave_public', 'create'),
                ('dcos:mesos:master:reservation:principal:dcos_marathon', 'delete'),
                ('dcos:mesos:master:task:user:nobody', 'create'),
                ('dcos:mesos:master:task:app_id', 'create'),
                ('dcos:mesos:master:volume:principal:dcos_marathon', 'delete'),
                ('dcos:mesos:master:volume:role:slave_public', 'create')
            ]

            iamcli = iam.IAMClient(self.iam_url, self.CA_certificate_filename)
            iamcli.create_acls(strict_acls, 'dcos_marathon')

    def metronome_zk_acls(self):
        acl = None
        if self.opts.config['zk_acls_enabled']:
            acl = ANYONE_READ + LOCALHOST_ALL + [self.make_service_acl('dcos_metronome', all=True)]
        self.ensure_zk_path('/metronome', acl=acl)

    def metronome_iam_acls(self):
        if self.opts.config['security'] == 'permissive':
            permissive_acls = [
                ('dcos:mesos:master:framework', 'create'),
                ('dcos:mesos:master:task', 'create')]

            iamcli = iam.IAMClient(self.iam_url, self.CA_certificate_filename)
            iamcli.create_acls(permissive_acls, 'dcos_metronome')

        elif self.opts.config['security'] == 'strict':
            # Can only register with '*' role,
            # only run tasks as linux user 'nobody',
            # but can create jobs in any folder/namespace.
            strict_acls = [
                ('dcos:mesos:master:framework:role:*', 'create'),
                ('dcos:mesos:master:task:app_id', 'create'),
                ('dcos:mesos:master:task:user:nobody', 'create')
            ]

            iamcli = iam.IAMClient(self.iam_url, self.CA_certificate_filename)
            iamcli.create_acls(strict_acls, 'dcos_metronome')

    def cosmos_acls(self):
        acl = None
        if self.opts.config['zk_acls_enabled']:
            acl = ANYONE_READ + LOCALHOST_ALL + [self.make_service_acl('dcos_cosmos', all=True)]
        self.ensure_zk_path('/cosmos', acl=acl)

    def bouncer_acls(self):
        acl = None
        if self.opts.config['zk_acls_enabled']:
            acl = LOCALHOST_ALL + [self.make_service_acl('dcos_bouncer', all=True)]
        self.ensure_zk_path('/bouncer', acl=acl)

    def dcos_ca_acls(self):
        acl = None
        if self.opts.config['zk_acls_enabled']:
            acl = LOCALHOST_ALL + [self.make_service_acl('dcos_ca', all=True)]
        self.ensure_zk_path('/dcos/ca', acl=acl)

    def dcos_secrets_acls(self):
        acl = None
        if self.opts.config['zk_acls_enabled']:
            acl = LOCALHOST_ALL + [self.make_service_acl('dcos_secrets', all=True)]
        self.ensure_zk_path('/dcos/secrets', acl=acl)

    def dcos_vault_default_acls(self):
        acl = None
        if self.opts.config['zk_acls_enabled']:
            acl = LOCALHOST_ALL + [self.make_service_acl('dcos_vault_default', all=True)]
        self.ensure_zk_path('/dcos/vault/default', acl=acl)

    def write_dcos_ca_creds(self, src, dst):
        with open(src, 'rb') as fh:
            ca_conf = json.loads(fh.read().decode('utf-8'))
        assert 'data_source' in ca_conf
        assert ca_conf['data_source'][:5] == 'file:'

        if self.opts.config['zk_acls_enabled']:
            zk_creds = self.secrets['zk']['dcos_ca']
            ca_conf['data_source'] = 'file:{}:{}@{}'.format(
                zk_creds['username'],
                zk_creds['password'],
                ca_conf['data_source'][5:]
            )

        blob = json.dumps(ca_conf, sort_keys=True, indent=True, ensure_ascii=False).encode('utf-8')
        _write_file(dst, blob, 0o400)
        shutil.chown(dst, user=self.opts.dcos_ca_user)

    def write_bouncer_env(self, filename):
        if not self.opts.config['zk_acls_enabled']:
            return

        zk_creds = self.secrets['zk']['dcos_bouncer']

        env = 'DATASTORE_ZK_USER={username}\nDATASTORE_ZK_SECRET={password}\n'
        env = bytes(env.format_map(zk_creds), 'ascii')

        log.info('Writing Bouncer ZK credentials to {}'.format(filename))
        _write_file(filename, env, 0o600)

    def write_vault_config(self, filename):
        if self.opts.config['zk_acls_enabled']:
            zk_creds = self.secrets['zk']['dcos_vault_default']
            user = zk_creds['username']
            pw = zk_creds['password']
            acl = make_digest_acl(user, pw, all=True)
            znode_owner = 'znode_owner = "digest:{}"'.format(acl.id.id)
            auth_info = 'auth_info = "digest:{}:{}"'.format(user, pw)
        else:
            znode_owner = ''
            auth_info = ''

        if self.opts.config['ssl_enabled']:
            scheme = 'https://'
        else:
            scheme = 'http://'

        ip = utils.detect_ip()
        advertise_addr = scheme + ip + '/vault/default'

        params = {
            'znode_owner': znode_owner,
            'auth_info': auth_info,
            'advertise_addr': advertise_addr,
        }
        cfg = vault_config_template % params
        cfg = cfg.strip() + '\n'
        cfg = cfg.encode('ascii')

        log.info('Writing Vault config to {}'.format(filename))
        _write_file(filename, cfg, 0o400)
        shutil.chown(filename, user=self.opts.dcos_vault_user)

    def write_secrets_env(self, filename):
        if not self.opts.config['zk_acls_enabled']:
            return

        zk_creds = self.secrets['zk']['dcos_secrets']
        user = zk_creds['username']
        pw = zk_creds['password']

        acl = make_digest_acl(user, pw, all=True)

        env = 'SECRETS_AUTH_INFO=digest:{}:{}\nSECRETS_ZNODE_OWNER=digest:{}\n'
        env = env.format(user, pw, acl.id.id)
        env = bytes(env, 'ascii')

        log.info('Writing Secrets ZK credentials to {}'.format(filename))
        _write_file(filename, env, 0o600)

    def write_mesos_master_env(self, filename):
        if not self.opts.config['zk_acls_enabled']:
            return

        zk_creds = self.secrets['zk']['dcos_mesos_master']

        env = 'MESOS_ZK=zk://{username}:{password}@127.0.0.1:2181/mesos\n'
        env = env.format_map(zk_creds)
        env = bytes(env, 'ascii')

        log.info('Writing Mesos Master ZK credentials to {}'.format(filename))
        _write_file(filename, env, 0o600)

    def write_cosmos_env(self, key_fn, crt_fn, ca_fn, env_fn):
        if not self.opts.config['zk_acls_enabled']:
            return

        zk_creds = self.secrets['zk']['dcos_cosmos']
        env = 'ZOOKEEPER_USER={username}\nZOOKEEPER_SECRET={password}\n'
        env = env.format_map(zk_creds)
        env = bytes(env, 'ascii')

        log.info('Writing Cosmos environment to {}'.format(env_fn))
        # environment file is owned by root because systemd reads it
        _write_file(env_fn, env, 0o600)

    def write_metronome_env(self, key_fn, crt_fn, ca_fn, env_fn):
        pfx_fn = os.path.splitext(key_fn)[0] + '.pfx'
        jks_fn = os.path.splitext(key_fn)[0] + '.jks'

        try:
            os.remove(jks_fn)
        except OSError:
            pass

        zk_creds = self.secrets['zk']['dcos_metronome']
        env1 = 'METRONOME_ZK_URL=zk://{username}:{password}@127.0.0.1:2181/metronome\n'
        env1 = env1.format_map(zk_creds)

        keystore_password = utils.random_string(64)
        env2 = 'METRONOME_PLAY_SERVER_HTTPS_KEYSTORE_PASSWORD={keystore_password}\n'
        env2 = env2.format(keystore_password=keystore_password)

        env = bytes(env1 + env2, 'ascii')

        log.info('Writing Metronome environment to {}'.format(env_fn))
        _write_file(env_fn, env, 0o600)

        service_name = 'metronome'

        cmd = [
            '/opt/mesosphere/bin/openssl',
            'pkcs12',
            '-export',
            '-out', pfx_fn,
            '-inkey', key_fn,
            '-in', crt_fn,
            '-chain',
            '-CAfile', ca_fn,
            '-name', service_name,
            '-password', 'env:SSL_KEYSTORE_PASSWORD',
        ]
        log.info('Converting PEM to PKCS12: {}'.format(' '.join(cmd)))
        env = {
            'SSL_KEYSTORE_PASSWORD': keystore_password,
            'RANDFILE': '/tmp/.rnd',
        }

        subprocess.check_call(cmd, preexec_fn=_set_umask, env=env)

        os.chmod(pfx_fn, stat.S_IRUSR | stat.S_IWUSR)

        cmd = [
            '/opt/mesosphere/bin/keytool',
            '-importkeystore',
            '-noprompt',
            '-srcalias', service_name,
            '-srckeystore', pfx_fn,
            '-srcstoretype', 'PKCS12',
            '-destkeystore', jks_fn,
            '-srcstorepass', keystore_password,
            '-deststorepass', keystore_password,
        ]
        log.info('Importing PKCS12 into Java KeyStore: {}'.format(' '.join(cmd)))
        proc = subprocess.Popen(cmd, shell=False, preexec_fn=_set_umask)
        if proc.wait() != 0:
            raise Exception('keytool failed')

        os.chmod(jks_fn, stat.S_IRUSR | stat.S_IWUSR)
        os.remove(pfx_fn)

    def write_marathon_zk_env(self, env_fn):
        zk_creds = self.secrets['zk']['dcos_marathon']
        env = 'MARATHON_ZK=zk://{username}:{password}@127.0.0.1:2181/marathon\n'
        env = env.format_map(zk_creds)
        env = bytes(env, 'ascii')

        log.info('Writing Marathon ZK environment to {}'.format(env_fn))
        _write_file(env_fn, env, 0o600)

    def write_marathon_tls_env(self, key_fn, crt_fn, ca_fn, env_fn):
        pfx_fn = os.path.splitext(key_fn)[0] + '.pfx'
        jks_fn = os.path.splitext(key_fn)[0] + '.jks'

        try:
            os.remove(jks_fn)
        except OSError:
            pass

        password = utils.random_string(256)
        env = 'SSL_KEYSTORE_PASSWORD={}\n'.format(password)
        env = bytes(env, 'ascii')

        _write_file(env_fn, env, 0o600)

        service_name = 'marathon'

        cmd = [
            '/opt/mesosphere/bin/openssl',
            'pkcs12',
            '-export',
            '-out', pfx_fn,
            '-inkey', key_fn,
            '-in', crt_fn,
            '-chain',
            '-CAfile', ca_fn,
            '-name', service_name,
            '-password', 'env:SSL_KEYSTORE_PASSWORD',
        ]
        log.info('Converting PEM to PKCS12: {}'.format(' '.join(cmd)))
        env = {
            'SSL_KEYSTORE_PASSWORD': password,
            'RANDFILE': '/tmp/.rnd',
        }
        proc = subprocess.Popen(cmd, shell=False, preexec_fn=_set_umask, env=env)
        if proc.wait() != 0:
            raise Exception('openssl failed')

        keytool = shutil.which('keytool')
        if not keytool:
            raise Exception('keytool not found')

        # TODO this will temporarily expose the password during bootstrap
        cmd = [
            keytool,
            '-importkeystore',
            '-noprompt',
            '-srcalias', service_name,
            '-srckeystore', pfx_fn,
            '-srcstoretype', 'PKCS12',
            '-destkeystore', jks_fn,
            '-srcstorepass', password,
            '-deststorepass', password,
        ]
        log.info('Importing PKCS12 into Java KeyStore: {}'.format(' '.join(cmd)))
        subprocess.check_call(cmd, preexec_fn=_set_umask)
        os.remove(pfx_fn)

    def write_truststore(self, ts_fn, ca_fn):
        keytool = shutil.which('keytool')
        if not keytool:
            raise Exception('keytool not found')

        try:
            os.remove(ts_fn)
            log.info("Removed existing TrustStore file: %s", ts_fn)
        except FileNotFoundError:
            log.info("TrustStore file does not yet exist: %s", ts_fn)

        cmd = [
            keytool,
            '-importkeystore',
            '-noprompt',
            '-srckeystore',
            '/opt/mesosphere/active/java/usr/java/jre/lib/security/cacerts',
            '-srcstorepass', 'changeit',
            '-deststorepass', 'changeit',
            '-destkeystore', ts_fn
        ]

        log.info('Copying system TrustStore: {}'.format(' '.join(cmd)))
        proc = subprocess.Popen(cmd, shell=False, preexec_fn=_set_umask)
        if proc.wait() != 0:
            raise Exception('keytool failed')

        cmd = [
            keytool,
            '-import',
            '-noprompt',
            '-trustcacerts',
            '-alias', 'dcos_root_ca',
            '-file', ca_fn,
            '-keystore', ts_fn,
            '-storepass', 'changeit',
        ]
        log.info('Importing CA into TrustStore: {}'.format(' '.join(cmd)))
        proc = subprocess.Popen(cmd, shell=False, preexec_fn=_set_umask)
        if proc.wait() != 0:
            raise Exception('keytool failed')

        os.chmod(ts_fn, 0o644)

    def service_auth_token(self, uid, exp=None):
        iam_cli = iam.IAMClient(self.iam_url, self.CA_certificate_filename)
        acc = self.secrets['services'][uid]
        log.info('Service account login as service {}'.format(uid))
        token = iam_cli.service_account_login(uid, private_key=acc['private_key'], exp=exp)
        return token

    def write_service_auth_token(self, uid, filename=None, exp=None):
        """Create service authentication token for given `uid` and `exp`.

        Create and return an environment variable declaration string
        of the format

            SERVICE_AUTH_TOKEN=<authtoken>\n

        If `filename` is given, write the environment variable declaration
        string to that file path.

        Returns:
            bytes: environment variable declaration
        """
        token = self.service_auth_token(uid, exp)
        env = bytes('SERVICE_AUTH_TOKEN={}\n'.format(token), 'ascii')
        if filename is not None:
            _write_file(filename, env, 0o600)
        return env

    def create_key_certificate(self, cn, key_filename, crt_filename,
                               service_account=None, master=False,
                               marathon=False, extra_san=None,
                               key_mode=0o600):
        log.info('Generating CSR for key {}'.format(key_filename))
        privkey_pem, csr_pem = utils.generate_key_CSR(cn,
                                                      master=master,
                                                      marathon=marathon,
                                                      extra_san=extra_san)

        headers = {}
        if service_account:
            token = self.service_auth_token(service_account)
            headers = {'Authorization': 'token=' + token}
        cacli = ca.CAClient(self.ca_url, headers, self.CA_certificate_filename)

        msg_fmt = 'Signing CSR at {} with service account {}'
        log.info(msg_fmt.format(self.ca_url, service_account))
        crt = cacli.sign(csr_pem)

        _write_file(key_filename, bytes(privkey_pem, 'ascii'), key_mode)
        _write_file(crt_filename, bytes(crt, 'ascii'), 0o644)

    def _key_cert_is_valid(self, key_filename, crt_filename):
        try:
            with open(crt_filename) as fh:
                crt = fh.read()
        except FileNotFoundError:
            log.warn('Certificate was not found')
            return False
        if 'BEGIN CERTIFICATE' not in crt:
            log.warn('Certificate is invalid')
            return False
        # Certificate validity (expiration, issuing CA, etc.) is not checked.
        # An administrator wishing to rotate a certificate should remove the
        # old certificate and key and restart the service.
        try:
            with open(key_filename) as fh:
                key = fh.read()
        except FileNotFoundError:
            log.warn('Private key was not found')
            return False
        if 'PRIVATE KEY' not in key:
            log.warn('Private key is invalid')
            return False

        return True

    def ensure_key_certificate(
            self, cn, key_filename, crt_filename, service_account=None,
            master=False, marathon=False, extra_san=None, key_mode=0o600):
        if not self._key_cert_is_valid(key_filename, crt_filename):
            log.info('Generating certificate {}'.format(crt_filename))
            self.create_key_certificate(cn, key_filename, crt_filename,
                                        service_account, master, marathon,
                                        extra_san, key_mode)
        else:
            log.debug('Certificate {} already exists'.format(crt_filename))

    def write_jwks_public_keys(self, filename):
        iamcli = iam.IAMClient(self.iam_url, self.CA_certificate_filename)
        jwks = iamcli.jwks()
        output = utils.jwks_to_public_keys(jwks)
        _write_file(filename, bytes(output, 'ascii'), 0o644)


def _write_file(path, data, mode):
    dirpath = os.path.dirname(os.path.abspath(path))
    with utils.Directory(dirpath) as d:
        with d.lock():
            umask_original = os.umask(0)
            try:
                flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                log.info('Writing {} with mode {:o}'.format(path, mode))
                with os.fdopen(os.open(path, flags, mode), 'wb') as f:
                    f.write(data)
            finally:
                os.umask(umask_original)


def _set_umask():
    os.setpgrp()
    # prevent other users from reading files created by this process
    os.umask(0o077)


def parse_args():
    parser = argparse.ArgumentParser()

    parser.add_argument('services', nargs='+')
    parser.add_argument(
        '--rundir',
        default='/run/dcos',
        help='Runtime directory')
    parser.add_argument(
        '--statedir',
        default='/var/lib/dcos',
        help='State direcotry')
    parser.add_argument(
        '--zk',
        default=None,
        help='Host string passed to Kazoo client constructor.')
    parser.add_argument(
        '--zk_super_creds',
        default='/opt/mesosphere/etc/zk_super_credentials',
        help='File with ZooKeeper super credentials')
    parser.add_argument(
        '--zk_master_creds',
        default='/opt/mesosphere/etc/zk_master_credentials',
        help='File with ZooKeeper master credentials')
    parser.add_argument(
        '--zk_agent_creds',
        default='/opt/mesosphere/etc/zk_agent_credentials',
        help='File with ZooKeeper agent credentials')
    parser.add_argument(
        '--zk_agent_digest',
        default='/opt/mesosphere/etc/zk_agent_digest',
        help='File with ZooKeeper agent digest')
    parser.add_argument(
        '--master_count',
        default='/opt/mesosphere/etc/master_count',
        help='File with number of master servers')
    parser.add_argument(
        '--iam_url',
        default=None,
        help='IAM Service (Bouncer) URL')
    parser.add_argument(
        '--ca_url',
        default=None,
        help='CA URL')
    parser.add_argument(
        '--config-path',
        default='/opt/mesosphere/etc/bootstrap-config.json',
        help='Path to config file for bootstrap')

    opts = parser.parse_args()

    with open(opts.config_path, 'rb') as f:
        opts.config = json.loads(f.read().decode('ascii'))

    opts.bouncer_user = 'dcos_bouncer'
    opts.dcos_secrets_user = 'dcos_secrets'
    opts.dcos_vault_user = 'dcos_vault'
    opts.dcos_ca_user = 'dcos_ca'
    opts.dcos_cosmos_user = 'dcos_cosmos'

    def _verify_and_set_zk_creds(credentials_path, credentials_type=None):
        if os.path.exists(credentials_path):
            log.info('Reading {credentials_type} credentials from {credentials_path}'.format(
                credentials_type=credentials_type, credentials_path=credentials_path))
            return utils.read_file_line(credentials_path)
        log.info('{credentials_type} credentials not available'.format(credentials_type=credentials_type))
        return None

    if opts.config['security'] == 'disabled':
        opts.zk_super_creds = None
        opts.zk_master_creds = None
        opts.zk_agent_creds = None
        opts.zk_agent_digest = None
    else:
        opts.zk_super_creds = _verify_and_set_zk_creds(opts.zk_super_creds, "ZooKeeper super")
        opts.zk_master_creds = _verify_and_set_zk_creds(opts.zk_master_creds, "ZooKeeper master")
        opts.zk_agent_creds = _verify_and_set_zk_creds(opts.zk_agent_creds, "ZooKeeper agent")
        opts.zk_agent_digest = _verify_and_set_zk_creds(opts.zk_agent_digest, "ZooKeeper agent digest")

    if os.path.exists('/opt/mesosphere/etc/roles/master'):
        zk_default = '127.0.0.1:2181'
        iam_default = 'http://127.0.0.1:8101'
        ca_default = 'http://127.0.0.1:8888'
    else:
        if os.getenv('MASTER_SOURCE') == 'master_list':
            # Spartan agents with static master list
            with open('/opt/mesosphere/etc/master_list', 'r') as f:
                master_list = json.load(f)
            assert len(master_list) > 0
            leader = random.choice(master_list)
        elif os.getenv('EXHIBITOR_ADDRESS'):
            # Spartan agents on AWS
            leader = os.getenv('EXHIBITOR_ADDRESS')
        else:
            # any other agent service
            leader = 'leader.mesos'

        zk_default = leader + ':2181'
        if opts.config['ssl_enabled']:
            iam_default = 'https://' + leader
            ca_default = 'https://' + leader
        else:
            iam_default = 'http://' + leader
            ca_default = 'http://' + leader

    if not opts.zk:
        opts.zk = zk_default
    if not opts.iam_url:
        opts.iam_url = iam_default
    if not opts.ca_url:
        opts.ca_url = ca_default

    return opts


def make_run_dirs(opts):
    dirs = [
        opts.rundir,
        opts.rundir + '/etc',
        opts.rundir + '/etc/3dt',
        opts.rundir + '/etc/dcos-ca',
        opts.rundir + '/etc/dcos-log',
        opts.rundir + '/etc/dcos-metrics',
        opts.rundir + '/etc/history-service',
        opts.rundir + '/etc/marathon',
        opts.rundir + '/etc/mesos',
        opts.rundir + '/etc/mesos-dns',
        opts.rundir + '/etc/metronome',
        opts.rundir + '/etc/signal-service',
        opts.rundir + '/pki/CA/certs',
        opts.rundir + '/pki/CA/private',
        opts.rundir + '/pki/tls/certs',
        opts.rundir + '/pki/tls/private'
    ]

    for d in dirs:
        log.info('Preparing directory {}'.format(d))
        os.makedirs(d, exist_ok=True)


def dcos_bouncer(b, opts):
    b.init_zk_acls()
    b.create_master_secrets()
    b.bouncer_acls()

    keypath = opts.rundir + '/pki/tls/private/bouncer.key'
    b.write_private_key('dcos_bouncer', keypath)
    shutil.chown(keypath, user=opts.bouncer_user)

    path = opts.rundir + '/etc/bouncer'
    b.write_bouncer_env(path)


def dcos_secrets(b, opts):
    b.init_zk_acls()
    b.create_master_secrets()
    b.dcos_secrets_acls()

    if opts.config['ssl_enabled']:
        keypath = opts.rundir + '/pki/tls/private/dcos-secrets.key'
        crtpath = opts.rundir + '/pki/tls/certs/dcos-secrets.crt'
        b.ensure_key_certificate('Secrets', keypath, crtpath, master=True)
        shutil.chown(keypath, user=opts.dcos_secrets_user)
        shutil.chown(crtpath, user=opts.dcos_secrets_user)

    path = opts.rundir + '/etc/dcos-secrets.env'
    b.write_secrets_env(path)

    secrets_dir = opts.statedir + '/secrets'
    try:
        os.makedirs(secrets_dir)
    except FileExistsError:
        pass
    shutil.chown(secrets_dir, user=opts.dcos_secrets_user)


def dcos_vault_default(b, opts):
    b.init_zk_acls()
    b.create_master_secrets()
    b.dcos_vault_default_acls()

    vault_dir = opts.statedir + '/secrets/vault'
    try:
        os.makedirs(vault_dir, exist_ok=True)
    except FileExistsError:
        pass

    vault_default_dir = opts.statedir + '/secrets/vault/default'
    try:
        os.makedirs(vault_default_dir, exist_ok=True)
    except FileExistsError:
        pass
    # secrets writes keys into this directory
    shutil.chown(vault_default_dir, user=opts.dcos_secrets_user)

    hcl = opts.rundir + '/etc/vault.hcl'
    b.write_vault_config(hcl)


def dcos_ca(b, opts):
    b.init_zk_acls()
    b.create_master_secrets()
    b.dcos_ca_acls()

    path = opts.rundir + '/etc/dcos-ca/dbconfig.json'
    b.write_dcos_ca_creds(src='/opt/mesosphere/etc/dcos-ca/dbconfig.json', dst=path)

    path = opts.rundir + '/pki/CA/certs/ca.crt'
    b.write_CA_certificate(filename=path)

    path = opts.rundir + '/pki/CA/private/ca.key'
    b.write_CA_key(path)
    shutil.chown(path, user=opts.dcos_ca_user)


def dcos_mesos_master(b, opts):
    b.init_zk_acls()
    b.create_master_secrets()
    b.mesos_zk_acls()

    b.write_mesos_master_env(opts.rundir + '/etc/mesos-master')

    if opts.config['ssl_enabled']:
        keypath = opts.rundir + '/pki/tls/private/mesos-master.key'
        crtpath = opts.rundir + '/pki/tls/certs/mesos-master.crt'
        b.ensure_key_certificate('Mesos Master', keypath, crtpath, master=True)

    # agent secrets are needed for it to contact the master
    b.create_agent_secrets(opts.zk_agent_digest)

    b.create_agent_service_accounts()

    # If permissive security is enabled, create the 'dcos_anonymous' account.
    if opts.config['security'] == 'permissive':
        # TODO(greggomann): add proper ACLs for 'dcos_anonymous'.
        # For now, we make dcos_anonymous a superuser, so security-ignorant scripts/frameworks
        # can still access Mesos endpoints and register however they like.
        b.create_service_account('dcos_anonymous', superuser=True)


def dcos_mesos_slave(b, opts):
    b.read_agent_secrets()
    b.write_CA_certificate()

    if opts.config['ssl_enabled']:
        keypath = opts.rundir + '/pki/tls/private/mesos-slave.key'
        crtpath = opts.rundir + '/pki/tls/certs/mesos-slave.crt'
        b.ensure_key_certificate('Mesos Agent', keypath, crtpath, service_account='dcos_agent')

    # Service account needed to
    # a) authenticate with master, and/or
    # b) retrieve ACLs from bouncer, and/or
    # c) fetch secrets
    # As a result, we always create this account.
    svc_acc_creds_fn = opts.rundir + '/etc/mesos/agent_service_account.json'
    b.write_service_account_credentials('dcos_mesos_agent', svc_acc_creds_fn)

    # TODO(adam): orchestration API should handle this in the future
    if opts.config['ssl_enabled']:
        keypath = opts.rundir + '/pki/tls/private/scheduler.key'
        crtpath = opts.rundir + '/pki/tls/certs/scheduler.crt'
        b.ensure_key_certificate('Mesos Schedulers', keypath, crtpath, service_account='dcos_agent', key_mode=0o644)


def dcos_mesos_slave_public(b, opts):
    b.read_agent_secrets()

    if opts.config['ssl_enabled']:
        b.write_CA_certificate()

        keypath = opts.rundir + '/pki/tls/private/mesos-slave.key'
        crtpath = opts.rundir + '/pki/tls/certs/mesos-slave.crt'
        b.ensure_key_certificate('Mesos Public Agent', keypath, crtpath, service_account='dcos_agent')

    # Service account needed to
    # a) authenticate with master, and/or
    # b) retrieve ACLs from bouncer, and/or
    # c) fetch secrets
    # As a result, we always create this account.
    svc_acc_creds_fn = opts.rundir + '/etc/mesos/agent_service_account.json'
    b.write_service_account_credentials('dcos_mesos_agent_public', svc_acc_creds_fn)

    # TODO(adam): orchestration API should handle this in the future
    if opts.config['ssl_enabled']:
        keypath = opts.rundir + '/pki/tls/private/scheduler.key'
        crtpath = opts.rundir + '/pki/tls/certs/scheduler.crt'
        b.ensure_key_certificate('Mesos Schedulers', keypath, crtpath, service_account='dcos_agent', key_mode=0o644)


def dcos_marathon(b, opts):
    b.init_zk_acls()
    b.create_master_secrets()
    b.marathon_zk_acls()

    if opts.config['zk_acls_enabled']:
        # Must be run after create_master_secrets.
        env = opts.rundir + '/etc/marathon/zk.env'
        b.write_marathon_zk_env(env)
        shutil.chown(env, user='dcos_marathon')

    # For libmesos scheduler SSL or Marathon UI/API SSL.
    if opts.config['ssl_enabled'] or opts.config['marathon_https_enabled']:
        key = opts.rundir + '/pki/tls/private/marathon.key'
        crt = opts.rundir + '/pki/tls/certs/marathon.crt'
        b.ensure_key_certificate('Marathon', key, crt, master=True, marathon=True)
        shutil.chown(key, user='dcos_marathon')
        shutil.chown(crt, user='dcos_marathon')

        ca = opts.rundir + '/pki/CA/certs/ca.crt'
        b.write_CA_certificate(filename=ca)

    # For Marathon UI/API SSL.
    if opts.config['marathon_https_enabled']:
        # file also used by the adminrouter /ca/cacerts.jks endpoint
        ts = opts.rundir + '/pki/CA/certs/cacerts.jks'
        b.write_truststore(ts, ca)

        env = opts.rundir + '/etc/marathon/tls.env'
        b.write_marathon_tls_env(key, crt, ca, env)
        shutil.chown(env, user='dcos_marathon')
        shutil.chown(opts.rundir + '/pki/tls/private/marathon.jks', user='dcos_marathon')

    # For framework authentication.
    if opts.config['framework_authentication_enabled']:
        b.create_service_account('dcos_marathon', superuser=False)
        svc_acc_creds_fn = opts.rundir + '/etc/marathon/service_account.json'
        b.write_service_account_credentials('dcos_marathon', svc_acc_creds_fn)
        shutil.chown(svc_acc_creds_fn, user='dcos_marathon')

    # IAM ACLs must be created after the service account.
    b.marathon_iam_acls()


def dcos_metronome(b, opts):
    b.init_zk_acls()
    b.create_master_secrets()
    b.metronome_zk_acls()

    # For libmesos scheduler SSL.
    if opts.config['ssl_enabled']:
        key = opts.rundir + '/pki/tls/private/metronome.key'
        crt = opts.rundir + '/pki/tls/certs/metronome.crt'
        b.ensure_key_certificate('Metronome', key, crt, master=True)
        shutil.chown(key, user='dcos_metronome')
        shutil.chown(crt, user='dcos_metronome')
        # ca.crt also only for libmesos SSL.
        ca = opts.rundir + '/pki/CA/certs/ca.crt'
        b.write_CA_certificate(filename=ca)

        # For Metronome UI/API SSL.
        ts = opts.rundir + '/pki/CA/certs/cacerts_metronome.jks'
        b.write_truststore(ts, ca)

        env = opts.rundir + '/etc/metronome/tls.env'
        b.write_metronome_env(key, crt, ca, env)
        shutil.chown(env, user='dcos_metronome')
        shutil.chown(opts.rundir + '/pki/tls/private/metronome.jks', user='dcos_metronome')

    # For framework authentication.
    if opts.config['framework_authentication_enabled']:
        b.create_service_account('dcos_metronome', superuser=False)
        svc_acc_creds_fn = opts.rundir + '/etc/metronome/service_account.json'
        b.write_service_account_credentials('dcos_metronome', svc_acc_creds_fn)
        shutil.chown(svc_acc_creds_fn, user='dcos_metronome')

    shutil.chown(opts.rundir + '/etc/metronome', user='dcos_metronome')

    # IAM ACLs must be created after the service account.
    b.metronome_iam_acls()


def dcos_mesos_dns(b, opts):
    b.init_zk_acls()
    b.create_master_secrets()

    b.create_service_account('dcos_mesos_dns', superuser=True)

    if opts.config['ssl_enabled']:
        path = opts.rundir + '/pki/CA/certs/ca.crt'
        b.write_CA_certificate(filename=path)

    if opts.config['mesos_authenticate_http']:
        svc_acc_creds_fn = opts.rundir + '/etc/mesos-dns/iam.json'
        b.write_service_account_credentials('dcos_mesos_dns', svc_acc_creds_fn)
        shutil.chown(svc_acc_creds_fn, user='dcos_mesos_dns')


def dcos_adminrouter(b, opts):
    b.init_zk_acls()
    b.create_master_secrets()

    b.cluster_id()

    b.create_service_account('dcos_adminrouter', superuser=True)

    extra_san = []
    internal_lb = os.getenv('INTERNAL_MASTER_LB_DNSNAME')
    if internal_lb:
        extra_san.append(utils.SanEntry('dns', internal_lb))
    external_lb = os.getenv('MASTER_LB_DNSNAME')
    if external_lb:
        extra_san.append(utils.SanEntry('dns', external_lb))

    machine_pub_ip = subprocess.check_output(
        ['/opt/mesosphere/bin/detect_ip_public'],
        stderr=subprocess.DEVNULL).decode('ascii').strip()
    gen.calc.validate_ipv4_addresses([machine_pub_ip])
    # We add ip as both DNS and IP entry so that old/broken software that does
    # not support IPAddress type SAN can still use it.
    extra_san.append(utils.SanEntry('dns', machine_pub_ip))
    extra_san.append(utils.SanEntry('ip', machine_pub_ip))

    if opts.config['ssl_enabled']:
        keypath = opts.rundir + '/pki/tls/private/adminrouter.key'
        crtpath = opts.rundir + '/pki/tls/certs/adminrouter.crt'
        b.ensure_key_certificate('AdminRouter', keypath, crtpath, master=True, extra_san=extra_san)

    b.write_jwks_public_keys(opts.rundir + '/etc/jwks.pub')

    # Generate SERVICE_AUTH_TOKEN=<authtoken> env var declaration.
    # Strip trailing newline returned by  `write_service_auth_token()`.
    service_auth_token_env_declaration = b.write_service_auth_token(
        uid='dcos_adminrouter',
        exp=0,
        filename=None).decode('ascii').strip()

    env_file_lines = [service_auth_token_env_declaration]

    # Optionally generate EXHIBITOR_ADMIN_HTTPBASICAUTH_CREDS=<creds> declaration.
    if opts.config['exhibitor_admin_password_enabled'] is True:
        pw = opts.config['exhibitor_admin_password']

        # Build HTTP Basic auth credential string.
        exhibitor_admin_basic_auth_creds = base64.b64encode(
            'admin:{}'.format(pw).encode('ascii')).decode('ascii')

        env_file_lines.append(
            'EXHIBITOR_ADMIN_HTTPBASICAUTH_CREDS={}'.format(
                exhibitor_admin_basic_auth_creds))

    env_file_contents_bytes = '\n'.join(env_file_lines).encode('ascii')
    env_file_path = opts.rundir + '/etc/adminrouter.env'
    _write_file(env_file_path, env_file_contents_bytes, 0o600)


def dcos_adminrouter_agent(b, opts):
    b.read_agent_secrets()

    if opts.config['ssl_enabled']:
        b.write_CA_certificate()

        keypath = opts.rundir + '/pki/tls/private/adminrouter-agent.key'
        crtpath = opts.rundir + '/pki/tls/certs/adminrouter-agent.crt'
        b.ensure_key_certificate('Adminrouter Agent', keypath, crtpath, service_account='dcos_agent')

    b.write_jwks_public_keys(opts.rundir + '/etc/jwks.pub')

    # write_service_auth_token must follow
    # write_CA_certificate on agents to allow
    # for a verified HTTPS connection on login
    b.write_service_auth_token('dcos_adminrouter_agent', opts.rundir + '/etc/adminrouter.env', exp=0)


def dcos_spartan(b, opts):
    if os.path.exists('/opt/mesosphere/etc/roles/master'):
        return dcos_spartan_master(b, opts)
    else:
        return dcos_spartan_agent(b, opts)


def dcos_spartan_master(b, opts):
    b.init_zk_acls()
    b.create_master_secrets()

    if opts.config['ssl_enabled']:
        b.write_CA_certificate()

        key = opts.rundir + '/pki/tls/private/spartan.key'
        crt = opts.rundir + '/pki/tls/certs/spartan.crt'
        b.ensure_key_certificate('Spartan Master', key, crt, master=True)


def dcos_spartan_agent(b, opts):
    b.read_agent_secrets()

    if opts.config['ssl_enabled']:
        b.write_CA_certificate()

        keypath = opts.rundir + '/pki/tls/private/spartan.key'
        crtpath = opts.rundir + '/pki/tls/certs/spartan.crt'
        b.ensure_key_certificate('Spartan Agent', keypath, crtpath, service_account='dcos_agent')


def dcos_erlang_service(servicename, b, opts):
    if servicename == 'networking_api':
        for file in ['/opt/mesosphere/active/networking_api/networking_api/releases/0.0.1/vm.args.2.config',
                     '/opt/mesosphere/active/networking_api/networking_api/releases/0.0.1/sys.config.2.config']:
            if not os.path.exists(file):
                open(file, 'a').close()
                shutil.chown(file, user='dcos_networking_api')
        shutil.chown('/opt/mesosphere/active/networking_api/networking_api', user='dcos_networking_api')
        shutil.chown('/opt/mesosphere/active/networking_api/networking_api/log', user='dcos_networking_api')
    if os.path.exists('/opt/mesosphere/etc/roles/master'):
        log.info('%s master bootstrap', servicename)
        return dcos_erlang_service_master(servicename, b, opts)
    else:
        log.info('%s agent bootstrap', servicename)
        return dcos_erlang_service_agent(servicename, b, opts)


def dcos_erlang_service_master(servicename, b, opts):
    b.init_zk_acls()
    b.create_master_secrets()

    b.create_service_account('dcos_{}_master'.format(servicename), superuser=True)

    user = 'dcos_' + servicename

    ca = opts.rundir + '/pki/CA/certs/ca.crt'
    b.write_CA_certificate(filename=ca)

    friendly_name = servicename[0].upper() + servicename[1:]
    key = opts.rundir + '/pki/tls/private/{}.key'.format(servicename)
    crt = opts.rundir + '/pki/tls/certs/{}.crt'.format(servicename)
    b.ensure_key_certificate(friendly_name, key, crt)
    if servicename == 'networking_api':
        shutil.chown(key, user=user)
        shutil.chown(crt, user=user)

    auth_env = opts.rundir + '/etc/{}_auth.env'.format(servicename)
    b.write_service_auth_token('dcos_{}_master'.format(servicename), auth_env, exp=0)
    if servicename == 'networking_api':
        shutil.chown(auth_env, user=user)


def dcos_erlang_service_agent(servicename, b, opts):
    b.read_agent_secrets()

    user = 'dcos_' + servicename

    if opts.config['ssl_enabled']:
        ca = opts.rundir + '/pki/CA/certs/ca.crt'
        b.write_CA_certificate(filename=ca)

        friendly_name = servicename[0].upper() + servicename[1:]
        key = opts.rundir + '/pki/tls/private/{}.key'.format(servicename)
        crt = opts.rundir + '/pki/tls/certs/{}.crt'.format(servicename)
        b.ensure_key_certificate(friendly_name, key, crt, service_account='dcos_agent')

    if servicename == 'networking_api':
        shutil.chown(key, user=user)
        shutil.chown(crt, user=user)

    auth_env = opts.rundir + '/etc/{}_auth.env'.format(servicename)
    b.write_service_auth_token('dcos_{}_agent'.format(servicename), auth_env, exp=0)
    if servicename == 'networking_api':
        shutil.chown(auth_env, user=user)


def dcos_cosmos(b, opts):
    b.init_zk_acls()
    b.create_master_secrets()
    b.cosmos_acls()

    key = opts.rundir + '/pki/tls/private/cosmos.key'
    crt = opts.rundir + '/pki/tls/certs/cosmos.crt'
    b.ensure_key_certificate('Cosmos', key, crt, master=True)
    shutil.chown(key, user='dcos_cosmos')
    shutil.chown(crt, user='dcos_cosmos')

    ca = opts.rundir + '/pki/CA/certs/ca.crt'
    b.write_CA_certificate(filename=ca)

    ts = opts.rundir + '/pki/CA/certs/cacerts_cosmos.jks'
    b.write_truststore(ts, ca)

    env = opts.rundir + '/etc/cosmos.env'
    b.write_cosmos_env(key, crt, ca, env)


def dcos_signal(b, opts):
    b.init_zk_acls()
    b.create_master_secrets()

    b.cluster_id()
    b.create_service_account('dcos_signal_service', superuser=True)

    svc_acc_creds_fn = opts.rundir + '/etc/signal-service/service_account.json'
    b.write_service_account_credentials('dcos_signal_service', svc_acc_creds_fn)
    shutil.chown(svc_acc_creds_fn, user='dcos_signal')


def dcos_metrics_master(b, opts):
    b.init_zk_acls()
    b.create_master_secrets()

    b.cluster_id()
    b.create_service_account('dcos_metrics_master', superuser=True)

    svc_acc_creds_fn = opts.rundir + '/etc/dcos-metrics/service_account.json'
    b.write_service_account_credentials('dcos_metrics_master', svc_acc_creds_fn)
    shutil.chown(svc_acc_creds_fn, user='dcos_metrics')


def dcos_metrics_agent(b, opts):
    b.read_agent_secrets()

    b.cluster_id(readonly=True)

    svc_acc_creds_fn = opts.rundir + '/etc/dcos-metrics/service_account.json'
    b.write_service_account_credentials('dcos_metrics_agent', svc_acc_creds_fn)
    shutil.chown(svc_acc_creds_fn, user='dcos_metrics')


def dcos_3dt_master(b, opts):
    b.init_zk_acls()
    b.create_master_secrets()

    b.create_service_account('dcos_3dt_master', superuser=True)
    svc_acc_creds_fn = opts.rundir + '/etc/3dt/master_service_account.json'
    b.write_service_account_credentials('dcos_3dt_master', svc_acc_creds_fn)
    shutil.chown(svc_acc_creds_fn, user='dcos_3dt')

    # 3dt agent secrets are needed for it to contact the 3dt master
    b.create_agent_secrets(opts.zk_agent_digest)
    b.create_service_account('dcos_3dt_agent', superuser=True)


def dcos_3dt_agent(b, opts):
    b.read_3dt_agent_secrets()
    svc_acc_creds_fn = opts.rundir + '/etc/3dt/agent_service_account.json'
    b.write_service_account_credentials('dcos_3dt_agent', svc_acc_creds_fn)
    shutil.chown(svc_acc_creds_fn, user='dcos_3dt')


def dcos_history(b, opts):
    b.create_master_secrets()

    b.create_service_account('dcos_history_service', superuser=True)

    svc_acc_creds_fn = opts.rundir + '/etc/history-service/service_account.json'
    b.write_service_account_credentials('dcos_history_service', svc_acc_creds_fn)
    shutil.chown(svc_acc_creds_fn, user='dcos_history')

    ca = opts.rundir + '/pki/CA/certs/ca.crt'
    b.write_CA_certificate(filename=ca)

    os.makedirs(opts.statedir + '/dcos-history', exist_ok=True)
    shutil.chown(opts.statedir + '/dcos-history', user='dcos_history')


def dcos_log_master(b, opts):
    b.init_zk_acls()
    b.create_master_secrets()

    b.create_service_account('dcos_log_master', superuser=True)
    svc_acc_creds_fn = opts.rundir + '/etc/dcos-log/dcos_log_service_account.json'
    b.write_service_account_credentials('dcos_log_master', svc_acc_creds_fn)
    shutil.chown(svc_acc_creds_fn, user='dcos_log')


def dcos_log_agent(b, opts):
    b.read_dcos_log_secrets()
    svc_acc_creds_fn = opts.rundir + '/etc/dcos-log/dcos_log_service_account.json'
    b.write_service_account_credentials('dcos_log_agent', svc_acc_creds_fn)
    shutil.chown(svc_acc_creds_fn, user='dcos_log')


bootstrappers = {
    'dcos-adminrouter': dcos_adminrouter,
    'dcos-adminrouter-agent': dcos_adminrouter_agent,
    'dcos-bouncer': dcos_bouncer,
    'dcos-ca': dcos_ca,
    'dcos-cosmos': dcos_cosmos,
    'dcos-3dt-agent': dcos_3dt_agent,
    'dcos-3dt-master': dcos_3dt_master,
    'dcos-history': dcos_history,
    'dcos-marathon': dcos_marathon,
    'dcos-mesos-slave': dcos_mesos_slave,
    'dcos-mesos-slave-public': dcos_mesos_slave_public,
    'dcos-mesos-dns': dcos_mesos_dns,
    'dcos-mesos-master': dcos_mesos_master,
    'dcos-metronome': dcos_metronome,
    'dcos-minuteman': (lambda b, opts: dcos_erlang_service('minuteman', b, opts)),
    'dcos-navstar': (lambda b, opts: dcos_erlang_service('navstar', b, opts)),
    'dcos-networking_api': (lambda b, opts: dcos_erlang_service('networking_api', b, opts)),
    'dcos-secrets': dcos_secrets,
    'dcos-signal': dcos_signal,
    'dcos-spartan': dcos_spartan,
    'dcos-vault_default': dcos_vault_default,
    'dcos-log-master': dcos_log_master,
    'dcos-log-agent': dcos_log_agent,
    'dcos-metrics-agent': dcos_metrics_agent,
    'dcos-metrics-master': dcos_metrics_master
}
