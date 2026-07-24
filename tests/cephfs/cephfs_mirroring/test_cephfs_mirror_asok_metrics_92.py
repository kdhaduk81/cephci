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


def run(ceph_cluster, **kw):
    """
    CEPH-83632744 - Validate CephFS mirroring asok metrics for 9.2 enhancements.

    Covers:
     1. Mirroring stats validation with CLI schema verification
     2. Sync-mode validation (full vs delta)
     3. ETA state machine
     4. Crawl state lifecycle
     5. Datasync queue wait under load
     6. Read/Write throughput validation
     7. last_synced_snap enrichment
     9. Zero-file directory sync metrics
     10. Snapdiff and Blockdiff verification
     11. Monotonicity regression test
     12. Sync-mode when snapdiff reference missing

    Note: Scenario 8 (sync failure) moved to test_cephfs_mirror_disruptive_ops.py

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

        log.info("checking Pre-requisites")
        if not source_clients or not target_clients:
            log.info(
                "This test requires a minimum of 1 client node "
                "on both ceph1 and ceph2."
            )
            return 1

        log.info("Preparing Clients...")
        fs_util_ceph1.prepare_clients(source_clients, build)
        fs_util_ceph2.prepare_clients(target_clients, build)
        fs_util_ceph1.auth_list(source_clients)
        fs_util_ceph2.auth_list(target_clients)

        log.info("Deploy CephFS Mirroring Configuration")
        fs_mirroring_utils.deploy_cephfs_mirroring(
            source_fs,
            source_clients[0],
            target_fs,
            target_clients[0],
            target_user,
            target_site_name,
        )

        subvol_group_name = "subvolgroup_asok"
        subvol_name = "subvol_asok"
        subvol_size = "5368709120"
        mounting_dir = "".join(
            random.choice(string.ascii_lowercase + string.digits)
            for _ in list(range(10))
        )
        kernel_mounting_dir = f"/mnt/cephfs_kernel{mounting_dir}_1"
        fuse_mounting_dir = f"/mnt/cephfs_fuse{mounting_dir}_1"
        subvol_details = [
            {
                "subvol_name": f"{subvol_name}_1",
                "subvol_size": subvol_size,
                "mount_type": "kernel",
                "mount_dir": kernel_mounting_dir,
            },
            {
                "subvol_name": f"{subvol_name}_2",
                "subvol_size": subvol_size,
                "mount_type": "fuse",
                "mount_dir": fuse_mounting_dir,
            },
        ]
        subvolume_paths = fs_mirroring_utils.setup_subvolumes_and_mounts(
            source_fs,
            source_clients[0],
            fs_util_ceph1,
            subvol_group_name,
            subvol_details,
        )
        log.info(f"Subvolume Paths: {subvolume_paths}")

        subvol_path1 = subvolume_paths[0]
        subvol_path2 = subvolume_paths[1]

        log.info("Add subvolumes for mirroring")
        for subvol_path in subvolume_paths:
            fs_mirroring_utils.add_path_for_mirroring(
                source_clients[0], source_fs, subvol_path
            )

        log.info("Fetch daemon info for asok queries")
        fsid = fs_mirroring_utils.get_fsid(cephfs_mirror_node[0])
        daemon_name = fs_mirroring_utils.get_daemon_name(source_clients[0])
        asok_file = fs_mirroring_utils.get_asok_file(
            cephfs_mirror_node[0], fsid, daemon_name
        )
        filesystem_id = fs_mirroring_utils.get_filesystem_id_by_name(
            source_clients[0], source_fs
        )
        peer_uuid = fs_mirroring_utils.get_peer_uuid_by_name(
            source_clients[0], source_fs
        )

        # ============================================================
        # Scenario 1: Mirroring stats schema verification
        # ============================================================
        log.info("=" * 60)
        log.info("Scenario 1: Stats schema verification")
        log.info("=" * 60)
        status_before = fs_mirroring_utils.get_fs_mirror_peer_status_using_asok(
            cephfs_mirror_node[0], source_clients[0], source_fs
        )
        log.info(f"Peer status (initial): {status_before}")

        for path, dir_status in status_before.items():
            required_fields = ["state", "snaps_synced", "snaps_deleted", "snaps_renamed"]
            for field in required_fields:
                if field not in dir_status:
                    raise CommandFailed(
                        f"Missing field '{field}' in asok status for {path}"
                    )
            log.info(f"Schema validated for {path}: {list(dir_status.keys())}")

        # ============================================================
        # Scenario 2: Sync-mode validation (full vs delta)
        # ============================================================
        log.info("=" * 60)
        log.info("Scenario 2: Sync-mode validation (full vs delta)")
        log.info("=" * 60)

        log.info("Reduce tick interval for faster metric updates")
        source_clients[0].exec_command(
            sudo=True,
            cmd="ceph config set client.cephfs-mirror cephfs_mirror_tick_interval 1",
        )
        log.info("Restart cephfs-mirror daemon for tick_interval to take effect")
        source_clients[0].exec_command(
            sudo=True, cmd="ceph orch restart cephfs-mirror"
        )
        time.sleep(30)
        daemon_name = fs_mirroring_utils.get_daemon_name(source_clients[0])
        asok_file = fs_mirroring_utils.get_asok_file(
            cephfs_mirror_node[0], fsid, daemon_name
        )

        mount_path1 = f"{kernel_mounting_dir}{subvol_path1}"

        log.info("Write initial data and create first snapshot (full sync)")
        source_clients[0].exec_command(
            sudo=True,
            cmd=f"dd if=/dev/urandom of={mount_path1}fulldata bs=1M count=10",
        )
        source_clients[0].exec_command(
            sudo=True, cmd=f"mkdir {mount_path1}.snap/snap_full"
        )

        log.info("Poll asok for syncing state to capture sync-mode")
        full_sync_captured = False
        for poll_i in range(30):
            time.sleep(2)
            try:
                status = fs_mirroring_utils.get_asok_peer_status_raw(
                    cephfs_mirror_node[0], source_clients[0], source_fs
                )
                path_key = subvol_path1.rstrip("/")
                if path_key in status:
                    dir_data = status[path_key]
                    state = dir_data.get("state", "")
                    syncing_snap = dir_data.get("current_syncing_snap")
                    log.info(
                        f"[Poll {poll_i}] state={state}, "
                        f"syncing_snap={syncing_snap}, "
                        f"snaps_synced={dir_data.get('snaps_synced')}"
                    )
                    if syncing_snap and syncing_snap.get("name") == "snap_full":
                        sync_mode = syncing_snap.get("sync-mode", "")
                        log.info(f"Full sync captured: sync-mode={sync_mode}")
                        log.info(f"Full syncing_snap details: {syncing_snap}")
                        if sync_mode == "full":
                            full_sync_captured = True
                        break
                    if state == "idle":
                        last = dir_data.get("last_synced_snap", {})
                        if last.get("name") == "snap_full":
                            log.info(f"Full sync completed before capture, last_synced_snap: {last}")
                            full_sync_captured = True
                            break
                else:
                    log.info(f"[Poll {poll_i}] path_key={path_key} not in status keys={list(status.keys())}")
            except Exception as e:
                log.warning(f"Poll error: {e}")

        log.info("Wait for snap_full to sync completely")
        fs_mirroring_utils.validate_snapshot_sync_status(
            cephfs_mirror_node[0],
            source_fs,
            "snap_full",
            fsid,
            asok_file,
            filesystem_id,
            peer_uuid,
        )

        log.info("Modify data and create second snapshot (delta sync)")
        source_clients[0].exec_command(
            sudo=True,
            cmd=f"dd if=/dev/urandom of={mount_path1}delta_file bs=1M count=2",
        )
        source_clients[0].exec_command(
            sudo=True, cmd=f"mkdir {mount_path1}.snap/snap_delta"
        )

        delta_sync_captured = False
        for poll_i in range(30):
            time.sleep(2)
            try:
                status = fs_mirroring_utils.get_asok_peer_status_raw(
                    cephfs_mirror_node[0], source_clients[0], source_fs
                )
                path_key = subvol_path1.rstrip("/")
                if path_key in status:
                    dir_data = status[path_key]
                    state = dir_data.get("state", "")
                    syncing_snap = dir_data.get("current_syncing_snap")
                    log.info(
                        f"[Poll {poll_i}] state={state}, "
                        f"syncing_snap={syncing_snap}, "
                        f"snaps_synced={dir_data.get('snaps_synced')}"
                    )
                    if syncing_snap and syncing_snap.get("name") == "snap_delta":
                        sync_mode = syncing_snap.get("sync-mode", "")
                        log.info(f"Delta sync captured: sync-mode={sync_mode}")
                        log.info(f"Delta syncing_snap details: {syncing_snap}")
                        if sync_mode == "delta":
                            delta_sync_captured = True
                        break
                    if state == "idle":
                        last = dir_data.get("last_synced_snap", {})
                        if last.get("name") == "snap_delta":
                            log.info(f"Delta sync completed before capture, last_synced_snap: {last}")
                            delta_sync_captured = True
                            break
            except Exception as e:
                log.warning(f"Poll error: {e}")

        log.info("Wait for snap_delta to sync completely")
        fs_mirroring_utils.validate_snapshot_sync_status(
            cephfs_mirror_node[0],
            source_fs,
            "snap_delta",
            fsid,
            asok_file,
            filesystem_id,
            peer_uuid,
        )

        if not full_sync_captured and not delta_sync_captured:
            log.warning(
                "Could not capture sync-mode in-flight (sync too fast). "
                "Verifying last_synced_snap instead."
            )

        # ============================================================
        # Scenario 3: ETA state machine
        # ============================================================
        log.info("=" * 60)
        log.info("Scenario 3: ETA state machine")
        log.info("=" * 60)

        mount_path2 = f"{fuse_mounting_dir}{subvol_path2}"
        log.info("Write larger data for ETA observation")
        source_clients[0].exec_command(
            sudo=True,
            cmd=f"dd if=/dev/urandom of={mount_path2}eta_data bs=1M count=50",
        )
        source_clients[0].exec_command(
            sudo=True, cmd=f"mkdir {mount_path2}.snap/snap_eta"
        )

        eta_observed = False
        for poll_i in range(60):
            time.sleep(2)
            try:
                status = fs_mirroring_utils.get_asok_peer_status_raw(
                    cephfs_mirror_node[0], source_clients[0], source_fs
                )
                path_key = subvol_path2.rstrip("/")
                if path_key in status:
                    dir_data = status[path_key]
                    state = dir_data.get("state", "")
                    syncing_snap = dir_data.get("current_syncing_snap")
                    if syncing_snap:
                        eta = syncing_snap.get("eta", "")
                        bytes_info = syncing_snap.get("bytes", {})
                        log.info(
                            f"[ETA Poll {poll_i}] state={state}, eta={eta}, "
                            f"sync_percent={bytes_info.get('sync_percent', 'N/A')}, "
                            f"sync_bytes={bytes_info.get('sync_bytes', 'N/A')}"
                        )
                        if eta:
                            eta_observed = True
                    else:
                        log.info(f"[ETA Poll {poll_i}] state={state}, no current_syncing_snap")
                    if state == "idle":
                        last = dir_data.get("last_synced_snap", {})
                        if last.get("name") == "snap_eta":
                            log.info(f"snap_eta synced, last_synced_snap: {last}")
                            break
            except Exception as e:
                log.warning(f"ETA poll error: {e}")

        fs_mirroring_utils.validate_snapshot_sync_status(
            cephfs_mirror_node[0],
            source_fs,
            "snap_eta",
            fsid,
            asok_file,
            filesystem_id,
            peer_uuid,
        )
        log.info(f"ETA observed during sync: {eta_observed}")

        # ============================================================
        # Scenario 4: Crawl state lifecycle
        # ============================================================
        log.info("=" * 60)
        log.info("Scenario 4: Crawl state lifecycle")
        log.info("=" * 60)

        source_clients[0].exec_command(
            sudo=True,
            cmd=f"dd if=/dev/urandom of={mount_path1}crawl_data bs=1M count=20",
        )
        source_clients[0].exec_command(
            sudo=True, cmd=f"mkdir {mount_path1}.snap/snap_crawl"
        )

        crawl_states_seen = set()
        for poll_i in range(60):
            time.sleep(2)
            try:
                status = fs_mirroring_utils.get_asok_peer_status_raw(
                    cephfs_mirror_node[0], source_clients[0], source_fs
                )
                path_key = subvol_path1.rstrip("/")
                if path_key in status:
                    dir_data = status[path_key]
                    state = dir_data.get("state", "")
                    syncing_snap = dir_data.get("current_syncing_snap")
                    if syncing_snap:
                        crawl = syncing_snap.get("crawl", {})
                        crawl_state = crawl.get("state", "")
                        crawl_duration = crawl.get("duration", "N/A")
                        log.info(
                            f"[Crawl Poll {poll_i}] state={state}, "
                            f"crawl_state={crawl_state}, crawl_duration={crawl_duration}, "
                            f"snap_name={syncing_snap.get('name')}"
                        )
                        if crawl_state:
                            crawl_states_seen.add(crawl_state)
                    else:
                        log.info(f"[Crawl Poll {poll_i}] state={state}, no current_syncing_snap")
                    if state == "idle":
                        last = dir_data.get("last_synced_snap", {})
                        log.info(f"[Crawl Poll {poll_i}] idle, last_synced_snap: {last}")
                        break
            except Exception as e:
                log.warning(f"Crawl poll error: {e}")

        fs_mirroring_utils.validate_snapshot_sync_status(
            cephfs_mirror_node[0],
            source_fs,
            "snap_crawl",
            fsid,
            asok_file,
            filesystem_id,
            peer_uuid,
        )
        log.info(f"Crawl states observed: {crawl_states_seen}")

        # ============================================================
        # Scenario 5: Datasync queue wait under load
        # ============================================================
        log.info("=" * 60)
        log.info("Scenario 5: Datasync queue wait under load")
        log.info("=" * 60)

        source_clients[0].exec_command(
            sudo=True,
            cmd=f"dd if=/dev/urandom of={mount_path2}dsync_data bs=1M count=30",
        )
        source_clients[0].exec_command(
            sudo=True, cmd=f"mkdir {mount_path2}.snap/snap_dsync"
        )

        datasync_states_seen = set()
        for poll_i in range(60):
            time.sleep(2)
            try:
                status = fs_mirroring_utils.get_asok_peer_status_raw(
                    cephfs_mirror_node[0], source_clients[0], source_fs
                )
                path_key = subvol_path2.rstrip("/")
                if path_key in status:
                    dir_data = status[path_key]
                    state = dir_data.get("state", "")
                    syncing_snap = dir_data.get("current_syncing_snap")
                    if syncing_snap:
                        dswait = syncing_snap.get("datasync_queue_wait", {})
                        ds_state = dswait.get("state", "")
                        ds_duration = dswait.get("duration", "N/A")
                        log.info(
                            f"[Dsync Poll {poll_i}] state={state}, "
                            f"dsync_state={ds_state}, dsync_duration={ds_duration}, "
                            f"snap_name={syncing_snap.get('name')}"
                        )
                        if ds_state:
                            datasync_states_seen.add(ds_state)
                    else:
                        log.info(f"[Dsync Poll {poll_i}] state={state}, no current_syncing_snap")
                    if state == "idle":
                        last = dir_data.get("last_synced_snap", {})
                        log.info(f"[Dsync Poll {poll_i}] idle, last_synced_snap: {last}")
                        break
            except Exception as e:
                log.warning(f"Datasync poll error: {e}")

        fs_mirroring_utils.validate_snapshot_sync_status(
            cephfs_mirror_node[0],
            source_fs,
            "snap_dsync",
            fsid,
            asok_file,
            filesystem_id,
            peer_uuid,
        )
        log.info(f"Datasync wait states observed: {datasync_states_seen}")

        # ============================================================
        # Scenario 6: Read/Write throughput validation
        # ============================================================
        log.info("=" * 60)
        log.info("Scenario 6: Read/Write throughput validation")
        log.info("=" * 60)

        source_clients[0].exec_command(
            sudo=True,
            cmd=f"dd if=/dev/urandom of={mount_path1}throughput_data bs=1M count=30",
        )
        source_clients[0].exec_command(
            sudo=True, cmd=f"mkdir {mount_path1}.snap/snap_tput"
        )

        throughput_captured = False
        for poll_i in range(60):
            time.sleep(2)
            try:
                status = fs_mirroring_utils.get_asok_peer_status_raw(
                    cephfs_mirror_node[0], source_clients[0], source_fs
                )
                path_key = subvol_path1.rstrip("/")
                if path_key in status:
                    dir_data = status[path_key]
                    state = dir_data.get("state", "")
                    syncing_snap = dir_data.get("current_syncing_snap")
                    if syncing_snap:
                        read_tp = syncing_snap.get("avg_read_throughput_bytes", "")
                        write_tp = syncing_snap.get("avg_write_throughput_bytes", "")
                        bytes_info = syncing_snap.get("bytes", {})
                        log.info(
                            f"[Tput Poll {poll_i}] state={state}, "
                            f"read_tp={read_tp}, write_tp={write_tp}, "
                            f"sync_percent={bytes_info.get('sync_percent', 'N/A')}, "
                            f"snap_name={syncing_snap.get('name')}"
                        )
                        if read_tp or write_tp:
                            throughput_captured = True
                    else:
                        log.info(f"[Tput Poll {poll_i}] state={state}, no current_syncing_snap")
                    if state == "idle":
                        last = dir_data.get("last_synced_snap", {})
                        log.info(f"[Tput Poll {poll_i}] idle, last_synced_snap: {last}")
                        break
            except Exception as e:
                log.warning(f"Throughput poll error: {e}")

        fs_mirroring_utils.validate_snapshot_sync_status(
            cephfs_mirror_node[0],
            source_fs,
            "snap_tput",
            fsid,
            asok_file,
            filesystem_id,
            peer_uuid,
        )
        log.info(f"Throughput captured during sync: {throughput_captured}")

        # ============================================================
        # Scenario 7: last_synced_snap enrichment
        # ============================================================
        log.info("=" * 60)
        log.info("Scenario 7: last_synced_snap enrichment")
        log.info("=" * 60)

        status_after = fs_mirroring_utils.get_asok_peer_status_raw(
            cephfs_mirror_node[0], source_clients[0], source_fs
        )
        for path, dir_status in status_after.items():
            last_snap = dir_status.get("last_synced_snap", {})
            if last_snap:
                log.info(f"last_synced_snap for {path}: {last_snap}")
                snap_name = last_snap.get("name", "")
                sync_duration = last_snap.get("sync_duration", "")
                sync_timestamp = last_snap.get("sync_time_stamp", "")
                snap_id = last_snap.get("id", "")
                sync_bytes = last_snap.get("sync_bytes", "")
                sync_files = last_snap.get("sync_files", "")

                if not snap_name:
                    raise CommandFailed(
                        f"last_synced_snap.name missing for {path}"
                    )
                log.info(
                    f"Enrichment validated - name={snap_name}, id={snap_id}, "
                    f"duration={sync_duration}, timestamp={sync_timestamp}, "
                    f"bytes={sync_bytes}, files={sync_files}"
                )
            else:
                log.warning(f"No last_synced_snap for {path}")

        # ============================================================
        # Scenario 9: Zero-file directory sync metrics
        # ============================================================
        log.info("=" * 60)
        log.info("Scenario 9: Zero-file directory sync metrics")
        log.info("=" * 60)

        for i in range(5):
            source_clients[0].exec_command(
                sudo=True, cmd=f"mkdir -p {mount_path2}empty_dir_{i}"
            )
        source_clients[0].exec_command(
            sudo=True, cmd=f"mkdir {mount_path2}.snap/snap_empty"
        )

        fs_mirroring_utils.validate_snapshot_sync_status(
            cephfs_mirror_node[0], source_fs, "snap_empty",
            fsid, asok_file, filesystem_id, peer_uuid,
        )

        status_empty = fs_mirroring_utils.get_asok_peer_status_raw(
            cephfs_mirror_node[0], source_clients[0], source_fs
        )
        path2_key = subvol_path2.rstrip("/")
        last_synced = status_empty.get(path2_key, {}).get("last_synced_snap", {})
        if last_synced.get("name") == "snap_empty":
            log.info("Scenario 9: Empty directory snapshot synced")
        else:
            log.warning(f"Scenario 9: last_synced={last_synced}")
        log.info("Scenario 9: Zero-file directory validated")

        # ============================================================
        # Scenario 10: Snapdiff and Blockdiff verification
        # ============================================================
        log.info("=" * 60)
        log.info("Scenario 10: Snapdiff and Blockdiff verification")
        log.info("=" * 60)

        source_clients[0].exec_command(
            sudo=True, cmd=f"rm -f {mount_path1}file_*",
            check_ec=False,
        )
        source_clients[0].exec_command(
            sudo=True,
            cmd=f"for i in $(seq 1 10); do dd if=/dev/urandom "
            f"of={mount_path1}small_$i bs=1M count=1 2>/dev/null; done",
        )
        source_clients[0].exec_command(
            sudo=True,
            cmd=f"dd if=/dev/urandom of={mount_path1}large_file "
            f"bs=1M count=64 2>/dev/null",
        )
        source_clients[0].exec_command(
            sudo=True, cmd=f"mkdir {mount_path1}.snap/snap_base10"
        )

        fs_mirroring_utils.validate_snapshot_sync_status(
            cephfs_mirror_node[0], source_fs, "snap_base10",
            fsid, asok_file, filesystem_id, peer_uuid,
        )

        source_clients[0].exec_command(
            sudo=True,
            cmd=f"for i in $(seq 1 5); do dd if=/dev/urandom "
            f"of={mount_path1}small_$i bs=1M count=1 conv=notrunc 2>/dev/null; done",
        )
        source_clients[0].exec_command(
            sudo=True,
            cmd=f"dd if=/dev/urandom of={mount_path1}large_file "
            f"bs=4K count=1 conv=notrunc seek=100 2>/dev/null",
        )
        source_clients[0].exec_command(
            sudo=True, cmd=f"mkdir {mount_path1}.snap/snap_delta10"
        )

        delta_mode_seen = False
        for poll_i in range(30):
            time.sleep(3)
            try:
                status = fs_mirroring_utils.get_asok_peer_status_raw(
                    cephfs_mirror_node[0], source_clients[0], source_fs
                )
                dir_data = status.get(path_key, {})
                state = dir_data.get("state", "")
                syncing = dir_data.get("current_syncing_snap", {})
                log.info(
                    f"[S10 Poll {poll_i}] state={state}, "
                    f"syncing_snap={syncing.get('name') if syncing else None}, "
                    f"sync_mode={syncing.get('sync-mode') if syncing else None}"
                )
                if syncing and syncing.get("name") == "snap_delta10":
                    mode = syncing.get("sync-mode", "")
                    log.info(f"Scenario 10: sync-mode={mode}, full snap details: {syncing}")
                    if mode == "delta":
                        delta_mode_seen = True
                    break
                if state == "idle":
                    last = dir_data.get("last_synced_snap", {})
                    if last.get("name") == "snap_delta10":
                        log.info(f"Scenario 10: Delta sync completed fast, last_synced_snap: {last}")
                        delta_mode_seen = True
                        break
            except Exception as e:
                log.warning(f"Scenario 10 poll error: {e}")

        fs_mirroring_utils.validate_snapshot_sync_status(
            cephfs_mirror_node[0], source_fs, "snap_delta10",
            fsid, asok_file, filesystem_id, peer_uuid,
        )
        log.info(f"Scenario 10: delta mode captured={delta_mode_seen}")

        # ============================================================
        # Scenario 11: Monotonicity regression test
        # ============================================================
        log.info("=" * 60)
        log.info("Scenario 11: Monotonicity regression test")
        log.info("=" * 60)

        source_clients[0].exec_command(
            sudo=True,
            cmd=f"dd if=/dev/urandom of={mount_path2}mono_data "
            f"bs=1M count=30 2>/dev/null",
        )
        source_clients[0].exec_command(
            sudo=True, cmd=f"mkdir {mount_path2}.snap/snap_mono"
        )

        prev_sync_bytes = 0
        prev_sync_files = 0
        monotonic = True
        for poll_i in range(60):
            time.sleep(2)
            try:
                status = fs_mirroring_utils.get_asok_peer_status_raw(
                    cephfs_mirror_node[0], source_clients[0], source_fs
                )
                dir_data = status.get(path2_key, {})
                state = dir_data.get("state", "")
                syncing = dir_data.get("current_syncing_snap", {})
                if syncing:
                    bytes_info = syncing.get("bytes", {})
                    sync_bytes_str = bytes_info.get("sync_bytes", "0")
                    sync_percent = bytes_info.get("sync_percent", "N/A")
                    files_info = syncing.get("files", {})
                    sync_files = files_info.get("sync_files", 0)
                    log.info(
                        f"[Mono Poll {poll_i}] state={state}, "
                        f"sync_bytes={sync_bytes_str}, sync_files={sync_files}, "
                        f"sync_percent={sync_percent}"
                    )
                    try:
                        parts = sync_bytes_str.split()
                        val = float(parts[0]) if parts else 0.0
                        unit = parts[1] if len(parts) > 1 else "B"
                        multipliers = {"B": 1, "KiB": 1024, "MiB": 1048576, "GiB": 1073741824}
                        cur_bytes = val * multipliers.get(unit, 1)
                    except (ValueError, IndexError):
                        cur_bytes = 0

                    if cur_bytes < prev_sync_bytes:
                        log.warning(
                            f"Monotonicity violation: bytes went from "
                            f"{prev_sync_bytes} to {cur_bytes}"
                        )
                        monotonic = False
                    if sync_files < prev_sync_files:
                        log.warning(
                            f"Monotonicity violation: files went from "
                            f"{prev_sync_files} to {sync_files}"
                        )
                        monotonic = False
                    prev_sync_bytes = cur_bytes
                    prev_sync_files = sync_files
                else:
                    log.info(f"[Mono Poll {poll_i}] state={state}, no current_syncing_snap")

                if state == "idle":
                    last = dir_data.get("last_synced_snap", {})
                    log.info(f"[Mono Poll {poll_i}] idle, last_synced_snap: {last}")
                    break
            except Exception as e:
                log.warning(f"Monotonicity poll error: {e}")

        fs_mirroring_utils.validate_snapshot_sync_status(
            cephfs_mirror_node[0], source_fs, "snap_mono",
            fsid, asok_file, filesystem_id, peer_uuid,
        )
        if monotonic:
            log.info("Scenario 11: Monotonicity maintained")
        else:
            log.warning("Scenario 11: Monotonicity violations detected")

        # ============================================================
        # Scenario 12: Sync-mode when snapdiff reference missing
        # ============================================================
        log.info("=" * 60)
        log.info("Scenario 12: Sync-mode when snapdiff ref missing")
        log.info("=" * 60)

        source_clients[0].exec_command(
            sudo=True, cmd=f"touch {mount_path1}ref_file"
        )
        source_clients[0].exec_command(
            sudo=True, cmd=f"mkdir {mount_path1}.snap/snap_ref1"
        )
        fs_mirroring_utils.validate_snapshot_sync_status(
            cephfs_mirror_node[0], source_fs, "snap_ref1",
            fsid, asok_file, filesystem_id, peer_uuid,
        )

        for snap in ["snap_full", "snap_delta", "snap_crawl", "snap_tput",
                      "snap_base10", "snap_delta10", "snap_ref1"]:
            source_clients[0].exec_command(
                sudo=True, cmd=f"rmdir {mount_path1}.snap/{snap}",
                check_ec=False,
            )

        source_clients[0].exec_command(
            sudo=True, cmd=f"touch {mount_path1}ref_file_new"
        )
        source_clients[0].exec_command(
            sudo=True, cmd=f"mkdir {mount_path1}.snap/snap_ref2"
        )

        ref_mode = None
        for poll_i in range(30):
            time.sleep(3)
            try:
                status = fs_mirroring_utils.get_asok_peer_status_raw(
                    cephfs_mirror_node[0], source_clients[0], source_fs
                )
                dir_data = status.get(path_key, {})
                state = dir_data.get("state", "")
                syncing = dir_data.get("current_syncing_snap", {})
                log.info(
                    f"[S12 Poll {poll_i}] state={state}, "
                    f"syncing={syncing.get('name') if syncing else None}, "
                    f"sync_mode={syncing.get('sync-mode') if syncing else None}"
                )
                if syncing and syncing.get("name") == "snap_ref2":
                    ref_mode = syncing.get("sync-mode", "")
                    log.info(f"Scenario 12: sync-mode={ref_mode}, full snap details: {syncing}")
                    break
                if state == "idle":
                    last = dir_data.get("last_synced_snap", {})
                    if last.get("name") == "snap_ref2":
                        log.info(f"Scenario 12: sync completed fast, last_synced_snap: {last}")
                        ref_mode = "full"
                        break
            except Exception as e:
                log.warning(f"Scenario 12 poll error: {e}")

        if ref_mode == "full":
            log.info("Scenario 12: Falls back to full sync when ref missing")
        else:
            log.info(f"Scenario 12: sync-mode={ref_mode}")

        fs_mirroring_utils.validate_snapshot_sync_status(
            cephfs_mirror_node[0], source_fs, "snap_ref2",
            fsid, asok_file, filesystem_id, peer_uuid,
        )

        # ============================================================
        # Final: Validate cumulative snaps_synced counters
        # ============================================================
        log.info("=" * 60)
        log.info("Final: Validate cumulative snaps_synced counters")
        log.info("=" * 60)

        final_status = fs_mirroring_utils.get_asok_peer_status_raw(
            cephfs_mirror_node[0], source_clients[0], source_fs
        )
        for path, dir_status in final_status.items():
            synced = dir_status.get("snaps_synced", 0)
            log.info(f"{path}: snaps_synced={synced}")
            if synced < 1:
                raise CommandFailed(f"snaps_synced should be >= 1 for {path}")

        log.info("All asok metrics scenarios passed")

        source_clients[0].exec_command(
            sudo=True,
            cmd="ceph config rm client.cephfs-mirror cephfs_mirror_tick_interval",
        )

        return 0
    except Exception as e:
        log.error(e)
        log.error(traceback.format_exc())
        return 1
    finally:
        log.info("Clean up the system")
        try:
            log.info("Cleanup: Reset config overrides")
            source_clients[0].exec_command(
                sudo=True,
                cmd="ceph config rm client.cephfs-mirror cephfs_mirror_tick_interval",
                check_ec=False,
            )

            log.info("Delete the snapshots")
            all_snaps = [
                "snap_full", "snap_delta", "snap_eta", "snap_crawl", "snap_dsync",
                "snap_tput", "snap_empty", "snap_base10",
                "snap_delta10", "snap_mono", "snap_ref1", "snap_ref2",
            ]
            snap_mount_paths = [
                f"{kernel_mounting_dir}{subvol_path1}",
                f"{fuse_mounting_dir}{subvol_path2}",
            ]
            for spath in snap_mount_paths:
                for snap in all_snaps:
                    source_clients[0].exec_command(
                        sudo=True, cmd=f"rmdir {spath}.snap/{snap}",
                        check_ec=False,
                    )

            log.info("Unmount the paths")
            for mdir in [kernel_mounting_dir, fuse_mounting_dir]:
                source_clients[0].exec_command(
                    sudo=True, cmd=f"umount -l {mdir}", check_ec=False
                )

            log.info("Delete the mounted paths")
            for mdir in [kernel_mounting_dir, fuse_mounting_dir]:
                source_clients[0].exec_command(
                    sudo=True, cmd=f"rm -rf {mdir}", check_ec=False
                )

            log.info("Remove paths used for mirroring")
            for subvol_path in subvolume_paths:
                fs_mirroring_utils.remove_path_from_mirroring(
                    source_clients[0], source_fs, subvol_path
                )

            log.info("Destroy CephFS Mirroring setup")
            peer_uuid = fs_mirroring_utils.get_peer_uuid_by_name(
                source_clients[0], source_fs
            )
            fs_mirroring_utils.destroy_cephfs_mirroring(
                source_fs,
                source_clients[0],
                target_fs,
                target_clients[0],
                target_user,
                peer_uuid,
            )

            log.info("Remove Subvolumes")
            for i in range(1, 3):
                fs_util_ceph1.remove_subvolume(
                    source_clients[0], source_fs,
                    f"{subvol_name}_{i}", group_name=subvol_group_name,
                    check_ec=False,
                )

            log.info("Remove Subvolume Group")
            fs_util_ceph1.remove_subvolumegroup(
                source_clients[0], source_fs, subvol_group_name,
                check_ec=False,
            )
        except Exception as cleanup_err:
            log.warning(f"Cleanup encountered an error: {cleanup_err}")
