import logging

from mocker.common import MockerBase
from mocker.endpoints.ee.iam import IamEndpoint
from mocker.endpoints.reflectors import (
    ReflectingTcpIpEndpoint,
    ReflectingUnixSocketEndpoint,
)

log = logging.getLogger(__name__)


class Mocker(MockerBase):
    def __init__(self):
        ee_endpoints = []

        # Default Vault endpoint:
        ee_endpoints.append(
            ReflectingTcpIpEndpoint(ip='127.0.0.1', port=8200))
        # Certificate authority
        ee_endpoints.append(
            ReflectingTcpIpEndpoint(ip='127.0.0.1', port=8888))
        # Secrets
        ee_endpoints.append(
            ReflectingTcpIpEndpoint(ip='127.0.0.1', port=1337))
        # DDDT:
        ee_endpoints.append(
            ReflectingUnixSocketEndpoint('/run/dcos/3dt.sock'))
        # IAM
        ee_endpoints.append(IamEndpoint(ip='127.0.0.1', port=8101))
        # Mesos DNS
        ee_endpoints.append(ReflectingTcpIpEndpoint(ip='127.0.0.1', port=8123))
        # Networking API
        ee_endpoints.append(ReflectingTcpIpEndpoint(ip='127.0.0.1', port=61430))
        # Metrics(agent):
        ee_endpoints.append(
            ReflectingUnixSocketEndpoint(path='/run/dcos/dcos-metrics-agent.sock'))

        # Add more EE endpoints here...

        super().__init__(ee_endpoints)