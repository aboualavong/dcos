"""
Automatically loaded by py.test.

This is the place to define globally visible fixtures.
"""
import json
import logging
from urllib.parse import urlparse

import pytest
from api_session_fixture import make_session_fixture
from jwt.utils import base64url_decode, base64url_encode

from test_util.dcos_api_session import DcosAuth, DcosUser

logging.basicConfig(format='[%(asctime)s] %(levelname)s: %(message)s', level=logging.INFO)
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("botocore").setLevel(logging.WARNING)
log = logging.getLogger(__name__)


def path_only(url):
    return urlparse(url).path


@pytest.fixture(scope='session')
def superuser_api_session():
    return make_session_fixture()


@pytest.fixture(scope='session')
def superuser(superuser_api_session):
    return superuser_api_session.auth_user


@pytest.fixture(scope='session')
def peter_api_session(superuser_api_session):
    """Provides a non-super user and deletes it after test
    This user can have its permissions changed by superuser
    to test authn/authz functions in DC/OS
    """
    uid = 'peter'
    password = 'peterpan'
    peter = DcosUser({'uid': uid, 'password': password})
    peter.uid = uid
    peter.password = password
    description = 'An ordinarily weak Peter'

    new_user_json = {'description': description, 'password': password}
    user_endpoint = '/users/{}'.format(uid)
    superuser_api_session.iam.put(user_endpoint, json=new_user_json)

    yield superuser_api_session.get_user_session(peter)

    log.info('Delete user {}'.format(peter.uid))
    r = superuser_api_session.iam.delete(user_endpoint)
    r.raise_for_status()


@pytest.fixture(scope='session')
def peter(peter_api_session):
    return peter_api_session.auth_user


@pytest.fixture(scope='session')
def noauth_api_session(superuser_api_session):
    return superuser_api_session.get_user_session(None)


@pytest.fixture()
def with_peter_in_superuser_acl(superuser_api_session, peter):
    """Grants peter user superuser priveleges
    """
    path = '/acls/dcos:superuser/users/{}/full'.format(peter.uid)
    # Add peter.
    r = superuser_api_session.iam.put(path)
    r.raise_for_status()

    yield

    # Teardown code, remove peter, accept any 2xx status code.
    r = superuser_api_session.iam.delete(path)
    r.raise_for_status()


@pytest.fixture()
def with_peter_in_superuser_group(peter, superuser_api_session):
    path = '/groups/superusers/users/{}'.format(peter.uid)

    # Add peter to the group.
    r = superuser_api_session.iam.put(path)
    assert r.status_code == 204

    yield

    # Teardown code, remove peter from the group, accept any 2xx status code.
    r = superuser_api_session.iam.delete(path)
    r.raise_for_status()


def iam_reset_undecorated(superuser_api_session, superuser, peter):
    """
    FIXME: change these fixtures to cleanup state after a test
        so that it matches state at the beginning. This current implemtation
        will produce unexpected behavior if a cluster is not 'fresh'
    1) Remove unexpected users.
    2) Remove unexpected groups.
    3) Remove ACLs that are not part of the initially seen ones.
    4) Remove Peter's direct permissions.
    5) Remove Peter's group memberships.
    """
    # Remove unexpected users.
    r = superuser_api_session.iam.get('/users')
    for u in r.json()['array']:
        if u['uid'] in (peter.uid, superuser.uid, 'dcos_marathon', 'dcos_metronome'):
            continue
        log.info('Delete user: {}'.format(u['url']))
        r = superuser_api_session.delete(path_only(u['url']))
        r.raise_for_status()

    # Remove unexpected groups.
    r = superuser_api_session.iam.get('/groups')
    for g in r.json()['array']:
        if g['gid'] == 'superusers':
            continue
        log.info('Delete group: {}'.format(g['url']))
        r = superuser_api_session.delete(path_only(g['url']))
        r.raise_for_status()

    # Remove ACLs that are not part of the initially seen ones.
    r = superuser_api_session.iam.get('/acls')
    for o in r.json()['array']:
        if o['rid'] in superuser_api_session.initial_resource_ids:
            continue
        log.info('Delete ACL: {}'.format(o['url']))
        r = superuser_api_session.delete(path_only(o['url']))
        r.raise_for_status()

    # Remove Peter's direct permissions (group permissions will be obliterated
    # by removing group memberships in the next step).

    r = superuser_api_session.iam.get('/users/{}/permissions'.format(peter.uid))
    for o in r.json()['direct']:
        for a in o['actions']:
            log.info("Delete Peter's permission: {}".format(a['url']))
            r = superuser_api_session.delete(path_only(a['url']))
            r.raise_for_status()

    # Remove Peter's group memberships.
    r = superuser_api_session.iam.get('/users/{}/groups'.format(peter.uid))
    for o in r.json()['array']:
        log.info("Delete Peter's group membership: {}".format(o['membershipurl']))
        r = superuser_api_session.delete(path_only(o['membershipurl']))
        r.raise_for_status()


def iam_verify_undecorated(superuser_api_session, superuser, peter):
    """
    1) Verify there are no other users except for superuser and Peter.
    2) Verify there are no groups other than 'superuser'.
    3) Verify Peter is not part of any group.
    4) Verify Peter has no permissions set.
    """
    # Verify there are no other users except for superuser and Peter.
    r = superuser_api_session.iam.get('/users')
    uids = [_['uid'] for _ in r.json()['array']]
    assert set(uids) == set((superuser.uid, peter.uid))

    # Verify there are no groups other than 'superuser'.
    r = superuser_api_session.iam.get('/groups')
    gids = [_['gid'] for _ in r.json()['array']]
    assert gids == ['superusers']

    # Verify Peter is not part of any group.
    r = superuser_api_session.iam.get('/users/{}/groups'.format(peter.uid))
    assert r.json()['array'] == []

    # Verify Peter has no permissions set.
    r = superuser_api_session.iam.get('/users/{}/permissions'.format(peter.uid))
    data = r.json()
    assert data['direct'] == []
    assert data['groups'] == []


@pytest.fixture()
def iam_verify_and_reset(superuser_api_session, superuser, peter):
    """
    Pre-test steps:

        1) Verify there are no other users except for superuser and Peter.
        2) Verify there are no groups other than 'superusers'.
        3) Verify Peter is not part of any group (could be in superusers
           otherwise).
        4) Verify Peter has no permissions set.

    Post-test steps:

        1) Remove unexpected users.
        2) Remove unexpected groups.
        3) Remove ACLs that are not part of the initially seen ones.
        4) Remove Peter's direct permissions.
        5) Remove Peter's group memberships.


    Only yield into test code if pre-test has succeeded. Perform post-test even
    if pre-test failed, i.e. always try to perform cleanup.
    """
    try:
        logging.info('Verifying superuser and peter are in original state')
        iam_verify_undecorated(superuser_api_session, superuser, peter)
    except Exception as e:
        log.error('Exception in iam_verify_undecorated(), reraise: {}'.format(str(e)))
        raise
    else:
        yield
    finally:
        logging.info('Returning superuser and peter to original state')
        iam_reset_undecorated(superuser_api_session, superuser, peter)


@pytest.fixture(scope='session')
def forged_superuser_session(peter, superuser, noauth_api_session):
    """ Returns an API session that uses auth from Peter's session
    with the superuser uid injected
    """
    # Decode Peter's authentication token.
    t = peter.auth_token
    header_bytes, payload_bytes, signature_bytes = [
        base64url_decode(_.encode('ascii')) for _ in t.split(".")]
    payload_dict = json.loads(payload_bytes.decode('ascii'))
    assert 'exp' in payload_dict
    assert 'uid' in payload_dict
    assert payload_dict['uid'] == peter.uid

    # Rewrite uid and invert token decode procedure.
    forged_payload_dict = payload_dict.copy()
    forged_payload_dict['uid'] = superuser.uid
    forged_payload_bytes = json.dumps(forged_payload_dict).encode('utf-8')

    forged_token = '.'.join(
        base64url_encode(_).decode('ascii') for _ in (
            header_bytes, forged_payload_bytes, signature_bytes)
        )
    forged_session = noauth_api_session.copy()
    forged_session.session.auth = DcosAuth(forged_token)
    return forged_session
