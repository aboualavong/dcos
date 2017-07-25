# Copyright (C) Mesosphere, Inc. See LICENSE file for details.

import logging
import re

from exceptions import EndpointException
from mocker.endpoints.recording import (
    RecordingHTTPRequestHandler,
    RecordingTcpIpEndpoint,
)

log = logging.getLogger(__name__)


class IamHTTPRequestHandler(RecordingHTTPRequestHandler):

    USERS_PERMISSIONS_REGEXP = re.compile(
        '^/acs/api/v1/users/([^/]+)/permissions$')

    def _calculate_response(self, base_path, url_args, body_args=None):
        if base_path == '/acs/api/v1/internal/policyquery':
            return self.__internal_policy_query_request_handler(
                url_args, body_args)

        result = self.USERS_PERMISSIONS_REGEXP.search(base_path)
        if result:
            return self.__users_permissions_request_handler(result.group(1))

        stub_paths = [
            '/acs/api/v1/foo/bar',
        ]
        if base_path in stub_paths:
            return self._convert_data_to_blob({})

        raise EndpointException(
            code=500,
            content="Path `{}` is not supported yet".format(
                base_path))

    def __users_permissions_request_handler(self, user):
        return self._convert_data_to_blob({
            'user': user,
            'permissions': True,
            })

    def __internal_policy_query_request_handler(self, url_args, body_args=None):
        ctx = self.server.context

        with ctx.lock:
            return self._convert_data_to_blob({'allowed': ctx.data['allowed']})


class IamEndpoint(RecordingTcpIpEndpoint):

    def __init__(self, port, ip=''):
        super().__init__(port, ip, IamHTTPRequestHandler)
        self._context.data["allowed"] = True

    def reset(self):
        super().reset()
        self._context.data["allowed"] = True

    def permit_all_queries(self, *_):
        self._context.data["allowed"] = True

    def deny_all_queries(self, *_):
        self._context.data["allowed"] = False