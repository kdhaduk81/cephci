"""
Test: Validate data integrity after cephfs-mirror daemon restart during active sync.

Bug reproduction scenario:
  After restarting the cephfs-mirror daemon while a snapshot is actively syncing,
  the sync mode may flip from "full" to "delta", potentially missing files.
  This test creates a mixed dataset, triggers a snapshot sync, restarts the daemon
  mid-sync, and validates data integrity via md5 checksums on the target.

Dataset:
  - 500 small files  (1KB - 100KB)
  - 50 medium files  (10MB - 50MB)
  - 10 large files   (100MB - 500MB)
  Total ~5-8 GiB

Workflow:
  1. Create subvolume, mount on source, add path for mirroring.
  2. Generate mixed dataset and compute source checksums.
  3. Create snapshot, poll until sync starts (mode should be "full").
  4. Restart cephfs-mirror daemon mid-sync, observe mode flip / counter reset.
  5. Wait for sync completion (timeout 25 min).
  6. Mount target, compute checksums on .snap/<snap>, compare.
  7. Return 0 if checksums match, 1 if data integrity is broken.
"""

import json
import random
import string
import time
import traceback

from tests.cephfs.cephfs_mirroring.cephfs_mirroring_utils import CephfsMirroringUtils
from tests.cephfs.cephfs_utilsV1 import FsUtils
from utility.log import Log

log = Log(__name__)

SUBVOLUME = "restart_test_sv"
SNAPSHOT = "snap_full"
SOURCE_FS = "cephfs"
TARGET_FS = "cephfs"
SYNC_TIMEOUT = 25 * 60
POLL_INTERVAL_FAST = 5
POLL_INTERVAL_SLOW = 10

SMALL_FILE_COUNT = 500
SMALL_MIN_KB, SMALL_MAX_KB = 1, 100
MEDIUM_FILE_COUNT = 50
MEDIUM_MIN_MB, MEDIUM_MAX_MB = 10, 50
LARGE_FILE_COUNT = 10
LARGE_MIN_MB, LARGE_MAX_MB = 100, 500


def _rand_mount_suffix():
    return "".join(
        random.choice(string.ascii_lowercase + string.digits) for _ in range(10)
    )


def _create_files(client, directory, count, min_bytes, max_bytes, label):
    """Generate *count* files filled with urandom data in *directory*."""
    client.exec_command(sudo=True, cmd=f"mkdir -p {directory}")
    for i in range(count):
        size = random.randint(min_bytes, max_bytes)
        bs = min(size, 1024 * 1024)
        count_arg = (size + bs - 1) // bs
        fname = f"{directory}/{label}_{i:04d}.dat"
        client.exec_command(
            sudo=True,
            cmd=f"dd if=/dev/urandom of={fname} bs={bs} count={count_arg} status=none",
            timeout=300,
        )
        if (i + 1) % 50 == 0 or (i + 1) == count:
            log.info("[%s] created %d / %d files", label, i + 1, count)


def _compute_checksums(client, root_path, output_file):
    """Run md5sum on every file under *root_path*, sort, write to *output_file*.

    Returns the raw sorted text.
    """
    client.exec_command(
        sudo=True,
        cmd=f"find {root_path} -type f -exec md5sum {{}} \\; | sort > {output_file}",
        timeout=1800,
    )
    out, _ = client.exec_command(sudo=True, cmd=f"cat {output_file}")
    return out.strip()


def _strip_path_prefix(checksum_text, prefix):
    """Normalise checksum lines to relative paths so source/target are comparable."""
    lines = []
    for line in checksum_text.splitlines():
        if not line.strip():
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        md5, fpath = parts
        rel = fpath.replace(prefix.rstrip("/"), "", 1).lstrip("/")
        lines.append(f"{md5}  {rel}")
    lines.sort()
    return lines


def _get_peer_status_via_asok(
    mirror_utils, source_client, cephfs_mirror_nodes, fs_name
):
    """Query the admin socket for peer status and return normalised data.

    Returns (data_dict, asok_meta) where asok_meta can be reused.
    """
    fsid_out, _ = source_client.exec_command(sudo=True, cmd="ceph fsid --format json")
    fsid = (
        json.loads(fsid_out).get("fsid", json.loads(fsid_out))
        if "{" in fsid_out
        else fsid_out.strip()
    )

    daemon_names = mirror_utils.get_daemon_name(source_client)
    filesystem_id = mirror_utils.get_filesystem_id_by_name(source_client, fs_name)
    peer_uuid = mirror_utils.get_peer_uuid_by_name(source_client, fs_name)

    asok_files = mirror_utils.get_asok_file_with_connectivity_check(
        cephfs_mirror_nodes, fsid, daemon_names
    )
    if not asok_files:
        log.warning("No accessible asok files found")
        return None, None

    from tests.cephfs.cephfs_mirroring.cephfs_mirroring_utils import (
        _normalize_asok_peer_status,
    )

    for hostname, asok in asok_files.items():
        node, asok_path = asok
        asok_basename = asok_path.rsplit("/", 1)[-1]
        asok_dir = f"/var/run/ceph/{fsid}"
        cmd = (
            f"cd {asok_dir} && ceph --admin-daemon {asok_basename} "
            f"fs mirror peer status {fs_name}@{filesystem_id} {peer_uuid} -f json"
        )
        out, _ = node.exec_command(sudo=True, cmd=cmd, check_ec=False)
        if out and out.strip().startswith("{"):
            data = _normalize_asok_peer_status(json.loads(out))
            return data, {
                "fsid": fsid,
                "daemon_names": daemon_names,
                "filesystem_id": filesystem_id,
                "peer_uuid": peer_uuid,
            }
    return None, None


def _log_peer_status(data, label=""):
    """Pretty-print peer status fields for a single path."""
    if not data:
        log.info("[%s] peer status: no data", label)
        return
    for path, status in data.items():
        log.info(
            "[%s] path=%s  state=%s  snaps_synced=%s  last_synced_snap=%s",
            label,
            path,
            status.get("state", "?"),
            status.get("snaps_synced", "?"),
            status.get("last_synced_snap", "?"),
        )


def run(ceph_cluster, **kw):
    source_mount = None
    target_mount = None
    source_client = None
    target_client = None
    fs_util_ceph1 = None
    mirror_utils = None
    subvol_path = None
    bugs_found = []

    try:
        config = kw.get("config", {}) or {}
        ceph_cluster_dict = kw.get("ceph_cluster_dict")
        test_data = kw.get("test_data")

        fs_util_ceph1 = FsUtils(ceph_cluster_dict.get("ceph1"), test_data=test_data)
        fs_util_ceph2 = FsUtils(ceph_cluster_dict.get("ceph2"), test_data=test_data)
        mirror_utils = CephfsMirroringUtils(
            ceph_cluster_dict.get("ceph1"), ceph_cluster_dict.get("ceph2")
        )

        source_clients = ceph_cluster_dict.get("ceph1").get_ceph_objects("client")
        target_clients = ceph_cluster_dict.get("ceph2").get_ceph_objects("client")
        cephfs_mirror_nodes = ceph_cluster_dict.get("ceph1").get_ceph_objects(
            "cephfs-mirror"
        )

        if not source_clients or not target_clients:
            log.error("Need at least 1 client on both ceph1 and ceph2")
            return 1
        if not cephfs_mirror_nodes:
            log.error("ceph1 must have at least one cephfs-mirror node")
            return 1

        source_client = source_clients[0]
        target_client = target_clients[0]

        build = config.get("build", config.get("rhbuild"))
        fs_util_ceph1.prepare_clients(source_clients, build)
        fs_util_ceph2.prepare_clients(target_clients, build)
        fs_util_ceph1.auth_list(source_clients)
        fs_util_ceph2.auth_list(target_clients)

        # ----------------------------------------------------------------
        # 1. Create subvolume and mount on source
        # ----------------------------------------------------------------
        log.info("=" * 60)
        log.info("STEP 1: Create subvolume and mount on source")
        log.info("=" * 60)

        fs_util_ceph1.create_subvolume(
            source_client, vol_name=SOURCE_FS, subvol_name=SUBVOLUME
        )

        subvol_path_raw, _ = source_client.exec_command(
            sudo=True,
            cmd=f"ceph fs subvolume getpath {SOURCE_FS} {SUBVOLUME}",
        )
        idx = subvol_path_raw.find(f"{SUBVOLUME}/")
        subvol_path = (
            subvol_path_raw[: idx + len(f"{SUBVOLUME}/")]
            if idx != -1
            else subvol_path_raw.strip()
        )
        log.info("Subvolume path: %s", subvol_path)

        mirror_utils.add_path_for_mirroring(source_client, SOURCE_FS, subvol_path)

        source_mount = f"/mnt/source_restart_{_rand_mount_suffix()}/"
        mon_ips = fs_util_ceph1.get_mon_node_ips()
        fs_util_ceph1.kernel_mount(
            [source_client],
            source_mount,
            ",".join(mon_ips),
            sub_dir=subvol_path,
            extra_params=f",fs={SOURCE_FS}",
        )
        log.info("Source mounted at %s", source_mount)

        # ----------------------------------------------------------------
        # 2. Create mixed dataset
        # ----------------------------------------------------------------
        log.info("=" * 60)
        log.info("STEP 2: Generate mixed dataset on source")
        log.info("=" * 60)

        small_dir = f"{source_mount}small_files"
        medium_dir = f"{source_mount}medium_files"
        large_dir = f"{source_mount}large_files"

        _create_files(
            source_client,
            small_dir,
            SMALL_FILE_COUNT,
            SMALL_MIN_KB * 1024,
            SMALL_MAX_KB * 1024,
            "small",
        )
        _create_files(
            source_client,
            medium_dir,
            MEDIUM_FILE_COUNT,
            MEDIUM_MIN_MB * 1024 * 1024,
            MEDIUM_MAX_MB * 1024 * 1024,
            "medium",
        )
        _create_files(
            source_client,
            large_dir,
            LARGE_FILE_COUNT,
            LARGE_MIN_MB * 1024 * 1024,
            LARGE_MAX_MB * 1024 * 1024,
            "large",
        )

        log.info("Computing source checksums ...")
        source_checksums = _compute_checksums(
            source_client, source_mount, "/tmp/source_checksums.txt"
        )
        src_file_count = len(source_checksums.splitlines())
        log.info("Source checksum file has %d entries", src_file_count)

        # ----------------------------------------------------------------
        # 3. Create snapshot and observe initial sync
        # ----------------------------------------------------------------
        log.info("=" * 60)
        log.info("STEP 3: Create snapshot and observe initial sync")
        log.info("=" * 60)

        source_client.exec_command(
            sudo=True,
            cmd=f"ceph fs subvolume snapshot create {SOURCE_FS} {SUBVOLUME} {SNAPSHOT}",
        )
        log.info("Snapshot '%s' created", SNAPSHOT)

        sync_started = False
        initial_mode = None
        pre_restart_snaps_synced = None
        sync_start_time = time.time()

        for _ in range(120):
            time.sleep(POLL_INTERVAL_FAST)
            elapsed = time.time() - sync_start_time
            data, meta = _get_peer_status_via_asok(
                mirror_utils, source_client, cephfs_mirror_nodes, SOURCE_FS
            )
            if not data:
                log.info("  [%.0fs] peer status not available yet", elapsed)
                continue

            for path, status in data.items():
                state = status.get("state", "")
                snaps_synced = status.get("snaps_synced", 0)
                last_snap = status.get("last_synced_snap", {})
                snap_name = (
                    last_snap.get("name", "") if isinstance(last_snap, dict) else ""
                )

                log.info(
                    "  [%.0fs] path=%s state=%s snaps_synced=%s last_snap=%s",
                    elapsed,
                    path,
                    state,
                    snaps_synced,
                    snap_name,
                )

                if snap_name == SNAPSHOT:
                    log.info("Snapshot already synced before daemon restart")
                    sync_started = True
                    break

                if "syncing" in str(state).lower():
                    sync_started = True
                    sync_mode = status.get("sync_mode", status.get("mode", "unknown"))
                    if initial_mode is None:
                        initial_mode = sync_mode
                        pre_restart_snaps_synced = snaps_synced
                        log.info(
                            "Sync started! mode=%s, snaps_synced=%s",
                            sync_mode,
                            snaps_synced,
                        )

            if sync_started:
                break

        if not sync_started:
            log.warning(
                "Could not observe 'syncing' state; proceeding with restart anyway"
            )

        # ----------------------------------------------------------------
        # 4. Restart daemon mid-sync
        # ----------------------------------------------------------------
        log.info("=" * 60)
        log.info("STEP 4: Restart cephfs-mirror daemon mid-sync")
        log.info("=" * 60)

        if sync_started and initial_mode:
            restart_threshold_pct = 10
            wait_limit = 30
            waited = 0
            while waited < wait_limit:
                time.sleep(5)
                waited += 5
                log.info(
                    "  Waiting for sync progress (%ds / %ds) ...", waited, wait_limit
                )

        log.info(
            "Restarting cephfs-mirror daemon via 'ceph orch restart cephfs-mirror'"
        )
        source_client.exec_command(
            sudo=True,
            cmd="ceph orch restart cephfs-mirror",
            timeout=120,
        )
        log.info("Daemon restart command issued, waiting 15s for recovery ...")
        time.sleep(15)

        post_restart_mode = None
        post_restart_snaps_synced = None

        for attempt in range(10):
            time.sleep(5)
            try:
                data, meta = _get_peer_status_via_asok(
                    mirror_utils, source_client, cephfs_mirror_nodes, SOURCE_FS
                )
            except Exception as e:
                log.warning(
                    "  asok query attempt %d failed (daemon recovering): %s", attempt, e
                )
                continue
            if not data:
                log.info("  asok attempt %d: no data yet", attempt)
                continue

            _log_peer_status(data, label="post-restart")
            for path, status in data.items():
                post_restart_mode = status.get("sync_mode", status.get("mode"))
                post_restart_snaps_synced = status.get("snaps_synced")
            break

        if initial_mode and post_restart_mode:
            if initial_mode != post_restart_mode:
                msg = (
                    f"BUG: sync mode flipped from '{initial_mode}' to "
                    f"'{post_restart_mode}' after daemon restart"
                )
                log.warning(msg)
                bugs_found.append(msg)
            else:
                log.info("Sync mode unchanged after restart: %s", post_restart_mode)

        if (
            pre_restart_snaps_synced is not None
            and post_restart_snaps_synced is not None
        ):
            if int(post_restart_snaps_synced) < int(pre_restart_snaps_synced):
                msg = (
                    f"BUG: snaps_synced counter reset from "
                    f"{pre_restart_snaps_synced} to {post_restart_snaps_synced}"
                )
                log.warning(msg)
                bugs_found.append(msg)

        # ----------------------------------------------------------------
        # 5. Wait for sync completion
        # ----------------------------------------------------------------
        log.info("=" * 60)
        log.info("STEP 5: Wait for sync completion (timeout %ds)", SYNC_TIMEOUT)
        log.info("=" * 60)

        sync_complete = False
        deadline = time.time() + SYNC_TIMEOUT

        while time.time() < deadline:
            time.sleep(POLL_INTERVAL_SLOW)
            try:
                data, meta = _get_peer_status_via_asok(
                    mirror_utils, source_client, cephfs_mirror_nodes, SOURCE_FS
                )
            except Exception as e:
                log.warning("  poll error: %s", e)
                continue
            if not data:
                continue

            for path, status in data.items():
                state = status.get("state", "")
                last_snap = status.get("last_synced_snap", {})
                snap_name = (
                    last_snap.get("name", "") if isinstance(last_snap, dict) else ""
                )

                log.info(
                    "  state=%s  last_synced_snap=%s  snaps_synced=%s",
                    state,
                    snap_name,
                    status.get("snaps_synced"),
                )

                if snap_name == SNAPSHOT:
                    log.info("Snapshot '%s' sync complete!", SNAPSHOT)
                    sync_complete = True
                    break

            if sync_complete:
                break

        if not sync_complete:
            log.error("Sync did not complete within %ds", SYNC_TIMEOUT)
            return 1

        # ----------------------------------------------------------------
        # 6. Validate checksums on target
        # ----------------------------------------------------------------
        log.info("=" * 60)
        log.info("STEP 6: Mount target and validate checksums")
        log.info("=" * 60)

        target_subvol_raw, _ = target_client.exec_command(
            sudo=True,
            cmd=f"ceph fs subvolume getpath {TARGET_FS} {SUBVOLUME}",
        )
        idx = target_subvol_raw.find(f"{SUBVOLUME}/")
        target_subvol_path = (
            target_subvol_raw[: idx + len(f"{SUBVOLUME}/")]
            if idx != -1
            else target_subvol_raw.strip()
        )
        log.info("Target subvolume path: %s", target_subvol_path)

        target_mount = f"/mnt/target_restart_{_rand_mount_suffix()}/"
        target_mon_ips = fs_util_ceph2.get_mon_node_ips()
        fs_util_ceph2.kernel_mount(
            [target_client],
            target_mount,
            ",".join(target_mon_ips),
            sub_dir=target_subvol_path,
            extra_params=f",fs={TARGET_FS}",
        )
        log.info("Target mounted at %s", target_mount)

        snap_root = f"{target_mount}.snap/{SNAPSHOT}"
        log.info("Computing target checksums under %s ...", snap_root)

        ls_out, _ = target_client.exec_command(
            sudo=True, cmd=f"ls {snap_root}/", check_ec=False
        )
        log.info("Target snap contents: %s", ls_out.strip()[:500])

        target_checksums = _compute_checksums(
            target_client, snap_root, "/tmp/target_checksums.txt"
        )
        tgt_file_count = len(target_checksums.splitlines())
        log.info("Target checksum file has %d entries", tgt_file_count)

        src_lines = _strip_path_prefix(source_checksums, source_mount)
        tgt_lines = _strip_path_prefix(target_checksums, snap_root)

        src_set = set(src_lines)
        tgt_set = set(tgt_lines)

        src_map = {line.split("  ", 1)[1]: line.split("  ", 1)[0] for line in src_lines}
        tgt_map = {line.split("  ", 1)[1]: line.split("  ", 1)[0] for line in tgt_lines}

        missing_on_target = set(src_map.keys()) - set(tgt_map.keys())
        extra_on_target = set(tgt_map.keys()) - set(src_map.keys())
        common_files = set(src_map.keys()) & set(tgt_map.keys())
        mismatched = {f for f in common_files if src_map[f] != tgt_map[f]}

        # ----------------------------------------------------------------
        # 7. Report results
        # ----------------------------------------------------------------
        log.info("=" * 60)
        log.info("STEP 7: Results summary")
        log.info("=" * 60)

        log.info("Source files: %d  |  Target files: %d", len(src_map), len(tgt_map))

        if missing_on_target:
            log.error("FILES MISSING ON TARGET (%d):", len(missing_on_target))
            for f in sorted(missing_on_target)[:20]:
                log.error("  MISSING: %s", f)
            if len(missing_on_target) > 20:
                log.error("  ... and %d more", len(missing_on_target) - 20)

        if extra_on_target:
            log.warning("EXTRA files on target (%d):", len(extra_on_target))
            for f in sorted(extra_on_target)[:10]:
                log.warning("  EXTRA: %s", f)

        if mismatched:
            log.error("CHECKSUM MISMATCHES (%d):", len(mismatched))
            for f in sorted(mismatched)[:20]:
                log.error("  MISMATCH: %s  src=%s  tgt=%s", f, src_map[f], tgt_map[f])
            if len(mismatched) > 20:
                log.error("  ... and %d more", len(mismatched) - 20)

        if bugs_found:
            log.warning("-" * 40)
            log.warning("BUGS DETECTED (logged, not failing test):")
            for b in bugs_found:
                log.warning("  * %s", b)
            log.warning("-" * 40)

        data_ok = not missing_on_target and not mismatched
        if data_ok:
            log.info("CHECKSUM VALIDATION PASSED - all %d files match", len(src_map))
            return 0
        else:
            log.error(
                "CHECKSUM VALIDATION FAILED - %d missing, %d mismatched",
                len(missing_on_target),
                len(mismatched),
            )
            return 1

    except Exception as e:
        log.error("Test failed with exception: %s", e)
        log.error(traceback.format_exc())
        return 1

    finally:
        log.info("=" * 60)
        log.info("CLEANUP")
        log.info("=" * 60)

        try:
            if target_mount and target_client:
                target_client.exec_command(
                    sudo=True, cmd=f"umount -l {target_mount}", check_ec=False
                )
                target_client.exec_command(
                    sudo=True, cmd=f"rm -rf {target_mount}", check_ec=False
                )
                log.info("Target unmounted and cleaned up")
        except Exception as e:
            log.warning("Target cleanup error: %s", e)

        try:
            if source_mount and source_client:
                source_client.exec_command(
                    sudo=True, cmd=f"umount -l {source_mount}", check_ec=False
                )
                source_client.exec_command(
                    sudo=True, cmd=f"rm -rf {source_mount}", check_ec=False
                )
                log.info("Source unmounted and cleaned up")
        except Exception as e:
            log.warning("Source cleanup error: %s", e)

        try:
            if subvol_path and mirror_utils and source_client:
                mirror_utils.remove_path_from_mirroring(
                    source_client, SOURCE_FS, subvol_path
                )
                log.info("Removed path from mirroring")
        except Exception as e:
            log.warning("Remove mirroring path error: %s", e)

        try:
            if source_client and fs_util_ceph1:
                source_client.exec_command(
                    sudo=True,
                    cmd=(
                        f"ceph fs subvolume snapshot rm {SOURCE_FS} "
                        f"{SUBVOLUME} {SNAPSHOT} --force"
                    ),
                    check_ec=False,
                )
                log.info("Snapshot removed on source")

                fs_util_ceph1.remove_subvolume(
                    source_client, SOURCE_FS, SUBVOLUME, validate=False
                )
                log.info("Subvolume removed on source")
        except Exception as e:
            log.warning("Subvolume cleanup error: %s", e)

        log.info("Cleanup complete")
