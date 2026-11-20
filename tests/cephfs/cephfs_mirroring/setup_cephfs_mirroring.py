"""
CephFS Mirroring Setup Module

This module is used at the start of a test suite to configure CephFS mirroring
between source and target clusters. It should be run once before functional tests.

Usage in suite YAML:
    - test:
        name: setup cephfs mirroring
        module: cephfs_mirroring.setup_cephfs_mirroring.py
        abort-on-fail: true
        clusters:
          ceph1:
            config:
              source_fs: cephfs
              target_fs: cephfs
              target_user: mirror_remote
              target_site_name: remote_site
"""

from tests.cephfs.cephfs_mirroring.cephfs_mirroring_utils import CephfsMirroringUtils
from tests.cephfs.cephfs_utilsV1 import FsUtils
from utility.log import Log

log = Log(__name__)


def run(ceph_cluster, **kw):
    """
    Setup CephFS mirroring between source and target clusters.

    This is a one-time setup module to be run at the start of a test suite.
    Subsequent tests can assume mirroring is already configured.

    Args:
        ceph_cluster: The Ceph cluster object
        **kw: Keyword arguments including config and cluster dict

    Returns:
        0 on success, 1 on failure
    """
    try:
        config = kw.get("config") or {}
        ceph_cluster_dict = kw.get("ceph_cluster_dict")
        test_data = kw.get("test_data")

        fs_util_ceph1 = FsUtils(ceph_cluster_dict.get("ceph1"), test_data=test_data)
        fs_util_ceph2 = FsUtils(ceph_cluster_dict.get("ceph2"), test_data=test_data)
        fs_mirroring_utils = CephfsMirroringUtils(
            ceph_cluster_dict.get("ceph1"), ceph_cluster_dict.get("ceph2")
        )

        build = config.get("build", config.get("rhbuild"))
        source_clients = ceph_cluster_dict.get("ceph1").get_ceph_objects("client")
        target_clients = ceph_cluster_dict.get("ceph2").get_ceph_objects("client")
        cephfs_mirror_nodes = ceph_cluster_dict.get("ceph1").get_ceph_objects(
            "cephfs-mirror"
        )

        # Config parameters with defaults
        source_fs = config.get("source_fs", "cephfs")
        target_fs = config.get("target_fs", "cephfs")
        target_user = config.get("target_user", "mirror_remote")
        target_site_name = config.get("target_site_name", "remote_site")

        log.info("=" * 60)
        log.info("CephFS Mirroring Setup Module")
        log.info("=" * 60)

        # Validate prerequisites
        if not source_clients or not target_clients:
            log.error(
                "This module requires at least 1 client node on both ceph1 and ceph2."
            )
            return 1

        # Prepare clients
        log.info("Preparing clients...")
        fs_util_ceph1.prepare_clients(source_clients, build)
        fs_util_ceph2.prepare_clients(target_clients, build)
        fs_util_ceph1.auth_list(source_clients)
        fs_util_ceph2.auth_list(target_clients)

        # Verify filesystems exist
        log.info("Verifying filesystems exist...")
        fs_details_source = fs_util_ceph1.get_fs_info(source_clients[0], source_fs)
        if not fs_details_source:
            log.info("Creating source filesystem: %s", source_fs)
            fs_util_ceph1.create_fs(source_clients[0], source_fs)

        fs_details_target = fs_util_ceph2.get_fs_info(target_clients[0], target_fs)
        if not fs_details_target:
            log.info("Creating target filesystem: %s", target_fs)
            fs_util_ceph2.create_fs(target_clients[0], target_fs)

        # Check if mirroring is already configured
        check_result = fs_mirroring_utils.is_mirroring_configured(
            source_clients[0], source_fs
        )
        if check_result["configured"]:
            log.info(
                "Mirroring already configured for %s (peer_uuid=%s), skipping setup",
                source_fs,
                check_result["peer_uuid"],
            )
            return 0

        # Deploy CephFS mirroring
        log.info("Deploying CephFS mirroring configuration...")
        log.info("  Source FS: %s", source_fs)
        log.info("  Target FS: %s", target_fs)
        log.info("  Target User: %s", target_user)
        log.info("  Target Site: %s", target_site_name)

        token = fs_mirroring_utils.deploy_cephfs_mirroring(
            source_fs,
            source_clients[0],
            target_fs,
            target_clients[0],
            target_user,
            target_site_name,
        )

        if token:
            log.info("CephFS mirroring setup completed successfully")
            log.info("Bootstrap token: %s", token[:50] + "..." if token else "N/A")
        else:
            log.info("CephFS mirroring setup completed (token not returned)")

        # Install ceph-common on cephfs-mirror daemon nodes (needed for asok commands)
        log.info("Installing ceph-common on cephfs-mirror daemon nodes...")
        for node in cephfs_mirror_nodes:
            try:
                node.exec_command(
                    sudo=True, cmd="dnf install -y ceph-common --nogpgcheck"
                )
                log.info(
                    "  Installed ceph-common on %s",
                    getattr(getattr(node, "node", node), "hostname", str(node)),
                )
            except Exception as e:
                log.warning("  Failed to install ceph-common (non-fatal): %s", e)

        # Verify setup
        peer_uuid = fs_mirroring_utils.get_peer_uuid_by_name(
            source_clients[0], source_fs
        )
        log.info("Verified peer connection: %s", peer_uuid)

        log.info("=" * 60)
        log.info("CephFS Mirroring Setup Complete")
        log.info("=" * 60)

        return 0

    except Exception as e:
        log.error("CephFS mirroring setup failed: %s", e)
        import traceback

        log.error(traceback.format_exc())
        return 1
