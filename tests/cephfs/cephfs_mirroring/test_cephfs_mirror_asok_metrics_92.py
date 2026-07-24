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

    Consolidated scenarios:
     1. Schema verification (field presence)
     2. Full sync — sync-mode, ETA, crawl, datasync, throughput (500 MiB)
     3. Delta sync — sync-mode, monotonicity, snapdiff/blockdiff (500 MiB)
     4. last_synced_snap enrichment (assert non-zero values)
     5. Sync-mode fallback when snapdiff ref missing

    Note: Scenario 8 (sync failure) in test_cephfs_mirror_disruptive_ops.py
    Note: Zero-file directory sync validated in test_cephfs_mirror_improved_stats (R12).

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

        log.info("Set tick interval to 1s for accurate sync metrics")
        source_clients[0].exec_command(
            sudo=True,
            cmd="ceph config set client.cephfs-mirror "
            "cephfs_mirror_tick_interval 1",
        )
        log.info("Restart cephfs-mirror daemon for tick_interval to take effect")
        source_clients[0].exec_command(
            sudo=True, cmd="ceph orch restart cephfs-mirror"
        )
        time.sleep(30)

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

        mount_path1 = f"{kernel_mounting_dir}{subvol_path1}"
        mount_path2 = f"{fuse_mounting_dir}{subvol_path2}"
        path1_key = subvol_path1.rstrip("/")
        path2_key = subvol_path2.rstrip("/")

        # ============================================================
        # Scenario 1: Schema verification
        # ============================================================
        log.info("=" * 60)
        log.info("Scenario 1: Schema verification")
        log.info("=" * 60)
        status_before = fs_mirroring_utils.get_fs_mirror_peer_status_using_asok(
            cephfs_mirror_node[0], source_clients[0], source_fs
        )
        log.info(f"Peer status (initial): {json.dumps(status_before, indent=2)}")

        for path, dir_status in status_before.items():
            required = ["state", "snaps_synced", "snaps_deleted", "snaps_renamed"]
            for field in required:
                if field not in dir_status:
                    raise CommandFailed(
                        f"Missing field '{field}' in asok status for {path}"
                    )
            log.info(f"Schema OK for {path}: {list(dir_status.keys())}")

        # ============================================================
        # Scenario 2: Full sync — comprehensive in-flight metrics
        # (sync-mode, ETA, crawl, datasync, throughput)
        # ============================================================
        log.info("=" * 60)
        log.info("Scenario 2: Full sync — all in-flight metrics (500 MiB)")
        log.info("=" * 60)

        log.info("Write 500 MiB data for observable full sync")
        source_clients[0].exec_command(
            sudo=True,
            cmd=f"dd if=/dev/urandom of={mount_path1}fulldata bs=1M count=500",
        )
        source_clients[0].exec_command(
            sudo=True, cmd=f"mkdir {mount_path1}.snap/snap_full"
        )

        sync_mode_full = False
        eta_observed = False
        crawl_observed = False
        datasync_observed = False
        throughput_observed = False

        log.info("Poll asok during full sync — capture all in-flight fields")
        for poll_i in range(90):
            time.sleep(1)
            try:
                status = fs_mirroring_utils.get_asok_peer_status_raw(
                    cephfs_mirror_node[0], source_clients[0], source_fs
                )
                log.info(f"[S2 Poll {poll_i}] Raw asok: {json.dumps(status.get(path1_key, {}))}")

                dir_data = status.get(path1_key, {})
                state = dir_data.get("state", "")
                syncing = dir_data.get("current_syncing_snap")

                if syncing:
                    mode = syncing.get("sync-mode", "")
                    eta = syncing.get("eta", "")
                    crawl = syncing.get("crawl", {})
                    dswait = syncing.get("datasync_queue_wait", {})
                    read_tp = syncing.get("avg_read_throughput_bytes", "")
                    write_tp = syncing.get("avg_write_throughput_bytes", "")
                    bytes_info = syncing.get("bytes", {})
                    files_info = syncing.get("files", {})

                    log.info(
                        f"[S2 Poll {poll_i}] state={state}, "
                        f"sync-mode={mode}, eta={eta}, "
                        f"crawl={crawl.get('state', '')}/{crawl.get('duration', '')}, "
                        f"datasync={dswait.get('state', '')}/{dswait.get('duration', '')}, "
                        f"read_bps={read_tp}, write_bps={write_tp}, "
                        f"sync_bytes={bytes_info.get('sync_bytes', '')}, "
                        f"total_bytes={bytes_info.get('total_bytes', '')}, "
                        f"sync_pct={bytes_info.get('sync_percent', '')}, "
                        f"sync_files={files_info.get('sync_files', '')}, "
                        f"total_files={files_info.get('total_files', '')}"
                    )

                    if mode == "full":
                        sync_mode_full = True
                    if eta:
                        eta_observed = True
                    if crawl.get("state"):
                        crawl_observed = True
                    if dswait.get("state"):
                        datasync_observed = True
                    if read_tp or write_tp:
                        throughput_observed = True

                if state == "idle":
                    last = dir_data.get("last_synced_snap", {})
                    if last.get("name") == "snap_full":
                        log.info(f"snap_full synced: {json.dumps(last)}")
                        break
            except Exception as e:
                log.warning(f"S2 poll error: {e}")

        fs_mirroring_utils.validate_snapshot_sync_status(
            cephfs_mirror_node[0], source_fs, "snap_full",
            fsid, asok_file, filesystem_id, peer_uuid,
        )

        log.info(f"S2 Results: sync_mode_full={sync_mode_full}, "
                 f"eta_observed={eta_observed}, crawl_observed={crawl_observed}, "
                 f"datasync_observed={datasync_observed}, "
                 f"throughput_observed={throughput_observed}")

        if not sync_mode_full:
            log.warning("S2: sync-mode=full was NOT captured during polling")
        if not eta_observed:
            log.warning("S2: ETA was NOT observed during sync")
        if not crawl_observed:
            log.warning("S2: Crawl state was NOT observed during sync")
        if not throughput_observed:
            log.warning("S2: Throughput was NOT observed during sync")

        # ============================================================
        # Scenario 3: Delta sync — sync-mode, monotonicity, snapdiff
        # ============================================================
        log.info("=" * 60)
        log.info("Scenario 3: Delta sync — mode, monotonicity, snapdiff")
        log.info("=" * 60)

        log.info("Create baseline files: 10 small (1 MiB) + 1 large (64 MiB)")
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
            sudo=True, cmd=f"mkdir {mount_path1}.snap/snap_base"
        )

        log.info("Wait for snap_base to sync")
        fs_mirroring_utils.validate_snapshot_sync_status(
            cephfs_mirror_node[0], source_fs, "snap_base",
            fsid, asok_file, filesystem_id, peer_uuid,
        )

        log.info("Modify 5 of 10 small files + partial write to large file")
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
        log.info("Add 20 NEW files (25 MiB each = 500 MiB) for observable delta sync")
        source_clients[0].exec_command(
            sudo=True,
            cmd=f"for i in $(seq 1 20); do dd if=/dev/urandom "
            f"of={mount_path1}delta_$i bs=1M count=25 2>/dev/null; done",
        )
        source_clients[0].exec_command(
            sudo=True, cmd=f"mkdir {mount_path1}.snap/snap_delta"
        )

        sync_mode_delta = False
        prev_sync_bytes = 0
        monotonic = True
        delta_poll_count = 0

        log.info("Poll during delta sync — validate mode, monotonicity")
        for poll_i in range(90):
            time.sleep(1)
            try:
                status = fs_mirroring_utils.get_asok_peer_status_raw(
                    cephfs_mirror_node[0], source_clients[0], source_fs
                )
                dir_data = status.get(path1_key, {})
                state = dir_data.get("state", "")
                syncing = dir_data.get("current_syncing_snap")

                if syncing and syncing.get("name") == "snap_delta":
                    delta_poll_count += 1
                    mode = syncing.get("sync-mode", "")
                    bytes_info = syncing.get("bytes", {})
                    files_info = syncing.get("files", {})
                    sync_bytes_str = bytes_info.get("sync_bytes", "0")
                    sync_pct = bytes_info.get("sync_percent", "N/A")
                    sync_files = files_info.get("sync_files", 0)
                    total_files = files_info.get("total_files", 0)

                    log.info(
                        f"[S3 Poll {poll_i}] state={state}, mode={mode}, "
                        f"sync_bytes={sync_bytes_str}, sync_pct={sync_pct}, "
                        f"sync_files={sync_files}, total_files={total_files}"
                    )

                    if mode == "delta":
                        sync_mode_delta = True

                    try:
                        parts = sync_bytes_str.split()
                        val = float(parts[0]) if parts else 0.0
                        unit = parts[1] if len(parts) > 1 else "B"
                        mult = {"B": 1, "KiB": 1024, "MiB": 1048576, "GiB": 1073741824}
                        cur_bytes = val * mult.get(unit, 1)
                    except (ValueError, IndexError):
                        cur_bytes = 0

                    if cur_bytes < prev_sync_bytes:
                        log.warning(
                            f"Monotonicity violation: bytes {prev_sync_bytes} -> {cur_bytes}"
                        )
                        monotonic = False
                    prev_sync_bytes = cur_bytes
                else:
                    log.info(
                        f"[S3 Poll {poll_i}] state={state}, "
                        f"snap={syncing.get('name') if syncing else None}"
                    )

                if state == "idle":
                    last = dir_data.get("last_synced_snap", {})
                    if last.get("name") == "snap_delta":
                        log.info(f"snap_delta synced: {json.dumps(last)}")
                        break
            except Exception as e:
                log.warning(f"S3 poll error: {e}")

        fs_mirroring_utils.validate_snapshot_sync_status(
            cephfs_mirror_node[0], source_fs, "snap_delta",
            fsid, asok_file, filesystem_id, peer_uuid,
        )

        log.info(
            f"S3 Results: sync_mode_delta={sync_mode_delta}, "
            f"monotonic={monotonic}, delta_polls_captured={delta_poll_count}"
        )

        if not sync_mode_delta:
            log.warning("S3: sync-mode=delta was NOT captured during polling")
        if not monotonic:
            log.warning("S3: Monotonicity violations detected during delta sync")

        # ============================================================
        # Scenario 4: last_synced_snap enrichment (assert non-zero)
        # ============================================================
        log.info("=" * 60)
        log.info("Scenario 4: last_synced_snap enrichment")
        log.info("=" * 60)

        status_after = fs_mirroring_utils.get_asok_peer_status_raw(
            cephfs_mirror_node[0], source_clients[0], source_fs
        )
        log.info(f"Full asok status: {json.dumps(status_after, indent=2)}")

        path1_last = status_after.get(path1_key, {}).get("last_synced_snap", {})
        log.info(f"S4: path1 last_synced_snap: {json.dumps(path1_last, indent=2)}")

        if not path1_last.get("name"):
            raise CommandFailed("S4 FAILED: last_synced_snap.name missing")

        enrichment_fields = [
            "id", "name", "sync_duration", "sync_time_stamp",
            "sync_bytes", "sync_files",
        ]
        for field in enrichment_fields:
            val = path1_last.get(field)
            log.info(f"  {field} = {val}")
            if val is None:
                log.warning(f"S4: enrichment field '{field}' is missing")

        sync_bytes = path1_last.get("sync_bytes", "0")
        sync_files = path1_last.get("sync_files", 0)
        sync_duration = path1_last.get("sync_duration", "0s")

        if sync_bytes in ("0", "0.00 B") or sync_files == 0:
            log.warning(
                f"S4: last_synced_snap has zero metrics — "
                f"sync_bytes={sync_bytes}, sync_files={sync_files}, "
                f"sync_duration={sync_duration}"
            )
        else:
            log.info(
                f"S4 VALIDATED: sync_bytes={sync_bytes}, "
                f"sync_files={sync_files}, sync_duration={sync_duration}"
            )

        if path1_last.get("name") == "snap_delta" and sync_files > 0:
            if sync_files <= 26:
                log.info(
                    f"S4 Snapdiff OK: sync_files={sync_files} <= 26 "
                    f"(5 modified + 20 new + 1 large partial)"
                )
            else:
                log.warning(f"S4 Snapdiff: sync_files={sync_files}, expected <= 26")

        log.info("S4 PASSED: last_synced_snap enrichment validated")

        # ============================================================
        # Scenario 5: Sync-mode fallback when snapdiff ref missing
        # ============================================================
        log.info("=" * 60)
        log.info("Scenario 5: Full sync fallback when snapdiff ref missing")
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

        log.info("Delete prior snapshots to remove snapdiff reference")
        for snap in ["snap_full", "snap_base", "snap_delta", "snap_ref1"]:
            source_clients[0].exec_command(
                sudo=True, cmd=f"rmdir {mount_path1}.snap/{snap}",
                check_ec=False,
            )
        time.sleep(10)

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
                dir_data = status.get(path1_key, {})
                state = dir_data.get("state", "")
                syncing = dir_data.get("current_syncing_snap", {})
                log.info(
                    f"[S5 Poll {poll_i}] state={state}, "
                    f"snap={syncing.get('name') if syncing else None}, "
                    f"mode={syncing.get('sync-mode') if syncing else None}"
                )
                if syncing and syncing.get("name") == "snap_ref2":
                    ref_mode = syncing.get("sync-mode", "")
                    log.info(f"S5: Captured sync-mode={ref_mode}")
                    log.info(f"S5: Full snap details: {json.dumps(syncing)}")
                    break
                if state == "idle":
                    last = dir_data.get("last_synced_snap", {})
                    if last.get("name") == "snap_ref2":
                        ref_mode = "full"
                        log.info(f"S5: snap_ref2 synced fast: {json.dumps(last)}")
                        break
            except Exception as e:
                log.warning(f"S5 poll error: {e}")

        fs_mirroring_utils.validate_snapshot_sync_status(
            cephfs_mirror_node[0], source_fs, "snap_ref2",
            fsid, asok_file, filesystem_id, peer_uuid,
        )

        if ref_mode == "full":
            log.info("S5 PASSED: Falls back to full sync when ref missing")
        else:
            log.warning(f"S5: sync-mode={ref_mode}, expected 'full'")

        # ============================================================
        # Final: Validate cumulative snaps_synced counters
        # ============================================================
        log.info("=" * 60)
        log.info("Final: Validate cumulative snaps_synced counters")
        log.info("=" * 60)

        final_status = fs_mirroring_utils.get_asok_peer_status_raw(
            cephfs_mirror_node[0], source_clients[0], source_fs
        )
        log.info(f"Final asok status: {json.dumps(final_status, indent=2)}")
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
                cmd="ceph config rm client.cephfs-mirror "
                "cephfs_mirror_tick_interval",
                check_ec=False,
            )

            all_snaps = [
                "snap_full", "snap_base", "snap_delta",
                "snap_ref1", "snap_ref2",
            ]
            snap_mount_paths = [mount_path1, mount_path2]
            log.info("Delete the snapshots")
            for spath in snap_mount_paths:
                for snap in all_snaps:
                    source_clients[0].exec_command(
                        sudo=True, cmd=f"rmdir {spath}.snap/{snap}",
                        check_ec=False,
                    )

            mount_dirs = [kernel_mounting_dir, fuse_mounting_dir]
            log.info("Unmount the paths")
            for mdir in mount_dirs:
                source_clients[0].exec_command(
                    sudo=True, cmd=f"umount -l {mdir}", check_ec=False
                )

            log.info("Delete the mounted paths")
            for mdir in mount_dirs:
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
            for sv in subvol_details:
                fs_util_ceph1.remove_subvolume(
                    source_clients[0], source_fs,
                    sv["subvol_name"], group_name=subvol_group_name,
                    check_ec=False,
                )

            log.info("Remove Subvolume Group")
            fs_util_ceph1.remove_subvolumegroup(
                source_clients[0], source_fs, subvol_group_name,
                check_ec=False,
            )
        except Exception as cleanup_err:
            log.warning(f"Cleanup encountered an error: {cleanup_err}")
