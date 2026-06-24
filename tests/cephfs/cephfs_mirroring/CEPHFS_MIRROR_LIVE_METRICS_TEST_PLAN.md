# CephFS Mirror Live Metrics — Detailed Executable Test Plan

**Scope:** PR #68018 (Asok live telemetry), PR #68827 (OMAP persistence + MGR `ceph fs snapshot mirror status`), PR #69074 (draft: `cephfs_mirror_directory` perf counters + Prometheus).

**Audience:** CephFS QA / cephci automation engineers.

**Baseline cephci references:**
| Artifact | Path |
|----------|------|
| Utilities | `tests/cephfs/cephfs_mirroring/cephfs_mirroring_utils.py` — `CephfsMirroringUtils` |
| Existing metrics | `test_cephfs_mirroring_metrics.py` |
| Asok snap status | `test_cephfs_mirroring_snap_status_using_asok.py` |
| HA metrics | `test_cephfs_mirroring_ha_metrics.py` |
| Suite (single mirror) | `suites/tentacle/cephfs/tier-2_cephfs_mirror_metrics.yaml` |
| Suite (HA) | `suites/tentacle/cephfs/tier-2_cephfs_mirror_ha_metrics.yaml` |

---

## Global Prerequisites & Conventions

### Cluster topology (default)
| Role | ceph1 (source) | ceph2 (target) |
|------|----------------|----------------|
| MON | 3 | 3 |
| MGR | 2 | 2 |
| MDS | 2 | 2 |
| OSD | 3+ | 3+ |
| cephfs-mirror | 1 (suites A–G) or 2 HA (suites F, H) | — |
| Client | 1+ | 1+ |

### Standard mirroring bootstrap
```bash
# On source (after deploy_cephfs_mirroring in cephci):
ceph mgr module enable mirroring
ceph fs snapshot mirror enable cephfs
ceph fs snapshot mirror peer_bootstrap create cephfs remote_site <target_mon_ips> cephfs
# import token on target, then source:
ceph fs snapshot mirror peer_bootstrap import cephfs <token>
ceph fs snapshot mirror add cephfs <dir_root_path>
```

### Identity helpers (cepci)
```python
fsid = fs_mirroring_utils.get_fsid(cephfs_mirror_node[0])
daemon_names = fs_mirroring_utils.get_daemon_name(source_clients[0])
asok_files = fs_mirroring_utils.get_asok_file(cephfs_mirror_node, fsid, daemon_names)
filesystem_id = fs_mirroring_utils.get_filesystem_id_by_name(source_clients[0], "cephfs")
peer_uuid = fs_mirroring_utils.get_peer_uuid_by_name(source_clients[0], "cephfs")
```

### Three metric interfaces
| Interface | Command / API | Owner |
|-----------|---------------|-------|
| **Asok** | `ceph --admin-daemon <asok> fs mirror peer status cephfs@<fs_id> <peer_uuid> -f json` | cephfs-mirror daemon (live) |
| **MGR** | `ceph fs snapshot mirror status cephfs -f json` | mirroring MGR module (OMAP-backed, cached) |
| **Perf** | `ceph --admin-daemon <asok> perf dump` or `counter dump -f json` | labeled `cephfs_mirror_directory` group |

### Polling defaults
| Scenario | Interval | Timeout |
|----------|----------|---------|
| Snap sync complete | 10s | 3600s (large datasets: 14400s) |
| Metric field monotonicity | 5s | 600s |
| Stale detection | 2× persist interval | 120s |
| HA failover recovery | 5s | 300s |

### Data setup templates
| Profile | Files | Size | Snap naming | Purpose |
|---------|-------|------|-------------|---------|
| **SMALL** | 100 | 4 KiB each | `snap_<dir>_NN` | Fast functional / tri-interface |
| **MEDIUM** | 500 | 1 MiB | `snap_<dir>_NN` | Progress % / throughput |
| **LARGE** | 50 | 100 MiB | `snap_<dir>_NN` | ETA, crawl duration |
| **MANY-SNAP** | 10 files × 1 MiB | — | `snap_seq_001`…`050` | snaps_synced counters |
| **EMPTY** | 0 | — | `snap_empty_01` | delta vs full, zero totals |

Use `create_files_for_snapdiff(client, dir_path, num_files, size)` or `generate_small_files_random_sizes.py` for variable sizes.

### Basis points (PR #69074)
`sync_percent_bps`: value `1745` ⇒ 17.45%. Assert: `abs(asok_percent * 100 - bps) <= 1` (1 bps tolerance).

### dir_state encoding
| Value | Meaning |
|-------|---------|
| 0 | idle |
| 1 | syncing |
| 2 | failed |

When `dir_state` is 0 or 2, all `current_*` gauges must be 0.

---

## Suite 1 — Asok JSON Schema & Breaking Change (PR #68018)

**Suggested suite:** `suites/tentacle/cephfs/tier-2_cephfs_mirror_live_metrics_asok.yaml`  
**Tier:** 2  
**Conf:** extend `tier-2_cephfs_mirror_metrics.yaml` cluster stanza (1× cephfs-mirror).

---

### TC-ASOK-001 — Nested peer_status schema by dir_root
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P0 |
| **Objective** | Confirm `fs mirror peer status` returns top-level keys = dir_root paths (not flat pre-68018 layout). |
| **Prerequisites** | Mirroring enabled; 2 dir_roots: `/volumes/subvolgroup_1/subvol_1` and `.../subvol_2` via `setup_subvolumes_and_mounts` + `add_path_for_mirroring`. |
| **Steps** | 1. Deploy mirroring (`deploy_cephfs_mirroring`). 2. Add both paths. 3. `peer_status = get_fs_mirror_peer_status_using_asok(...)`. 4. Compare keys to `ceph fs snapshot mirror ls cephfs`. |
| **Validation** | `set(peer_status.keys()) == set(mirrored_paths)`; each value has keys: `state`, `current_syncing_snap`, `last_synced_snap`, `snaps_synced`, `snaps_deleted`, `snaps_renamed`. No peer UUID as top-level key. |
| **Cleanup** | `remove_path_from_mirroring` both paths. |
| **Automation** | Extend `get_fs_mirror_peer_status_using_asok`; add `assert_peer_status_schema(peer_status, paths)`. New: `test_cephfs_mirror_live_metrics_asok_schema.py`. |
| **Risk** | Breaks dashboards/scripts parsing old JSON — production monitoring outage. |

---

### TC-ASOK-002 — current_syncing_snap required fields during active sync
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P0 |
| **Objective** | Validate full telemetry object under `current_syncing_snap` while `state == "syncing"`. |
| **Prerequisites** | MEDIUM profile on dir `/d1`; snap `snap_d1_01` created; sync in progress. |
| **Steps** | 1. Create snap: `mkdir <mnt>/.snap/snap_d1_01` or `ceph fs subvolume snapshot create`. 2. Poll asok every 10s until `peer_status["/d1"]["state"] == "syncing"`. 3. Capture JSON once. |
| **Validation** | `current_syncing_snap` contains: `id`, `name`, `sync-mode` ∈ {`full`,`delta`}, `avg_read_throughput_bytes`, `avg_write_throughput_bytes`, `crawl` (`state`, `duration`), `datasync_queue_wait` (`state`, `duration`), `bytes` (`sync_bytes`, `total_bytes`, `sync_percent`), `files` (`sync_files`, `total_files`, `sync_percent`), `eta`. Duration strings match regex `^\d+[smh](\s|$)|calculating\.\.\.`. |
| **Cleanup** | Wait idle; remove path. |
| **Automation** | `validate_current_syncing_snap_fields(snap_obj)` helper. |
| **Risk** | Missing fields → blind ops during long syncs. |

---

### TC-ASOK-003 — Idle state after sync completion
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P0 |
| **Objective** | After sync, `state` is idle and `current_syncing_snap` absent or empty per implementation. |
| **Prerequisites** | SMALL profile; one snap synced (verify target `.snap`). |
| **Steps** | 1. Sync snap. 2. Poll until `snaps_synced` increments. 3. Poll until `state != "syncing"` (timeout 3600s). |
| **Validation** | `state` in (`idle`, `stopped` — match product string); `last_synced_snap.id` == synced snap id; `last_synced_snap.name` == `snap_d1_01`; `snaps_synced >= 1`. |
| **Cleanup** | Standard. |
| **Automation** | Reuse `validate_snaps_status_increment`. |
| **Risk** | Stuck "syncing" blocks next snap scheduling. |

---

### TC-ASOK-004 — sync-mode full vs delta
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P1 |
| **Objective** | First snap on dir reports `full`; subsequent delta snaps report `delta`. |
| **Prerequisites** | EMPTY dir; snaps `snap_d1_01`, `snap_d1_02`. |
| **Steps** | 1. Create `snap_d1_01`; capture `sync-mode` during sync. 2. After idle, add 10 files; create `snap_d1_02`; capture during sync. |
| **Validation** | First: `sync-mode == "full"`. Second: `sync-mode == "delta"`. |
| **Cleanup** | Remove dir from mirror. |
| **Automation** | Poll helper with mode extraction. |
| **Risk** | Wrong mode → incorrect capacity planning. |

---

### TC-ASOK-005 — bytes/files progress monotonicity
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P0 |
| **Objective** | `sync_percent` and `sync_files` never decrease during a single snap sync. |
| **Prerequisites** | LARGE profile; active sync. |
| **Steps** | 1. Start sync. 2. Every 5s for up to 600s, sample `bytes.sync_percent`, `files.sync_files` (parse % float). 3. Build time series. |
| **Validation** | Monotonic non-decreasing series; final `sync_percent` → 100% (±0.5%); `sync_files == total_files` at end. |
| **Cleanup** | Wait completion. |
| **Automation** | `poll_monotonic_progress(asok_fn, path, timeout=600)`. |
| **Risk** | Regressing progress breaks ETA and operator trust. |

---

### TC-ASOK-006 — Throughput strings non-negative semantics
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P1 |
| **Objective** | Read/write throughput fields present and parseable during IO-heavy sync. |
| **Prerequisites** | MEDIUM profile + `fio` sequential write before snap. |
| **Steps** | 1. Run `fio --rw=write --bs=1M --size=500M` on mount. 2. Snapshot. 3. During sync, sample 10 readings 5s apart. |
| **Validation** | Fields exist; parsed numeric rate ≥ 0; at least one sample with write throughput > 0 B/s during active datasync phase. |
| **Cleanup** | Stop fio; umount if needed. |
| **Automation** | Parse `X.XX KiB/s` style strings to bytes. |
| **Risk** | Zero throughput while syncing indicates broken instrumentation. |

---

### TC-ASOK-007 — crawl state transitions
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P1 |
| **Objective** | `crawl.state` transitions and `duration` increases while in-progress. |
| **Prerequisites** | MANY-SNAP dir with nested subdirs (create 20 dirs × 10 files). |
| **Steps** | 1. Snap after tree creation. 2. Poll `crawl.state` every 5s. |
| **Validation** | Observed states include `in-progress` then terminal (`complete`/`done` — match product); `duration` string length increases while in-progress. |
| **Cleanup** | Standard. |
| **Automation** | State machine assertion helper. |
| **Risk** | Infinite crawl → hang misdiagnosed as network issue. |

---

### TC-ASOK-008 — datasync_queue_wait during backpressure
| Field | Value |
|-------|-------|
| **Category** | Edge-Corner |
| **Priority** | P1 |
| **Objective** | Queue wait metrics reflect waiting state under multithreaded load. |
| **Prerequisites** | Pattern from `test_cephfs_mirror_multithreaded_empty_dataq_hang.py`: 8+ dir_roots, parallel snap creation. |
| **Steps** | 1. Add 8 paths with SMALL data. 2. Create snaps on all within 60s. 3. Poll asok on busiest path. |
| **Validation** | `datasync_queue_wait.state == "waiting"` observed at least once; `duration` present. |
| **Cleanup** | Remove all mirror paths. |
| **Automation** | Reuse multithreaded test harness; extend with queue wait asserts. |
| **Risk** | **Blocker:** empty dataq hang — queue wait is primary diagnostic. |

---

### TC-ASOK-009 — ETA transitions from calculating to estimate
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P1 |
| **Objective** | `eta` begins as `calculating...` then becomes time estimate. |
| **Prerequisites** | LARGE profile sync. |
| **Steps** | 1. Trigger sync. 2. Poll `eta` every 10s for 300s. |
| **Validation** | First reading contains `calculating`; later readings match time pattern (e.g. `Nm`, `Ns`, `Nh`) OR remain calculating only if sync < min sample window (document skip). |
| **Cleanup** | Standard. |
| **Automation** | Regex validator for ETA. |
| **Risk** | Bad ETA → wrong maintenance windows. |

---

### TC-ASOK-010 — snaps_synced / deleted / renamed counters
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P0 |
| **Objective** | Directory-level snap lifecycle counters increment correctly. |
| **Prerequisites** | Kernel-mounted subvol; 2 snaps synced. |
| **Steps** | 1. Baseline asok. 2. Create + sync `snap_a`. 3. Rename `snap_a` → `snap_a_renamed` on source. 4. Delete `snap_b` on source after sync. 5. Poll after each op. |
| **Validation** | `snaps_synced` increases after sync; `snaps_renamed` increases after rename; `snaps_deleted` increases after delete (per path). |
| **Cleanup** | `test_cephfs_mirroring_snap_status_using_asok.py` pattern cleanup. |
| **Automation** | Extend `validate_snaps_status_increment` for renamed/deleted keys. |
| **Risk** | Drift between actual snap state and metrics. |

---

### TC-ASOK-011 — Invalid peer UUID negative test
| Field | Value |
|-------|-------|
| **Category** | Negative |
| **Priority** | P2 |
| **Objective** | Admin socket rejects bad peer UUID gracefully. |
| **Prerequisites** | Valid asok, fs_id. |
| **Steps** | `ceph --admin-daemon <asok> fs mirror peer status cephfs@<fs_id> 00000000-0000-0000-0000-000000000000 -f json` |
| **Validation** | Non-zero exit or JSON error field; no daemon crash (`systemctl` / `ceph orch ps` still running). |
| **Cleanup** | None. |
| **Automation** | Expect `CommandFailed`. |
| **Risk** | Crash on bad input → DoS vector. |

---

### TC-ASOK-012 — Path not mirrored returns empty or error
| Field | Value |
|-------|-------|
| **Category** | Negative |
| **Priority** | P2 |
| **Objective** | Unmirrored dir_root absent from peer_status keys. |
| **Prerequisites** | One mirrored path only. |
| **Steps** | 1. Get peer_status. 2. Confirm non-mirror subvol path not in keys. |
| **Validation** | `"/nonexistent/path" not in peer_status`. |
| **Cleanup** | N/A |
| **Automation** | Simple membership assert. |
| **Risk** | False positives in monitoring. |

---

## Suite 2 — MGR CLI & OMAP Persistence (PR #68827)

**Suggested suite:** `suites/tentacle/cephfs/tier-2_cephfs_mirror_live_metrics_mgr.yaml`  
**Tier:** 2

---

### TC-MGR-001 — `ceph fs snapshot mirror status` basic schema
| Field | Value |
|-------|-------|
| **Category** | Acceptance |
| **Priority** | P0 |
| **Objective** | New MGR command returns per-dir status aligned with asok semantics. |
| **Prerequisites** | Mirroring + 1 dir SMALL; sync complete for one snap. |
| **Steps** | `ceph fs snapshot mirror status cephfs -f json` on source client. |
| **Validation** | JSON contains filesystem `cephfs`, peer entries, dir_roots with `state`, snap fields analogous to asok; parse succeeds. |
| **Cleanup** | Standard. |
| **Automation** | **New** `get_fs_snapshot_mirror_status(client, fs_name)` in utils. |
| **Risk** | Primary operator-facing API for RHCS — schema break is release blocker. |

---

### TC-MGR-002 — Default persist interval 5s
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P0 |
| **Objective** | Confirm default `cephfs_mirror_live_metrics_persist_interval` is 5 seconds. |
| **Prerequisites** | Fresh cluster post-68827. |
| **Steps** | `ceph config get client.cephfs-mirror cephfs_mirror_live_metrics_persist_interval` |
| **Validation** | Value `5` (or `5s` per ceph convention — match build). |
| **Cleanup** | None. |
| **Automation** | One-liner in suite setup. |
| **Risk** | Wrong default → excessive OMAP writes or stale MGR view. |

---

### TC-MGR-003 — OMAP object presence on cephfs_mirror
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P1 |
| **Objective** | Mirror daemon persists stats to OMAP on `cephfs_mirror` object. |
| **Prerequisites** | Active sync; persist interval default. |
| **Steps** | 1. `rados -p <metadata_pool> listomapkeys cephfs_mirror` (or `getomapheader`). 2. During sync, re-run after 10s. |
| **Validation** | OMAP keys exist/change while syncing; keys reference dir/peer metrics (exact key names per implementation). |
| **Cleanup** | None. |
| **Automation** | Shell via `source_clients.exec_command`; pool from `ceph fs dump`. |
| **Risk** | No persistence → MGR always empty after daemon restart. |

---

### TC-MGR-004 — MGR status updates within 2× persist interval
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P0 |
| **Objective** | MGR-visible `sync_percent` advances within bounded time after asok shows progress. |
| **Prerequisites** | LARGE sync in progress; interval=5s. |
| **Steps** | 1. Read asok `bytes.sync_percent` → P1. 2. Within 15s, read MGR status same dir → P2. 3. Repeat 5 times. |
| **Validation** | `P2 >= P1` (parsed floats); lag ≤ 15s between asok increase and MGR increase. |
| **Cleanup** | Wait idle. |
| **Automation** | Dual-read poll loop. |
| **Risk** | Stale MGR → wrong automation decisions. |

---

### TC-MGR-005 — Custom persist interval 30s
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P1 |
| **Objective** | Tunable interval slows OMAP/MGR refresh predictably. |
| **Prerequisites** | Mirror daemon restart acceptable. |
| **Steps** | 1. `ceph config set client.cephfs-mirror cephfs_mirror_live_metrics_persist_interval 30`. 2. `ceph orch restart cephfs-mirror`. 3. Start LARGE sync. 4. Sample MGR every 5s for 90s. |
| **Validation** | MGR percent changes at most once per ~30s (allow ±5s jitter). |
| **Cleanup** | Reset config to 5; restart daemon. |
| **Automation** | Timestamp delta analysis. |
| **Risk** | Mis-tuned interval in production. |

---

### TC-MGR-006 — Default stats for newly added dir_root
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P0 |
| **Objective** | Immediately after `snapshot mirror add`, MGR shows default/idle stats (not missing, not garbage). |
| **Prerequisites** | One existing mirrored dir idle. |
| **Steps** | 1. `ceph fs snapshot mirror add cephfs <new_path>`. 2. Within 10s: `ceph fs snapshot mirror status cephfs -f json`. |
| **Validation** | `<new_path>` present; `state` idle-like; counters zeroed; no stale data from other dirs. |
| **Cleanup** | `remove_path_from_mirroring`. |
| **Automation** | Compare to asok idle template. |
| **Risk** | **Blocker:** missing new dir in UI after add. |

---

### TC-MGR-007 — Removed dir_root disappears from MGR status
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P0 |
| **Objective** | `snapshot mirror remove` purges dir from MGR output. |
| **Prerequisites** | Two mirrored dirs. |
| **Steps** | 1. Remove dir B. 2. `ceph fs snapshot mirror status cephfs -f json`. 3. Check OMAP keys if exposed. |
| **Validation** | Dir B absent; dir A unchanged. |
| **Cleanup** | Remove dir A. |
| **Automation** | `remove_path_from_mirroring` + assert. |
| **Risk** | Ghost metrics → false alerts. |

---

### TC-MGR-008 — Old persisted dir stats mechanism removed
| Field | Value |
|-------|-------|
| **Category** | Upgrade |
| **Priority** | P1 |
| **Objective** | Post-upgrade, legacy OMAP keys/API no longer updated (68827 removal). |
| **Prerequisites** | Cluster upgraded from pre-68827 with known legacy keys (document baseline). |
| **Steps** | 1. Record legacy omap keys pre-upgrade. 2. Upgrade with ongoing sync. 3. Post-upgrade: sync new snap; compare key set. |
| **Validation** | Legacy keys frozen/removed; new `cephfs_mirror` layout keys active. MGR status works. |
| **Cleanup** | N/A |
| **Automation** | Manual baseline + automated post-check; suite in upgrade pipeline. |
| **Risk** | Dual-write or orphan keys confuse monitoring. |

---

### TC-MGR-009 — MGR module disabled → command fails cleanly
| Field | Value |
|-------|-------|
| **Category** | Negative |
| **Priority** | P2 |
| **Objective** | Without mirroring module, status command errors clearly. |
| **Prerequisites** | `disable_mirroring_module`. |
| **Steps** | `ceph fs snapshot mirror status cephfs` |
| **Validation** | Error message mentions module/state; no MGR crash. |
| **Cleanup** | `enable_mirroring_module`. |
| **Automation** | `disable_mirroring_module` / enable pair. |
| **Risk** | MGR assert on missing module. |

---

### TC-MGR-010 — Multi-filesystem status isolation
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P1 |
| **Objective** | Status for `cephfs` does not include dirs from `cephfs2`. |
| **Prerequisites** | Two FS with mirror enabled (see `test_cephfs_mirroring_validate_cephfs-mirroring_on_multifs_setup.py`). |
| **Steps** | 1. Add path on FS2 only. 2. `ceph fs snapshot mirror status cephfs` and `... cephfs2`. |
| **Validation** | Each command lists only its FS dirs. |
| **Cleanup** | Disable mirror on FS2. |
| **Automation** | Multifs test extension. |
| **Risk** | Cross-FS metric leakage. |

---

## Suite 3 — Tri-Interface Consistency

**Suggested suite:** `suites/tentacle/cephfs/tier-2_cephfs_mirror_live_metrics_tri_interface.yaml`  
**Tier:** 2 (add `--devops-pipeline` nightly)

---

### TC-TRI-001 — Idle tri-interface snapshot
| Field | Value |
|-------|-------|
| **Category** | Acceptance |
| **Priority** | P0 |
| **Objective** | Idle dir: asok, MGR, perf counters agree on state and zeroed progress. |
| **Prerequisites** | Synced SMALL; idle 60s. |
| **Steps** | 1. Read asok path entry. 2. `ceph fs snapshot mirror status cephfs`. 3. `counter dump` → `cephfs_mirror_directory` label match. |
| **Validation** | `state`/dir_state idle equivalent; perf `dir_state==0`; all `current_*` perf gauges == 0; MGR percents 0. |
| **Cleanup** | Standard. |
| **Automation** | **New** `assert_tri_interface_consistency(path, peer_uuid, ...)`. Module: `test_cephfs_mirror_live_metrics_tri_interface.py`. |
| **Risk** | **Blocker:** Prometheus shows syncing while CLI shows idle. |

---

### TC-TRI-002 — Active sync progress alignment
| Field | Value |
|-------|-------|
| **Category** | Acceptance |
| **Priority** | P0 |
| **Objective** | During sync, bytes/files % within tolerance across three interfaces. |
| **Prerequisites** | MEDIUM active sync. |
| **Steps** | 1. Poll every 10s (max 60 samples). 2. Parse asok %, MGR %, perf bps (`current_sync_bytes_percent_bps` etc.). |
| **Validation** | `abs(asok_pct - mgr_pct) <= 2.0`; `abs(asok_pct - bps/100) <= 2.0`; `dir_state==1`. |
| **Cleanup** | Wait complete. |
| **Automation** | Tri-interface poll loop. |
| **Risk** | Autoscaling on wrong signal. |

---

### TC-TRI-003 — last_synced_snap id/name match
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P0 |
| **Objective** | `last_synced_snap` consistent across asok and MGR; perf `last_synced_snap_id` matches. |
| **Prerequisites** | Completed sync; snap id from `ceph fs subvolume snapshot ls` or inode. |
| **Steps** | 1. Get snap id from FS. 2. Read three interfaces. |
| **Validation** | id and name equal across sources. |
| **Cleanup** | N/A |
| **Automation** | Field map dict per interface. |
| **Risk** | Wrong snap reported as last synced. |

---

### TC-TRI-004 — current_syncing_snap id during active sync
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P0 |
| **Objective** | Active snap id/name match asok ↔ MGR ↔ perf `current_syncing_snap_*`. |
| **Prerequisites** | Active sync. |
| **Steps** | Single-point read all three during `state==syncing`. |
| **Validation** | ids equal; names equal; perf dir_state==1. |
| **Cleanup** | N/A |
| **Automation** | Part of TRI-002 harness. |
| **Risk** | Mis-identified snap under load. |

---

### TC-TRI-005 — snaps_synced counter alignment
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P1 |
| **Objective** | Summary counter `snaps_synced` matches after 3 snaps. |
| **Prerequisites** | MANY-SNAP sequential sync. |
| **Steps** | After each sync completes, record three interfaces. |
| **Validation** | `asok.snaps_synced == mgr.snaps_synced == perf.snaps_synced` (±0). |
| **Cleanup** | Standard. |
| **Automation** | Loop per snap. |
| **Risk** | Summary drift breaks SLA reporting. |

---

### TC-TRI-006 — sync-mode full/delta consistency
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P1 |
| **Objective** | `sync-mode` in asok matches perf enum/label during sync. |
| **Prerequisites** | TC-ASOK-004 setup. |
| **Steps** | Capture during first and second snap sync. |
| **Validation** | full/delta strings ↔ perf counter value map (document enum). |
| **Cleanup** | N/A |
| **Automation** | Map table in utils. |
| **Risk** | Mode mismatch in Grafana. |

---

### TC-TRI-007 — Throughput cross-check order of magnitude
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P2 |
| **Objective** | Asok throughput strings same order of magnitude as perf byte counters (if exposed). |
| **Prerequisites** | IO-heavy sync. |
| **Steps** | Parse asok `avg_write_throughput_bytes`; read perf `current_avg_write_throughput_bytes` (or equivalent). |
| **Validation** | Ratio between 0.5 and 2.0 (allow implementation smoothing). |
| **Cleanup** | N/A |
| **Automation** | Unit parser helper. |
| **Risk** | Misleading throughput dashboards. |

---

### TC-TRI-008 — Failed state tri-interface (injected)
| Field | Value |
|-------|-------|
| **Category** | Negative |
| **Priority** | P0 |
| **Objective** | On sync failure, all interfaces show failed and perf zeros current_* with dir_state=2. |
| **Prerequisites** | `inject_sync_failure` from utils (target snap collision). |
| **Steps** | 1. Inject failure. 2. Poll asok/MGR/perf. |
| **Validation** | `state==failed` (or equivalent); `dir_state==2`; `current_*==0`; snaps_synced not falsely incrementing. |
| **Cleanup** | Remove bogus target snap; resume sync. |
| **Automation** | Extend `test_cephfs_mirroring_metrics.py` failure section. |
| **Risk** | **Blocker:** silent failure with idle metrics. |

---

## Suite 4 — Stale Metric Detection (PR #68827)

**Suggested suite:** `suites/tentacle/cephfs/tier-2_cephfs_mirror_live_metrics_stale.yaml`  
**Tier:** 2

---

### TC-STALE-001 — Mirror daemon stop → MGR marks stale
| Field | Value |
|-------|-------|
| **Category** | Blocker-Finding |
| **Priority** | P0 |
| **Objective** | When cephfs-mirror stops, MGR status shows stale/not-live (not last good values as current). |
| **Prerequisites** | Sync in progress at 50%+. |
| **Steps** | 1. Record MGR progress P. 2. `ceph orch stop cephfs-mirror` on source. 3. Wait 3× persist interval (15s default). 4. `ceph fs snapshot mirror status cephfs -f json`. |
| **Validation** | Stale indicator present (field name per 68827: e.g. `stale`, `live`, `last_update` age > threshold); progress must NOT advance. |
| **Cleanup** | `ceph orch start cephfs-mirror`; wait recovery. |
| **Automation** | New stale assert helper; reuse `wait_for_daemon_recovery`. |
| **Risk** | **Blocker:** ops believes sync active after daemon death. |

---

### TC-STALE-002 — Daemon restart mid-sync → stale then recovery
| Field | Value |
|-------|-------|
| **Category** | Blocker-Finding |
| **Priority** | P0 |
| **Objective** | Restart clears live asok but MGR transitions stale → fresh after daemon returns. |
| **Prerequisites** | LARGE sync active. |
| **Steps** | 1. `systemctl restart cephfs-mirror@...` or orch restart. 2. Poll MGR 5s × 24. 3. Poll asok after recovery. |
| **Validation** | Stale within 15s of restart; after recovery, stale cleared; sync resumes or restarts with consistent ids. |
| **Cleanup** | Wait idle. |
| **Automation** | HA metrics restart pattern. |
| **Risk** | Indefinite stale after recovery. |

---

### TC-STALE-003 — MGR module restart with running mirror
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P1 |
| **Objective** | `ceph mgr fail` or module restart does not lose OMAP-backed stats permanently. |
| **Prerequisites** | Idle synced dir. |
| **Steps** | 1. Record MGR status. 2. `ceph mgr module disable mirroring`; enable. 3. Re-read status within 30s. |
| **Validation** | Dir entries restored; not stale if daemon running; counters match pre-restart. |
| **Cleanup** | None. |
| **Automation** | disable/enable module helpers. |
| **Risk** | Empty status after MGR bounce. |

---

### TC-STALE-004 — Network partition to peer (stale vs failed)
| Field | Value |
|-------|-------|
| **Category** | Edge-Corner |
| **Priority** | P1 |
| **Objective** | Block target MON port; distinguish failed sync vs stale live metrics. |
| **Prerequisites** | `iptables -A OUTPUT -d <target_mon> -j DROP` on mirror node. |
| **Steps** | 1. Start snap sync. 2. Apply drop mid-sync. 3. Poll 120s. |
| **Validation** | Eventually `failed` or stale with last progress frozen; asok on mirror still updates local crawl until error. Document expected behavior per PR. |
| **Cleanup** | Flush iptables; heal peer. |
| **Automation** | Similar to `test_cephfs_mirror_disconnect.py`. |
| **Risk** | Mis-classified state prolongs outage. |

---

### TC-STALE-005 — Persist interval 1s rapid refresh
| Field | Value |
|-------|-------|
| **Category** | Edge-Corner |
| **Priority** | P2 |
| **Objective** | Minimum interval keeps MGR fresh without OMAP bloat errors. |
| **Prerequisites** | Set interval=1; restart mirror. |
| **Steps** | 1. Run MEDIUM sync. 2. Monitor `ceph -w` / cluster health. 3. `rados listomapkeys` count growth. |
| **Validation** | No health WARN from OMAP size; MGR updates ≤3s lag; no stale while daemon healthy. |
| **Cleanup** | Reset interval 5. |
| **Automation** | Config setter + health check. |
| **Risk** | OMAP exhaustion on large dir counts. |

---

### TC-STALE-006 — Stale cleared after sync completes post-recovery
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P1 |
| **Objective** | After TC-STALE-002, completing sync clears stale and aligns last_synced_snap. |
| **Prerequisites** | Follow STALE-002. |
| **Steps** | Wait sync complete; read MGR + asok. |
| **Validation** | `stale==false`; last_synced_snap updated; tri-interface idle match. |
| **Cleanup** | Standard. |
| **Automation** | Chained test. |
| **Risk** | Permanent stale flag. |

---

## Suite 5 — HA Failover & Metrics

**Suggested suite:** `suites/tentacle/cephfs/tier-2_cephfs_mirror_live_metrics_ha.yaml`  
**Tier:** 2 (reuse `tier-2_cephfs_mirror_ha_metrics.yaml` cluster: 2× cephfs-mirror)

---

### TC-HA-001 — Only leader exports live asok metrics
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P0 |
| **Objective** | Non-leader mirror daemon asok peer_status empty or stale; leader has full JSON. |
| **Prerequisites** | HA deploy (`test_cephfs_mirroring_configure_cephfs_mirroring_ha.py`); identify leader via `ceph fs snapshot mirror daemon status`. |
| **Steps** | 1. Query asok on both nodes. 2. Compare dir entries during sync. |
| **Validation** | Leader: populated syncing JSON. Follower: no active sync OR documented minimal status. |
| **Cleanup** | N/A |
| **Automation** | Extend HA metrics test with `get_fs_mirror_peer_status_using_asok` per node. |
| **Risk** | Scraping wrong node → false idle. |

---

### TC-HA-002 — Failover during active sync preserves MGR progress
| Field | Value |
|-------|-------|
| **Category** | Blocker-Finding |
| **Priority** | P0 |
| **Objective** | Kill leader mid-sync; new leader resumes; MGR/OMAP not reset to 0% incorrectly. |
| **Prerequisites** | LARGE sync ~30%; HA 2 nodes. |
| **Steps** | 1. Record MGR percent. 2. `ceph orch daemon stop` leader mirror. 3. Poll failover ≤300s. 4. Compare MGR percent monotonicity (no large backward jump). |
| **Validation** | Failover ≤300s; progress ≥ P-5% (allow small reset only if documented); sync completes. |
| **Cleanup** | Ensure both daemons up. |
| **Automation** | `test_cephfs_mirroring_ha_metrics.py` extension. |
| **Risk** | **Blocker:** progress reset breaks ETA and operator trust. |

---

### TC-HA-003 — MGR status stable across failover (stale window)
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P0 |
| **Objective** | Brief stale during failover; not permanent. |
| **Prerequisites** | TC-HA-002 setup. |
| **Steps** | During failover window, poll MGR every 5s for 60s. |
| **Validation** | Stale true for < 60s; then false with updated timestamps. |
| **Cleanup** | N/A |
| **Automation** | Stale poll helper. |
| **Risk** | Stuck stale after HA event. |

---

### TC-HA-004 — Perf counters on both daemons vs leader
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P1 |
| **Objective** | `cephfs_mirror_directory` counters only on leader (or follower zeroed). |
| **Prerequisites** | Active sync HA. |
| **Steps** | `counter dump` on node1 and node2; filter by dir label. |
| **Validation** | Exactly one node has `dir_state==1` for given (dir, peer); other `dir_state==0` and current_* zero. |
| **Cleanup** | N/A |
| **Automation** | `get_labels_and_counters` extended for `cephfs_mirror_directory`. |
| **Risk** | Duplicate Prometheus series from two nodes. |

---

### TC-HA-005 — Dual scrape Prometheus HA anti-affinity
| Field | Value |
|-------|-------|
| **Category** | Acceptance |
| **Priority** | P1 |
| **Objective** | Prometheus rules use `on()` leader label or scrape only leader service. |
| **Prerequisites** | Monitoring stack optional; perf exporter on both. |
| **Steps** | 1. Scrape both mirror pods. 2. Query `cephfs_mirror_directory_dir_state`. |
| **Validation** | One time series per (dir, peer) with syncing value; no duplicate conflicting states. |
| **Cleanup** | N/A |
| **Automation** | Document manual; optional curl prometheus. |
| **Risk** | Alert storms from duplicate metrics. |

---

### TC-HA-006 — Add dir during HA steady state
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P1 |
| **Objective** | New dir_root appears in MGR/asok on leader after add without full cluster restart. |
| **Prerequisites** | HA idle. |
| **Steps** | `add_path_for_mirroring` third path; poll 30s. |
| **Validation** | TC-MGR-006 + leader asok contains path. |
| **Cleanup** | Remove path. |
| **Automation** | HA metrics workflow. |
| **Risk** | Split-brain dir registry. |

---

## Suite 6 — Perf Counters & Prometheus (PR #69074)

**Suggested suite:** `suites/tentacle/cephfs/tier-2_cephfs_mirror_live_metrics_perf.yaml`  
**Tier:** 2 (gated on draft PR merge)

---

### TC-PERF-001 — `cephfs_mirror_directory` group exists
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P0 |
| **Objective** | counter dump includes labeled perf group. |
| **Prerequisites** | 69074 build; one mirrored dir. |
| **Steps** | `ceph --admin-daemon <asok> counter dump -f json \| jq '.cephfs_mirror_directory'` |
| **Validation** | Array length ≥ 1; required labels present. |
| **Cleanup** | N/A |
| **Automation** | Extend `get_cephfs_mirror_counters` or separate dump parser. |
| **Risk** | Missing group → no Prometheus alerts. |

---

### TC-PERF-002 — Label cardinality correctness
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P0 |
| **Objective** | Labels: source_fscid, source_filesystem, peer_uuid, peer_cluster_name, peer_cluster_filesystem, directory. |
| **Prerequisites** | Known peer_site `remote_site`, paths `/p1`. |
| **Steps** | Parse labels from dump for `/p1`. |
| **Validation** | `source_filesystem==cephfs`; `peer_cluster_name==remote_site`; `peer_uuid==<uuid>`; `directory` matches dir_root path; fscid matches `ceph fs dump`. |
| **Cleanup** | N/A |
| **Automation** | Label assert dict. |
| **Risk** | Wrong labels → mis-routed alerts. |

---

### TC-PERF-003 — One PerfCounters instance per (dir, peer)
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P0 |
| **Objective** | Two dirs × one peer ⇒ exactly 2 labeled counter sets. |
| **Prerequisites** | Two mirrored paths same peer. |
| **Steps** | counter dump; filter `peer_uuid`. |
| **Validation** | Count == 2; distinct `directory` label. |
| **Cleanup** | Remove one path → count 1. |
| **Automation** | Lifecycle test TC-PERF-010. |
| **Risk** | Cardinality explosion or missing dirs. |

---

### TC-PERF-004 — dir_state syncing during active sync
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P0 |
| **Objective** | `dir_state==1` iff syncing. |
| **Prerequisites** | Active MEDIUM sync. |
| **Steps** | Parallel poll asok `state` and perf `dir_state`. |
| **Validation** | When asok `syncing`, perf `dir_state==1`; when idle, `==0`. |
| **Cleanup** | N/A |
| **Automation** | TRI harness subset. |
| **Risk** | State desync. |

---

### TC-PERF-005 — current_* zeroed when idle
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P0 |
| **Objective** | All `current_*` gauges 0 at idle per 69074 spec. |
| **Prerequisites** | Idle post-sync 120s. |
| **Steps** | counter dump for dir. |
| **Validation** | `dir_state==0`; `current_sync_bytes==0`; `current_sync_files==0`; percent bps == 0. |
| **Cleanup** | N/A |
| **Automation** | Template assert list. |
| **Risk** | Stale non-zero gauges → false syncing alerts. |

---

### TC-PERF-006 — current_* zeroed when failed
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P0 |
| **Objective** | Failed state zeros current_* with dir_state=2. |
| **Prerequisites** | `inject_sync_failure`. |
| **Steps** | Poll perf after failure detected. |
| **Validation** | `dir_state==2`; all `current_*==0`. |
| **Cleanup** | Heal failure. |
| **Automation** | TRI-008 extension. |
| **Risk** | Grafana shows progress on failed dir. |

---

### TC-PERF-007 — Basis points encoding accuracy
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P0 |
| **Objective** | bps counters match asok percent strings. |
| **Prerequisites** | Sync at ~17–50% (MEDIUM). |
| **Steps** | 1. Parse asok `bytes.sync_percent` → F. 2. Read perf bps → B. |
| **Validation** | `abs(F*100 - B) <= 1` for bytes and files independently. |
| **Cleanup** | N/A |
| **Automation** | `percent_to_bps` helper. |
| **Risk** | Off-by-100× Prometheus alerts. |

---

### TC-PERF-008 — snaps_* summary counters monotonic
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P1 |
| **Objective** | `snaps_synced`, `snaps_deleted`, `snaps_renamed` summary types increment. |
| **Prerequisites** | TC-ASOK-010 operations. |
| **Steps** | After each op, read perf summary. |
| **Validation** | Non-decreasing; final matches asok integers. |
| **Cleanup** | N/A |
| **Automation** | Snap lifecycle loop. |
| **Risk** | Summary reset on daemon restart (document if allowed). |

---

### TC-PERF-009 — Prometheus exporter scrape smoke
| Field | Value |
|-------|-------|
| **Category** | Acceptance |
| **Priority** | P1 |
| **Objective** | Metrics appear on mgr prometheus endpoint with expected name prefix. |
| **Prerequisites** | `ceph mgr module enable prometheus`; monitoring not skipped in bootstrap. |
| **Steps** | `curl -k https://<mgr>:9283/metrics \| grep cephfs_mirror_directory` |
| **Validation** | Lines contain label set; `dir_state` sample present; HELP/TYPE lines exist. |
| **Cleanup** | None. |
| **Automation** | Optional curl from installer node. |
| **Risk** | Metrics not exported → monitoring blind spot. |

---

### TC-PERF-010 — Remove directory removes perf labels
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P0 |
| **Objective** | After `snapshot mirror remove`, counter set for dir gone. |
| **Prerequisites** | Two dirs mirrored. |
| **Steps** | 1. Dump → count N. 2. Remove dir2. 3. Restart mirror (if required per impl). 4. Dump → count N-1. |
| **Validation** | No series with `directory==<removed_path>`. |
| **Cleanup** | Remove dir1. |
| **Automation** | `remove_path_from_mirroring` + counter count. |
| **Risk** | **Blocker:** cardinality leak on dynamic dirs. |

---

### TC-PERF-011 — Add directory creates new perf instance
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P0 |
| **Objective** | New dir_root gets new labeled instance with idle defaults. |
| **Prerequisites** | One dir active. |
| **Steps** | Add second path; dump within 30s. |
| **Validation** | New label row; `dir_state==0`; counters 0. |
| **Cleanup** | Remove. |
| **Automation** | Inverse of TC-PERF-010. |
| **Risk** | Missing perf row for new export. |

---

### TC-PERF-012 — Multi-peer two counter sets same directory path
| Field | Value |
|-------|-------|
| **Category** | Edge-Corner |
| **Priority** | P2 |
| **Objective** | If multiple peers configured, same dir has one instance per peer UUID. |
| **Prerequisites** | Two peers (advanced multifs test). |
| **Steps** | Add same logical path to two peers if supported; else skip with doc. |
| **Validation** | Two entries differing only in `peer_uuid`. |
| **Cleanup** | Remove peers. |
| **Automation** | Multifs peer test. |
| **Risk** | Label collision in Prometheus. |

---

## Suite 7 — Counter Lifecycle & Dynamic Topology

**Suggested suite:** merge into `tier-2_cephfs_mirror_live_metrics_tri_interface.yaml`  
**Tier:** 2

---

### TC-LIFE-001 — Enable mirror on FS after dirs populated
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P1 |
| **Objective** | Metrics appear only after `snapshot mirror enable` + add path. |
| **Prerequisites** | FS without mirror; subvol with data. |
| **Steps** | 1. counter dump baseline. 2. `deploy_cephfs_mirroring`. 3. add path. 4. dump. |
| **Validation** | No `cephfs_mirror_directory` before; appears after with correct labels. |
| **Cleanup** | `cleanup_cephfs_mirroring` if available. |
| **Automation** | `test_cephfs_mirroring_metrics.py` workflow. |
| **Risk** | Phantom counters before enable. |

---

### TC-LIFE-002 — Disable mirroring on FS clears MGR dirs
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P1 |
| **Objective** | `snapshot mirror disable` empties status for FS. |
| **Prerequisites** | Active mirror setup. |
| **Steps** | `ceph fs snapshot mirror disable cephfs`; status + dump. |
| **Validation** | Empty or error; perf instances removed after daemon restart. |
| **Cleanup** | Re-enable for other tests. |
| **Automation** | disable helper in utils. |
| **Risk** | Leftover metrics after disable. |

---

### TC-LIFE-003 — Peer remove drops peer-labeled counters
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P1 |
| **Objective** | `peer_remove` removes all counters for that peer_uuid. |
| **Prerequisites** | One peer, two dirs. |
| **Steps** | `remove_peer_connection`; counter dump. |
| **Validation** | Zero entries with old `peer_uuid`. |
| **Cleanup** | Re-bootstrap peer. |
| **Automation** | `remove_peer_connection` in utils. |
| **Risk** | Alerts fire on removed peer. |

---

### TC-LIFE-004 — 10 dir_roots cardinality stress
| Field | Value |
|-------|-------|
| **Category** | Edge-Corner |
| **Priority** | P1 |
| **Objective** | 10 paths × tri-interface remains responsive (<5s CLI). |
| **Prerequisites** | Multithreaded test cluster; SMALL data each. |
| **Steps** | Add 10 paths; `ceph fs snapshot mirror status` timing; dump size. |
| **Validation** | Command latency <5s; JSON size < 1MB; all paths listed. |
| **Cleanup** | Remove all 10. |
| **Automation** | `test_cephfs_mirror_multithreaded_empty_dataq_hang.py` scale. |
| **Risk** | **Blocker:** MGR timeout with many dirs. |

---

### TC-LIFE-005 — Rename dir on source (if supported) metrics path key
| Field | Value |
|-------|-------|
| **Category** | Edge-Corner |
| **Priority** | P2 |
| **Objective** | Document behavior when source path renamed/moved outside mirror API. |
| **Prerequisites** | Mirrored subvol path. |
| **Steps** | Attempt rename within subvol; re-check mirror ls key. |
| **Validation** | Per product: either old key stale or new key — assert documented outcome. |
| **Cleanup** | Restore path. |
| **Automation** | Manual/doc test. |
| **Risk** | Orphan metric keys. |

---

## Suite 8 — Upgrade Path

**Suggested suite:** `suites/tentacle/cephfs/tier-1_cephfs_mirror_live_metrics_upgrade.yaml`  
**Tier:** 1 (release gate)

---

### TC-UPG-001 — Upgrade pre-68018 → 68018 with ongoing sync
| Field | Value |
|-------|-------|
| **Category** | Upgrade |
| **Priority** | P0 |
| **Objective** | Upgrade does not crash mirror; post-upgrade asok uses nested JSON. |
| **Prerequisites** | N-1 build syncing LARGE; upgrade to 68018+. |
| **Steps** | 1. Start sync on N-1. 2. `ceph orch upgrade start`. 3. Monitor until mirror upgraded. 4. Query new asok schema. |
| **Validation** | Daemon running; nested schema; sync completes or resumes; no core dumps. |
| **Cleanup** | Cluster health OK. |
| **Automation** | Upgrade pipeline job; manual gate. |
| **Risk** | **Blocker:** upgrade kills long sync. |

---

### TC-UPG-002 — Upgrade 68018 → 68827 mid-sync OMAP migration
| Field | Value |
|-------|-------|
| **Category** | Upgrade |
| **Priority** | P0 |
| **Objective** | MGR status available immediately after 68827 upgrade; stale logic active. |
| **Prerequisites** | 68018 cluster syncing; upgrade to 68827. |
| **Steps** | 1. Mid-sync upgrade. 2. Within 60s: `ceph fs snapshot mirror status`. 3. Compare to asok. |
| **Validation** | MGR command works; not empty; stale false while daemon up. |
| **Cleanup** | N/A |
| **Automation** | Upgrade suite step. |
| **Risk** | Missing MGR data post-upgrade. |

---

### TC-UPG-003 — Upgrade 68827 → 69074 perf counter registration
| Field | Value |
|-------|-------|
| **Category** | Upgrade |
| **Priority** | P0 |
| **Objective** | New perf group appears without reset cluster. |
| **Prerequisites** | 68827 idle mirrored dir; upgrade 69074. |
| **Steps** | 1. counter dump before/after. 2. Trigger new snap sync. |
| **Validation** | `cephfs_mirror_directory` present after; TRI-001 passes post-upgrade. |
| **Cleanup** | N/A |
| **Automation** | Upgrade + perf smoke. |
| **Risk** | Perf schema mismatch breaks dashboards. |

---

### TC-UPG-004 — Rolling MDS upgrade during metrics collection
| Field | Value |
|-------|-------|
| **Category** | Upgrade |
| **Priority** | P1 |
| **Objective** | MDS rolling restart does not corrupt mirror metrics. |
| **Prerequisites** | Sync idle → active during test. |
| **Steps** | 1. `ceph orch restart mds.cephfs.*` rolling. 2. Poll tri-interface during MDS down. |
| **Validation** | No daemon crash; metrics may stale briefly but recover. |
| **Cleanup** | All MDS up. |
| **Automation** | orch restart loop. |
| **Risk** | False failed state. |

---

### TC-UPG-005 — Client tool upgrade only (ceph-common) admin socket ABI
| Field | Value |
|-------|-------|
| **Category** | Upgrade |
| **Priority** | P2 |
| **Objective** | Newer `ceph --admin-daemon` against older mirror socket fails gracefully until daemon upgraded. |
| **Prerequisites** | Split versions (lab). |
| **Steps** | Run peer status with mismatched versions. |
| **Validation** | Clear error until versions match; no segfault. |
| **Cleanup** | Align versions. |
| **Automation** | Negative manual. |
| **Risk** | ABI break confuses operators. |

---

## Suite 9 — Negative, Security & Blocker-Finding

**Suggested suite:** append to `tier-2_cephfs_mirror_live_metrics_asok.yaml`  
**Tier:** 2

---

### TC-NEG-001 — JSON parse robustness under concurrent admin socket queries
| Field | Value |
|-------|-------|
| **Category** | Blocker-Finding |
| **Priority** | P1 |
| **Objective** | 20 parallel `fs mirror peer status` do not corrupt output or hang daemon. |
| **Prerequisites** | Active sync. |
| **Steps** | `for i in $(seq 1 20); do ceph --admin-daemon ... & done; wait` |
| **Validation** | All exits 0; valid JSON each; single `cephfs-mirror` process healthy. |
| **Cleanup** | N/A |
| **Automation** | Shell loop from mirror node. |
| **Risk** | Race crash under monitoring scrape + CLI. |

---

### TC-NEG-002 — Very large dir file count (100k files) progress sanity
| Field | Value |
|-------|-------|
| **Category** | Blocker-Finding |
| **Priority** | P0 |
| **Objective** | `total_files` and percents correct at scale; no overflow in bps. |
| **Prerequisites** | 100k SMALL files (use `generate_small_files_random_sizes.py`); extend timeout 14400s. |
| **Steps** | Snap + sync; sample metrics at 10%, 50%, 90%. |
| **Validation** | `total_files==100000`; percent monotonic; bps ≤ 10000 (100%). |
| **Cleanup** | Remove path. |
| **Automation** | `test_cephfs_mirror_snapshot_sync_perf_small_files.py` + metrics asserts. |
| **Risk** | **Blocker:** integer overflow / hang at scale. |

---

### TC-NEG-003 — Erasure-coded FS mirror metrics
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P1 |
| **Objective** | Metrics work on `cephfs-ec` (erasure test flag). |
| **Prerequisites** | `erasure: true` in test_data per snap_status test. |
| **Steps** | Full tri-interface on EC FS. |
| **Validation** | Same as TC-TRI-001 on EC. |
| **Cleanup** | EC teardown. |
| **Automation** | `test_cephfs_mirroring_snap_status_using_asok.py` erasure branch. |
| **Risk** | EC-only bugs in fscid labeling. |

---

### TC-NEG-004 — Subvolume snapshot schedule + live metrics
| Field | Value |
|-------|-------|
| **Category** | Edge-Corner |
| **Priority** | P2 |
| **Objective** | Scheduled snaps increment snaps_synced without metric gaps. |
| **Prerequisites** | `test_cephfs_mirroring_with_snap_schedule.py` setup. |
| **Steps** | Enable schedule; wait 3 snaps; poll MGR. |
| **Validation** | `snaps_synced` increases by 3; no perpetual stale. |
| **Cleanup** | Disable schedule. |
| **Automation** | Snap schedule test hook. |
| **Risk** | Missed scheduled snap metrics. |

---

### TC-NEG-005 — `ceph daemonperf` compatibility (if applicable)
| Field | Value |
|-------|-------|
| **Category** | Functional |
| **Priority** | P2 |
| **Objective** | `ceph daemonperf cephfs-mirror.<id> cephfs_mirror_directory` displays counters. |
| **Prerequisites** | 69074 build. |
| **Steps** | Run daemonperf for 30s during sync. |
| **Validation** | Non-empty output; rates change. |
| **Cleanup** | N/A |
| **Automation** | Optional CLI check. |
| **Risk** | Ops tooling gap. |

---

## Suite 10 — Acceptance / End-to-End Workflows

**Suggested suite:** extend `suites/tentacle/cephfs/tier-2_cephfs_mirror_metrics.yaml`  
**Tier:** 2

---

### TC-E2E-001 — Full workflow: deploy → 2 paths → 2 snaps → tri-interface idle
| Field | Value |
|-------|-------|
| **Category** | Acceptance |
| **Priority** | P0 |
| **Objective** | Reproduce and extend `test_cephfs_mirroring_metrics.py` with all three PR interfaces. |
| **Prerequisites** | Greenfield cluster per tier-2 yaml. |
| **Steps** | `deploy_cephfs_mirroring` → 2 subvols → MEDIUM data → snaps `snap_k1`, `snap_f1` → wait sync → assert TRI-001 + PERF-005 + MGR-001. |
| **Validation** | All pass; polarion CEPH-83584059 criteria + new fields. |
| **Cleanup** | `cleanup` if test config sets cleanup true. |
| **Automation** | Extend `test_cephfs_mirroring_metrics.py` (primary module). |
| **Risk** | Regression in existing shipped test. |

---

### TC-E2E-002 — HA full workflow with failover checkpoint
| Field | Value |
|-------|-------|
| **Category** | Acceptance |
| **Priority** | P0 |
| **Objective** | Extend `test_cephfs_mirroring_ha_metrics.py` with live metrics + stale + tri-interface. |
| **Prerequisites** | `tier-2_cephfs_mirror_ha_metrics.yaml` (2 mirror daemons). |
| **Steps** | HA metrics test flow + TC-HA-002 + TC-TRI-002 during sync. |
| **Validation** | HA counters + new metrics; failover monotonic progress. |
| **Cleanup** | HA cleanup. |
| **Automation** | `test_cephfs_mirroring_ha_metrics.py`. |
| **Risk** | HA production incidents without metrics. |

---

### TC-E2E-003 — Snap rename/delete workflow (asok + MGR + perf)
| Field | Value |
|-------|-------|
| **Category** | Acceptance |
| **Priority** | P0 |
| **Objective** | Port `test_cephfs_mirroring_snap_status_using_asok.py` to tri-interface. |
| **Prerequisites** | Two subvol paths kernel+fuse. |
| **Steps** | TC-ASOK-010 + TRI-005 + PERF-008. |
| **Validation** | All interfaces increment renamed/deleted counters. |
| **Cleanup** | Unmount subvols. |
| **Automation** | Extend snap_status test module. |
| **Risk** | Lifecycle ops invisible to monitoring. |

---

### TC-E2E-004 — Sync failure injection tri-interface
| Field | Value |
|-------|-------|
| **Category** | Acceptance |
| **Priority** | P0 |
| **Objective** | Extend metrics test failure act with TRI-008. |
| **Prerequisites** | `inject_sync_failure` path from metrics test. |
| **Steps** | Inject → assert failed → heal → assert idle. |
| **Validation** | failed state all interfaces; recovery clears failed. |
| **Cleanup** | Remove target bogus snap. |
| **Automation** | `test_cephfs_mirroring_metrics.py` failure section. |
| **Risk** | Silent failure in production. |

---

### TC-E2E-005 — Multifs four-path metrics isolation
| Field | Value |
|-------|-------|
| **Category** | Acceptance |
| **Priority** | P1 |
| **Objective** | Multifs setup reports correct per-FS counters (extend multifs test). |
| **Prerequisites** | `test_cephfs_mirroring_validate_cephfs-mirroring_on_multifs_setup.py`. |
| **Steps** | 4 paths across 2 FS; dump counters filtered by `source_filesystem`. |
| **Validation** | 4 perf instances; correct FS labels; no cross-mix. |
| **Cleanup** | Multifs cleanup. |
| **Automation** | Multifs test module. |
| **Risk** | Wrong FS in enterprise multi-tenant. |

---

## Automation Roadmap (cepci)

### New utilities (`cephfs_mirroring_utils.py`)
| Function | Purpose |
|----------|---------|
| `get_fs_snapshot_mirror_status(client, fs_name)` | MGR CLI JSON (68827) |
| `parse_human_size_to_bytes(s)` | Parse `110.99 MiB`, `0.00 B/s` |
| `parse_percent_string(s)` | Parse `17.45%` |
| `get_cephfs_mirror_directory_counters(node, fsid, asok)` | Filter 69074 group |
| `assert_peer_status_schema(status, paths)` | 68018 nested schema |
| `assert_tri_interface_consistency(...)` | Cross-interface tolerances |
| `poll_until_stale(mgr_status, expect_stale, timeout)` | Stale detection |
| `percent_to_bps(pct_float)` | Compare asok ↔ perf |

### New test modules
| Module | Cases |
|--------|-------|
| `test_cephfs_mirror_live_metrics_asok_schema.py` | ASOK-001–012 |
| `test_cephfs_mirror_live_metrics_mgr.py` | MGR-001–010 |
| `test_cephfs_mirror_live_metrics_tri_interface.py` | TRI-001–008 |
| `test_cephfs_mirror_live_metrics_stale.py` | STALE-001–006 |
| `test_cephfs_mirror_live_metrics_perf.py` | PERF-001–012 |
| `test_cephfs_mirror_live_metrics_upgrade.py` | UPG-001–005 (pipeline) |

### Extend existing
- `test_cephfs_mirroring_metrics.py` → TC-E2E-001, E2E-004  
- `test_cephfs_mirroring_ha_metrics.py` → TC-E2E-002, HA-*  
- `test_cephfs_mirroring_snap_status_using_asok.py` → TC-E2E-003  
- `test_cephfs_mirror_multithreaded_empty_dataq_hang.py` → ASOK-008, LIFE-004  

### Suggested suite YAML summary
| Suite file | Tier | Tests |
|------------|------|-------|
| `tier-2_cephfs_mirror_live_metrics_asok.yaml` | 2 | ASOK, NEG-001 |
| `tier-2_cephfs_mirror_live_metrics_mgr.yaml` | 2 | MGR |
| `tier-2_cephfs_mirror_live_metrics_tri_interface.yaml` | 2 | TRI, LIFE |
| `tier-2_cephfs_mirror_live_metrics_stale.yaml` | 2 | STALE |
| `tier-2_cephfs_mirror_live_metrics_ha.yaml` | 2 | HA (or merge ha_metrics) |
| `tier-2_cephfs_mirror_live_metrics_perf.yaml` | 2 | PERF (gated on 69074) |
| `tier-1_cephfs_mirror_live_metrics_upgrade.yaml` | 1 | UPG |
| `tier-2_cephfs_mirror_metrics.yaml` (existing) | 2 | E2E regression |

---

## Test Case Index (58 cases)

| ID | Title | Priority |
|----|-------|----------|
| TC-ASOK-001 | Nested peer_status schema | P0 |
| TC-ASOK-002 | current_syncing_snap fields | P0 |
| TC-ASOK-003 | Idle after sync | P0 |
| TC-ASOK-004 | full vs delta mode | P1 |
| TC-ASOK-005 | Progress monotonicity | P0 |
| TC-ASOK-006 | Throughput fields | P1 |
| TC-ASOK-007 | crawl transitions | P1 |
| TC-ASOK-008 | datasync_queue_wait | P1 |
| TC-ASOK-009 | ETA transitions | P1 |
| TC-ASOK-010 | snaps_* counters | P0 |
| TC-ASOK-011 | Invalid peer UUID | P2 |
| TC-ASOK-012 | Unmirrored path absent | P2 |
| TC-MGR-001 | MGR status schema | P0 |
| TC-MGR-002 | Default persist 5s | P0 |
| TC-MGR-003 | OMAP on cephfs_mirror | P1 |
| TC-MGR-004 | MGR lag ≤2× interval | P0 |
| TC-MGR-005 | Custom interval 30s | P1 |
| TC-MGR-006 | New dir default stats | P0 |
| TC-MGR-007 | Removed dir purged | P0 |
| TC-MGR-008 | Legacy OMAP removed | P1 |
| TC-MGR-009 | Module disabled error | P2 |
| TC-MGR-010 | Multi-FS isolation | P1 |
| TC-TRI-001 | Idle tri-interface | P0 |
| TC-TRI-002 | Active sync alignment | P0 |
| TC-TRI-003 | last_synced_snap match | P0 |
| TC-TRI-004 | current_syncing_snap match | P0 |
| TC-TRI-005 | snaps_synced alignment | P1 |
| TC-TRI-006 | sync-mode consistency | P1 |
| TC-TRI-007 | Throughput magnitude | P2 |
| TC-TRI-008 | Failed state all interfaces | P0 |
| TC-STALE-001 | Daemon stop → stale | P0 |
| TC-STALE-002 | Restart mid-sync | P0 |
| TC-STALE-003 | MGR module bounce | P1 |
| TC-STALE-004 | Network partition | P1 |
| TC-STALE-005 | interval=1 stress | P2 |
| TC-STALE-006 | Stale cleared post-sync | P1 |
| TC-HA-001 | Leader-only live asok | P0 |
| TC-HA-002 | Failover progress monotonic | P0 |
| TC-HA-003 | Stale window on failover | P0 |
| TC-HA-004 | Perf on leader only | P1 |
| TC-HA-005 | Prometheus duplicate avoidance | P1 |
| TC-HA-006 | Add dir under HA | P1 |
| TC-PERF-001 | perf group exists | P0 |
| TC-PERF-002 | Label cardinality | P0 |
| TC-PERF-003 | One instance per (dir,peer) | P0 |
| TC-PERF-004 | dir_state syncing | P0 |
| TC-PERF-005 | current_* zero idle | P0 |
| TC-PERF-006 | current_* zero failed | P0 |
| TC-PERF-007 | Basis points accuracy | P0 |
| TC-PERF-008 | snaps_* summary | P1 |
| TC-PERF-009 | Prometheus scrape | P1 |
| TC-PERF-010 | Remove dir drops labels | P0 |
| TC-PERF-011 | Add dir new instance | P0 |
| TC-PERF-012 | Multi-peer labels | P2 |
| TC-LIFE-001 | Enable mirror lifecycle | P1 |
| TC-LIFE-002 | Disable mirror | P1 |
| TC-LIFE-003 | Peer remove | P1 |
| TC-LIFE-004 | 10 dir stress | P1 |
| TC-LIFE-005 | Path rename behavior | P2 |
| TC-UPG-001 | Upgrade to 68018 mid-sync | P0 |
| TC-UPG-002 | Upgrade to 68827 OMAP | P0 |
| TC-UPG-003 | Upgrade to 69074 perf | P0 |
| TC-UPG-004 | Rolling MDS upgrade | P1 |
| TC-UPG-005 | Client/daemon version skew | P2 |
| TC-NEG-001 | Parallel asok stress | P1 |
| TC-NEG-002 | 100k files scale | P0 |
| TC-NEG-003 | EC filesystem | P1 |
| TC-NEG-004 | Snap schedule | P2 |
| TC-NEG-005 | daemonperf | P2 |
| TC-E2E-001 | Full metrics workflow | P0 |
| TC-E2E-002 | HA workflow | P0 |
| TC-E2E-003 | Snap lifecycle E2E | P0 |
| TC-E2E-004 | Failure injection E2E | P0 |
| TC-E2E-005 | Multifs isolation | P1 |

---

## P0 Blocker-Finding Checklist (release gate)

1. TC-ASOK-001, 002, 005, 010 — schema + live progress  
2. TC-MGR-001, 004, 006, 007 — operator CLI correctness  
3. TC-TRI-001, 002, 008 — monitoring truth  
4. TC-STALE-001, 002 — daemon death visibility  
5. TC-HA-002, 003 — failover progress integrity  
6. TC-PERF-005, 007, 010 — Prometheus correctness & no leak  
7. TC-NEG-002 — scale  
8. TC-UPG-001, 002, 003 — upgrade safety  
9. TC-E2E-001, 002, 004 — cephci regression parity  

---

*Document version: 1.0 — aligned with cephci utilities as of workspace snapshot. Update stale/MGR field names when 68827/69074 land in target build.*
