# CephFS Mirror Live Metrics — Consolidated Test Plan (v3.0)

**Single source of truth** for PR #68018, #68827, #69074 verification and cephci automation.

**Replaces:** v2.0 (18 scenarios) and `CEPHFS_MIRROR_LIVE_METRICS_TEST_PLAN.md` (74 isolated TC-* definitions).

**Baseline cephci:** `cephfs_mirroring_utils.py`, `test_cephfs_mirroring_metrics.py`, `test_cephfs_mirroring_snap_status_using_asok.py`, `test_cephfs_mirroring_ha_metrics.py`, `tier-2_cephfs_mirror_metrics.yaml`.

---

## Section 1: Commands Reference by PR

### Variable resolution (run once per session)

```bash
# On cephfs-mirror node
FSID=$(ceph fsid)
DAEMON=$(ceph orch ps --daemon-type cephfs-mirror --format json | jq -r '.[0].daemon_id')
ASOK=/var/run/ceph/${FSID}/cephfs-mirror.${DAEMON}.asok

# On source client (MON access)
FS_NAME=cephfs
FS_ID=$(ceph fs dump -f json | jq -r ".filesystems[] | select(.mdsmap.fs_name==\"${FS_NAME}\") | .id")
PEER_UUID=$(ceph fs snapshot mirror peer list ${FS_NAME} -f json | jq -r '.[0].uuid')
DIR_PATH=/volumes/<group>/<subvol>   # dir_root from: ceph fs snapshot mirror ls cephfs
METADATA_POOL=$(ceph fs dump -f json | jq -r ".filesystems[] | select(.mdsmap.fs_name==\"${FS_NAME}\") | .mdsmap.metadata_pool")
MGR_HOST=<active-mgr-host>
```

---

### PR #68018 — Asok Metrics (admin socket)

#### 1.1 Peer status (nested JSON by dir_root)

```bash
cd /var/run/ceph/${FSID}
ceph --admin-daemon ${ASOK} \
  fs mirror peer status ${FS_NAME}@${FS_ID} ${PEER_UUID} -f json
```

| When | Use |
|------|-----|
| During sync | Live progress; poll every 5–10s |
| After idle (60s+) | `last_synced_snap`, lifecycle counters |
| Debugging | Compare crawl/queue/throughput vs MGR lag |

**Expected top-level shape:** Keys = mirrored `dir_root` paths only (not peer UUID). Each path object:

| Field | Idle | Syncing | Failed |
|-------|------|---------|--------|
| `state` | `idle` | `syncing` | `failed` |
| `current_syncing_snap` | absent/empty | full object | absent/empty |
| `last_synced_snap` | `{id,name}` after sync | prior snap | prior snap |
| `snaps_synced` / `snaps_deleted` / `snaps_renamed` | integers | monotonic | no false ++ on fail |

**`current_syncing_snap` (syncing only) — validate:**

```bash
ceph --admin-daemon ${ASOK} fs mirror peer status ${FS_NAME}@${FS_ID} ${PEER_UUID} -f json \
  | jq --arg p "${DIR_PATH}" '.[$p].current_syncing_snap'
```

| Subfield | Parse / assert |
|----------|----------------|
| `id`, `name` | Match source snap id/name |
| `sync-mode` | `full` (first snap on dir) or `delta` |
| `bytes` | `sync_bytes`, `total_bytes`, `sync_percent` (e.g. `17.45%` → float 17.45) |
| `files` | `sync_files`, `total_files`, `sync_percent` |
| `eta` | `calculating...` early; later `Nm`/`Ns`/`Nh` regex |
| `crawl` | `state`, `duration` — `in-progress` → terminal |
| `datasync_queue_wait` | `state`, `duration` — `waiting` under load |
| `avg_read_throughput_bytes`, `avg_write_throughput_bytes` | Human rates → bytes; ≥0; write >0 in datasync |
| Throughput | `^\d+(\.\d+)?\s*(B\|KiB\|MiB\|GiB)/s` |

**Monotonicity (LARGE sync):**

```bash
# Sample every 5s; assert non-decreasing:
jq -r --arg p "${DIR_PATH}" '.[$p].current_syncing_snap.bytes.sync_percent'   # strip %
jq -r --arg p "${DIR_PATH}" '.[$p].current_syncing_snap.files.sync_files'
```

#### 1.2 Filesystem mirror status (daemon scope)

```bash
ceph --admin-daemon ${ASOK} fs mirror status ${FS_NAME}@${FS_ID} -f json
```

**When:** Daemon health, peer list; not per-dir progress (use peer status).

#### 1.3 Cross-check mirrored paths

```bash
ceph fs snapshot mirror ls ${FS_NAME} -f json
# set(peer_status keys) == set(mirror ls dir_roots)
```

#### 1.4 Negative — invalid peer UUID

```bash
ceph --admin-daemon ${ASOK} \
  fs mirror peer status ${FS_NAME}@${FS_ID} 00000000-0000-0000-0000-000000000000 -f json
# Expect: non-zero exit or JSON error; daemon still up:
ceph orch ps --daemon-type cephfs-mirror
```

#### 1.5 Negative — unmirrored path absent

```bash
# peer_status must NOT contain keys for paths not in mirror ls
jq 'keys'   # after peer status fetch
```

---

### PR #68827 — MGR Interface (OMAP-backed)

#### 2.1 Operator CLI — snapshot mirror status

```bash
ceph fs snapshot mirror status ${FS_NAME} -f json
```

| When | Use |
|------|-----|
| During sync | Cached progress (≤2× persist interval lag vs asok) |
| After daemon stop | Stale detection (do not trust frozen % as live) |
| Post-idle | Operator dashboard; no asok access needed |

**Parse (jq examples):**

```bash
# All dir_roots for first peer (adjust peer index):
jq -r '.filesystems[] | select(.name=="'"${FS_NAME}"'") | .peers[0].directory_states[] | .path'

# Per-dir state:
jq --arg d "${DIR_PATH}" \
  '.filesystems[] | select(.name=="'"${FS_NAME}"'") | .peers[0].directory_states[] | select(.path==$d)'

# Progress (field names per build — typical):
# .sync_percent or nested .bytes.sync_percent
# .stale / .live / .last_update_age — stale when mirror down
```

**Tri-interface tolerance (during sync):** `abs(asok_pct - mgr_pct) <= 2.0`; MGR must advance within 15s of asok increase (default persist 5s).

#### 2.2 Persist interval config

```bash
ceph config get client.cephfs-mirror cephfs_mirror_live_metrics_persist_interval
# Default: 5 or 5s

ceph config set client.cephfs-mirror cephfs_mirror_live_metrics_persist_interval 30
ceph orch restart cephfs-mirror
# Sample MGR every 5s for 90s — ≤1 % step per ~30s window

ceph config set client.cephfs-mirror cephfs_mirror_live_metrics_persist_interval 5
ceph orch restart cephfs-mirror
```

| When | Use |
|------|-----|
| Tuning test | Validate OMAP/MGR cadence |
| Stale test | Wait ≥3× interval after daemon stop (15s @ 5s) |

#### 2.3 OMAP inspection (metadata pool)

```bash
rados -p ${METADATA_POOL} listomapkeys cephfs_mirror
rados -p ${METADATA_POOL} getomapval cephfs_mirror <key> -f json-pretty   # when key known

# During active sync — re-list after 10s; keys exist and may change
```

| When | Use |
|------|-----|
| During sync | Confirm daemon persists live stats |
| Debugging MGR empty | OMAP empty ⇒ persistence broken |
| Post-remove dir | Ghost keys should not drive MGR entries |

#### 2.4 Dir add / remove vs MGR

```bash
ceph fs snapshot mirror add ${FS_NAME} ${NEW_DIR_PATH}
sleep 10
ceph fs snapshot mirror status ${FS_NAME} -f json | jq --arg d "${NEW_DIR_PATH}" '...'
# Expect: path present, idle, zeroed progress

ceph fs snapshot mirror remove ${FS_NAME} ${DIR_PATH_B}
ceph fs snapshot mirror status ${FS_NAME} -f json
# Expect: B absent; other paths unchanged
```

#### 2.5 Stale detection workflow

```bash
# 1) Mid-sync — record MGR percent P
ceph fs snapshot mirror status ${FS_NAME} -f json | jq --arg d "${DIR_PATH}" '...'

# 2) Stop mirror
ceph orch stop cephfs-mirror
sleep 15    # 3× default persist interval

# 3) Stale assert — progress must NOT advance; stale/live flag set
ceph fs snapshot mirror status ${FS_NAME} -f json

# 4) Recovery
ceph orch start cephfs-mirror
# Poll 5s × 24: stale clears; progress resumes

# 5) MGR module bounce (daemon still up)
ceph mgr module disable mirroring
ceph mgr module enable mirroring
# Within 30s: entries restored, not stale if daemon healthy
```

| When | Use |
|------|-----|
| Daemon stop | **Blocker** — ops must not see live progress when mirror dead |
| Restart mid-sync | Brief stale window then clear |
| MGR bounce | OMAP survives |

#### 2.6 Multi-FS isolation

```bash
ceph fs snapshot mirror status cephfs -f json
ceph fs snapshot mirror status cephfs2 -f json
# Each lists only its filesystem's dir_roots
```

#### 2.7 Negative — module disabled

```bash
ceph mgr module disable mirroring
ceph fs snapshot mirror status ${FS_NAME}
# Expect: clear error, no MGR crash
ceph mgr module enable mirroring
```

#### 2.8 HA — leader identification

```bash
ceph fs snapshot mirror daemon status ${FS_NAME} -f json
# Use leader host for authoritative MGR/asok reads in HA tests
```

---

### PR #69074 — Perf Counters (Prometheus)

#### 3.1 counter dump — `cephfs_mirror_directory` group

```bash
cd /var/run/ceph/${FSID}
ceph --admin-daemon ${ASOK} counter dump -f json \
  | jq '.cephfs_mirror_directory'
```

**Filter one directory:**

```bash
ceph --admin-daemon ${ASOK} counter dump -f json \
  | jq --arg d "${DIR_PATH}" \
    '.cephfs_mirror_directory[] | select(.labels.directory==$d)'
```

| Label | Source |
|-------|--------|
| `source_fscid` | `ceph fs dump` → filesystem id |
| `source_filesystem` | `cephfs` |
| `peer_uuid` | `ceph fs snapshot mirror peer list` |
| `peer_cluster_name` | remote site name (bootstrap) |
| `peer_cluster_filesystem` | target FS name |
| `directory` | dir_root path |

**Key counters & states:**

| Counter / gauge | Idle (`dir_state=0`) | Syncing (`dir_state=1`) | Failed (`dir_state=2`) |
|-----------------|----------------------|-------------------------|------------------------|
| `dir_state` | 0 | 1 | 2 |
| `current_sync_bytes`, `current_sync_files` | 0 | >0 | 0 |
| `sync_percent_bps` (bytes/files) | 0 | 1745 ⇒ 17.45% | 0 |
| `current_syncing_snap_id`, `current_syncing_snap_name` | empty/0 | match asok | empty/0 |
| `last_synced_snap_id` | match snap id | — | — |
| `snaps_synced`, `snaps_deleted`, `snaps_renamed` | summary | ++ with ops | no false ++ |

**Basis points validation:**

```bash
# asok percent F; perf bps B → abs(F*100 - B) <= 1
```

**Sync mode enum:** map asok `sync-mode` `full`/`delta` to perf mode counter via `SYNC_MODE_PERF_MAP` (fill from target build).

#### 3.2 Legacy groups (regression)

```bash
ceph --admin-daemon ${ASOK} counter dump -f json \
  | jq '{mirrored_fs: .cephfs_mirror_mirrored_filesystems, peers: .cephfs_mirror_peers}'
# directory_count, mirroring_peers — parity with pre-69074 tests
```

#### 3.3 Prometheus scrape

```bash
# Active MGR metrics port (default 9283)
curl -sk https://${MGR_HOST}:9283/metrics | grep cephfs_mirror_directory
```

**Expect:**

```
# HELP cephfs_mirror_directory_dir_state ...
# TYPE cephfs_mirror_directory_dir_state gauge
cephfs_mirror_directory_dir_state{...,directory="/volumes/..."} 1.0
cephfs_mirror_directory_sync_percent_bps{...} 1745.0
```

| When | Use |
|------|-----|
| During sync | Alerting rules, label cardinality |
| Idle | `dir_state==0`, all `current_*` zero |
| HA | Scrape both nodes — exactly one `dir_state==1` per (directory, peer_uuid) |

#### 3.4 daemonperf

```bash
ceph daemonperf cephfs-mirror.${DAEMON} cephfs_mirror_directory 30
```

**Validate:** Non-empty output; rates change during active sync; stable at idle.

#### 3.5 Tri-interface one-liner (automation pattern)

```bash
# Per poll: asok peer status + MGR status + counter dump (same DIR_PATH, PEER_UUID)
# assert: state alignment, snap id/name, bps vs %, dir_state vs state
```

| Phase | Primary interface |
|-------|-------------------|
| Live debugging | Asok |
| Operator / no socket access | MGR |
| Monitoring / alerts | Perf + Prometheus |
| Daemon dead | MGR stale flags (not asok) |

---

## Section 2: Consolidated Scenarios (8)

**Design:** One deploy per scenario where possible; tri-interface poll loop on every progress sample; happy-path negatives at end of S01.

| ID | Name | Duration | Priority |
|----|------|----------|----------|
| **S01** | Functional Mega (happy path) | ~2–3 h | P0 |
| **S02** | Progress, ETA & Concurrency | ~1.5–2 h | P0 |
| **S03** | Failure & Resilience | ~1.5–2 h | P0 |
| **S04** | Variant Topologies | ~45–60 min | P1 |
| **S05** | HA Failover | ~55–75 min | P0 |
| **S06** | Scale 100k Files | ~2–4 h | P0 |
| **S07** | Upgrade Gate | Pipeline | P0 |
| **S08** | Regression Parity Smoke | ~20 min | P0 |

---

### S01 — Functional Mega (Happy Path)

**One cluster state:** deploy mirror → 2 subvol paths (kernel + fuse) → optional third path for add-defaults → EMPTY path for full/delta → remove third/EMPTY as needed.

**Phases (single test module, no redeploy between phases):**

| Phase | What | Original TCs |
|-------|------|----------------|
| A Bootstrap | `deploy_cephfs_mirroring`, add 2 paths, schema, MGR baseline, persist default 5s, perf labels (2 rows), legacy counters | 01: ASOK-001, MGR-001/002/006, PERF-001/002/003, LIFE-001, E2E-001 |
| B Tri-interface sync | SMALL path A + MEDIUM path B; poll syncing; `assert_tri_interface_consistency`; bps vs %; idle 60s; `last_synced_snap` | 02: ASOK-002/003, MGR-004, TRI-001–004, PERF-004/005/007, E2E-001 |
| C Full vs delta | Add EMPTY path; snap₁ `full`, snap₂ `delta`; tri-interface mode map | 03: ASOK-004, TRI-006 |
| D Snap lifecycle | Rename/delete snaps; `snaps_*` aligned asok/MGR/perf | 06: ASOK-010, TRI-005, PERF-008, E2E-003 |
| E MGR dir lifecycle | OMAP keys during sync; add dir C / remove dir B; perf cardinality | 07: MGR-003/007, PERF-010/011 |
| F Prometheus smoke | During phase B MEDIUM sync: curl metrics + daemonperf 30s | 12: PERF-009, NEG-005 |
| G Negatives (tail) | Bad UUID, unmirrored path absent, module disable/enable | 14: ASOK-011/012, MGR-009 |

**Poll:** 10s until sync done (3600s); tri-interface 5s during monotonic checks.

**Module:** `test_cephfs_mirror_live_metrics_mega.py` → `test_functional_mega_happy_path`  
**Suite:** `tier-2_cephfs_mirror_live_metrics.yaml` test 1

---

### S02 — Progress, ETA & Concurrency

**Prerequisites:** Reuse cluster from S01 or standalone deploy + 1 LARGE path + 8 paths for stress.

| Phase | What | Original TCs |
|-------|------|----------------|
| A LARGE | 50×100 MiB snap; monotonic %/files; ETA calculating→estimate; crawl; throughput | 04: ASOK-005/006/007/009, TRI-007 |
| B Multithreaded | 8 dir_roots, parallel snaps; `datasync_queue_wait`; 20× parallel asok; MGR status <5s | 05: ASOK-008, LIFE-004, NEG-001 |

**Module:** `test_cephfs_mirror_live_metrics_progress_stress.py`  
**Suite:** test 2 (weekly OK to skip phase B)

---

### S03 — Failure & Resilience

**One session:** mirror active, MEDIUM/LARGE path for mid-sync injections.

| Phase | What | Original TCs |
|-------|------|----------------|
| A Persist tuning | interval 30s → LARGE sync cadence; optional 1s sub-step; reset 5s | 08: MGR-005, STALE-005 |
| B Stale daemon | stop mirror 15s; stale frozen progress; restart; MGR module bounce; post-sync stale clear | 09: STALE-001/002/003/006, TRI-001 |
| C Network partition | iptables DROP to target MON; 120s poll; flush; heal | 10: STALE-004 |
| D Sync failure | `inject_sync_failure`; failed tri-interface; heal | 13: TRI-008, PERF-006, E2E-004 |

**Module:** `test_cephfs_mirror_live_metrics_failure.py`  
**Suite:** test 3 (partition phase optional nightly)

---

### S04 — Variant Topologies

| Branch | What | Original TCs |
|--------|------|----------------|
| A Multi-FS | `cephfs2`; per-FS MGR isolation; perf labels; peer remove/re-add; disable mirror FS2 | 15: MGR-010, E2E-005, LIFE-002/003, PERF-012 |
| B EC | `cephfs-ec` + SMALL; tri-interface subset (idle + one sync) | 17: NEG-003 |
| C Snap schedule | 3 scheduled snaps; `snaps_synced += 3`; no perpetual stale | 17: NEG-004 |

**Module:** `test_cephfs_mirror_live_metrics_variants.py`  
**Suite:** test 4 (`test_data.erasure` variant for branch B)

---

### S05 — HA Failover

**Topology:** 2× cephfs-mirror (`tier-2_cephfs_mirror_ha_metrics.yaml`).

| Step | What | Original TCs |
|------|------|----------------|
| 1 | LARGE sync ~30%; leader asok full, follower minimal | HA-001, TRI-002 |
| 2 | `counter dump` both nodes — one `dir_state==1` | HA-004 |
| 3 | Stop leader; failover ≤300s; MGR ≥ P-5%; stale <60s | HA-002/003, STALE |
| 4 | Add third path under HA | HA-006 |
| 5 | Prometheus both nodes — no duplicate conflicting `dir_state` | HA-005, PERF-009 |

**Module:** extend `test_cephfs_mirroring_ha_metrics.py`  
**Suite:** `tier-2_cephfs_mirror_live_metrics_ha.yaml`

---

### S06 — Scale 100k Files

| Step | What | Original TCs |
|------|------|----------------|
| 1 | `generate_small_files_random_sizes.py` → 100k files | 16: NEG-002 |
| 2 | Snap; poll 14400s; sample 10/50/90%: `total_files==100000`, monotonic %, bps≤10000, tri-interface | |

**Module:** extend `test_cephfs_mirror_snapshot_sync_perf_small_files.py`  
**Suite:** `tier-2_cephfs_mirror_snapshot_sync_perf.yaml` or nightly job

---

### S07 — Upgrade Gate

**Pipeline only** (N-1 → target); separate tier-1 suite.

| Step | Upgrade | Gate | Original TCs |
|------|---------|------|----------------|
| 1 | → 68018+ mid-sync | Nested asok schema; sync completes | UPG-001 |
| 2 | 68018 → 68827 | MGR status ≤60s; OMAP layout; not stale if daemon up | UPG-002, MGR-008 |
| 3 | 68827 → 69074 | `cephfs_mirror_directory` appears; idle tri-interface | UPG-003 |
| 4 | Rolling MDS restart | Brief stale OK; recovery | UPG-004 |
| 5 | Version skew (lab) | Clear error, no segfault | UPG-005 |

**Module:** `test_cephfs_mirror_live_metrics_upgrade.py`  
**Suite:** `tier-1_cephfs_mirror_live_metrics_upgrade.yaml`

---

### S08 — Regression Parity Smoke

Run existing `test_cephfs_mirroring_metrics.py` (CEPH-83584059) until S01–S03 stable; asserts legacy failure injection + counter paths.

**Suite:** keep `tier-2_cephfs_mirror_metrics.yaml` until deprecation.

---

## Section 3: Coverage Matrix

### Original 18 scenarios → 8 consolidated

| v2 Scenario | v3 Scenario |
|-------------|-------------|
| 01 Bootstrap | **S01** phase A |
| 02 Tri-interface lifecycle | **S01** phase B |
| 03 Full/delta | **S01** phase C |
| 04 LARGE progress | **S02** phase A |
| 05 Multithreaded | **S02** phase B |
| 06 Snap lifecycle | **S01** phase D |
| 07 MGR OMAP/lifecycle | **S01** phase E |
| 08 Persist interval | **S03** phase A |
| 09 Stale daemon | **S03** phase B |
| 10 Network partition | **S03** phase C |
| 11 HA | **S05** |
| 12 Prometheus | **S01** phase F (+ **S05** HA scrape) |
| 13 Failure injection | **S03** phase D |
| 14 Negatives | **S01** phase G |
| 15 Multifs | **S04** branch A |
| 16 Scale 100k | **S06** |
| 17 EC + schedule | **S04** branches B/C |
| 18 Upgrade | **S07** |

### TC-* index → v3 scenario

| TC | S | TC | S | TC | S |
|----|---|----|---|----|---|
| TC-ASOK-001..003 | S01 | TC-MGR-001..002,006 | S01 | TC-STALE-001..003,006 | S03 |
| TC-ASOK-004 | S01 | TC-MGR-003,007 | S01 | TC-STALE-004 | S03 |
| TC-ASOK-005..007,009 | S02 | TC-MGR-004 | S01,S02 | TC-STALE-005 | S03 |
| TC-ASOK-008 | S02 | TC-MGR-005 | S03 | TC-HA-001..006 | S05 |
| TC-ASOK-010 | S01 | TC-MGR-008 | S07 | TC-PERF-001..003 | S01 |
| TC-ASOK-011..012 | S01 | TC-MGR-009 | S01 | TC-PERF-004..008 | S01,S02,S03 |
| TC-TRI-001..006 | S01 | TC-MGR-010 | S04 | TC-PERF-009 | S01,S05 |
| TC-TRI-007 | S02 | TC-TRI-008 | S03 | TC-PERF-010..011 | S01 |
| TC-UPG-001..005 | S07 | TC-LIFE-001..004 | S01,S02,S04 | TC-E2E-001..005 | S01,S04,S05 |
| TC-NEG-001..005 | S02,S01,S08 | TC-NEG-002 | S06 | | |

**Coverage:** All 74 indexed TC-* IDs from `CEPHFS_MIRROR_LIVE_METRICS_TEST_PLAN.md` map to ≥1 v3 scenario.

---

## Section 4: Utility Requirements (condensed)

**File:** `tests/cephfs/cephfs_mirroring/cephfs_mirroring_utils.py`

### P0 (S01–S03)

| Function | Purpose |
|----------|---------|
| `get_fs_snapshot_mirror_status(client, fs_name)` | MGR JSON (#68827) |
| `get_cephfs_mirror_directory_counters(...)` | Filter `cephfs_mirror_directory` (#69074) |
| `get_tri_interface_snapshot(...)` | `{asok, mgr, perf}` one dir |
| `assert_peer_status_schema(peer_status, paths)` | #68018 nested keys |
| `assert_tri_interface_consistency(snapshot, ...)` | State/snap/%/idle/failed |
| `validate_current_syncing_snap_fields(snap_obj)` | Asok syncing schema |
| `parse_percent_string` / `parse_human_size_to_bytes` / `percent_to_bps` | Encoding |
| `poll_monotonic_progress` / `poll_until_mirror_state` | S02 |
| `poll_until_stale(...)` | S03 |
| `assert_idle_tri_interface` / `assert_failed_tri_interface` | Lifecycle tail |
| `get_metadata_pool_name` / `list_cephfs_mirror_omap_keys` | OMAP |
| `timed_mgr_status` | MGR latency (S02) |

### P1 (S04–S07)

| Function | Purpose |
|----------|---------|
| `assert_sync_mode_tri_interface` | full/delta + perf enum |
| `get_mirror_leader_node` | HA (#68827 daemon status) |
| `parallel_admin_socket_peer_status(n=20)` | Stress |
| `SYNC_MODE_PERF_MAP` | Build-time enum |

### Extend existing

`get_fs_mirror_peer_status_using_asok` (path-keyed dict), `validate_snaps_status_increment` (+renamed/deleted), `inject_sync_failure`, `wait_for_daemon_recovery`.

### Poll loop (mandatory pattern)

```python
snap = utils.get_tri_interface_snapshot(mirror_node, client, fs_name, dir_path, peer_uuid)
if syncing:
    utils.assert_tri_interface_consistency(snap, state="syncing", pct_tolerance=2.0)
```

**Feature gates:** Skip MGR if build < 68827; skip perf/Prometheus if < 69074.

---

## Section 5: CI Execution Plan (simplified)

### Job A — P0 smoke (~4–5 h chained)

| Order | Scenario | Module |
|-------|----------|--------|
| 1 | S01 Functional Mega | `test_cephfs_mirror_live_metrics_mega.py` |
| 2 | S03 Failure (no partition) | `test_cephfs_mirror_live_metrics_failure.py` |
| 3 | S08 Regression | `test_cephfs_mirroring_metrics.py` |

**Suite:** `suites/tentacle/cephfs/tier-2_cephfs_mirror_live_metrics.yaml`  
**Conf:** `conf/tentacle/cephfs/tier-2_cephfs_mirror_metrics.yaml`  
**Pre-task:** `test_cephfs_mirroring_configure_cephfs_mirroring.py` (`cleanup: false`)

### Job B — Depth (~3 h)

| Order | Scenario |
|-------|----------|
| 1 | S02 Progress & Concurrency |
| 2 | S04 Variant Topologies |

### Job C — HA (~1.5 h)

| Order | Scenario |
|-------|----------|
| 1 | S05 HA Failover |

**Suite:** `tier-2_cephfs_mirror_live_metrics_ha.yaml`

### Job D — Nightly (~6 h+)

| Order | Scenario |
|-------|----------|
| 1 | S06 Scale 100k |
| 2 | S03 phase C (network partition) |
| 3 | S02 phase B (if skipped in Job B) |

### Job E — Release (tier-1)

| Order | Scenario |
|-------|----------|
| 1 | S07 Upgrade |
| 2 | Job A on upgraded cluster |

### P0 release gate

| Gate | Scenario |
|------|----------|
| Schema + tri-interface | S01 |
| Progress monotonicity | S02 |
| Stale + failed visibility | S03 |
| HA | S05 |
| Scale | S06 |
| Upgrade | S07 |

---

---

## Section 6: Gap Analysis vs RHCS 9.1 Test Plan

This section documents gaps between the existing "Ceph9.1 - CephFS Test Plan: Mirroring Improvements" document and the actual PR implementation (#68018, #68827, #69074).

### GAP 1: No Specific Command References

**Existing plan says:** "Run `ceph fs mirror status` (or the new equivalent)"

**Actual commands from PRs:**

| PR | Exact Command | What It Returns |
|----|---------------|-----------------|
| #68018 | `ceph --admin-daemon /var/run/ceph/${FSID}/cephfs-mirror.${DAEMON}.asok fs mirror peer status ${FS_NAME}@${FS_ID} ${PEER_UUID} -f json` | Live per-dir nested JSON with bytes/files/crawl/eta/throughput/sync-mode |
| #68827 | `ceph fs snapshot mirror status ${FS_NAME} -f json` | Cluster-wide OMAP-backed status (no asok needed) |
| #69074 | `ceph --admin-daemon ${ASOK} counter dump -f json \| jq '.cephfs_mirror_directory'` | Labeled perf counters for Prometheus |

**Impact:** Without explicit commands, testers may validate wrong interfaces or miss the new MGR command entirely.

---

### GAP 2: Missing Tri-Interface Consistency Testing

**Existing plan:** Tests asok, MGR, and perf dumps in isolation across separate table sections.

**What's missing:** The three PRs expose the SAME underlying sync progress through THREE interfaces. No test validates they agree within tolerance during an active sync.

**Required assertion pattern:**

```bash
# Poll simultaneously during active sync:
ASOK_PCT=$(ceph --admin-daemon ${ASOK} fs mirror peer status ... -f json \
  | jq -r --arg p "${DIR}" '.[$p].current_syncing_snap.bytes.sync_percent' | tr -d '%')

MGR_PCT=$(ceph fs snapshot mirror status ${FS} -f json \
  | jq -r --arg d "${DIR}" '... .sync_percent' | tr -d '%')

PERF_BPS=$(ceph --admin-daemon ${ASOK} counter dump -f json \
  | jq --arg d "${DIR}" '.cephfs_mirror_directory[] | select(.labels.directory==$d) | .counters.sync_percent_bps')

# Assertions:
# abs(ASOK_PCT - MGR_PCT) <= 2.0
# abs(ASOK_PCT * 100 - PERF_BPS) <= 1
```

**Tolerance rationale:** MGR reads from OMAP which is persisted at `cephfs_mirror_live_metrics_persist_interval` (default 5s), so up to 2% drift is acceptable during fast syncs.

---

### GAP 3: Missing OMAP Persistence Validation

**Existing plan:** No mention of OMAP or `rados` commands.

**PR #68827 mechanism:** Mirror daemon writes sync_stat to OMAP on `cephfs_mirror` object in the metadata pool at configurable interval.

**Required tests:**

```bash
# Verify OMAP keys exist during active sync:
METADATA_POOL=$(ceph fs dump -f json | jq -r '.filesystems[0].mdsmap.metadata_pool')
rados -p ${METADATA_POOL} listomapkeys cephfs_mirror

# Verify keys change over time (10s apart):
KEYS_T0=$(rados -p ${METADATA_POOL} listomapkeys cephfs_mirror | md5sum)
sleep 10
KEYS_T1=$(rados -p ${METADATA_POOL} listomapkeys cephfs_mirror | md5sum)

# After remove_path_from_mirroring:
# Ghost keys for removed path must NOT drive MGR entries

# Config tuning:
ceph config get client.cephfs-mirror cephfs_mirror_live_metrics_persist_interval
# Expected: 5 (default)
```

---

### GAP 4: Missing Stale Detection Testing (Critical Blocker)

**Existing plan:** "Stop one cephfs-mirror daemon → Verify ceph health HEALTH_WARN" under Health Warning section.

**What's missing:** The plan does NOT validate that `ceph fs snapshot mirror status` shows stale/frozen progress when daemon is dead. This is the #1 blocker scenario — operators must NOT see "50% synced" as live data when the daemon has been dead for hours.

**Required stale detection workflow:**

```bash
# 1) Record MGR progress mid-sync
P_BEFORE=$(ceph fs snapshot mirror status ${FS} -f json | jq '... .sync_percent')

# 2) Kill daemon
ceph orch stop cephfs-mirror
sleep 15    # 3x default persist interval (3 × 5s)

# 3) ASSERT: MGR must indicate stale — progress must NOT advance
P_AFTER=$(ceph fs snapshot mirror status ${FS} -f json | jq '... .sync_percent')
# P_AFTER == P_BEFORE (frozen)
# stale/live flag must indicate NOT live

# 4) Recovery
ceph orch start cephfs-mirror
# Poll 5s × 24: stale clears; progress resumes; sync completes

# 5) MGR module bounce (daemon still running)
ceph mgr module disable mirroring
ceph mgr module enable mirroring
# Within 30s: entries restored from OMAP, not stale if daemon healthy
```

---

### GAP 5: Missing Nested JSON Structure Validation (Breaking Change)

**Existing plan:** References "updated JSON schema" vaguely.

**PR #68018 breaking change:** `peer_status` is now nested by `dir_root` path as top-level key. Previous format had different nesting. This breaks any parser that assumes the old structure.

**Required schema validation:**

```bash
ceph --admin-daemon ${ASOK} fs mirror peer status ${FS}@${FS_ID} ${PEER_UUID} -f json | jq 'keys'
# Expected: ["/volumes/group/subvol1", "/volumes/group/subvol2", ...]
# NOT: peer UUID as key

# Per path, required fields:
jq --arg p "${DIR}" '.[$p] | keys' 
# Must contain: "state", "current_syncing_snap", "last_synced_snap", "snaps_synced", "snaps_deleted", "snaps_renamed"

# current_syncing_snap (when syncing) required subfields:
jq --arg p "${DIR}" '.[$p].current_syncing_snap | keys'
# Must contain: "id", "name", "sync-mode", "avg_read_throughput_bytes", "avg_write_throughput_bytes",
#               "crawl", "datasync_queue_wait", "bytes", "files", "eta"
```

---

### GAP 6: Weak Perf Counter Lifecycle Testing

**Existing plan:** "Validate each daemon's perf dump reflects only its workload" — vague.

**Missing specifics:**

```bash
# Counter creation on add_directory():
COUNT_BEFORE=$(ceph --admin-daemon ${ASOK} counter dump -f json | jq '.cephfs_mirror_directory | length')
ceph fs snapshot mirror add ${FS} ${NEW_PATH}
sleep 10
COUNT_AFTER=$(ceph --admin-daemon ${ASOK} counter dump -f json | jq '.cephfs_mirror_directory | length')
# COUNT_AFTER == COUNT_BEFORE + 1

# Counter removal on remove_directory():
ceph fs snapshot mirror remove ${FS} ${NEW_PATH}
sleep 10
COUNT_REMOVED=$(ceph --admin-daemon ${ASOK} counter dump -f json | jq '.cephfs_mirror_directory | length')
# COUNT_REMOVED == COUNT_BEFORE (no leak)

# Basis points encoding:
# When asok shows sync_percent "17.45%", perf counter sync_percent_bps must be 1745 (integer)
# Formula: abs(asok_float * 100 - bps_integer) <= 1

# dir_state zeroing:
# When state=idle: dir_state=0, ALL current_* counters = 0
# When state=failed: dir_state=2, ALL current_* counters = 0
# When state=syncing: dir_state=1, current_sync_bytes > 0

# Label validation:
jq '.cephfs_mirror_directory[0].labels'
# Must have: source_fscid, source_filesystem, peer_uuid, peer_cluster_name, peer_cluster_filesystem, directory
```

---

### GAP 7: Missing HA-Specific Metrics Testing

**Existing plan:** Health Warning section covers daemon stop/mixed state but NOT metrics behavior.

**Missing:**

```bash
# Identify leader:
ceph fs snapshot mirror daemon status ${FS} -f json
# Use leader node for authoritative reads

# Leader vs follower metrics:
# On LEADER node:
ceph --admin-daemon ${LEADER_ASOK} fs mirror peer status ... -f json
# Full populated syncing JSON with progress

# On FOLLOWER node:
ceph --admin-daemon ${FOLLOWER_ASOK} fs mirror peer status ... -f json
# Minimal/empty — follower does NOT drive sync

# counter dump on both:
# Exactly ONE node has dir_state==1 for a given (directory, peer_uuid)
# Other node: dir_state==0, current_*==0

# After failover:
# Brief stale window (<60s) acceptable, then new leader resumes
# No duplicate conflicting dir_state in Prometheus scrapes
```

---

### GAP 8: Missing Upgrade Path for Metrics Specifically

**Existing plan:** "Upgrade from an older Ceph version with mirroring configured using peer_add — validate no data loss."

**Missing metrics-specific upgrade validation:**

```bash
# Post-upgrade to #68018 build:
# Asok peer_status returns nested JSON (new schema) — old parsers will break

# Post-upgrade to #68827 build:
ceph fs snapshot mirror status ${FS} -f json
# Command must now work (didn't exist before); not empty; not stale if daemon up

# OMAP format migration:
rados -p ${METADATA_POOL} listomapkeys cephfs_mirror
# Old format keys removed; new sync_stat keys active

# Post-upgrade to #69074 build:
ceph --admin-daemon ${ASOK} counter dump -f json | jq '.cephfs_mirror_directory | length'
# New group appears; > 0 for mirrored dirs
```

---

### GAP 9: Missing ETA and Crawl State Machine Validation

**Existing plan:** "Observe progress metrics (% complete, remaining bytes, throughput)" — no specific assertions.

**Required state machine tests:**

```bash
# ETA lifecycle (poll during LARGE sync):
# Phase 1: eta == "calculating..." (first ~30s)
# Phase 2: eta matches time regex /^\d+[smh]$/ or /^\d+m\s+\d+s$/
# Phase 3: eta absent or reset after sync completes

# Crawl state machine:
# crawl.state: "in-progress" → "completed" (never skips directly to idle)
# crawl.duration: monotonically increases while in-progress, stable after completed

# datasync_queue_wait (under backpressure):
# state: "waiting" observed at least once during large parallel sync
# duration: > 0 while waiting; stable after "complete"
```

---

### GAP 10: Missing Sync Mode (full vs delta) Validation

**Existing plan:** No mention of `sync-mode` field.

**Required:**

```bash
# First snapshot on a directory (no prior synced snap):
# sync-mode == "full"

# Subsequent snapshot (snapdiff available):
# sync-mode == "delta"

# Cross-interface check:
# Asok: "sync-mode": "full" or "delta"
# Perf counter: current_sync_mode = 0 (full) or 1 (delta)
# SYNC_MODE_PERF_MAP = {"full": 0, "delta": 1}
```

---

### GAP 11: Prometheus/Grafana End-to-End Missing

**Existing plan:** "perf dump" mentioned but no Prometheus/scrape test.

**Required:**

```bash
# Prometheus endpoint scrape:
curl -sk https://${MGR_HOST}:9283/metrics | grep cephfs_mirror_directory

# Expected output pattern:
# # HELP cephfs_mirror_directory_dir_state ...
# # TYPE cephfs_mirror_directory_dir_state gauge
# cephfs_mirror_directory_dir_state{source_fscid="1",source_filesystem="cephfs",...,directory="/volumes/..."} 1.0

# daemonperf (real-time rates):
ceph daemonperf cephfs-mirror.${DAEMON} cephfs_mirror_directory 30
# Non-empty; rates change during active sync; stable at idle
```

---

### GAP 12: No "Default Stats on New Dir" Test

**Existing plan:** Does not cover what happens immediately after adding a new directory for mirroring (before first snap).

**Required:**

```bash
ceph fs snapshot mirror add ${FS} ${NEW_DIR}
sleep 10

# MGR status must show dir immediately with idle defaults (NOT empty/error):
ceph fs snapshot mirror status ${FS} -f json | jq --arg d "${NEW_DIR}" '...'
# state: idle, zeros for progress, no error

# Perf counter must have new labeled row:
ceph --admin-daemon ${ASOK} counter dump -f json \
  | jq --arg d "${NEW_DIR}" '.cephfs_mirror_directory[] | select(.labels.directory==$d)'
# dir_state: 0, all current_*: 0
```

---

## Section 7: Specific Improvements Per Test Plan Section

### 7.1 Section "Improve Mirroring Stats" — Add These Assertions

| Current Test | Missing Assertion | Exact Validation Command |
|--------------|-------------------|--------------------------|
| "Validate total files, bytes" | Field-by-field `current_syncing_snap` schema | `jq '.[$p].current_syncing_snap \| keys' \| sort` must equal `["avg_read_throughput_bytes","avg_write_throughput_bytes","bytes","crawl","datasync_queue_wait","eta","files","id","name","sync-mode"]` |
| "Observe progress metrics" | Monotonicity of `sync_bytes` and `sync_files` | Poll 5s × N; each sample >= previous; no regression |
| "Ensure metrics reset after completion" | `last_synced_snap.crawl_duration` populated | `jq '.[$p].last_synced_snap'` must include crawl_duration field (not just id/name) |
| "Validate per-directory isolation" | Full/delta mode correctness | First snap: `sync-mode == "full"`; second snap: `sync-mode == "delta"` |
| "Compare reported stats with du" | Cross-interface parity | asok bytes vs MGR bytes vs perf `current_total_bytes` within 1 persist interval |

### 7.2 Section "Interface for Mirroring Stats" — Add These Tests

| New Test | Steps | Assertion |
|----------|-------|-----------|
| Tri-interface consistency | Poll asok + MGR + perf simultaneously during sync | `abs(asok_pct - mgr_pct) <= 2.0`; `abs(asok_pct*100 - perf_bps) <= 1` |
| OMAP persistence | `rados -p ${POOL} listomapkeys cephfs_mirror` during sync | Keys present; change over 10s |
| Stale detection lifecycle | `orch stop` → 15s → MGR status → `orch start` → poll | Frozen progress flagged stale; cleared after restart |
| Default stats on new dir | `mirror add` → immediate MGR status query | Dir present, idle, zeroed (no empty/error) |
| Persist interval tuning | Set 30s → verify cadence → reset 5s | MGR % steps at ~30s intervals, not faster |
| OMAP cleanup on dir remove | `mirror remove` → `rados listomapkeys` | Removed dir's keys gone from OMAP |

### 7.3 Section "Enhance Mirroring Perf Dumps" — Add These Tests

| New Test | Steps | Assertion |
|----------|-------|-----------|
| `cephfs_mirror_directory` group presence | `counter dump \| jq '.cephfs_mirror_directory'` | Group exists; length == number of mirrored dirs |
| Label validation | Inspect `.labels` on each counter row | All 6 labels present: `source_fscid`, `source_filesystem`, `peer_uuid`, `peer_cluster_name`, `peer_cluster_filesystem`, `directory` |
| Basis points encoding | Compare asok `sync_percent` (float) vs perf `sync_percent_bps` (int) | `abs(float*100 - int) <= 1` |
| Counter lifecycle add | `mirror add` new dir → counter dump | New row appears with `dir_state=0`, all `current_*=0` |
| Counter lifecycle remove | `mirror remove` → counter dump | Row disappears; count decrements; no stale row |
| Counter leak soak | Loop 50 add/remove cycles → counter dump | Final count == original count; no memory growth |
| `dir_state` semantics | During sync: 1; after idle: 0; after inject failure: 2 | Matches asok `state` field |
| Idle zeroing | After sync completes, wait 60s | `dir_state=0`; every `current_*` field = 0 |
| Failed zeroing | After `inject_sync_failure` | `dir_state=2`; every `current_*` field = 0; `snaps_synced` unchanged |
| Prometheus HELP/TYPE | `curl` MGR metrics endpoint | `# HELP cephfs_mirror_directory_*` and `# TYPE ... gauge` lines present |

### 7.4 Section "Health Warning" — Add These Cross-References

| Existing Test | Add This Cross-Check |
|---------------|---------------------|
| "Stop one daemon → HEALTH_WARN" | Also verify: `ceph fs snapshot mirror status` shows stale for that daemon's dirs |
| "All daemons down → HEALTH_ERR" | Also verify: MGR status shows stale for ALL dirs (not false "live 50%") |
| "Peer disconnected → HEALTH_WARN" | Also verify: perf counter `dir_state` transitions to 2 (failed) |
| "Recovery → alert clears" | Also verify: tri-interface returns to idle/syncing; stale cleared |

### 7.5 Section "Performance" — Add Metrics-Based Validation

| Existing Test | Add This |
|---------------|----------|
| "Measure sync completion time" | Capture `crawl.duration` from asok as automated timing (not manual) |
| "Track backlog reduction rate" | Use `avg_write_throughput_bytes` from asok as throughput metric |
| "Capture pending files/size every minute" | Use `current_syncing_snap.files.sync_files` and `bytes.sync_bytes` (built-in, no external tool needed) |
| "Compare file count/sizes" | Cross-validate asok `total_files`/`total_bytes` with `du` on source |
| All performance tests | Add: compare ETA prediction vs actual completion time (accuracy within 2x) |

### 7.6 Section "Mirroring Checkpoints" — No Gaps (Feature Not in These PRs)

Checkpoint features (`--sync_from_snapshot`, `--sync-latest-snapshot`) are separate from the metrics PRs. No metrics-specific gaps here, but add: "After checkpoint reached, verify `snaps_synced` increments and `last_synced_snap` matches checkpoint snap."

### 7.7 Section "Dashboard Testing" — Add Metrics Integration

| New Test | Steps | Assertion |
|----------|-------|-----------|
| Dashboard shows new metrics | Navigate to mirroring status page during sync | Progress %, throughput, ETA visible (sourced from MGR status) |
| Dashboard stale indicator | Kill daemon → check dashboard | Stale/warning indicator; not false "healthy progress" |
| Dashboard per-dir status | Multiple mirrored dirs | Each dir shows independent state (idle/syncing/failed) |

---

## Section 8: Priority Matrix — Missing Scenarios to Add

| Priority | Scenario | Why It's a Blocker Risk | Test Plan Section to Add To |
|----------|----------|-------------------------|----------------------------|
| **P0** | Tri-interface drift validation during large sync | #1 integration failure mode; ops lose trust if interfaces disagree | Interface for Mirroring Stats |
| **P0** | Stale metrics after daemon kill (frozen progress) | Customer sees "50% synced" forever when daemon dead | Health Warning + Interface |
| **P0** | Counter lifecycle leak soak (100 add/remove) | Long-running daemon leaks memory/counters | Enhance Perf Dumps |
| **P0** | Nested JSON schema validation | Breaks all existing automation parsers | Improve Mirroring Stats |
| **P0** | HA leader-only OMAP writer | Dual-writer corrupts OMAP state | Simplify Mirroring Setup (HA) |
| **P1** | Persist interval tuning (5s → 30s → 5s) | Config silently broken = no OMAP updates | Interface for Mirroring Stats |
| **P1** | ETA/crawl state machine validation | "ETA: -5m" or stuck "calculating" destroys trust | Improve Mirroring Stats |
| **P1** | Full vs delta sync-mode correctness | Wrong mode label confuses operator decisions | Improve Mirroring Stats |
| **P1** | Prometheus scrape end-to-end | Monitoring team can't build dashboards | Enhance Perf Dumps |
| **P1** | Default stats on new dir (no empty/error) | First-use UX broken | Interface for Mirroring Stats |
| **P2** | OMAP key cleanup on dir remove | Ghost entries in long-running clusters | Interface for Mirroring Stats |
| **P2** | Basis points encoding (1745 = 17.45%) | Grafana shows wrong values | Enhance Perf Dumps |

---

## Section 9: Summary of 12 Recommended Changes

1. **Add a Commands Reference section** listing exact CLI for each PR (Section 1 of this document)
2. **Add Tri-Interface Consistency as a test category** — cross-validate asok/MGR/perf on same poll cycle
3. **Expand Stale Detection** from a health-warning sub-bullet to a full test scenario with specific timing (3x persist interval)
4. **Add OMAP Persistence validation** — `rados listomapkeys/getomapval` during sync and after dir removal
5. **Add Counter Lifecycle tests** — add/remove/leak soak with exact count assertions
6. **Add Upgrade-Specific Metrics validation** — nested JSON appears, MGR command works, perf group appears
7. **Add Prometheus/Grafana scrape validation** — curl endpoint, HELP/TYPE lines, labels
8. **Specify sync-mode (full/delta) validation** — first snap full, subsequent delta, cross-interface enum
9. **Add ETA/crawl/queue-wait state machine tests** — lifecycle transitions, monotonic duration, regex for time
10. **Add "Default Stats on New Dir" test** — immediate idle/zero response from MGR after add
11. **Add HA leader-only metrics** — only leader writes OMAP/counters; no dup in Prometheus
12. **Specify exact JSON field assertions** — replace vague "validate metrics" with jq commands and expected keys

---

*Document version: 3.1 — Added Sections 6-9 (Gap Analysis, Improvements, Priority Matrix, Recommendations) based on review of RHCS 9.1 CephFS Mirroring Test Plan document.*
