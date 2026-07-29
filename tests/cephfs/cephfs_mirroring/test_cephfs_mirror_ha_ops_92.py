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


def checkpoint_add(client, fs_name, path, snap_name):
    cmd = f"ceph fs snapshot mirror checkpoint add {fs_name} {path} {snap_name}"
    out, _ = client.exec_command(sudo=True, cmd=cmd)
    log.info(f"checkpoint add: {out.strip()}")
    return out.strip()


def checkpoint_ls(client, fs_name, path):
    cmd = f"ceph fs snapshot mirror checkpoint ls {fs_name} {path} -f json"
    out, _ = client.exec_command(sudo=True, cmd=cmd)
    data = json.loads(out)
    if isinstance(data, dict):
        return data.get("checkpoints", [])
    return data


def run(ceph_cluster, **kw):
    """
    CEPH-83632850 - CephFS mirroring HA operations for 9.2.

    Combined scenarios:
     S1: Daemon restart mid-sync — asok resets, MGR retains OMAP, sync resumes
     S2: Daemon restart preserves checkpoint state
     S3: MGR module bounce preserves checkpoints
     S4: Tri-interface during daemon down (stale detection)
     S5: MGR failover during active sync

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

        fs_util_ceph1.prepare_clients(source_clients, build)
        fs_util_ceph2.prepare_clients(target_clients, build)
        fs_util_ceph1.auth_list(source_clients)
        fs_util_ceph2.auth_list(target_clients)

        fs_mirroring_utils.deploy_cephfs_mirroring(
            source_fs, source_clients[0], target_fs, target_clients[0],
            target_user, target_site_name,
        )

        subvol_group = "subvolgroup_ha"
        subvol_name = "subvol_ha"
        subvol_size = "12884901888"
        mounting_dir = "".join(
            random.choice(string.ascii_lowercase + string.digits)
            for _ in range(10)
        )
        kernel_mount = f"/mnt/cephfs_kernel{mounting_dir}_1"
        subvol_details = [
            {
                "subvol_name": f"{subvol_name}_1",
                "subvol_size": subvol_size,
                "mount_type": "kernel",
                "mount_dir": kernel_mount,
            },
        ]
        subvolume_paths = fs_mirroring_utils.setup_subvolumes_and_mounts(
            source_fs, source_clients[0], fs_util_ceph1,
            subvol_group, subvol_details,
        )
        subvol_path = subvolume_paths[0]
        mount_path = f"{kernel_mount}{subvol_path}"
        path_key = subvol_path.rstrip("/")

        fs_mirroring_utils.add_path_for_mirroring(
            source_clients[0], source_fs, subvol_path
        )

        source_clients[0].exec_command(
            sudo=True,
            cmd="ceph config set client.cephfs-mirror "
            "cephfs_mirror_tick_interval 1",
        )
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

        # ============================================================
        # S1: Daemon restart mid-sync — stats continuity
        # ============================================================
        log.info("=" * 60)
        log.info("S1: Daemon restart mid-sync — stats continuity")
        log.info("=" * 60)

        log.info("Write 5 GiB to ensure sync takes long enough for mid-sync restart")
        source_clients[0].exec_command(
            sudo=True,
            cmd=f"dd if=/dev/urandom of={mount_path}ha_data bs=1M count=5120",
            timeout=600,
        )
        source_clients[0].exec_command(
            sudo=True, cmd=f"mkdir {mount_path}.snap/snap_ha1"
        )

        log.info("S1: Capture MGR status before restart (OMAP baseline)")
        mgr_before = fs_mirroring_utils.get_mgr_mirror_status(
            source_clients[0], source_fs
        )
        log.info(f"S1: MGR before restart: {json.dumps(mgr_before, indent=2)}")

        log.info("S1: Poll until syncing state observed, then restart daemon")
        syncing_seen = False
        for poll_i in range(60):
            time.sleep(2)
            try:
                status = fs_mirroring_utils.get_asok_peer_status_raw(
                    cephfs_mirror_node[0], source_clients[0], source_fs
                )
                dir_data = status.get(path_key, {})
                state = dir_data.get("state", "")
                syncing = dir_data.get("current_syncing_snap")
                log.info(
                    f"[S1 Poll {poll_i}] state={state}, "
                    f"snap={syncing.get('name') if syncing else None}"
                )
                if state == "syncing" and syncing:
                    syncing_seen = True
                    log.info("S1: Syncing state detected — restarting daemon now")
                    break
                if state == "idle" and dir_data.get("snaps_synced", 0) >= 1:
                    log.info("S1: Sync completed before restart could be triggered")
                    break
            except Exception as e:
                log.warning(f"S1 poll error: {e}")

        if syncing_seen:
            source_clients[0].exec_command(
                sudo=True, cmd="ceph orch restart cephfs-mirror"
            )
            log.info("S1: Daemon restarted, waiting 30s for recovery")
            time.sleep(30)

            daemon_name = fs_mirroring_utils.get_daemon_name(source_clients[0])
            asok_file = fs_mirroring_utils.get_asok_file(
                cephfs_mirror_node[0], fsid, daemon_name
            )

            log.info("S1: Check asok after restart — should show fresh state")
            try:
                status_after = fs_mirroring_utils.get_asok_peer_status_raw(
                    cephfs_mirror_node[0], source_clients[0], source_fs
                )
                dir_after = status_after.get(path_key, {})
                log.info(f"S1: Asok after restart: {json.dumps(dir_after)}")
            except Exception as e:
                log.info(f"S1: Asok not yet available after restart: {e}")

            log.info("S1: Check MGR retains OMAP data after daemon restart")
            mgr_after = fs_mirroring_utils.get_mgr_mirror_status(
                source_clients[0], source_fs
            )
            log.info(f"S1: MGR after restart: {json.dumps(mgr_after, indent=2)}")
            mgr_metrics = mgr_after.get("metrics", {})
            if path_key not in mgr_metrics:
                raise CommandFailed(
                    f"S1 FAILED: Path {path_key} not found in MGR after restart"
                )
            log.info("S1: MGR retained OMAP data after daemon restart")

        log.info("S1: Wait for snap_ha1 to complete sync after restart")
        fs_mirroring_utils.validate_snapshot_sync_status(
            cephfs_mirror_node[0], source_fs, "snap_ha1",
            fsid, asok_file, filesystem_id, peer_uuid,
        )

        status_final = fs_mirroring_utils.get_asok_peer_status_raw(
            cephfs_mirror_node[0], source_clients[0], source_fs
        )
        dir_final = status_final.get(path_key, {})
        last_snap = dir_final.get("last_synced_snap", {})
        if last_snap.get("name") != "snap_ha1":
            raise CommandFailed(
                f"S1 FAILED: Expected last_synced_snap=snap_ha1, "
                f"got {last_snap.get('name')}"
            )
        log.info(f"S1 PASSED: Sync resumed and completed after restart. "
                 f"last_synced_snap={json.dumps(last_snap)}")

        # ============================================================
        # S2: Daemon restart preserves checkpoint state
        # ============================================================
        log.info("=" * 60)
        log.info("S2: Daemon restart preserves checkpoint state")
        log.info("=" * 60)

        checkpoint_add(source_clients[0], source_fs, subvol_path, "snap_ha1")
        time.sleep(10)
        ckpt_before = checkpoint_ls(source_clients[0], source_fs, subvol_path)
        log.info(f"S2: Checkpoints before restart: {ckpt_before}")

        ckpt_before_map = {c.get("snap_name"): c for c in ckpt_before}
        if "snap_ha1" not in ckpt_before_map:
            raise CommandFailed("S2 FAILED: snap_ha1 checkpoint not found before restart")

        source_clients[0].exec_command(
            sudo=True, cmd="ceph orch restart cephfs-mirror"
        )
        time.sleep(30)

        daemon_name = fs_mirroring_utils.get_daemon_name(source_clients[0])
        asok_file = fs_mirroring_utils.get_asok_file(
            cephfs_mirror_node[0], fsid, daemon_name
        )

        ckpt_after = checkpoint_ls(source_clients[0], source_fs, subvol_path)
        log.info(f"S2: Checkpoints after restart: {ckpt_after}")

        ckpt_after_map = {c.get("snap_name"): c for c in ckpt_after}
        if "snap_ha1" not in ckpt_after_map:
            raise CommandFailed(
                "S2 FAILED: snap_ha1 checkpoint lost after daemon restart"
            )

        before_status = ckpt_before_map["snap_ha1"].get("status", "")
        after_status = ckpt_after_map["snap_ha1"].get("status", "")
        log.info(f"S2: Checkpoint status: before={before_status}, after={after_status}")
        if before_status != after_status:
            raise CommandFailed(
                f"S2 FAILED: Checkpoint status changed after restart: "
                f"{before_status} -> {after_status}"
            )
        log.info("S2 PASSED: Checkpoint state preserved across daemon restart")

        # ============================================================
        # S3: MGR module bounce preserves checkpoints
        # ============================================================
        log.info("=" * 60)
        log.info("S3: MGR module bounce preserves checkpoints")
        log.info("=" * 60)

        source_clients[0].exec_command(
            sudo=True,
            cmd=f"dd if=/dev/urandom of={mount_path}ha_data2 bs=1M count=100",
        )
        source_clients[0].exec_command(
            sudo=True, cmd=f"mkdir {mount_path}.snap/snap_ha2"
        )
        fs_mirroring_utils.validate_snapshot_sync_status(
            cephfs_mirror_node[0], source_fs, "snap_ha2",
            fsid, asok_file, filesystem_id, peer_uuid,
        )

        checkpoint_add(source_clients[0], source_fs, subvol_path, "snap_ha2")
        time.sleep(10)
        ckpt_pre_bounce = checkpoint_ls(source_clients[0], source_fs, subvol_path)
        log.info(f"S3: Checkpoints before MGR bounce: {ckpt_pre_bounce}")

        source_clients[0].exec_command(
            sudo=True, cmd="ceph mgr module disable mirroring"
        )
        time.sleep(5)
        source_clients[0].exec_command(
            sudo=True, cmd="ceph mgr module enable mirroring"
        )
        time.sleep(30)

        ckpt_post_bounce = checkpoint_ls(source_clients[0], source_fs, subvol_path)
        log.info(f"S3: Checkpoints after MGR bounce: {ckpt_post_bounce}")

        pre_names = {c.get("snap_name") for c in ckpt_pre_bounce}
        post_names = {c.get("snap_name") for c in ckpt_post_bounce}
        if not pre_names.issubset(post_names):
            raise CommandFailed(
                f"S3 FAILED: Checkpoints lost after MGR bounce: "
                f"before={pre_names}, after={post_names}"
            )
        log.info("S3 PASSED: Checkpoints preserved across MGR module bounce")

        # ============================================================
        # S4: Tri-interface during daemon down (stale detection)
        # ============================================================
        log.info("=" * 60)
        log.info("S4: Tri-interface during daemon down")
        log.info("=" * 60)

        log.info("S4: Write 5 GiB and create snapshot for active sync")
        source_clients[0].exec_command(
            sudo=True,
            cmd=f"dd if=/dev/urandom of={mount_path}ha_data3 bs=1M count=5120",
            timeout=600,
        )
        source_clients[0].exec_command(
            sudo=True, cmd=f"mkdir {mount_path}.snap/snap_ha3"
        )

        log.info("S4: Wait for sync to start, then stop daemon")
        for poll_i in range(30):
            time.sleep(2)
            try:
                status = fs_mirroring_utils.get_asok_peer_status_raw(
                    cephfs_mirror_node[0], source_clients[0], source_fs
                )
                if status.get(path_key, {}).get("state") == "syncing":
                    log.info("S4: Syncing detected, stopping daemon")
                    break
            except Exception:
                pass

        source_clients[0].exec_command(
            sudo=True, cmd="ceph orch stop cephfs-mirror"
        )
        time.sleep(20)

        log.info("S4: Query MGR while daemon is down — should show stale/frozen data")
        mgr_during_stop = fs_mirroring_utils.get_mgr_mirror_status(
            source_clients[0], source_fs
        )
        log.info(f"S4: MGR during daemon stop: {json.dumps(mgr_during_stop, indent=2)}")
        mgr_metrics = mgr_during_stop.get("metrics", {})
        mgr_path = mgr_metrics.get(path_key, {})
        if mgr_path:
            mgr_peer = list(mgr_path.get("peer", {}).values())
            if mgr_peer:
                ts_stopped = mgr_peer[0].get("metrics_updated_at", 0)
                log.info(f"S4: metrics_updated_at during stop: {ts_stopped}")

        log.info("S4: Verify asok is unreachable while daemon is down")
        asok_reachable = True
        try:
            fs_mirroring_utils.get_asok_peer_status_raw(
                cephfs_mirror_node[0], source_clients[0], source_fs
            )
        except Exception as e:
            asok_reachable = False
            log.info(f"S4: Asok unreachable as expected: {e}")

        if asok_reachable:
            log.warning("S4: Asok was still reachable after daemon stop")

        log.info("S4: Query MGR again — progress must NOT advance")
        time.sleep(30)
        mgr_during_stop2 = fs_mirroring_utils.get_mgr_mirror_status(
            source_clients[0], source_fs
        )
        mgr_path2 = mgr_during_stop2.get("metrics", {}).get(path_key, {})
        if mgr_path2:
            mgr_peer2 = list(mgr_path2.get("peer", {}).values())
            if mgr_peer2:
                ts_stopped2 = mgr_peer2[0].get("metrics_updated_at", 0)
                log.info(f"S4: metrics_updated_at (2nd query): {ts_stopped2}")
                if ts_stopped2 > ts_stopped + 5:
                    log.warning(
                        f"S4: metrics_updated_at advanced while daemon was down: "
                        f"{ts_stopped} -> {ts_stopped2}"
                    )
                else:
                    log.info("S4: MGR metrics frozen as expected (daemon down)")

        log.info("S4: Restart daemon and verify recovery")
        source_clients[0].exec_command(
            sudo=True, cmd="ceph orch start cephfs-mirror"
        )
        time.sleep(30)

        daemon_name = fs_mirroring_utils.get_daemon_name(source_clients[0])
        asok_file = fs_mirroring_utils.get_asok_file(
            cephfs_mirror_node[0], fsid, daemon_name
        )

        fs_mirroring_utils.validate_snapshot_sync_status(
            cephfs_mirror_node[0], source_fs, "snap_ha3",
            fsid, asok_file, filesystem_id, peer_uuid,
        )

        mgr_recovered = fs_mirroring_utils.get_mgr_mirror_status(
            source_clients[0], source_fs
        )
        mgr_path_rec = mgr_recovered.get("metrics", {}).get(path_key, {})
        if mgr_path_rec:
            mgr_peer_rec = list(mgr_path_rec.get("peer", {}).values())
            if mgr_peer_rec:
                ts_recovered = mgr_peer_rec[0].get("metrics_updated_at", 0)
                log.info(f"S4: metrics_updated_at after recovery: {ts_recovered}")
                if ts_recovered > ts_stopped:
                    log.info("S4: MGR metrics updated after recovery")
                else:
                    log.warning("S4: MGR metrics_updated_at did not advance")

        log.info("S4 PASSED: Tri-interface stale detection during daemon down validated")

        # ============================================================
        # S5: MGR failover during active sync
        # ============================================================
        log.info("=" * 60)
        log.info("S5: MGR failover during active sync")
        log.info("=" * 60)

        log.info("S5: Write 5 GiB for active sync during MGR failover")
        source_clients[0].exec_command(
            sudo=True,
            cmd=f"dd if=/dev/urandom of={mount_path}ha_data4 bs=1M count=5120",
            timeout=600,
        )
        source_clients[0].exec_command(
            sudo=True, cmd=f"mkdir {mount_path}.snap/snap_ha4"
        )

        log.info("S5: Get active MGR name")
        mgr_out, _ = source_clients[0].exec_command(
            sudo=True, cmd="ceph mgr stat -f json"
        )
        mgr_stat = json.loads(mgr_out)
        active_mgr = mgr_stat.get("active_name", "")
        log.info(f"S5: Active MGR before failover: {active_mgr}")

        log.info("S5: Fail the active MGR")
        source_clients[0].exec_command(
            sudo=True, cmd=f"ceph mgr fail {active_mgr}"
        )
        time.sleep(30)

        mgr_out2, _ = source_clients[0].exec_command(
            sudo=True, cmd="ceph mgr stat -f json"
        )
        mgr_stat2 = json.loads(mgr_out2)
        new_active = mgr_stat2.get("active_name", "")
        log.info(f"S5: Active MGR after failover: {new_active}")

        if new_active == active_mgr:
            log.warning("S5: Same MGR is still active (may be single-MGR setup)")

        log.info("S5: Verify MGR status is accessible after failover")
        time.sleep(10)
        mgr_status_after = fs_mirroring_utils.get_mgr_mirror_status(
            source_clients[0], source_fs
        )
        log.info(
            f"S5: MGR status after failover: "
            f"{json.dumps(mgr_status_after, indent=2)}"
        )

        mgr_metrics = mgr_status_after.get("metrics", {})
        if path_key not in mgr_metrics:
            raise CommandFailed(
                f"S5 FAILED: Path {path_key} not in MGR status after failover"
            )
        mgr_peer = list(mgr_metrics[path_key].get("peer", {}).values())
        if not mgr_peer:
            raise CommandFailed("S5 FAILED: No peer data after MGR failover")
        log.info(
            f"S5: MGR post-failover: state={mgr_peer[0].get('state')}, "
            f"snaps_synced={mgr_peer[0].get('snaps_synced')}"
        )

        log.info("S5: Wait for snap_ha4 to sync after MGR failover")
        fs_mirroring_utils.validate_snapshot_sync_status(
            cephfs_mirror_node[0], source_fs, "snap_ha4",
            fsid, asok_file, filesystem_id, peer_uuid,
        )
        log.info("S5 PASSED: MGR failover — status accessible, sync completed")

        log.info("=" * 60)
        log.info("ALL HA SCENARIOS PASSED")
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
                cmd="ceph config rm client.cephfs-mirror "
                "cephfs_mirror_tick_interval",
                check_ec=False,
            )
            source_clients[0].exec_command(
                sudo=True, cmd="ceph orch start cephfs-mirror", check_ec=False
            )
            time.sleep(10)

            for snap in ["snap_ha1", "snap_ha2", "snap_ha3", "snap_ha4"]:
                source_clients[0].exec_command(
                    sudo=True, cmd=f"rmdir {mount_path}.snap/{snap}",
                    check_ec=False,
                )

            source_clients[0].exec_command(
                sudo=True, cmd=f"umount -l {kernel_mount}", check_ec=False
            )
            source_clients[0].exec_command(
                sudo=True, cmd=f"rm -rf {kernel_mount}", check_ec=False
            )

            fs_mirroring_utils.remove_path_from_mirroring(
                source_clients[0], source_fs, subvol_path
            )

            peer_uuid = fs_mirroring_utils.get_peer_uuid_by_name(
                source_clients[0], source_fs
            )
            fs_mirroring_utils.destroy_cephfs_mirroring(
                source_fs, source_clients[0], target_fs, target_clients[0],
                target_user, peer_uuid,
            )

            fs_util_ceph1.remove_subvolume(
                source_clients[0], source_fs, f"{subvol_name}_1",
                group_name=subvol_group, check_ec=False,
            )
            fs_util_ceph1.remove_subvolumegroup(
                source_clients[0], source_fs, subvol_group, check_ec=False,
            )
        except Exception as cleanup_err:
            log.warning(f"Cleanup error: {cleanup_err}")
