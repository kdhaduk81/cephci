"""Log cluster health, crash status, MDS state, and FS status.

This is a lightweight diagnostic test that captures cluster state at a
specific point in the suite execution.  It always returns 0 (pass) so
it never blocks subsequent tests.
"""
import traceback

from utility.log import Log

log = Log(__name__)


def run(ceph_cluster, **kw):
    try:
        clients = ceph_cluster.get_ceph_objects("client")
        if not clients:
            log.error("No client nodes found")
            return 0

        client = clients[0]
        cmds = [
            "ceph health detail",
            "ceph -s",
            "ceph crash ls-new",
            "ceph fs status",
            "ceph orch ps --daemon-type mds -f json-pretty",
        ]
        for cmd in cmds:
            try:
                out, _ = client.exec_command(sudo=True, cmd=cmd, check_ec=False)
                log.info(">>> %s\n%s", cmd, out)
            except Exception:
                log.warning("Command failed: %s", cmd)

        return 0
    except Exception as e:
        log.error(e)
        log.error(traceback.format_exc())
        return 0
