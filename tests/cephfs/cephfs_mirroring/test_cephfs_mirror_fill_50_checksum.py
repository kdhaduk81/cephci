"""
Test: Fill source cluster to ~50%, setup CephFS mirroring with minutely
snap-schedule, and leave it running.

This is a fire-and-forget setup script — it configures everything and exits.
No sync validation or cleanup is performed.

Workflow:
  1. Create CephFS on both clusters, deploy mirroring.
  2. Create a subvolume, mount it on source, add its path for mirroring.
  3. Query cluster capacity and write data until ~50% raw usage is reached.
  4. Enable minutely snap-schedule on the subvolume path.
  5. Exit — mirroring and snap-schedule continue running.

Suite YAML entry:
  - test:
      abort-on-fail: false
      desc: "Fill source to 50%, setup mirroring, enable minutely snap-schedule"
      clusters:
        ceph1:
          config:
            name: Fill 50% and setup mirroring with snap-schedule
            fill_percent: 50
      module: cephfs_mirroring.test_cephfs_mirror_fill_50_checksum.py
      name: Fill 50% and setup mirroring with snap-schedule
"""

import json
import random
import string
import time
import traceback

from tests.cephfs.cephfs_mirroring.cephfs_mirroring_utils import CephfsMirroringUtils
from tests.cephfs.cephfs_utilsV1 import FsUtils
from tests.cephfs.snapshot_clone.cephfs_snap_utils import SnapUtils
from utility.log import Log

log = Log(__name__)

FILL_PERCENT = 50
IO_FILE_SIZE_MB = 512
IO_BATCH_FILES = 20
SNAP_SCHEDULE_INTERVAL = "1m"


def get_cluster_raw_usage(client):
    """Return (total_bytes, used_bytes, avail_bytes, used_pct) from 'ceph df'."""
    out, _ = client.exec_command(sudo=True, cmd="ceph df --format json")
    df = json.loads(out)
    stats = df["stats"]
    total = stats["total_bytes"]
    used = stats["total_used_raw_bytes"]
    avail = stats["total_avail_bytes"]
    pct = (used / total * 100) if total > 0 else 0
    return total, used, avail, pct


def fill_cluster_to_percent(client, mount_path, target_pct, fs_name):
    """Write data into mount_path until cluster raw usage reaches target_pct."""
    total, used, avail, current_pct = get_cluster_raw_usage(client)
    log.info(
        "Cluster usage before fill: %.2f%% (used=%d, total=%d)",
        current_pct,
        used,
        total,
    )

    if current_pct >= target_pct:
        log.info(
            "Cluster already at %.2f%% >= target %d%%, skipping fill",
            current_pct,
            target_pct,
        )
        return

    target_bytes = int(total * target_pct / 100)
    bytes_to_write = target_bytes - used

    # Get the actual data pool name from the filesystem
    fs_out, _ = client.exec_command(
        sudo=True, cmd=f"ceph fs get {fs_name} --format json"
    )
    fs_info = json.loads(fs_out)
    data_pool_id = fs_info["mdsmap"]["data_pools"][0]
    pool_ls_out, _ = client.exec_command(
        sudo=True, cmd="ceph osd pool ls detail --format json"
    )
    pool_list = json.loads(pool_ls_out)
    repl_size = 3
    for pool in pool_list:
        if pool["pool_id"] == data_pool_id:
            repl_size = pool.get("size", 3)
            log.info(
                "Data pool: %s (id=%d), replication size: %d",
                pool["pool_name"],
                data_pool_id,
                repl_size,
            )
            break
    actual_write = bytes_to_write // repl_size
    log.info(
        "Need to write ~%d MB of data (repl_size=%d) to reach %d%%",
        actual_write // (1024 * 1024),
        repl_size,
        target_pct,
    )

    data_dir = f"{mount_path}/fill_data"
    client.exec_command(sudo=True, cmd=f"mkdir -p {data_dir}")

    written = 0
    batch = 0
    file_size_bytes = IO_FILE_SIZE_MB * 1024 * 1024

    while written < actual_write:
        batch += 1
        for i in range(IO_BATCH_FILES):
            fname = f"{data_dir}/fill_batch{batch}_file{i}.dat"
            client.exec_command(
                sudo=True,
                cmd=f"dd if=/dev/zero of={fname} bs=1M count={IO_FILE_SIZE_MB} status=none",
                timeout=600,
            )
            written += file_size_bytes
            if written >= actual_write:
                break

        _, _, _, current_pct = get_cluster_raw_usage(client)
        log.info(
            "Batch %d done: wrote ~%d MB total, cluster at %.2f%%",
            batch,
            written // (1024 * 1024),
            current_pct,
        )
        if current_pct >= target_pct:
            log.info("Target usage reached: %.2f%%", current_pct)
            break

    _, _, _, final_pct = get_cluster_raw_usage(client)
    log.info("Final cluster usage: %.2f%%", final_pct)


def run(ceph_cluster, **kw):
    try:
        config = kw.get("config")
        ceph_cluster_dict = kw.get("ceph_cluster_dict")
        test_data = kw.get("test_data")
        snap_util = SnapUtils(ceph_cluster)
        fill_pct = config.get("fill_percent", FILL_PERCENT) if config else FILL_PERCENT

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

        log.info("Checking pre-requisites")
        log.info("source_clients: %s", source_clients)
        log.info("target_clients: %s", target_clients)
        log.info("cephfs_mirror_node: %s", cephfs_mirror_node)
        if not source_clients or not target_clients:
            log.error(
                "This test requires at least 1 client node on both ceph1 and ceph2."
            )
            return 1
        if not cephfs_mirror_node:
            log.error("ceph1 must have at least one cephfs-mirror node.")
            return 1

        log.info("Preparing clients")
        fs_util_ceph1.prepare_clients(source_clients, build)
        fs_util_ceph2.prepare_clients(target_clients, build)
        fs_util_ceph1.auth_list(source_clients)
        fs_util_ceph2.auth_list(target_clients)

        source_fs = "cephfs"
        target_fs = "cephfs"

        fs_details = fs_util_ceph1.get_fs_info(source_clients[0], source_fs)
        if not fs_details:
            fs_util_ceph1.create_fs(source_clients[0], source_fs)
            fs_util_ceph1.wait_for_mds_process(source_clients[0], source_fs)
        fs_details = fs_util_ceph2.get_fs_info(target_clients[0], target_fs)
        if not fs_details:
            fs_util_ceph2.create_fs(target_clients[0], target_fs)
            fs_util_ceph2.wait_for_mds_process(target_clients[0], target_fs)

        # --- Deploy mirroring ---
        log.info("Deploying CephFS mirroring")
        target_user = "mirror_remote_fill"
        target_site_name = "remote_site_fill"
        token = fs_mirroring_utils.deploy_cephfs_mirroring(
            source_fs,
            source_clients[0],
            target_fs,
            target_clients[0],
            target_user,
            target_site_name,
        )
        log.info("Mirroring deployed. Token: %s", token)

        # --- Create subvolume ---
        subvolgroup = "svgroup_fill"
        subvolume = "subvol_fill"
        fs_util_ceph1.create_subvolumegroup(
            source_clients[0], vol_name=source_fs, group_name=subvolgroup
        )
        fs_util_ceph1.create_subvolume(
            source_clients[0],
            vol_name=source_fs,
            subvol_name=subvolume,
            group_name=subvolgroup,
        )

        subvol_path_raw, _ = source_clients[0].exec_command(
            sudo=True,
            cmd=f"ceph fs subvolume getpath {source_fs} {subvolume} {subvolgroup}",
        )
        idx = subvol_path_raw.find(f"{subvolume}/")
        subvol_path = (
            subvol_path_raw[: idx + len(f"{subvolume}/")]
            if idx != -1
            else subvol_path_raw.strip()
        )
        log.info("Subvolume path: %s", subvol_path)

        # --- Mount on source ---
        mounting_dir = "".join(
            random.choice(string.ascii_lowercase + string.digits) for _ in range(10)
        )
        source_mount = f"/mnt/cephfs_fill_{mounting_dir}/"
        mon_node_ips = fs_util_ceph1.get_mon_node_ips()
        fs_util_ceph1.kernel_mount(
            [source_clients[0]],
            source_mount,
            ",".join(mon_node_ips),
            extra_params=f",fs={source_fs}",
        )

        # --- Add path for mirroring ---
        log.info("Adding subvolume path for mirroring")
        fs_mirroring_utils.add_path_for_mirroring(
            source_clients[0], source_fs, subvol_path
        )

        # --- Fill source cluster to target percentage ---
        # Write fill data outside the mirrored subvolume so it doesn't sync to target
        fill_data_path = f"{source_mount}/fill_data_no_mirror"
        source_clients[0].exec_command(sudo=True, cmd=f"mkdir -p {fill_data_path}")
        log.info("Filling source cluster to %d%% (outside mirrored path)", fill_pct)
        fill_cluster_to_percent(source_clients[0], fill_data_path, fill_pct, source_fs)

        # --- Enable snap schedule ---
        log.info("Enabling snap schedule module")
        snap_util.enable_snap_schedule(source_clients[0])
        time.sleep(10)
        snap_util.allow_minutely_schedule(source_clients[0], allow=True)
        time.sleep(10)

        log.info("Creating minutely snap schedule on %s", subvol_path)
        snap_params = {
            "client": source_clients[0],
            "path": subvol_path,
            "sched": SNAP_SCHEDULE_INTERVAL,
            "fs_name": source_fs,
            "validate": True,
        }
        result = snap_util.create_snap_schedule(snap_params)
        if result != 0:
            log.error("Failed to create snap schedule")
            return 1

        sched_list = snap_util.get_snap_schedule_list(
            source_clients[0], subvol_path, source_fs
        )
        log.info("Snap schedule active: %s", sched_list)

        log.info("=" * 60)
        log.info("SETUP COMPLETE - mirroring and snap-schedule are running")
        log.info("Source FS: %s | Target FS: %s", source_fs, target_fs)
        log.info("Subvolume path: %s", subvol_path)
        log.info("Snap schedule: %s", SNAP_SCHEDULE_INTERVAL)
        log.info("Source mount: %s", source_mount)
        log.info("Source cluster filled to ~%d%%", fill_pct)
        log.info("=" * 60)

        # --- Lightweight IO on mirrored path for longevity testing ---
        mirror_io_path = f"{source_mount}{subvol_path}/mirror_io"
        source_clients[0].exec_command(sudo=True, cmd=f"mkdir -p {mirror_io_path}")
        io_duration = config.get("io_duration", 172800) if config else 172800
        log.info("Starting lightweight IO on mirrored path for %d seconds", io_duration)
        from datetime import datetime, timedelta

        stop_time = datetime.now() + timedelta(seconds=io_duration)
        cycle = 0
        while datetime.now() < stop_time:
            cycle += 1
            batch_dir = f"{mirror_io_path}/cycle_{cycle}"
            source_clients[0].exec_command(
                sudo=True, cmd=f"mkdir -p {batch_dir}", timeout=60
            )
            for i in range(5):
                source_clients[0].exec_command(
                    sudo=True,
                    cmd=f"dd if=/dev/urandom of={batch_dir}/file_{i}.dat bs=1M count=10 status=none",
                    timeout=120,
                )
            log.info("Cycle %d: created 5x10MB files in %s", cycle, batch_dir)
            time.sleep(120)
            source_clients[0].exec_command(
                sudo=True, cmd=f"rm -rf {batch_dir}", timeout=60
            )
            log.info("Cycle %d: cleaned up %s", cycle, batch_dir)
            time.sleep(60)

        log.info("Lightweight IO completed after %d cycles", cycle)
        return 0

    except Exception as e:
        log.error(e)
        log.error(traceback.format_exc())
        return 1
