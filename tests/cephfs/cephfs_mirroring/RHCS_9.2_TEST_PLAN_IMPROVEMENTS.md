# CephFS RHCS 9.2 Mirroring Test Plan — Improvements & Additions

**Purpose:** This document provides ready-to-insert test rows for each section of the RHCS 9.2 CephFS Mirroring Test Plan, plus a new "Tri-Interface Consistency" section. Copy the relevant rows into the corresponding tables in the .docx.

**PRs covered:** #68018 (Asok metrics), #68827 (MGR + OMAP), #69074 (Perf counters + Prometheus)

**Sections unchanged:** Mirroring Checkpoints, Checkpoint Features (sync_from_snapshot)

---

## Commands Quick Reference (Add as Appendix or Reference Section)

```bash
# Variable resolution (run once per session on cephfs-mirror node):
FSID=$(ceph fsid)
DAEMON=$(ceph orch ps --daemon-type cephfs-mirror --format json | jq -r '.[0].daemon_id')
ASOK=/var/run/ceph/${FSID}/cephfs-mirror.${DAEMON}.asok
FS_NAME=cephfs
FS_ID=$(ceph fs dump -f json | jq -r ".filesystems[] | select(.mdsmap.fs_name==\"${FS_NAME}\") | .id")
PEER_UUID=$(ceph fs snapshot mirror peer list ${FS_NAME} -f json | jq -r 'keys[0]')
METADATA_POOL=$(ceph fs dump -f json | jq -r ".filesystems[] | select(.mdsmap.fs_name==\"${FS_NAME}\") | .mdsmap.metadata_pool")
```

| PR | Primary Command | Interface |
|----|-----------------|-----------|
| #68018 | `ceph --admin-daemon ${ASOK} fs mirror peer status ${FS_NAME}@${FS_ID} ${PEER_UUID} -f json` | Admin socket (live, daemon-local) |
| #68827 | `ceph fs snapshot mirror status ${FS_NAME} -f json` | MGR CLI (cluster-wide, OMAP-backed) |
| #69074 | `ceph --admin-daemon ${ASOK} counter dump -f json \| jq '.cephfs_mirror_directory'` | Perf counters (Prometheus-scrapable) |

---

## Section: Improve Mirroring Stats (Table 0)

### Existing tests to MODIFY (add specifics)

**Row R1 (Functional - Large/small dirs):** Add to Expected column:

> Additionally validate the new `current_syncing_snap` nested fields:
> - `sync-mode`: must be `full` for first snap on a directory, `delta` for subsequent snaps
> - `bytes.sync_bytes` and `files.sync_files`: must be monotonically non-decreasing when polled every 5s during sync
> - `bytes.sync_percent`: must reach `100.00%` (or near) at sync completion
> - `eta`: must transition from `calculating...` to a valid time estimate during sync
> - `crawl.state`: must transition `in-progress` → `completed`; `crawl.duration` increases monotonically while in-progress
> - `avg_write_throughput_bytes`: must be > `0.00 B/s` during active data sync phase
> - `last_synced_snap`: after completion, must include `crawl_duration` field in addition to `id` and `name`
>
> **Command:** `ceph --admin-daemon ${ASOK} fs mirror peer status ${FS_NAME}@${FS_ID} ${PEER_UUID} -f json | jq --arg p "${DIR_PATH}" '.[$p].current_syncing_snap'`

**Row R3 (Acceptance - Measure sync time/memory/CPU):** Replace vague "before and after enabling new stats" with specific testable approach:

> **Steps to validate:**
>
> 1. Deploy mirroring, add one directory path, prepare dataset (1000 files × 10 MiB)
>
> 2. **Run 1 — Metrics persist disabled (baseline):**
>    ```bash
>    ceph config set client.cephfs-mirror cephfs_mirror_live_metrics_persist_interval 3600
>    ceph orch restart cephfs-mirror
>    # Wait for daemon recovery
>    ```
>    - Record daemon memory before sync:
>      ```bash
>      # Via perf dump (works inside container):
>      ceph --admin-daemon ${ASOK} perf dump | jq '.mempool.buffer_anon_bytes'
>      # Or via podman (from host):
>      CONTAINER_ID=$(podman ps --filter name=cephfs-mirror -q)
>      podman stats --no-stream --format "{{.MemUsage}}" ${CONTAINER_ID}
>      ```
>    - Create snapshot, wait for sync to complete
>    - Record `sync_duration` from asok:
>      ```bash
>      ceph --admin-daemon ${ASOK} fs mirror peer status ${FS}@${FS_ID} ${PEER_UUID} -f json \
>        | jq --arg p "${DIR}" '.[$p].last_synced_snap.sync_duration'
>      ```
>    - Record daemon memory after sync (same command as above)
>    - Record daemon CPU during sync (from host):
>      ```bash
>      podman stats --no-stream --format "{{.CPUPerc}}" ${CONTAINER_ID}
>      # Sample every 5s during sync for average
>      ```
>
> 3. **Reset:** Delete synced snapshot from source, wait for idle state
>
> 4. **Run 2 — Metrics persist enabled (default 5s):**
>    ```bash
>    ceph config set client.cephfs-mirror cephfs_mirror_live_metrics_persist_interval 5
>    ceph orch restart cephfs-mirror
>    # Wait for daemon recovery
>    ```
>    - Repeat same measurements as Run 1 with identical dataset
>
> 5. **Compare:**
>    - `sync_duration` difference < 5%
>    - `memory` difference < 5 MiB
>    - `CPU` < 5%
>
> **No N-1 release needed** — this isolates the overhead of OMAP persistence on the same build.

**Row R2 (Functional - CLI & Schema):** Replace vague "new equivalent" with:

> **Exact command:** `ceph --admin-daemon ${ASOK} fs mirror peer status ${FS_NAME}@${FS_ID} ${PEER_UUID} -f json`
>
> Validate nested JSON structure — top-level keys are dir_root paths (e.g., `/volumes/group/subvol`). Each path must contain: `state`, `current_syncing_snap`, `last_synced_snap`, `snaps_synced`, `snaps_deleted`, `snaps_renamed`.
>
> The `current_syncing_snap` object (when syncing) must contain exactly: `id`, `name`, `sync-mode`, `avg_read_throughput_bytes`, `avg_write_throughput_bytes`, `crawl`, `datasync_queue_wait`, `bytes`, `files`, `eta`.

### NEW rows to ADD

| S. No | Test Type | Test Scenario | Expected | Result/Status |
|-------|-----------|---------------|----------|---------------|
| | Functional | **Sync-mode validation (full vs delta):** Create first snapshot on a directory → sync. Then modify files and create second snapshot → sync. | First snap: `sync-mode` == `full`. Second snap (with snapdiff available): `sync-mode` == `delta`. Validate via: `ceph --admin-daemon ${ASOK} fs mirror peer status ... -f json \| jq '.[$p].current_syncing_snap."sync-mode"'` | |
| | Edge | **Sync-mode when snapdiff reference missing:** Delete all snapshots on source except the latest. Create new snapshot and trigger sync. | If no prior synced snap exists for delta reference, `sync-mode` reports `full`. Validates the metric accurately reflects the sync method the daemon actually uses. | |
| | Functional | **ETA state machine:** During a LARGE sync (50 files × 100 MiB), poll `eta` field every 10s for 5 minutes. | Initially: `eta` == `calculating...`. After sufficient progress: `eta` matches time pattern (e.g., `5m 30s` or `330s`). ETA decreases over time (roughly). After sync completes: `eta` absent from output (or empty). No negative ETA values. | |
| | Functional | **Crawl state lifecycle:** During sync, poll `crawl.state` and `crawl.duration` every 5s. | `crawl.state` transitions: `in-progress` → `completed`. Never skips directly to idle without completing. `crawl.duration` monotonically increases while `in-progress`, then stabilizes at `completed`. | |
| | Functional | **Datasync queue wait under load:** Mirror 4+ directories in parallel, each with 500 MiB data. Poll `datasync_queue_wait` during peak sync. | `datasync_queue_wait.state` == `waiting` observed at least once during parallel sync. `datasync_queue_wait.duration` > `0s` while waiting. Transitions to `complete` or absent when datasync queue drains. | |
| | Functional | **Read/Write throughput validation:** During active sync of LARGE dataset, poll `avg_read_throughput_bytes` and `avg_write_throughput_bytes`. | Both fields match regex `^\d+(\.\d+)?\s*(B\|KiB\|MiB\|GiB)/s$`. `avg_write_throughput_bytes` > `0.00 B/s` during data transfer phase. Values settle to `0.00 B/s` after sync completes and dir becomes idle. | |
| | Acceptance | **Backward compatibility and observability after upgrade:** After upgrading from 9.1 to 9.2 with mirroring already configured and directories already synced: (1) Run `perf dump` — verify existing groups/keys unchanged. (2) Run `counter dump` — verify `cephfs_mirror_peers` and `cephfs_mirror_mirrored_filesystems` groups retain same labels and counter names as 9.1. (3) Verify new `cephfs_mirror_directory` group appears additionally (does not replace existing groups). (4) Check `peer_status` — new fields (`sync-mode`, `crawl`, `bytes`, `files`, `eta`, `datasync_queue_wait`, `avg_read/write_throughput_bytes`) appear for directories that were added BEFORE upgrade. (5) Existing fields (`state`, `snaps_synced`, `snaps_deleted`, `snaps_renamed`, `last_synced_snap.id/name`) still present and unchanged. | `perf dump`: groups `cephfs_mirror`, `cephfs_mirror_peers` (with `snaps_synced`, `sync_bytes`, `avg_sync_time` etc.) unchanged in structure. `counter dump`: existing labeled groups intact + new `cephfs_mirror_directory` group added. `peer_status`: existing fields preserved, new fields added alongside. Pre-upgrade directories show new metrics after next sync. No parsing errors for tools consuming old format (`perf dump` unchanged). | |
| | Functional | **last_synced_snap enrichment:** After a successful sync completes, verify `last_synced_snap` contains new fields. | `last_synced_snap` must contain: `id`, `name`, and `crawl_duration` (new field from PR #68018). `crawl_duration` must be a valid duration string (e.g., `37s`). `id` and `name` must match the source snapshot that was synced. | |
| | Negative | **Sync failure does not corrupt metrics:** Inject sync failure (e.g., create conflicting snapshot on target). Check metrics state. | `state` transitions to `failed`. `snaps_synced` does NOT falsely increment. `current_syncing_snap` absent or cleared. After removing the conflict and waiting for retry, `state` returns to `syncing` then `idle`. Metrics resume correctly. | |
| | Negative | **Daemon restart mid-sync preserves stats:** Start LARGE sync, restart cephfs-mirror daemon at ~50% progress. | After daemon restart, sync resumes (possibly from beginning or checkpoint). Metrics reflect resumed sync accurately — no stale 50% frozen forever. `snaps_synced` increments only after full successful completion. | |
| | Edge | **Monotonicity regression test:** During LARGE sync, poll `bytes.sync_bytes` and `files.sync_files` every 5 seconds for the full sync duration. | Neither value EVER decreases between consecutive polls (strict monotonic non-decreasing). If a decrease is observed, this is a P0 blocker — indicates metrics regression or race condition. | |
| | Edge | **Zero-file directory sync metrics:** Add a directory containing only empty subdirectories (no regular files) for mirroring. Create snapshot and sync. | `bytes.total_bytes` == `0.00 B`, `files.total_files` == 0, `bytes.sync_percent` == `100.00%` immediately or after brief crawl. No hang or stuck state. `crawl.state` still reaches `completed`. | |

---

## Section: Interface for Mirroring Stats (Table 1)

### Existing tests to MODIFY

**Row R1 (Functional - Verify new mirror stat command):** Replace with specific command:

> **Command:** `ceph fs snapshot mirror status ${FS_NAME} -f json`
>
> This is the NEW MGR-based command (PR #68827). Validate:
> - Per-directory fields present: `state` (idle/syncing/failed), progress %, bytes, files, last_synced_snap, snaps_synced/deleted/renamed
> - JSON format (`-f json`) and verbose options work correctly
> - Output is sourced from OMAP (not direct daemon connection) — works even without admin socket access
> - Response time < 5s even with 10+ mirrored directories

**Row R2 (Functional - stat accuracy for multiple daemons):** Add:

> Verify daemon isolation by checking that `ceph fs snapshot mirror status` aggregates per-filesystem regardless of which daemon handles which directory. Cross-reference with `ceph fs snapshot mirror daemon status ${FS_NAME} -f json` to identify which daemon owns which path.

### NEW rows to ADD

| S. No | Test Type | Test Scenario | Expected | Result/Status |
|-------|-----------|---------------|----------|---------------|
| | Functional | **OMAP persistence verification:** During active sync, inspect OMAP on cephfs_mirror object. Command: `rados -p ${METADATA_POOL} listomapkeys cephfs_mirror` | OMAP keys exist during active sync. Keys change/update over a 10-second window (proving daemon persists at interval). After `mirror remove` a path, that path's OMAP keys are cleaned up. | |
| | Functional | **Persist interval config validation:** Check default: `ceph config get client.cephfs-mirror cephfs_mirror_live_metrics_persist_interval`. Then set to 30s, restart daemon, start large sync. Poll MGR status every 5s for 90s. | Default value: `5` (seconds). After setting 30s: MGR status shows at most 1 progress step per ~30s window (not faster). Reset to 5s and verify normal cadence resumes. | |
| | Functional | **Default stats on newly added directory:** Add a new path for mirroring (`ceph fs snapshot mirror add ${FS} ${NEW_PATH}`). Immediately query `ceph fs snapshot mirror status ${FS} -f json`. | New path appears within 10 seconds. Shows `state: idle` with zeroed progress (not empty, not error). Perf counter row also appears with `dir_state=0`, all `current_*=0`. This validates the "default stats on new dir_root" feature from PR #68827. | |
| | Functional | **Stale detection — daemon stop:** Start sync on LARGE dataset (~50% progress). Stop mirror daemon: `ceph orch stop cephfs-mirror`. Wait 15s (3× persist interval). Query `ceph fs snapshot mirror status`. | Progress must NOT advance after daemon stop. MGR status indicates stale/not-live (via stale flag, last_update_age, or equivalent field). Operator must NOT see "50% synced" as if live. This is a **P0 blocker scenario**. | |
| | Functional | **Stale detection — daemon restart recovery:** After stale detection (above), restart daemon: `ceph orch start cephfs-mirror`. Poll status every 5s for 120s. | Stale indicator clears within 30s of daemon restart. Progress resumes advancing. Sync eventually completes successfully. `last_synced_snap` updates after completion. | |
| | Functional | **MGR module bounce preserves state:** With daemon running and paths idle, disable then re-enable mirroring module: `ceph mgr module disable mirroring` then `ceph mgr module enable mirroring`. | Within 30s of re-enable: `ceph fs snapshot mirror status` returns valid data (restored from OMAP). Entries not stale (daemon is healthy). No data loss or duplicate entries. | |
| | Functional | **OMAP cleanup on directory removal:** Add a path, sync one snapshot (reach idle). Remove path: `ceph fs snapshot mirror remove ${FS} ${PATH}`. Inspect OMAP. | `ceph fs snapshot mirror status` no longer lists removed path. `rados -p ${POOL} listomapkeys cephfs_mirror` no longer contains keys for removed path. Other paths' entries unchanged. | |
| | Functional | **Multi-FS isolation:** With two filesystems (cephfs, cephfs2) both mirrored: query status for each independently. | `ceph fs snapshot mirror status cephfs -f json` lists ONLY cephfs dir_roots. `ceph fs snapshot mirror status cephfs2 -f json` lists ONLY cephfs2 dir_roots. No cross-contamination. | |
| | Negative | **Module disabled — clear error:** Disable mirroring module: `ceph mgr module disable mirroring`. Run `ceph fs snapshot mirror status ${FS}`. | Returns clear error message (e.g., "module not enabled" or "command not found"). No MGR crash or hang. After `ceph mgr module enable mirroring`, command works again. | |
| | Negative | **Status on non-mirrored filesystem:** Query status for a filesystem that has mirroring disabled. | Returns empty result or clear "mirroring not enabled" error. No crash. | |
| | Edge | **MGR failover during active sync:** During sync, restart the active MGR node. | Brief interruption acceptable. After new active MGR takes over, `ceph fs snapshot mirror status` returns valid data (read from OMAP). No permanent data loss. Status matches daemon's actual state. | |

---

## NEW Section: Tri-Interface Consistency (Add as New Table)

**Context:** PRs #68018, #68827, and #69074 expose the same underlying sync metrics through three independent interfaces. This section validates they agree within acceptable tolerance.

**Commands used simultaneously per poll:**
1. **Asok:** `ceph --admin-daemon ${ASOK} fs mirror peer status ${FS}@${FS_ID} ${PEER_UUID} -f json`
2. **MGR:** `ceph fs snapshot mirror status ${FS} -f json`
3. **Perf:** `ceph --admin-daemon ${ASOK} counter dump -f json | jq '.cephfs_mirror_directory[] | select(.labels.directory=="${DIR}")'`

**Tolerance rules:**
- Asok vs MGR progress: `abs(asok_percent - mgr_percent) <= 2.0` (MGR lags by up to 1 persist interval)
- Asok vs Perf basis points: `abs(asok_percent * 100 - perf_bps) <= 1` (same daemon, same refresh cycle)
- State alignment: all three must report same state (idle/syncing/failed) within 10s of each other

| S. No | Test Type | Test Scenario | Expected | Result/Status |
|-------|-----------|---------------|----------|---------------|
| | Functional | **Tri-interface during active sync (MEDIUM dataset):** Deploy mirroring with 500 files × 1 MiB. Create snapshot. During sync, poll all 3 interfaces simultaneously every 5s for the full sync duration. | State agrees across all interfaces (all show `syncing`). Progress: asok vs MGR within 2% tolerance. Perf `sync_percent_bps` matches asok within 1 basis point. Snap `id` and `name` match across all three. `sync-mode` in asok matches `current_sync_mode` enum in perf (full=0, delta=1). | |
| | Functional | **Tri-interface at idle (post-sync):** After sync completes, wait 60s. Query all 3 interfaces. | Asok: `state=idle`, no `current_syncing_snap`, `last_synced_snap` present with id/name. MGR: same dir shows idle, progress 0 or absent. Perf: `dir_state=0`, ALL `current_*` counters = 0, `last_synced_snap_id` matches. | |
| | Functional | **Tri-interface on failure:** Inject sync failure (conflicting target snap). Query all 3 interfaces. | Asok: `state=failed`. MGR: shows failed status for that directory. Perf: `dir_state=2`, ALL `current_*` counters = 0. `snaps_synced` does NOT increment. After clearing failure and recovery: all three return to idle. | |
| | Functional | **Tri-interface snap lifecycle counters:** Create snap → sync → rename snap → delete another snap. After each operation completes, compare `snaps_synced`, `snaps_renamed`, `snaps_deleted` across all 3 interfaces. | All three interfaces report identical summary counters (no tolerance — exact match after settling). Counters are non-decreasing across sequential operations. | |
| | Functional | **Tri-interface sync-mode alignment:** First snap on new dir (full mode). Second snap with modifications (delta mode). During each sync, verify mode field across interfaces. | Asok: `sync-mode: "full"` / `"delta"`. Perf: `current_sync_mode` = 0 (full) / 1 (delta). MGR: equivalent mode field if exposed. All agree per snap. | |
| | Functional | **Tri-interface with multiple directories:** Mirror 3 directories. During parallel sync, verify each dir's metrics are isolated across all interfaces. | Each directory has independent progress in asok, MGR, and perf counters. No cross-contamination (dir A's progress doesn't appear under dir B's entry). Perf counter rows distinguished by `directory` label. | |
| | Negative | **Tri-interface during stale (daemon down):** Stop daemon mid-sync. Check all interfaces. | Asok: unreachable (daemon down — expected). MGR: shows stale/frozen progress (NOT advancing). Perf: unreachable (daemon down — expected). Key assertion: MGR must NOT show live progress when daemon is dead. | |
| | Negative | **Tri-interface after HA failover:** In HA setup (2 mirror daemons), stop leader. After failover to new leader: | New leader's asok shows syncing (progress resumes). MGR shows brief stale then resumes. New leader's perf counters show `dir_state=1`. Old leader's asok/perf unreachable. No duplicate conflicting states in Prometheus if both nodes scraped. | |
| | Edge | **Tri-interface basis points precision:** At exactly 17.45% progress (approximate): | Asok: `bytes.sync_percent` = `17.45%`. Perf: `sync_percent_bps` = `1745` (integer, NOT 17.45 float). Conversion rule: `abs(asok_float * 100 - perf_integer) <= 1`. This validates the basis-point encoding is correct. | |
| | Edge | **Tri-interface rapid state transitions:** Create snap on small data (fast sync). Poll all interfaces at 2s interval. | May miss `syncing` state entirely (sync completes within 1 persist cycle). Terminal state (idle) must eventually agree across all three. No interface stuck in stale `syncing` after dir is clearly idle. | |

---

## Section: Enhance Mirroring Perf Dumps (Table 3)

### Existing tests to MODIFY

**Row R1 (Functional - Validate new changes in perf dump):** Add specifics:

> **Exact command:** `ceph --admin-daemon ${ASOK} counter dump -f json | jq '.cephfs_mirror_directory'`
>
> Validate the NEW `cephfs_mirror_directory` group (PR #69074) contains:
> - One entry per mirrored directory per peer
> - Each entry has labels: `source_fscid`, `source_filesystem`, `peer_uuid`, `peer_cluster_name`, `peer_cluster_filesystem`, `directory`
> - Counters include: `dir_state`, `current_sync_bytes`, `current_total_bytes`, `current_sync_files`, `current_total_files`, `sync_percent_bps`, `current_sync_mode`, `current_read_bps`, `current_write_bps`, `crawl_state`, `crawl_duration_seconds`, `datasync_wait_state`, `datasync_wait_duration_seconds`, `current_eta_valid`, `current_eta_seconds`, `snaps_synced`, `snaps_deleted`, `snaps_renamed`, `last_snap_id`, `last_crawl_duration_seconds`, `last_sync_duration_seconds`, `last_sync_timestamp`, `last_sync_bytes`, `last_sync_files`
>
> Also verify Prometheus scrape: `curl -sk https://${MGR_HOST}:9283/metrics | grep cephfs_mirror_directory`
> Must show `# HELP` and `# TYPE` lines for each counter.

**Row R5 (Functional - Multiple daemons/filesystems):** Add:

> Perf counter labels must include `source_filesystem` to distinguish entries. Filter: `jq '.cephfs_mirror_directory[] | select(.labels.source_filesystem=="cephfs")'` vs `cephfs2`. No label collision or data leakage.

### NEW rows to ADD

| S. No | Test Type | Test Scenario | Expected | Result/Status |
|-------|-----------|---------------|----------|---------------|
| | Functional | **Label validation:** After mirroring setup with 2 directories, dump counters and inspect labels on each entry. | Each `cephfs_mirror_directory` entry has ALL 6 labels: `source_fscid` (matches `ceph fs dump`), `source_filesystem` (e.g., "cephfs"), `peer_uuid` (matches `peer_list`), `peer_cluster_name` (remote site), `peer_cluster_filesystem` (target FS), `directory` (dir_root path). Labels are correct per directory. | |
| | Functional | **Counter lifecycle — add directory:** Add a new path for mirroring. Within 10s, run `counter dump`. | New labeled row appears for the added directory. `dir_state=0`, all `current_*=0`. Count of `cephfs_mirror_directory` entries increments by 1. | |
| | Functional | **Counter lifecycle — remove directory:** Remove a mirrored path. Within 10s, run `counter dump`. | Row for removed directory disappears. Count decrements by 1. No stale/orphan entry remains. Other directories' entries unchanged. | |
| | Functional | **dir_state semantics during sync:** Start sync → verify. Wait idle → verify. Inject failure → verify. | Syncing: `dir_state=1`, `current_sync_bytes > 0`. Idle: `dir_state=0`, ALL `current_* = 0`. Failed: `dir_state=2`, ALL `current_* = 0`, `snaps_synced` unchanged. | |
| | Functional | **Basis points encoding:** During sync when asok reports `bytes.sync_percent: "42.37%"`, simultaneously read perf `sync_percent_bps`. | Perf value = `4237` (integer). Rule: `abs(42.37 * 100 - 4237) <= 1`. NOT a float (42.37) — must be integer basis points. This is critical for Prometheus/Grafana correctness. | |
| | Functional | **Prometheus scrape end-to-end:** During active sync, curl the MGR metrics endpoint. | `curl -sk https://${MGR_HOST}:9283/metrics \| grep cephfs_mirror_directory` returns: `# HELP cephfs_mirror_directory_dir_state ...`, `# TYPE cephfs_mirror_directory_dir_state gauge`, sample lines with all labels and numeric values. At least one directory shows non-zero progress counters. | |
| | Functional | **daemonperf output:** During active sync, run `ceph daemonperf cephfs-mirror.${DAEMON} cephfs_mirror_directory 30`. | Non-empty tabular output. Rates change over the 30s observation window (not all zeros). After sync completes and dir idles: rates stabilize to 0. | |
| | Functional | **Legacy groups still present (regression):** After #69074, verify old counter groups still work. | `counter dump | jq '.cephfs_mirror_mirrored_filesystems'` — still present, `directory_count` matches active mirrored dirs. `counter dump | jq '.cephfs_mirror_peers'` — still present, shows peer count. No regression in legacy monitoring. | |
| | Edge | **Counter leak soak test:** Loop 50 times: add directory → wait 5s → remove directory → wait 5s. After loop, count `cephfs_mirror_directory` entries. | Final count equals count before loop started (original mirrored dirs only). No orphan/leaked counter instances. Memory usage of mirror daemon stable (no growth). | |
| | Edge | **HA perf counter isolation:** In HA setup (2 mirror daemons), dump counters from both nodes during active sync. | Exactly ONE node shows `dir_state=1` for each (directory, peer_uuid) pair. Other node shows `dir_state=0` or no entry. No conflicting duplicate `dir_state=1` for same directory. | |
| | Negative | **Counters survive daemon restart:** Start sync, note perf counter state. Restart mirror daemon (`ceph orch restart cephfs-mirror`). After recovery, dump counters. | Counter group re-created on restart. If sync resumes: `dir_state=1` with progress. Summary counters (`snaps_synced` etc.) preserved (not reset to 0). | |

---

## Section: Dashboard Testing (Table 5)

### NEW rows to ADD (merge with existing)

| Test Type | Test Scenario | Expected | Result/Status |
|-----------|---------------|----------|---------------|
| Functional | **Dashboard displays new live metrics:** During active sync, verify Dashboard shows: sync progress %, bytes synced/total, files synced/total, sync-mode (full/delta), ETA. | All new metric fields from PR #68018/#68827 visible in Dashboard. Values update as sync progresses (sourced from `ceph fs snapshot mirror status` MGR command). | |
| Functional | **Dashboard stale indicator:** Kill mirror daemon. Check Dashboard mirroring status after 15s. | Dashboard shows stale/warning indicator for affected directories. Does NOT display "50% complete" as if live when daemon is dead. | |
| Functional | **Dashboard per-directory state isolation:** With 3+ mirrored dirs (one idle, one syncing, one failed): | Dashboard shows each directory with its own independent state. No state bleeding between directories. | |
| Negative | **Dashboard during MGR failover:** Trigger MGR failover while viewing mirroring status. | Brief interruption acceptable. After new active MGR, Dashboard data restores from OMAP cache. | |

---

## Section: Performance (Table 6)

### Existing tests to MODIFY

**All performance rows (R0-R4):** Add to "During sync" capture steps:

> Use new built-in metrics for performance measurement (no external tools needed):
> - `avg_write_throughput_bytes` from asok — built-in throughput metric
> - `crawl.duration` — built-in crawl time metric
> - `bytes.sync_bytes` / `bytes.total_bytes` — built-in progress (replaces manual `du` comparison)
> - `eta` field — built-in ETA estimate
>
> **Capture command:** `ceph --admin-daemon ${ASOK} fs mirror peer status ${FS}@${FS_ID} ${PEER_UUID} -f json | jq --arg p "${DIR}" '.[$p].current_syncing_snap | {mode: ."sync-mode", throughput: .avg_write_throughput_bytes, crawl_state: .crawl.state, crawl_dur: .crawl.duration, sync_pct: .bytes.sync_percent, eta: .eta}'`

### NEW rows to ADD

| Test Type | Test Scenario | Expected | Result/Status |
|-----------|---------------|----------|---------------|
| Performance | **Metrics overhead measurement:** Run identical sync workload (1000 files × 10 MiB) with default persist_interval=5s. Measure: sync completion time, daemon CPU%, daemon RSS memory. Compare with persist_interval=60s (minimal metrics activity). | Overhead of 5s persist interval: sync time increase < 5%, CPU increase < 10%, memory increase < 50 MiB. New metrics do NOT introduce significant performance regression. | |
| Performance | **ETA accuracy validation:** During LARGE sync, record first valid ETA prediction and actual completion time. | `actual_time / predicted_ETA` ratio between 0.5 and 2.0 (within 2x accuracy). ETA gets more accurate as sync progresses (later predictions closer to actual). | |
| Performance | **Throughput metric vs wall-clock:** Compare `avg_write_throughput_bytes` (from asok) with `total_bytes_synced / wall_clock_time`. | Asok-reported throughput within 20% of wall-clock calculated throughput. Validates throughput metric is reliable for performance monitoring. | |

---

## Section: Stale Detection & Health (Add as New Sub-Section or Merge with Interface)

If the Health Warning section was removed, these tests should be added to the "Interface for Mirroring Stats" section or as a standalone section:

| S. No | Test Type | Test Scenario | Expected | Result/Status |
|-------|-----------|---------------|----------|---------------|
| | Functional | **Single daemon down — stale detection:** Stop cephfs-mirror daemon (`ceph orch stop cephfs-mirror`). Wait 15s. Query `ceph fs snapshot mirror status`. | MGR status indicates stale/not-live for all directories previously handled by stopped daemon. Progress values frozen (not advancing). `ceph health` shows HEALTH_WARN with mirror daemon down. | |
| | Functional | **Daemon restart — stale clears:** After stale detection above, restart daemon (`ceph orch start cephfs-mirror`). Poll status every 5s. | Within 30s: stale indicator clears. Progress resumes if sync was in-flight. `ceph health` returns to HEALTH_OK. | |
| | Functional | **Partial HA daemon down:** In 2-daemon HA setup, stop one daemon. | Only directories owned by stopped daemon show stale. Other daemon's directories continue syncing normally. Health shows partial warning (1/2 down). | |
| | Negative | **Force-kill daemon process:** `kill -9 <mirror_daemon_pid>` instead of graceful stop. | Same stale detection behavior as graceful stop. Health warning still triggers. No orphan OMAP locks or corruption. | |
| | Edge | **Rapid stop/start cycle:** Stop daemon, wait 5s, start daemon, wait 5s, stop again, wait 15s. | Final state: stale detected after the last stop. No stuck "live" state from brief intermediate restart. System handles rapid transitions without confusion. | |

---

## Summary of All Changes

| Section | Action | Count |
|---------|--------|-------|
| Improve Mirroring Stats | Modify 2 existing rows + Add 10 new rows | 12 |
| Interface for Mirroring Stats | Modify 2 existing rows + Add 11 new rows | 13 |
| **Tri-Interface Consistency (NEW)** | New section with 10 rows | 10 |
| Enhance Mirroring Perf Dumps | Modify 2 existing rows + Add 11 new rows | 13 |
| Dashboard Testing | Add 4 new rows | 4 |
| Performance | Modify all existing rows + Add 3 new rows | 3+ |
| Stale Detection & Health | New sub-section with 5 rows | 5 |
| Mirroring Checkpoints | **UNCHANGED** | 0 |
| Checkpoint Features | **UNCHANGED** | 0 |

**Total new/modified test entries:** ~58 (covering all 12 gaps from the gap analysis)
