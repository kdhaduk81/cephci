"""
CephFS Mirroring Teardown Module

This module is used at the end of a test suite to destroy CephFS mirroring
configuration between source and target clusters.

Usage in suite YAML:
    - test:
        name: teardown cephfs mirroring
        module: cephfs_mirroring.teardown_cephfs_mirroring.py
        abort-on-fail: false
        clusters:
          ceph1:
            config:
              source_fs: cephfs
              target_fs: cephfs
              target_user: mirror_remote
"""

from tests.cephfs.cephfs_mirroring.cephfs_mirroring_utils import CephfsMirroringUtils
from utility.log import Log

log = Log(__name__)


def run(ceph_cluster, **kw):
    """
    Teardown CephFS mirroring between source and target clusters.

    This is a cleanup module to be run at the end of a test suite.
    It destroys the mirroring peer connection and disables mirroring modules.

    Args:
        ceph_cluster: The Ceph cluster object
        **kw: Keyword arguments including config and cluster dict

    Returns:
        0 on success, 1 on failure
    """
    try:
        config = kw.get("config") or {}
        ceph_cluster_dict = kw.get("ceph_cluster_dict")

        fs_mirroring_utils = CephfsMirroringUtils(
            ceph_cluster_dict.get("ceph1"), ceph_cluster_dict.get("ceph2")
        )

        source_clients = ceph_cluster_dict.get("ceph1").get_ceph_objects("client")
        target_clients = ceph_cluster_dict.get("ceph2").get_ceph_objects("client")

        # Config parameters with defaults
        source_fs = config.get("source_fs", "cephfs")
        target_fs = config.get("target_fs", "cephfs")
        target_user = config.get("target_user", "mirror_remote")

        log.info("=" * 60)
        log.info("CephFS Mirroring Teardown Module")
        log.info("=" * 60)

        # Validate prerequisites
        if not source_clients or not target_clients:
            log.error(
                "This module requires at least 1 client node on both ceph1 and ceph2."
            )
            return 1

        # Check if mirroring is configured
        check_result = fs_mirroring_utils.is_mirroring_configured(
            source_clients[0], source_fs
        )
        if not check_result["configured"]:
            log.info("Mirroring not configured for %s, nothing to teardown", source_fs)
            return 0

        peer_uuid = check_result["peer_uuid"]
        log.info("Found mirroring configuration:")
        log.info("  Source FS: %s", source_fs)
        log.info("  Peer UUID: %s", peer_uuid)

        # Remove any remaining mirrored paths
        log.info("Checking for remaining mirrored paths...")
        try:
            out, _ = source_clients[0].exec_command(
                sudo=True,
                cmd=f"ceph fs snapshot mirror ls {source_fs} --format json",
                check_ec=False,
            )
            if out.strip():
                import json

                paths = json.loads(out.strip())
                if paths:
                    log.info("Removing %d mirrored paths...", len(paths))
                    for path in paths:
                        try:
                            fs_mirroring_utils.remove_path_from_mirroring(
                                source_clients[0], source_fs, path
                            )
                            log.info("  Removed: %s", path)
                        except Exception as e:
                            log.warning("  Failed to remove %s: %s", path, e)
        except Exception as e:
            log.warning("Could not list mirrored paths: %s", e)

        # Destroy mirroring configuration
        log.info("Destroying CephFS mirroring configuration...")
        fs_mirroring_utils.destroy_cephfs_mirroring(
            source_fs,
            source_clients[0],
            target_fs,
            target_clients[0],
            target_user,
            peer_uuid,
        )

        log.info("=" * 60)
        log.info("CephFS Mirroring Teardown Complete")
        log.info("=" * 60)

        return 0

    except Exception as e:
        log.error("CephFS mirroring teardown failed: %s", e)
        import traceback

        log.error(traceback.format_exc())
        # Return 0 anyway to not fail the suite on cleanup errors
        return 0
