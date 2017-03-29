# Copyright (C) Mesosphere, Inc. See LICENSE file for details.

from contextlib import contextmanager


@contextmanager
def assert_iam_queried_for_uid_and_rid(mocker, uid, rid, iam_request=False):
    """Asserts that IAM mock has been queried for given UID and RID

    Arguments:
        mocker (Mocker): Mocker instance with all server mocks
        uid (str): User ID that should have been queried
        rid (str): Role ID that should have been queried
    """
    mocker.send_command(
        endpoint_id='http://127.0.0.1:8101',
        func_name='record_requests',
        )

    yield

    upstream_requests = mocker.send_command(
        endpoint_id='http://127.0.0.1:8101',
        func_name='get_recorded_requests',
        )

    if not iam_request:
        assert len(upstream_requests) == 1
    else:
        assert len(upstream_requests) == 2
    expected_path = (
        '/acs/api/v1/internal/policyquery'
        '?rid={}&uid={}&action=full'.format(rid, uid)
        )
    assert upstream_requests[0]['path'] == expected_path
    assert upstream_requests[0]['method'] == 'GET'
    assert upstream_requests[0]['request_version'] == 'HTTP/1.0'
