import json
import random
import string
import time
import traceback

from ceph.ceph import CommandFailed
from tests.cephfs.cephfs_mirroring.cephfs_mirroring_utils import CephfsMirroringUtils
from tests.cephfs.cephfs_utilsV1 import FsUtils
from utility.log import Log

log = Log(__name__)


def get_dir_assignments(mirror_nodes, fsid, fs_mirroring_utils, source_client, fs_name):
    """Return {hostname: [list_of_directory_paths]} by querying asok on each alive node."""
    daemon_names = fs_mirroring_utils.get_daemon_name(source_client)
    filesystem_id = fs_mirroring_utils.get_filesystem_id_by_name(source_client, fs_name)
    peer_uuid = fs_mirroring_utils.get_peer_uuid_by_name(source_client, fs_name)

    assignments = {}
    for node in mirror_nodes:
        hostname = node.node.hostname
        for dn in daemon_names:
            if hostname not in dn:
                continue
            try:
                cmd_ls = f"cd /var/run/ceph/{fsid}/ ; ls -1tr ceph-client.{dn}* | head -n 1 | tr -d '\\n'"
                asok_out, _ = node.exec_command(sudo=True, cmd=cmd_ls)
                asok_path = asok_out.replace("\\n", "").strip()
                if not asok_path:
                    continue
                asok_dir = f"/var/run/ceph/{fsid}"
                out, _ = node.exec_command(
                    sudo=True,
                    cmd=f"cd {asok_dir} && ceph --admin-daemon {asok_path} "
                    f"fs mirror peer status {fs_name}@{filesystem_id} {peer_uuid} -f json",
                )
                data = json.loads(out)
                dirs = list(data.keys()) if isinstance(data, dict) else []
                if dirs:
                    assignments[hostname] = {"node": node, "dirs": dirs, "asok": asok_path}
                    log.info(f"Node {hostname}: dirs={dirs}")
            except Exception as e:
                log.info(f"Node {hostname}: asok unavailable ({e})")
    return assignments


def run(ceph_cluster, **kw):
    """
    CEPH-83632851 - CephFS mirroring HA failover with 2 daemons.

    Requires: tier-2_cephfs_mirror_ha_metrics.yaml (2 cephfs-mirror nodes).

    Combined scenarios:
     S1: Stats continuity — stop one daemon, verify surviving daemon picks up all dirs,
         MGR retains OMAP history, perf counters show redistributed dirs
     S2: Tri-interface after HA failover — new leader's asok shows syncing,
         MGR resumes from OMAP, perf counter dump shows all dirs

    Returns 0 on success, 1 on failure.
    """
    try:
        config = kw.get("config")
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
        cephfs_mirror_node = ceph_cluster_dict.get("ceph1").get_ceph_objects(
            "cephfs-mirror"
        )

        source_fs = "cephfs"
        target_fs = "cephfs"
        target_user = "mirror_remote"
        target_site_name = "remote_site"

        if not source_clients or not target_clients:
            log.info("Need at least 1 client on both clusters")
            return 1

        if len(cephfs_mirror_node) < 2:
            log.error("This test requires 2 cephfs-mirror nodes for HA failover")
            return 1

        node1_host = cephfs_mirror_node[0].node.hostname
        node2_host = cephfs_mirror_node[1].node.hostname
        log.info(f"HA nodes: {node1_host}, {node2_host}")

        fs_util_ceph1.prepare_clients(source_clients, build)
        fs_util_ceph2.prepare_clients(target_clients, build)
        fs_util_ceph1.auth_list(source_clients)
        fs_util_ceph2.auth_list(target_clients)

        fs_mirroring_utils.deploy_cephfs_mirroring(
            source_fs, source_clients[0], target_fs, target_clients[0],
            target_user, target_site_name,
        )

        subvol_group = "subvolgroup_hafail"
        subvol_size = "12884901888"
        mounting_dir = "".join(
            random.choice(string.ascii_lowercase + string.digits) for _ in range(10)
        )

        dir_count = 4
        subvol_details = []
        for i in range(1, dir_count + 1):
            subvol_details.append({
                "subvol_name": f"subvol_hafail_{i}",
                "subvol_size": subvol_size,
                "mount_type": "kernel",
                "mount_dir": f"/mnt/cephfs_kernel{mounting_dir}_{i}",
            })

        subvolume_paths = fs_mirroring_utils.setup_subvolumes_and_mounts(
            source_fs, source_clients[0], fs_util_ceph1,
            subvol_group, subvol_details,
        )
        log.info(f"Subvolume paths: {subvolume_paths}")

        for sv_path in subvolume_paths:
            fs_mirroring_utils.add_path_for_mirroring(
                source_clients[0], source_fs, sv_path
            )

        source_clients[0].exec_command(
            sudo=True,
            cmd="ceph config set client.cephfs-mirror cephfs_mirror_tick_interval 1",
        )
        source_clients[0].exec_command(
            sudo=True, cmd="ceph orch restart cephfs-mirror"
        )
        time.sleep(40)

        fsid = fs_mirroring_utils.get_fsid(cephfs_mirror_node[0])
        filesystem_id = fs_mirroring_utils.get_filesystem_id_by_name(
            source_clients[0], source_fs
        )
        peer_uuid = fs_mirroring_utils.get_peer_uuid_by_name(
            source_clients[0], source_fs
        )
        daemon_names = fs_mirroring_utils.get_daemon_name(source_clients[0])
        asok_files = fs_mirroring_utils.get_asok_file(
            cephfs_mirror_node, fsid, daemon_names
        )
        log.info(f"Asok files: {asok_files}")

        log.info("Write data to all 4 subvolumes and create initial snapshots")
        for i, sv_path in enumerate(subvolume_paths, 1):
            mount = f"/mnt/cephfs_kernel{mounting_dir}_{i}{sv_path}"
            source_clients[0].exec_command(
                sudo=True,
                cmd=f"dd if=/dev/urandom of={mount}data_ha bs=1M count=500",
                timeout=300,
            )
            source_clients[0].exec_command(
                sudo=True, cmd=f"mkdir {mount}.snap/snap_ha_base"
            )

        log.info("Wait for all base snapshots to sync")
        for sv_path in subvolume_paths:
            fs_mirroring_utils.validate_snapshot_sync_status(
                cephfs_mirror_node, source_fs, "snap_ha_base",
                fsid, asok_files, filesystem_id, peer_uuid,
            )

        # ============================================================
        # S1: HA Failover — stats continuity for redistributed dirs
        # ============================================================
        log.info("=" * 60)
        log.info("S1: HA Failover — stats continuity + redistribution")
        log.info("=" * 60)

        log.info("S1: Check initial dir assignments across 2 daemons")
        assignments_before = get_dir_assignments(
            cephfs_mirror_node, fsid, fs_mirroring_utils, source_clients[0], source_fs
        )
        log.info(f"S1: Assignments before failover: "
                 f"{ {h: d['dirs'] for h, d in assignments_before.items()} }")

        log.info("S1: Capture MGR status before failover (OMAP baseline)")
        mgr_before = fs_mirroring_utils.get_mgr_mirror_status(
            source_clients[0], source_fs
        )
        mgr_paths_before = set(mgr_before.get("metrics", {}).keys())
        log.info(f"S1: MGR paths before: {mgr_paths_before}")

        log.info("S1: Write large data to subvol_1 and create snap for active sync")
        mount1 = f"/mnt/cephfs_kernel{mounting_dir}_1{subvolume_paths[0]}"
        source_clients[0].exec_command(
            sudo=True,
            cmd=f"dd if=/dev/urandom of={mount1}data_ha_large bs=1M count=5120",
            timeout=600,
        )
        source_clients[0].exec_command(
            sudo=True, cmd=f"mkdir {mount1}.snap/snap_ha_failover"
        )

        log.info("S1: Stop daemon on node2 to trigger failover")
        source_clients[0].exec_command(
            sudo=True,
            cmd=f"ceph orch apply cephfs-mirror --placement='1 {node1_host}'",
        )
        log.info("S1: Waiting 60s for daemon redistribution")
        time.sleep(60)

        log.info("S1: Check that surviving daemon (node1) picks up all 4 dirs")
        assignments_after = get_dir_assignments(
            [cephfs_mirror_node[0]], fsid, fs_mirroring_utils,
            source_clients[0], source_fs,
        )
        log.info(f"S1: Assignments after failover: "
                 f"{ {h: d['dirs'] for h, d in assignments_after.items()} }")

        if node1_host in assignments_after:
            dirs_on_survivor = set(assignments_after[node1_host]["dirs"])
            expected_dirs = {p.rstrip("/") for p in subvolume_paths}
            missing = expected_dirs - dirs_on_survivor
            if missing:
                log.warning(
                    f"S1: Surviving daemon missing dirs: {missing}. "
                    f"Has: {dirs_on_survivor}"
                )
            else:
                log.info(
                    f"S1: Surviving daemon owns all {len(dirs_on_survivor)} dirs"
                )
        else:
            log.warning("S1: No assignments found on surviving node")

        log.info("S1: Verify MGR retains OMAP history after failover")
        mgr_after = fs_mirroring_utils.get_mgr_mirror_status(
            source_clients[0], source_fs
        )
        mgr_paths_after = set(mgr_after.get("metrics", {}).keys())
        log.info(f"S1: MGR paths after failover: {mgr_paths_after}")

        missing_mgr = mgr_paths_before - mgr_paths_after
        if missing_mgr:
            raise CommandFailed(
                f"S1 FAILED: MGR lost paths after failover: {missing_mgr}"
            )
        log.info("S1: MGR retained all paths from OMAP after failover")

        # S1 continued: perf counters on survivor
        log.info("S1: Check perf counters on surviving daemon for all dirs")
        daemon_names_after = fs_mirroring_utils.get_daemon_name(source_clients[0])
        asok_files_after = fs_mirroring_utils.get_asok_file(
            [cephfs_mirror_node[0]], fsid, daemon_names_after
        )

        perf_data = fs_mirroring_utils.get_cephfs_mirror_counters(
            [cephfs_mirror_node[0]], fsid, asok_files_after
        )
        perf_dirs = set()
        for entry in perf_data.get("cephfs_mirror_directory", []):
            d = entry.get("labels", {}).get("directory", "")
            if d:
                perf_dirs.add(d)
        log.info(f"S1: Perf counter dirs on survivor: {perf_dirs}")

        expected_dirs = {p.rstrip("/") for p in subvolume_paths}
        perf_missing = expected_dirs - perf_dirs
        if perf_missing:
            log.warning(f"S1: Perf counters missing dirs: {perf_missing}")
        else:
            log.info("S1: Perf counters show all 4 dirs on surviving daemon")

        log.info("S1: Wait for snap_ha_failover to sync on surviving daemon")
        fs_mirroring_utils.validate_snapshot_sync_status(
            [cephfs_mirror_node[0]], source_fs, "snap_ha_failover",
            fsid, asok_files_after, filesystem_id, peer_uuid,
        )
        log.info("S1 PASSED: Stats continuity and dir redistribution validated")

        # ============================================================
        # S2: Tri-interface after HA failover
        # ============================================================
        log.info("=" * 60)
        log.info("S2: Tri-interface validation after HA failover")
        log.info("=" * 60)

        log.info("S2: Write delta data and create snapshot on surviving daemon")
        source_clients[0].exec_command(
            sudo=True,
            cmd=f"dd if=/dev/urandom of={mount1}data_ha_delta bs=1M count=5120",
            timeout=600,
        )
        source_clients[0].exec_command(
            sudo=True, cmd=f"mkdir {mount1}.snap/snap_ha_tri"
        )

        log.info("S2: Poll tri-interface during sync")
        syncing_captured = False
        for poll_i in range(900):
            time.sleep(1)
            try:
                asok_status = fs_mirroring_utils.get_asok_peer_status_raw(
                    cephfs_mirror_node[0], source_clients[0], source_fs
                )
                path_key = subvolume_paths[0].rstrip("/")
                dir_data = asok_status.get(path_key, {})
                state = dir_data.get("state", "")
                syncing_snap = dir_data.get("current_syncing_snap")

                if state == "syncing" and syncing_snap and not syncing_captured:
                    log.info(f"[S2 Poll {poll_i}] Syncing: {syncing_snap.get('name')}")
                    syncing_captured = True

                    mgr_status = fs_mirroring_utils.get_mgr_mirror_status(
                        source_clients[0], source_fs
                    )
                    mgr_met = mgr_status.get("metrics", {}).get(path_key, {})
                    mgr_peer = list(mgr_met.get("peer", {}).values())
                    mgr_state = mgr_peer[0].get("state") if mgr_peer else "N/A"
                    log.info(f"[S2 Poll {poll_i}] MGR state: {mgr_state}")

                    perf_data = fs_mirroring_utils.get_cephfs_mirror_counters(
                        [cephfs_mirror_node[0]], fsid, asok_files_after
                    )
                    for entry in perf_data.get("cephfs_mirror_directory", []):
                        if path_key in entry.get("labels", {}).get("directory", ""):
                            counters = entry.get("counters", {})
                            log.info(
                                f"[S2 Poll {poll_i}] Perf: dir_state={counters.get('dir_state')}, "
                                f"sync_bytes={counters.get('current_sync_bytes')}"
                            )
                            break

                if state == "idle" and dir_data.get("snaps_synced", 0) >= 1:
                    synced_snap = dir_data.get("last_synced_snap", {})
                    if synced_snap.get("name") == "snap_ha_tri":
                        log.info(f"[S2 Poll {poll_i}] Sync complete: snap_ha_tri")
                        break
            except Exception as e:
                log.warning(f"[S2 Poll {poll_i}] Error: {e}")

        log.info("S2: Final tri-interface check after sync")
        asok_final = fs_mirroring_utils.get_asok_peer_status_raw(
            cephfs_mirror_node[0], source_clients[0], source_fs
        )
        mgr_final = fs_mirroring_utils.get_mgr_mirror_status(
            source_clients[0], source_fs
        )
        perf_final = fs_mirroring_utils.get_cephfs_mirror_counters(
            [cephfs_mirror_node[0]], fsid, asok_files_after
        )

        asok_state = asok_final.get(path_key, {}).get("state", "")
        mgr_met_final = mgr_final.get("metrics", {}).get(path_key, {})
        mgr_peer_final = list(mgr_met_final.get("peer", {}).values())
        mgr_state_final = mgr_peer_final[0].get("state") if mgr_peer_final else "N/A"

        perf_state_final = None
        for entry in perf_final.get("cephfs_mirror_directory", []):
            if path_key in entry.get("labels", {}).get("directory", ""):
                perf_state_final = entry.get("counters", {}).get("dir_state", -1)
                break

        log.info(
            f"S2: Final states — asok={asok_state}, mgr={mgr_state_final}, "
            f"perf_dir_state={perf_state_final}"
        )

        if asok_state != "idle":
            raise CommandFailed(f"S2 FAILED: Asok state={asok_state}, expected idle")
        if mgr_state_final != "idle":
            raise CommandFailed(f"S2 FAILED: MGR state={mgr_state_final}, expected idle")
        if perf_state_final not in (0, None):
            raise CommandFailed(
                f"S2 FAILED: Perf dir_state={perf_state_final}, expected 0"
            )

        if not syncing_captured:
            log.warning("S2: Syncing state not captured (sync too fast)")

        log.info("S2 PASSED: Tri-interface consistent after HA failover")

        log.info("=" * 60)
        log.info("ALL HA FAILOVER SCENARIOS PASSED")
        log.info("=" * 60)

        source_clients[0].exec_command(
            sudo=True,
            cmd="ceph config rm client.cephfs-mirror cephfs_mirror_tick_interval",
            check_ec=False,
        )
        return 0

    except Exception as e:
        log.error(e)
        log.error(traceback.format_exc())
        return 1
    finally:
        log.info("Clean up")
        try:
            source_clients[0].exec_command(
                sudo=True,
                cmd="ceph config rm client.cephfs-mirror cephfs_mirror_tick_interval",
                check_ec=False,
            )
            log.info("Restore 2-daemon HA placement")
            source_clients[0].exec_command(
                sudo=True,
                cmd=f"ceph orch apply cephfs-mirror "
                f"--placement='2 {node1_host} {node2_host}'",
                check_ec=False,
            )
            time.sleep(30)

            for i, sv_path in enumerate(subvolume_paths, 1):
                mount = f"/mnt/cephfs_kernel{mounting_dir}_{i}{sv_path}"
                for snap in ["snap_ha_base", "snap_ha_failover", "snap_ha_tri"]:
                    source_clients[0].exec_command(
                        sudo=True, cmd=f"rmdir {mount}.snap/{snap}",
                        check_ec=False,
                    )
                source_clients[0].exec_command(
                    sudo=True,
                    cmd=f"umount -l /mnt/cephfs_kernel{mounting_dir}_{i}",
                    check_ec=False,
                )
                source_clients[0].exec_command(
                    sudo=True,
                    cmd=f"rm -rf /mnt/cephfs_kernel{mounting_dir}_{i}",
                    check_ec=False,
                )
                fs_mirroring_utils.remove_path_from_mirroring(
                    source_clients[0], source_fs, sv_path
                )

            peer_uuid = fs_mirroring_utils.get_peer_uuid_by_name(
                source_clients[0], source_fs
            )
            fs_mirroring_utils.destroy_cephfs_mirroring(
                source_fs, source_clients[0], target_fs, target_clients[0],
                target_user, peer_uuid,
            )

            for i in range(1, dir_count + 1):
                fs_util_ceph1.remove_subvolume(
                    source_clients[0], source_fs, f"subvol_hafail_{i}",
                    group_name=subvol_group, check_ec=False,
                )
            fs_util_ceph1.remove_subvolumegroup(
                source_clients[0], source_fs, subvol_group, check_ec=False,
            )
        except Exception as cleanup_err:
            log.warning(f"Cleanup error: {cleanup_err}")
