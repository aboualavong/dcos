import hashlib
from base64 import b64encode

from gen.calc import validate_true_false
from gen.internals import validate_one_of


def validate_customer_key(customer_key):
    assert isinstance(customer_key, str), "'customer_key' must be a string."
    if customer_key == "Cloud Template Missing Parameter" or customer_key == "CUSTOMER KEY NOT SET":
        return
    assert len(customer_key) == 36, "'customer_key' must be 36 characters long with hyphens"


def calculate_ssl_enabled(security):
    return {
        'strict': 'true',
        'permissive': 'true',
        'disabled': 'false'
        }[security]


def calculate_ssl_support_downgrade(security):
    return {
        'strict': 'false',
        'permissive': 'true',
        'disabled': 'true'
        }[security]


def calculate_adminrouter_master_enforce_https(security):
    return {
        'strict': 'all',
        'permissive': 'only_root_path',
        'disabled': 'none'
        }[security]


def calculate_adminrouter_agent_enforce_https(security):
    return {
        'strict': 'all',
        'permissive': 'none',
        'disabled': 'none'
        }[security]


def calculate_adminrouter_master_default_scheme(security):
    return {
        'strict': 'https://',
        'permissive': 'https://',
        'disabled': 'http://'
        }[security]


def calculate_firewall_enabled(security):
    return {
        'strict': 'true',
        'permissive': 'false',
        'disabled': 'false'
        }[security]


def calculate_mesos_authenticate_http(security):
    return {
        'strict': 'true',
        'permissive': 'true',
        'disabled': 'false'
        }[security]


def calculate_mesos_authz_enforced(security):
    return {
        'strict': 'true',
        'permissive': 'false',
        'disabled': 'false'
        }[security]


def calculate_mesos_elevate_unknown_users(security):
    return {
        'strict': 'false',
        'permissive': 'true',
        'disabled': 'false'
        }[security]


def calculate_mesos_authorizer(mesos_authz_enforced):
    if mesos_authz_enforced == 'true':
        return 'com_mesosphere_dcos_Authorizer'

    else:
        return 'local'


def calculate_framework_authentication_required(security):
    return {
        'strict': 'true',
        'permissive': 'false',
        'disabled': 'false'
        }[security]


def calculate_agent_authentication_required(security):
    return {
        'strict': 'true',
        'permissive': 'false',
        'disabled': 'false'
        }[security]


def calculate_framework_authentication_enabled(security):
    return {
        'strict': 'true',
        'permissive': 'true',
        'disabled': 'false'
        }[security]


def calculate_agent_authn_enabled(security):
    return {
        'strict': 'true',
        'permissive': 'true',
        'disabled': 'false'
        }[security]


def calculate_mesos_classic_authenticator(framework_authentication_enabled, agent_authn_enabled):
    if framework_authentication_enabled == 'true' or agent_authn_enabled == 'true':
        return 'com_mesosphere_dcos_ClassicRPCAuthenticator'
    else:
        return 'crammd5'


def calculate_default_task_user(security):
    return {
        'strict': 'nobody',
        'permissive': 'root',
        'disabled': 'root'
        }[security]


def calculate_marathon_authn_mode(security):
    return {
        'strict': 'dcos/jwt',
        'permissive': 'dcos/jwt+anonymous',
        'disabled': 'disabled'
        }[security]


def calculate_marathon_https_enabled(security):
    return {
        'strict': 'true',
        'permissive': 'true',
        'disabled': 'false'
        }[security]


def calculate_marathon_extra_args(security):
    return {
        'strict': '--disable_http',
        'permissive': '',
        'disabled': ''
        }[security]


def calculate_zk_acls_enabled(security):
    return {
        'strict': 'true',
        'permissive': 'true',
        'disabled': 'false'
        }[security]


def empty(s):
    return s == ''


def validate_zk_credentials(credentials, human_name):
    if credentials == '':
        return
    assert len(credentials.split(':', 1)) == 2, (
        "{human_name} must of the form username: password".format(human_name=human_name))


def validate_zk_super_credentials(zk_super_credentials):
    validate_zk_credentials(zk_super_credentials, "Super ZK")


def validate_zk_master_credentials(zk_master_credentials):
    validate_zk_credentials(zk_master_credentials, "Master ZK")


def validate_zk_agent_credentials(zk_agent_credentials):
    validate_zk_credentials(zk_agent_credentials, "Agent ZK")


def validate_bouncer_expiration_auth_token_days(bouncer_expiration_auth_token_days):
    try:
        float(bouncer_expiration_auth_token_days)
    except ValueError:
        raise AssertionError(
            "bouncer_expiration_auth_token_days must be a number of days or decimal thereof.")
    assert float(bouncer_expiration_auth_token_days) > 0, "bouncer_expiration_auth_token_days must be greater than 0."


def calculate_superuser_credentials_given(superuser_username, superuser_password_hash):
    pair = (superuser_username, superuser_password_hash)

    if all(pair):
        return 'true'

    if not any(pair):
        return 'false'

    # `calculate_` functions are not supposed to error out, but
    # in this case here (multi-arg input) this check cannot
    # currently be replaced by a `validate_` function.
    raise AssertionError(
        "'superuser_username' and 'superuser_password_hash' "
        "must both be empty or both be non-emtpy")


def calculate_digest(credentials):
    if empty(credentials):
        return ''
    username, password = credentials.split(':', 1)
    credential = username.encode('utf-8') + b":" + password.encode('utf-8')
    cred_hash = b64encode(hashlib.sha1(credential).digest()).strip()
    return username + ":" + cred_hash.decode('utf-8')


def calculate_zk_agent_digest(zk_agent_credentials):
    return calculate_digest(zk_agent_credentials)


def calculate_zk_super_digest(zk_super_credentials):
    return calculate_digest(zk_super_credentials)


def calculate_zk_super_digest_jvmflags(zk_super_credentials):
    if empty(zk_super_credentials):
        return ''
    digest = calculate_zk_super_digest(zk_super_credentials)
    return "JVMFLAGS=-Dzookeeper.DigestAuthenticationProvider.superDigest=" + digest


def calculate_mesos_enterprise_isolation(mesos_isolation, ssl_enabled):
    isolation = ','.join([
        mesos_isolation,
        'com_mesosphere_dcos_SecretsIsolator'
    ])
    if ssl_enabled == 'true':
        isolation += ',com_mesosphere_dcos_SSLExecutorIsolator'
    return isolation


def get_ui_auth_json(ui_organization, ui_networking, ui_secrets, ui_auth_providers):
    # Hacky. Use '%' rather than .format() to avoid dealing with escaping '{'
    return '"authentication":{"enabled":true},"oauth":{"enabled":false}, ' \
        '"organization":{"enabled":%s}, ' \
        '"networking":{"enabled":%s},' \
        '"secrets":{"enabled":%s},' \
        '"auth-providers":{"enabled":%s},' \
        % (ui_organization, ui_networking, ui_secrets, ui_auth_providers)


def calculate_mesos_enterprise_hooks(dcos_remove_dockercfg_enable, ssl_enabled):
    hooks = 'com_mesosphere_dcos_SecretsHook'
    if ssl_enabled == 'true':
        hooks += ',com_mesosphere_dcos_SSLExecutorHook'
    if dcos_remove_dockercfg_enable == 'true':
        hooks += ",com_mesosphere_dcos_RemoverHook"
    return hooks


def calculate_marathon_port(security):
    if security in ('strict', 'permissive'):
        return "8443"
    assert security == 'disabled'
    return "8080"


def calculate_adminrouter_master_port(security):
    if security in ('strict', 'permissive'):
        return "443"
    assert security == 'disabled'
    return "80"


def calculate_adminrouter_agent_port(security):
    if security in ('strict', 'permissive'):
        return "61002"
    assert security == 'disabled'
    return "61001"


entry = {
    'validate': [
        validate_bouncer_expiration_auth_token_days,
        validate_customer_key,
        validate_zk_super_credentials,
        validate_zk_master_credentials,
        validate_zk_agent_credentials,
        lambda auth_cookie_secure_flag: validate_true_false(auth_cookie_secure_flag),
        lambda security: validate_one_of(security, ['strict', 'permissive', 'disabled']),
        lambda dcos_audit_logging: validate_true_false(dcos_audit_logging),
    ],
    'default': {
        'bouncer_expiration_auth_token_days': '5',
        'security': 'permissive',
        'dcos_audit_logging': 'true',
        'superuser_username': '',
        'superuser_password_hash': '',
        'superuser_credentials_given': calculate_superuser_credentials_given,
        'zk_super_credentials': 'super:secret',
        'zk_master_credentials': 'dcos-master:secret1',
        'zk_agent_credentials': 'dcos-agent:secret2',
        'customer_key': 'CUSTOMER KEY NOT SET',
        'ui_tracking': 'true',
        'ui_banner': 'false',
        'ui_banner_background_color': '#1E232F',
        'ui_banner_foreground_color': '#FFFFFF',
        'ui_banner_header_title': 'null',
        'ui_banner_header_content': 'null',
        'ui_banner_footer_content': 'null',
        'ui_banner_image_path': 'null',
        'ui_banner_dismissible': 'null'
    },
    'must': {
        'oauth_available': 'false',
        'zk_super_digest_jvmflags': calculate_zk_super_digest_jvmflags,
        'zk_agent_digest': calculate_zk_agent_digest,
        'adminrouter_auth_enabled': 'true',
        'adminrouter_master_enforce_https': calculate_adminrouter_master_enforce_https,
        'adminrouter_agent_enforce_https': calculate_adminrouter_agent_enforce_https,
        'adminrouter_master_default_scheme': calculate_adminrouter_master_default_scheme,
        'bootstrap_secrets': 'true',
        'ui_auth_providers': 'true',
        'ui_secrets': 'true',
        'ui_networking': 'true',
        'ui_organization': 'true',
        'ui_external_links': 'true',
        'ui_branding': 'true',
        'ui_telemetry_metadata': '{"openBuild": false}',
        'minuteman_forward_metrics': 'true',
        'custom_auth': 'true',
        'custom_auth_json': get_ui_auth_json,
        'mesos_http_authenticators': 'com_mesosphere_dcos_http_Authenticator',
        'mesos_authenticate_http': calculate_mesos_authenticate_http,
        'mesos_classic_authenticator': calculate_mesos_classic_authenticator,
        'framework_authentication_required': calculate_framework_authentication_required,
        'agent_authentication_required': calculate_agent_authentication_required,
        'agent_authn_enabled': calculate_agent_authn_enabled,
        'framework_authentication_enabled': calculate_framework_authentication_enabled,
        'mesos_authz_enforced': calculate_mesos_authz_enforced,
        'mesos_master_authorizers': calculate_mesos_authorizer,
        'mesos_agent_authorizer': calculate_mesos_authorizer,
        'mesos_elevate_unknown_users': calculate_mesos_elevate_unknown_users,
        'mesos_hooks': calculate_mesos_enterprise_hooks,
        'mesos_enterprise_isolation': calculate_mesos_enterprise_isolation,
        'firewall_enabled': calculate_firewall_enabled,
        'ssl_enabled': calculate_ssl_enabled,
        'ssl_support_downgrade': calculate_ssl_support_downgrade,
        'default_task_user': calculate_default_task_user,
        'marathon_authn_mode': calculate_marathon_authn_mode,
        'marathon_https_enabled': calculate_marathon_https_enabled,
        'marathon_extra_args': calculate_marathon_extra_args,
        'zk_acls_enabled': calculate_zk_acls_enabled,
        'marathon_port': calculate_marathon_port,
        'adminrouter_master_port': calculate_adminrouter_master_port,
        'adminrouter_agent_port': calculate_adminrouter_agent_port
    }
}

provider_template_defaults = {
    'superuser_username': '',
    'superuser_password_hash': '',
    'customer_key': 'Cloud Template Missing Parameter'
}