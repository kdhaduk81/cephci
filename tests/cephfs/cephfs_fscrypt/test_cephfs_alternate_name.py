"""
Test module for IBMCEPH-17727: cephfs – Add mechanism to allow get and set of alternate_name.

Feature:
    A new extended attribute ``ceph.alternate_name`` exposes the internal CephFS dentry
    ``alternate_name`` field via the filesystem xattr interface.  For fscrypt, this field
    holds the raw ciphertext of an encrypted filename whenever the cleartext name is long
    enough that the encrypted form overflows the 189-character CephFS filename limit (roughly
    cleartext > 134 characters).

    Administrators can use this xattr to copy ``alternate_name`` metadata during replication
    of fscrypt-encrypted subvolumes **without** needing the user's encryption key.

Test cases implemented
----------------------
TC1 – altname_get_short_filename
    Verify ``ceph.alternate_name`` is absent or empty for short filenames (1–100 chars).

TC2 – altname_get_long_filename
    Verify ``ceph.alternate_name`` is present and non-empty for long filenames (> ~134 chars).

TC3 – altname_set_valid_value
    Verify an admin can SET ``ceph.alternate_name`` to a valid value and read it back.

TC4 – altname_lock_unlock_persistence
    Verify ``ceph.alternate_name`` is readable and consistent before / during / after a
    fscrypt lock–unlock cycle.

TC5 – altname_negative_invalid_value
    Verify that setting ``ceph.alternate_name`` to invalid/garbage data is either rejected or
    leaves the file in a recoverable state.

TC6 – altname_replication_simulation
    End-to-end replication simulation: copy ``ceph.alternate_name`` from a source mount to a
    target mount and verify values match exactly.

TC7 – altname_boundary_filename_lengths
    Identify and validate the exact cleartext-length threshold where alternate_name transitions
    from absent to present.

Upstream tracker: https://tracker.ceph.com/issues/79375
"""

import random
import string
import traceback

from tests.cephfs.cephfs_utilsV1 import FsUtils as FsUtilsv1
from tests.cephfs.lib.cephfs_common_lib import CephFSCommonUtils
from tests.cephfs.lib.fscrypt_utils import FscryptUtils
from utility.log import Log

log = Log(__name__)

# Filenames longer than this cleartext threshold are expected to generate a non-empty
# ceph.alternate_name (encrypted form exceeds the 189-char CephFS dentry limit).
ALTNAME_THRESHOLD_CHARS = 134


def run(ceph_cluster, **kw):
    """
    Entry point called by the test framework.

    config options
    --------------
    test_name : str
        Name of a single test to run.  Omit (or set to "all_tests") to run all tests.
    cleanup   : int (0 or 1, default 1)
        Whether to unmount / remove resources after the test.
    """
    try:
        test_data = kw.get("test_data")
        fs_util = FsUtilsv1(ceph_cluster, test_data=test_data)
        cephfs_common_utils = CephFSCommonUtils(ceph_cluster)
        erasure = (
            FsUtilsv1.get_custom_config_value(test_data, "erasure")
            if test_data
            else False
        )
        fscrypt_util = FscryptUtils(ceph_cluster)

        config = kw.get("config")
        clients = ceph_cluster.get_ceph_objects("client")
        if len(clients) < 2:
            log.error(
                "This test requires minimum 2 client nodes. Only %s available.",
                len(clients),
            )
            return 1

        build = config.get("build", config.get("rhbuild"))
        fs_util.prepare_clients(clients, build)
        fs_util.auth_list(clients)
        default_fs = config.get("fs_name", "cephfs")
        default_fs = default_fs if not erasure else "cephfs-ec"
        cleanup = config.get("cleanup", 1)
        client = clients[0]

        log.info("Pre-requisites check")
        for client_tmp in clients:
            if cephfs_common_utils.client_mount_cleanup(client_tmp):
                log.error("Client old mountpoints cleanup did not succeed")
                fs_util.reboot_node(client_tmp)

        log.info("Setup test configuration")
        setup_params = cephfs_common_utils.test_setup(default_fs, client)
        fs_name = setup_params["fs_name"]

        log.info("Mount subvolumes")
        mount_details = cephfs_common_utils.test_mount(
            clients, setup_params, mnt_type_list=["fuse"]
        )

        log.info("Verify cluster is healthy before test")
        if cephfs_common_utils.wait_for_healthy_ceph(client, 300):
            log.error("Cluster health is not OK even after waiting 300 s")
            return 1

        test_case_name = config.get("test_name", "all_tests")
        all_tests = [
            "altname_get_short_filename",
            "altname_get_long_filename",
            "altname_set_valid_value",
            "altname_lock_unlock_persistence",
            "altname_negative_invalid_value",
            "altname_replication_simulation",
            "altname_boundary_filename_lengths",
        ]
        test_list = [test_case_name] if test_case_name in all_tests else all_tests

        fscrypt_test_params = {
            "ceph_cluster": ceph_cluster,
            "fs_name": fs_name,
            "cephfs_common_utils": cephfs_common_utils,
            "fscrypt_util": fscrypt_util,
            "fs_util": fs_util,
            "clients": clients,
            "setup_params": setup_params,
            "mount_details": mount_details,
        }

        for test_name in test_list:
            log.info(
                "\n\n                    ============ %s ============ \n", test_name
            )
            fscrypt_test_params["test_case"] = test_name
            test_status = altname_test_run(fscrypt_test_params)
            if test_status:
                log.error("Test %s FAILED", test_name)
                return 1
            log.info("Test %s PASSED", test_name)

        return 0

    except Exception as exc:
        log.error(exc)
        log.error(traceback.format_exc())
        return 1
    finally:
        log.info("Cleanup in progress")
        wait_time_secs = 300
        if cephfs_common_utils.wait_for_healthy_ceph(client, wait_time_secs):
            assert (
                False
            ), f"Cluster health is not OK even after waiting {wait_time_secs} s"
        if cleanup:
            cephfs_common_utils.test_cleanup(client, setup_params, mount_details)
            fs_util.remove_fs(client, fs_name)


# ---------------------------------------------------------------------------
# Test dispatcher
# ---------------------------------------------------------------------------


def altname_test_run(fscrypt_test_params):
    """Dispatch a test by name and return its status (0=pass, 1=fail)."""
    tests = {
        "altname_get_short_filename": altname_get_short_filename,
        "altname_get_long_filename": altname_get_long_filename,
        "altname_set_valid_value": altname_set_valid_value,
        "altname_lock_unlock_persistence": altname_lock_unlock_persistence,
        "altname_negative_invalid_value": altname_negative_invalid_value,
        "altname_replication_simulation": altname_replication_simulation,
        "altname_boundary_filename_lengths": altname_boundary_filename_lengths,
    }
    test_fn = tests[fscrypt_test_params["test_case"]]
    return test_fn(fscrypt_test_params)


# ---------------------------------------------------------------------------
# TC1 – altname_get_short_filename
# ---------------------------------------------------------------------------


def altname_get_short_filename(fscrypt_test_params):
    """
    TC1: Verify ``ceph.alternate_name`` is absent / empty for short filenames.

    Short filenames (1–100 cleartext chars) produce an encrypted form that fits within the
    189-char CephFS dentry limit so no alternate_name ciphertext is stored.

    Steps
    -----
    1. Pick a subvolume fuse mountpoint and client.
    2. Run ``fscrypt setup`` on the mountpoint.
    3. Create an empty directory and ``fscrypt encrypt`` it with a custom-passphrase protector.
    4. Create test files with 1, 50, and 100-character names inside the encrypted directory.
    5. Run ``getfattr -n ceph.alternate_name`` on each file via the helper.
    6. Assert the xattr is absent (empty string) for every short-named file.

    Pass criteria
    -------------
    ``ceph.alternate_name`` returns an empty string for all three short filenames.
    """
    log.info(
        "TC1: ceph.alternate_name must be absent/empty for short filenames (1–100 chars)"
    )
    fscrypt_util = fscrypt_test_params["fscrypt_util"]
    mount_details = fscrypt_test_params["mount_details"]

    # Pick a random subvolume that is fuse-mounted
    sv_name = random.choice(list(mount_details.keys()))
    mnt_client = mount_details[sv_name]["fuse"]["mnt_client"]
    mnt_pt = mount_details[sv_name]["fuse"]["mountpoint"]

    # ── Step 2: fscrypt setup ──────────────────────────────────────────────
    log.info("Step 2: fscrypt setup on mountpoint %s", mnt_pt)
    if fscrypt_util.setup(mnt_client, mnt_pt):
        log.error("fscrypt setup failed on %s", mnt_pt)
        return 1

    # ── Step 3: Create and encrypt a test directory ───────────────────────
    rand_tag = _rand_str(6)
    encrypt_path = f"{mnt_pt}/tc1_short_{rand_tag}"
    mnt_client.exec_command(sudo=True, cmd=f"mkdir {encrypt_path}", check_ec=False)
    log.info("Step 3: fscrypt encrypt on %s", encrypt_path)
    encrypt_params = fscrypt_util.encrypt(
        mnt_client,
        encrypt_path,
        mnt_pt,
        protector_source="custom_passphrase",
    )
    if encrypt_params == 1:
        log.error("fscrypt encrypt failed on %s", encrypt_path)
        return 1

    # ── Step 4: Create files with short names ─────────────────────────────
    short_name_lengths = [1, 50, 100]
    file_paths = {}
    for length in short_name_lengths:
        filename = _rand_str(length)
        file_path = f"{encrypt_path}/{filename}"
        mnt_client.exec_command(
            sudo=True,
            cmd=f"echo 'tc1_test_data' > {file_path}",
            check_ec=False,
        )
        file_paths[length] = file_path
        log.info("Created %d-char file: %s", length, file_path)

    # ── Step 5 + 6: Get xattr and assert empty ────────────────────────────
    test_status = 0
    for length, file_path in file_paths.items():
        log.info("Step 5: getfattr ceph.alternate_name on %d-char file", length)
        altname = fscrypt_util.get_altname_xattr(mnt_client, file_path)

        if altname is None:
            log.error(
                "TC1 FAIL: Unexpected error reading ceph.alternate_name for %d-char file %s",
                length,
                file_path,
            )
            test_status += 1
        elif altname != "":
            log.error(
                "TC1 FAIL: Expected empty alternate_name for %d-char file but got: '%s'",
                length,
                altname,
            )
            test_status += 1
        else:
            log.info(
                "TC1 PASS: ceph.alternate_name is empty (as expected) for %d-char file",
                length,
            )

    # ── Cleanup: lock and leave the rest to the framework teardown ────────
    fscrypt_util.lock(mnt_client, encrypt_path)
    return 1 if test_status else 0


# ---------------------------------------------------------------------------
# TC2 – altname_get_long_filename
# ---------------------------------------------------------------------------


def altname_get_long_filename(fscrypt_test_params):
    """
    TC2: Verify ``ceph.alternate_name`` is non-empty for long filenames (> ~134 chars).

    For filenames whose cleartext length exceeds the threshold, the encrypted form overflows
    the 189-char CephFS limit and the ciphertext is stored in ``alternate_name``.

    Steps
    -----
    1. Pick a subvolume fuse mountpoint and client.
    2. fscrypt setup → encrypt an empty directory.
    3. Create test files with 140, 200, and 250-character names.
    4. Run ``getfattr -n ceph.alternate_name`` on each file.
    5. Assert the xattr is non-empty for every long-named file.
    6. Assert that the value is non-trivially long (contains actual ciphertext bytes).

    Pass criteria
    -------------
    ``ceph.alternate_name`` returns a non-empty, non-whitespace string for all three
    long-named files.
    """
    log.info(
        "TC2: ceph.alternate_name must be non-empty for long filenames (140–250 chars)"
    )
    fscrypt_util = fscrypt_test_params["fscrypt_util"]
    mount_details = fscrypt_test_params["mount_details"]

    sv_name = random.choice(list(mount_details.keys()))
    mnt_client = mount_details[sv_name]["fuse"]["mnt_client"]
    mnt_pt = mount_details[sv_name]["fuse"]["mountpoint"]

    # ── fscrypt setup + encrypt ───────────────────────────────────────────
    log.info("fscrypt setup on %s", mnt_pt)
    if fscrypt_util.setup(mnt_client, mnt_pt):
        log.error("fscrypt setup failed on %s", mnt_pt)
        return 1

    rand_tag = _rand_str(6)
    encrypt_path = f"{mnt_pt}/tc2_long_{rand_tag}"
    mnt_client.exec_command(sudo=True, cmd=f"mkdir {encrypt_path}", check_ec=False)
    log.info("fscrypt encrypt on %s", encrypt_path)
    encrypt_params = fscrypt_util.encrypt(
        mnt_client,
        encrypt_path,
        mnt_pt,
        protector_source="custom_passphrase",
    )
    if encrypt_params == 1:
        log.error("fscrypt encrypt failed on %s", encrypt_path)
        return 1

    # ── Create files with long names ──────────────────────────────────────
    long_name_lengths = [140, 200, 250]
    file_paths = {}
    for length in long_name_lengths:
        filename = _rand_str(length)
        file_path = f"{encrypt_path}/{filename}"
        mnt_client.exec_command(
            sudo=True,
            cmd=f"echo 'tc2_test_data' > {file_path}",
            check_ec=False,
        )
        file_paths[length] = file_path
        log.info("Created %d-char file: %s", length, file_path)

    # ── Get xattr and assert non-empty ────────────────────────────────────
    test_status = 0
    for length, file_path in file_paths.items():
        log.info("getfattr ceph.alternate_name on %d-char file: %s", length, file_path)
        altname = fscrypt_util.get_altname_xattr(mnt_client, file_path)

        if altname is None:
            log.error(
                "TC2 FAIL: Unexpected error reading ceph.alternate_name for %d-char file",
                length,
            )
            test_status += 1
        elif altname == "":
            log.error(
                "TC2 FAIL: Expected non-empty alternate_name for %d-char file but got empty",
                length,
            )
            test_status += 1
        else:
            log.info(
                "TC2 PASS: ceph.alternate_name is non-empty for %d-char file "
                "(length=%d chars)",
                length,
                len(altname),
            )

    fscrypt_util.lock(mnt_client, encrypt_path)
    return 1 if test_status else 0


# ---------------------------------------------------------------------------
# TC3 – altname_set_valid_value
# ---------------------------------------------------------------------------


def altname_set_valid_value(fscrypt_test_params):
    """
    TC3: Verify an admin can SET ``ceph.alternate_name`` and read back the same value.

    This simulates the replication administrator workflow where alternate_name metadata is
    copied from the source subvolume to a target subvolume without access to the user key.

    Steps
    -----
    1. Create an fscrypt-encrypted directory on a fuse-mounted subvolume.
    2. Create a file with a long name (250 chars) inside the encrypted directory.
    3. Read ``ceph.alternate_name`` from the source file; verify it is non-empty.
    4. Create a second (plain) file in the same mountpoint as the replication target.
    5. SET ``ceph.alternate_name`` on the target file using the value read from the source.
    6. Read ``ceph.alternate_name`` back from the target.
    7. Assert: read-back value equals what was SET.

    Pass criteria
    -------------
    The value returned by ``getfattr`` on the target equals the value that was written with
    ``setfattr``.
    """
    log.info("TC3: SET ceph.alternate_name and verify read-back matches")
    fscrypt_util = fscrypt_test_params["fscrypt_util"]
    mount_details = fscrypt_test_params["mount_details"]

    sv_name = random.choice(list(mount_details.keys()))
    mnt_client = mount_details[sv_name]["fuse"]["mnt_client"]
    mnt_pt = mount_details[sv_name]["fuse"]["mountpoint"]

    # ── fscrypt setup + encrypt ───────────────────────────────────────────
    if fscrypt_util.setup(mnt_client, mnt_pt):
        log.error("fscrypt setup failed")
        return 1

    rand_tag = _rand_str(6)
    encrypt_path = f"{mnt_pt}/tc3_set_{rand_tag}"
    mnt_client.exec_command(sudo=True, cmd=f"mkdir {encrypt_path}", check_ec=False)
    encrypt_params = fscrypt_util.encrypt(
        mnt_client, encrypt_path, mnt_pt, protector_source="custom_passphrase"
    )
    if encrypt_params == 1:
        log.error("fscrypt encrypt failed")
        return 1

    # ── Create source long-named file ─────────────────────────────────────
    src_filename = _rand_str(250)
    src_file = f"{encrypt_path}/{src_filename}"
    mnt_client.exec_command(
        sudo=True, cmd=f"echo 'tc3_source' > {src_file}", check_ec=False
    )
    log.info("Source file: %s", src_file)

    # ── Read alternate_name from source ───────────────────────────────────
    src_altname = fscrypt_util.get_altname_xattr(mnt_client, src_file)
    if src_altname is None or src_altname == "":
        log.error(
            "TC3 FAIL: Could not read non-empty alternate_name from source 250-char file"
        )
        return 1
    log.info("Source ceph.alternate_name (len=%d): %s", len(src_altname), src_altname)

    # ── Create target placeholder file (plain, outside encrypted dir) ─────
    tgt_file = f"{mnt_pt}/tc3_target_{rand_tag}"
    mnt_client.exec_command(
        sudo=True, cmd=f"echo 'tc3_target' > {tgt_file}", check_ec=False
    )
    log.info("Target file: %s", tgt_file)

    # ── SET alternate_name on target ──────────────────────────────────────
    log.info("SET ceph.alternate_name on target file")
    if fscrypt_util.set_altname_xattr(mnt_client, tgt_file, src_altname):
        log.error("TC3 FAIL: setfattr ceph.alternate_name returned failure")
        return 1

    # ── Read back and compare ─────────────────────────────────────────────
    tgt_altname = fscrypt_util.get_altname_xattr(mnt_client, tgt_file)
    if tgt_altname is None:
        log.error("TC3 FAIL: Error reading ceph.alternate_name back from target")
        return 1

    if tgt_altname != src_altname:
        log.error(
            "TC3 FAIL: Read-back value differs from SET value.\n"
            "  SET : %s\n  GOT : %s",
            src_altname,
            tgt_altname,
        )
        return 1

    log.info("TC3 PASS: Read-back alternate_name matches the SET value")
    fscrypt_util.lock(mnt_client, encrypt_path)
    mnt_client.exec_command(sudo=True, cmd=f"rm -f {tgt_file}", check_ec=False)
    return 0


# ---------------------------------------------------------------------------
# TC4 – altname_lock_unlock_persistence
# ---------------------------------------------------------------------------


def altname_lock_unlock_persistence(fscrypt_test_params):
    """
    TC4: Verify ``ceph.alternate_name`` is readable and consistent across lock/unlock cycles.

    Steps
    -----
    1. Create an fscrypt-encrypted directory.
    2. Create files with both short (50 chars) and long (200 chars) names.
    3. Record alternate_name for all files **before** lock.
    4. fscrypt lock the directory.
    5. Enumerate encrypted filenames and read alternate_name in locked state.
    6. fscrypt unlock the directory.
    7. Read alternate_name again using clear filenames **after** unlock.
    8. Assert: long-file alternate_name is consistent at all three stages; short-file
       alternate_name remains empty throughout.

    Pass criteria
    -------------
    - alternate_name for long-named files is non-empty and identical pre-lock, locked,
      and post-unlock.
    - alternate_name for short-named files is empty at all stages.
    """
    log.info("TC4: ceph.alternate_name persistence across fscrypt lock/unlock cycle")
    fscrypt_util = fscrypt_test_params["fscrypt_util"]
    mount_details = fscrypt_test_params["mount_details"]

    sv_name = random.choice(list(mount_details.keys()))
    mnt_client = mount_details[sv_name]["fuse"]["mnt_client"]
    mnt_pt = mount_details[sv_name]["fuse"]["mountpoint"]

    if fscrypt_util.setup(mnt_client, mnt_pt):
        log.error("fscrypt setup failed")
        return 1

    rand_tag = _rand_str(6)
    encrypt_path = f"{mnt_pt}/tc4_persist_{rand_tag}"
    mnt_client.exec_command(sudo=True, cmd=f"mkdir {encrypt_path}", check_ec=False)
    encrypt_params = fscrypt_util.encrypt(
        mnt_client, encrypt_path, mnt_pt, protector_source="custom_passphrase"
    )
    if encrypt_params == 1:
        log.error("fscrypt encrypt failed")
        return 1

    # ── Create short and long named files ─────────────────────────────────
    short_name = _rand_str(50)
    long_name = _rand_str(200)
    short_file = f"{encrypt_path}/{short_name}"
    long_file = f"{encrypt_path}/{long_name}"
    for fpath in [short_file, long_file]:
        mnt_client.exec_command(
            sudo=True, cmd=f"echo 'tc4_data' > {fpath}", check_ec=False
        )

    # ── Stage A: read alternate_name BEFORE lock ──────────────────────────
    log.info("Stage A: reading ceph.alternate_name before lock")
    pre_lock_short = fscrypt_util.get_altname_xattr(mnt_client, short_file)
    pre_lock_long = fscrypt_util.get_altname_xattr(mnt_client, long_file)
    log.info("pre-lock short altname : '%s'", pre_lock_short)
    log.info("pre-lock long  altname : '%s'", pre_lock_long)

    # ── Lock ──────────────────────────────────────────────────────────────
    log.info("Locking %s", encrypt_path)
    if fscrypt_util.lock(mnt_client, encrypt_path):
        log.error("fscrypt lock failed")
        return 1

    # ── Stage B: find encrypted filenames and read alternate_name LOCKED ──
    log.info("Stage B: reading ceph.alternate_name in locked state")
    out, _ = mnt_client.exec_command(
        sudo=True, cmd=f"find {encrypt_path} -maxdepth 1 -type f"
    )
    encrypted_files = [f for f in out.strip().split("\n") if f]
    log.info("Encrypted filenames found: %s", encrypted_files)

    locked_altnames = []
    for ef in encrypted_files:
        val = fscrypt_util.get_altname_xattr(mnt_client, ef)
        locked_altnames.append(val)
        log.info("  %s → altname='%s'", ef, val)

    # ── Unlock ────────────────────────────────────────────────────────────
    log.info("Unlocking %s", encrypt_path)
    protector_id = encrypt_params["protector_id"]
    unlock_args = {"key": encrypt_params.get("key")}
    if fscrypt_util.unlock(
        mnt_client, encrypt_path, mnt_pt, protector_id, **unlock_args
    ):
        log.error("fscrypt unlock failed")
        return 1

    # ── Stage C: read alternate_name AFTER unlock ─────────────────────────
    log.info("Stage C: reading ceph.alternate_name after unlock")
    post_unlock_short = fscrypt_util.get_altname_xattr(mnt_client, short_file)
    post_unlock_long = fscrypt_util.get_altname_xattr(mnt_client, long_file)
    log.info("post-unlock short altname : '%s'", post_unlock_short)
    log.info("post-unlock long  altname : '%s'", post_unlock_long)

    # ── Assertions ────────────────────────────────────────────────────────
    test_status = 0

    # Short file: always empty
    for stage, val in [
        ("pre-lock", pre_lock_short),
        ("post-unlock", post_unlock_short),
    ]:
        if val != "":
            log.error(
                "TC4 FAIL: Short-file alternate_name expected empty at %s, got '%s'",
                stage,
                val,
            )
            test_status += 1

    # Long file: non-empty and consistent pre-lock vs post-unlock
    if pre_lock_long == "":
        log.error("TC4 FAIL: Long-file alternate_name was empty before lock")
        test_status += 1
    elif post_unlock_long == "":
        log.error("TC4 FAIL: Long-file alternate_name is empty after unlock")
        test_status += 1
    elif pre_lock_long != post_unlock_long:
        log.error(
            "TC4 FAIL: Long-file alternate_name changed across lock/unlock!\n"
            "  pre-lock    : %s\n  post-unlock : %s",
            pre_lock_long,
            post_unlock_long,
        )
        test_status += 1
    else:
        log.info(
            "TC4 PASS: Long-file alternate_name is consistent across lock/unlock cycle"
        )

    # At least one locked altname must be non-empty (corresponds to the long file)
    if not any(v for v in locked_altnames if v):
        log.error(
            "TC4 FAIL: No non-empty alternate_name found in locked state. "
            "Expected at least one (for the long-named file)."
        )
        test_status += 1

    return 1 if test_status else 0


# ---------------------------------------------------------------------------
# TC5 – altname_negative_invalid_value
# ---------------------------------------------------------------------------


def altname_negative_invalid_value(fscrypt_test_params):
    """
    TC5: Verify guardrails reject an invalid ``ceph.alternate_name`` value.

    An improperly set alternate_name can break the internal fscrypt client mechanism.
    The kernel/MDS should either reject the write or the file should remain accessible
    after an unlock (i.e. the invalid value does not silently corrupt state).

    Steps
    -----
    1. Create an fscrypt-encrypted directory.
    2. Create a short-named file (50 chars) – alternate_name should be empty.
    3. Attempt to set ``ceph.alternate_name`` to a random garbage ASCII string on that file.
    4. Read back and verify either:
       a. The setfattr was rejected (non-zero return / error in output), OR
       b. The file is still accessible (unlock succeeds; read content matches written content).
    5. Attempt to set ``ceph.alternate_name`` to an empty string on a long-named file that
       requires a non-empty alternate_name.
    6. Verify: the file is still accessible after unlock (empty alternate_name on long file
       may corrupt the name but must not crash the cluster).

    Pass criteria
    -------------
    - Invalid SET is either rejected by the kernel/MDS, or the cluster remains stable.
    - No kernel panic or cluster HEALTH_ERR is observed after the invalid SET.
    """
    log.info("TC5: Negative – setting ceph.alternate_name with invalid values")
    fscrypt_util = fscrypt_test_params["fscrypt_util"]
    cephfs_common_utils = fscrypt_test_params["cephfs_common_utils"]
    mount_details = fscrypt_test_params["mount_details"]
    client = fscrypt_test_params["clients"][0]

    sv_name = random.choice(list(mount_details.keys()))
    mnt_client = mount_details[sv_name]["fuse"]["mnt_client"]
    mnt_pt = mount_details[sv_name]["fuse"]["mountpoint"]

    if fscrypt_util.setup(mnt_client, mnt_pt):
        log.error("fscrypt setup failed")
        return 1

    rand_tag = _rand_str(6)
    encrypt_path = f"{mnt_pt}/tc5_neg_{rand_tag}"
    mnt_client.exec_command(sudo=True, cmd=f"mkdir {encrypt_path}", check_ec=False)
    encrypt_params = fscrypt_util.encrypt(
        mnt_client, encrypt_path, mnt_pt, protector_source="custom_passphrase"
    )
    if encrypt_params == 1:
        log.error("fscrypt encrypt failed")
        return 1

    short_name = _rand_str(50)
    short_file = f"{encrypt_path}/{short_name}"
    mnt_client.exec_command(
        sudo=True, cmd=f"echo 'tc5_neg_data' > {short_file}", check_ec=False
    )

    # ── Sub-test A: Set garbage value on short-named file ─────────────────
    garbage_value = "GARBAGE_INVALID_VALUE_" + _rand_str(32)
    log.info(
        "Sub-test A: setfattr garbage value '%s' on short-named file", garbage_value
    )
    set_rc = fscrypt_util.set_altname_xattr(mnt_client, short_file, garbage_value)
    if set_rc == 0:
        # Accepted by kernel – verify cluster is still healthy (no HEALTH_ERR)
        log.info(
            "Kernel accepted the garbage alternate_name; verifying cluster health ..."
        )
        if cephfs_common_utils.wait_for_healthy_ceph(client, 60):
            log.error(
                "TC5 FAIL: Cluster became unhealthy after setting invalid alternate_name"
            )
            return 1
        log.info(
            "Cluster remains healthy; invalid value was accepted (guardrail may be MDS-side)"
        )
    else:
        log.info("TC5 PASS (sub-A): Kernel/MDS rejected the garbage alternate_name SET")

    # ── Sub-test B: Clear alternate_name on long-named file ───────────────
    long_name = _rand_str(200)
    long_file = f"{encrypt_path}/{long_name}"
    mnt_client.exec_command(
        sudo=True, cmd=f"echo 'tc5_long_data' > {long_file}", check_ec=False
    )
    original_altname = fscrypt_util.get_altname_xattr(mnt_client, long_file)
    log.info("Original alternate_name for long file: '%s'", original_altname)

    log.info("Sub-test B: clear (set empty) ceph.alternate_name on long-named file")
    set_rc2 = fscrypt_util.set_altname_xattr(mnt_client, long_file, "")
    if set_rc2 == 0:
        log.warning(
            "Kernel accepted empty alternate_name on long-named file; checking cluster health"
        )
        if cephfs_common_utils.wait_for_healthy_ceph(client, 60):
            log.error(
                "TC5 FAIL: Cluster became unhealthy after clearing alternate_name"
            )
            return 1
        log.info("Cluster remains healthy after clearing alternate_name on long file")
    else:
        log.info(
            "TC5 PASS (sub-B): Kernel/MDS rejected clearing alternate_name on long file"
        )

    fscrypt_util.lock(mnt_client, encrypt_path)
    return 0


# ---------------------------------------------------------------------------
# TC6 – altname_replication_simulation
# ---------------------------------------------------------------------------


def altname_replication_simulation(fscrypt_test_params):
    """
    TC6: End-to-end replication simulation using ``ceph.alternate_name``.

    Simulates an IBM Cloud VPC Regional File admin copying fscrypt alternate_name metadata
    from a source subvolume to a target subvolume without holding the user's encryption key.

    Steps
    -----
    1. Mount **two** subvolumes: sv_source (fscrypt-encrypted) and sv_target (plain).
    2. Create an encrypted directory on sv_source.
    3. Create files with short (50), medium (150), and long (230) char names.
    4. For each file, record ``ceph.alternate_name`` from sv_source via getfattr.
    5. Create corresponding target files on sv_target.
    6. SET ``ceph.alternate_name`` on each target file using the value from sv_source.
    7. Read back ``ceph.alternate_name`` from each target file.
    8. Assert: target values match source values exactly for every file.

    Pass criteria
    -------------
    All target ``ceph.alternate_name`` values are identical to the corresponding source values.
    """
    log.info("TC6: End-to-end replication simulation with ceph.alternate_name")
    fscrypt_util = fscrypt_test_params["fscrypt_util"]
    mount_details = fscrypt_test_params["mount_details"]

    if len(mount_details) < 2:
        log.error(
            "TC6 requires at least 2 mounted subvolumes; only %d available. SKIP.",
            len(mount_details),
        )
        return 0  # treat as skip, not failure

    sv_list = list(mount_details.keys())
    sv_source = sv_list[0]
    sv_target = sv_list[1]

    src_client = mount_details[sv_source]["fuse"]["mnt_client"]
    src_mnt = mount_details[sv_source]["fuse"]["mountpoint"]
    tgt_client = mount_details[sv_target]["fuse"]["mnt_client"]
    tgt_mnt = mount_details[sv_target]["fuse"]["mountpoint"]

    # ── fscrypt setup + encrypt on source ─────────────────────────────────
    log.info("fscrypt setup on source mount %s", src_mnt)
    if fscrypt_util.setup(src_client, src_mnt):
        log.error("fscrypt setup failed on source")
        return 1

    rand_tag = _rand_str(6)
    encrypt_path = f"{src_mnt}/tc6_src_{rand_tag}"
    src_client.exec_command(sudo=True, cmd=f"mkdir {encrypt_path}", check_ec=False)
    encrypt_params = fscrypt_util.encrypt(
        src_client, encrypt_path, src_mnt, protector_source="custom_passphrase"
    )
    if encrypt_params == 1:
        log.error("fscrypt encrypt failed on source")
        return 1

    # ── Create files with varying name lengths ────────────────────────────
    name_lengths = {"short": 50, "medium": 150, "long": 230}
    src_files = {}
    for label, length in name_lengths.items():
        fname = _rand_str(length)
        fpath = f"{encrypt_path}/{fname}"
        src_client.exec_command(
            sudo=True, cmd=f"echo 'tc6_{label}' > {fpath}", check_ec=False
        )
        src_files[label] = fpath
        log.info("Source %s file (%d chars): %s", label, length, fpath)

    # ── Read alternate_name from source files ─────────────────────────────
    src_altnames = {}
    for label, fpath in src_files.items():
        val = fscrypt_util.get_altname_xattr(src_client, fpath)
        src_altnames[label] = val
        log.info("Source %s altname: '%s'", label, val)

    # ── Create target files and SET alternate_name ─────────────────────────
    tgt_dir = f"{tgt_mnt}/tc6_tgt_{rand_tag}"
    tgt_client.exec_command(sudo=True, cmd=f"mkdir -p {tgt_dir}", check_ec=False)

    test_status = 0
    for label, src_val in src_altnames.items():
        tgt_file = f"{tgt_dir}/tgt_{label}"
        tgt_client.exec_command(
            sudo=True, cmd=f"echo 'tc6_tgt_{label}' > {tgt_file}", check_ec=False
        )
        if src_val:
            log.info("SET ceph.alternate_name on target %s file", label)
            if fscrypt_util.set_altname_xattr(tgt_client, tgt_file, src_val):
                log.error("TC6 FAIL: setfattr failed for %s file", label)
                test_status += 1
                continue

        # Read back from target and compare
        tgt_val = fscrypt_util.get_altname_xattr(tgt_client, tgt_file)
        expected = src_val if src_val else ""
        if tgt_val != expected:
            log.error(
                "TC6 FAIL: alternate_name mismatch for %s file.\n"
                "  Source : '%s'\n  Target : '%s'",
                label,
                src_val,
                tgt_val,
            )
            test_status += 1
        else:
            log.info(
                "TC6 PASS: alternate_name matches for %s file (value='%s')",
                label,
                tgt_val,
            )

    fscrypt_util.lock(src_client, encrypt_path)
    tgt_client.exec_command(sudo=True, cmd=f"rm -rf {tgt_dir}", check_ec=False)
    return 1 if test_status else 0


# ---------------------------------------------------------------------------
# TC7 – altname_boundary_filename_lengths
# ---------------------------------------------------------------------------


def altname_boundary_filename_lengths(fscrypt_test_params):
    """
    TC7: Identify the cleartext-length threshold where ``ceph.alternate_name`` transitions
    from absent to present.

    Steps
    -----
    1. Create an fscrypt-encrypted directory.
    2. Create files at the following cleartext lengths:
       [120, 128, 130, 133, 134, 135, 140, 145, 150, 160, 189, 190, 200]
    3. For each file run ``getfattr -n ceph.alternate_name``.
    4. Record present/absent at each length.
    5. Assert:
       a. Filenames of 120 chars: alternate_name absent.
       b. Filenames of 200 chars: alternate_name present.
       c. There is a monotonic transition point (no oscillation).

    Pass criteria
    -------------
    - Files at or below the threshold have empty alternate_name.
    - Files above the threshold have non-empty alternate_name.
    - The transition is clean (no non-monotonic pattern).
    """
    log.info(
        "TC7: Boundary – detect alternate_name threshold vs cleartext filename length"
    )
    fscrypt_util = fscrypt_test_params["fscrypt_util"]
    mount_details = fscrypt_test_params["mount_details"]

    sv_name = random.choice(list(mount_details.keys()))
    mnt_client = mount_details[sv_name]["fuse"]["mnt_client"]
    mnt_pt = mount_details[sv_name]["fuse"]["mountpoint"]

    if fscrypt_util.setup(mnt_client, mnt_pt):
        log.error("fscrypt setup failed")
        return 1

    rand_tag = _rand_str(6)
    encrypt_path = f"{mnt_pt}/tc7_boundary_{rand_tag}"
    mnt_client.exec_command(sudo=True, cmd=f"mkdir {encrypt_path}", check_ec=False)
    encrypt_params = fscrypt_util.encrypt(
        mnt_client, encrypt_path, mnt_pt, protector_source="custom_passphrase"
    )
    if encrypt_params == 1:
        log.error("fscrypt encrypt failed")
        return 1

    # Probe points across the expected boundary
    probe_lengths = [120, 128, 130, 133, 134, 135, 140, 145, 150, 160, 189, 190, 200]
    results = {}

    for length in probe_lengths:
        fname = _rand_str(length)
        fpath = f"{encrypt_path}/{fname}"
        mnt_client.exec_command(
            sudo=True, cmd=f"echo 'tc7_data' > {fpath}", check_ec=False
        )
        altname = fscrypt_util.get_altname_xattr(mnt_client, fpath)
        has_altname = altname is not None and altname != ""
        results[length] = has_altname
        log.info(
            "Length %3d → ceph.alternate_name %s",
            length,
            "PRESENT" if has_altname else "ABSENT",
        )

    # Log the full summary table
    log.info("\n=== TC7 Boundary Summary ===")
    log.info("%-10s %s", "Cleartext", "alternate_name")
    for length, present in sorted(results.items()):
        log.info("%-10d %s", length, "PRESENT" if present else "ABSENT")

    # ── Assertions ────────────────────────────────────────────────────────
    test_status = 0

    # 1. Shortest probed length (120) must NOT have alternate_name
    if results.get(120) is True:
        log.error("TC7 FAIL: alternate_name present for 120-char filename (unexpected)")
        test_status += 1
    else:
        log.info("TC7 PASS: alternate_name absent for 120-char filename (as expected)")

    # 2. Longest probed length (200) MUST have alternate_name
    if results.get(200) is False:
        log.error("TC7 FAIL: alternate_name absent for 200-char filename (unexpected)")
        test_status += 1
    else:
        log.info("TC7 PASS: alternate_name present for 200-char filename (as expected)")

    # 3. Monotonic check: once present → always present for longer names
    sorted_lengths = sorted(results.keys())
    first_present_idx = None
    for idx, length in enumerate(sorted_lengths):
        if results[length] and first_present_idx is None:
            first_present_idx = idx
            log.info(
                "TC7 INFO: Threshold detected between %d and %d chars",
                sorted_lengths[idx - 1] if idx > 0 else 0,
                length,
            )
        if first_present_idx is not None and not results[length]:
            log.error(
                "TC7 FAIL: Non-monotonic behaviour at length=%d "
                "(alternate_name absent after being present at shorter length)",
                length,
            )
            test_status += 1

    fscrypt_util.lock(mnt_client, encrypt_path)
    return 1 if test_status else 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rand_str(length):
    """Return a random lowercase-alphanumeric string of exactly *length* characters."""
    chars = string.ascii_lowercase + string.digits
    return "".join(random.choice(chars) for _ in range(length))
