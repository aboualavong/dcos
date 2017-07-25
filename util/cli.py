import logging
import os
import shutil
import stat
import subprocess
import tempfile

import requests

log = logging.getLogger(__name__)

DCOS_CLI_URL = "https://downloads.dcos.io/binaries/cli/linux/x86-64/latest/dcos"


def dcoscli_fixture():
    tmpdir = tempfile.mkdtemp()
    dcos_cli_path = os.path.join(tmpdir, "dcos")

    requests.packages.urllib3.disable_warnings()
    with open(dcos_cli_path, 'wb') as f:
        r = requests.get(DCOS_CLI_URL, stream=True, verify=True)
        for chunk in r.iter_content(1024):
            f.write(chunk)

    # make binary executable
    st = os.stat(dcos_cli_path)
    os.chmod(dcos_cli_path, st.st_mode | stat.S_IEXEC)

    return DCOSCLI(tmpdir)

    shutil.rmtree(os.path.expanduser("~/.dcos"))
    shutil.rmtree(tmpdir, ignore_errors=True)


class DCOSCLI():

    def __init__(self, directory):
        updated_env = os.environ.copy()
        updated_env.update({
            'PATH': "{}:{}".format(
                os.path.join(os.getcwd(), directory), os.environ['PATH']),
            'PYTHONIOENCODING': 'utf-8',
            'PYTHONUNBUFFERED': 'x'
        })
        self.env = updated_env

    def exec_command(self, cmd, stdin=None):
        """Execute CLI command

        :param cmd: Program and arguments
        :type cmd: [str]
        :param stdin: File to use for stdin
        :type stdin: file
        :returns: A tuple with stdout and stderr
        :rtype: (str, str)
        """

        log.info('CMD: {!r}'.format(cmd))

        try:
            process = subprocess.run(
                cmd,
                stdin=stdin, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.env,
                check=False)

            stdout, stderr = process.stdout.decode('utf-8'), process.stderr.decode('utf-8')

            log.info('STDOUT: {}'.format(stdout))
            log.info('STDERR: {}'.format(stderr))

            return (stdout, stderr)
        except Exception as e:
            log.info(repr(e))