# Migration: isolate DragonTTS onto a dedicated node pool (e2-standard-4) + 250 Gi cache PVC

**One-line summary:** Move DragonTTS off the shared `beta-pool` onto its own
dedicated `e2-standard-4` node pool, expand its cache PVC from 30 Gi → 250 Gi,
and raise its CPU so it can actually use the bigger node. Clairvoyance + redis
stay on `beta-pool`, untouched.

> **Who runs what:** Every `gcloud`/`kubectl` command below is run **by you** in
> Cloud Shell (per our standing rule — I never run infra mutations). Copy each
> block, verify the output matches the "expected" line, then proceed.

---

## 1. Goal & end state

| | Before | After |
|---|---|---|
| DragonTTS node pool | `beta-pool` (shared) | **`dragontts-pool` (dedicated)** |
| DragonTTS machine type | e2-standard-2 (2 vCPU / 8 GB) | **e2-standard-4 (4 vCPU / 16 GB)** |
| CPU limit | 1000m | **3000m** (request 2000m) |
| Cache PVC | 30 Gi (`premium-rwo` / pd-ssd) | **250 Gi** |
| Clairvoyance + redis | `beta-pool` | **`beta-pool` (unchanged)** |

**Why:** CPU contention (clarity: all three workloads share one e2-standard-2) is
a plausible cause of the ~2s Gemini synth latency. Giving DragonTTS a dedicated,
2× CPU node removes the contention and lets synthesis + the 24k→16k downsample +
format-conversion run with headroom. 250 Gi gives the write-through cache room to
grow for weeks/months without eviction pressure.

---

## 2. Cost impact (asia-south1, on-demand, approximate)

| Item | Before | After | Δ/month |
|---|---|---|---|
| DragonTTS node (VM) | (share of beta-pool) | **e2-standard-4** | **~+$100** |
| DragonTTS boot disk | — | 100 Gi pd-balanced | ~+$10 |
| Cache PVC (pd-ssd) | 30 Gi ≈ $5 | **250 Gi ≈ $42** | **~+$37** |
| `beta-pool` (clairvoyance+redis) | unchanged | unchanged | $0 |

**Net add: ~$145/mo before sustained-use discount; ~$110/mo after SUD** (always-on
nodes get up to ~30% off the VM). Confirm in the
[pricing calculator](https://cloud.google.com/products/calculator). `beta-pool`
keeps running (it still hosts clairvoyance + redis), so its cost is unchanged.

---

## 3. Current state (facts, for the record)

- **Cluster:** `breeze-automatic-mum-01`, region `asia-south1`, project `breeze-automatic-prod`
- **Namespace:** `beta`
- **`beta-pool`:** e2-standard-2, 100 Gi pd-balanced, COS_CONTAINERD, default SA,
  `gke-default` scopes, **autoscaling off**, `initialNodeCount: 1`
- **Deployment `dragontts`:** `replicas: 1`, `strategy: Recreate`,
  `nodeSelector: cloud.google.com/gke-node-pool: beta-pool`,
  resources request `1000m / 1900Mi`, limit `1000m / 2300Mi`
- **PVC `dragontts-data`:** `storageClassName: premium-rwo` (**pd-ssd, ZONAL**),
  30 Gi, `volumeBindingMode: WaitForFirstConsumer`

---

## 4. ⚠️ The one gotcha that actually bites: the PVC is ZONAL

`premium-rwo` creates a **zonal** disk in the zone of the node DragonTTS first
ran on. A zonal disk **can only attach to a node in that same zone.** If the new
pool has no node in the PVC's zone, the pod goes `Pending` forever on the move.

**Mitigation (built into the steps below):** find the PVC's zone first, then
create `dragontts-pool` **in that exact zone**. (DragonTTS is `replicas:1` + RWO,
so a single-zone pool is correct and cheapest.)

---

## 5. Pre-flight checks (run first — capture the current state)

```bash
# 5.1 confirm you're pointed at the right project/cluster
gcloud config get-value project          # expect: breeze-automatic-prod
kubectl config current-context           # expect: ...breeze-automatic-mum-01...

# 5.2 where is DragonTTS running now + its current resources/nodeSelector
kubectl -n beta get pod -l app=dragontts -o wide
kubectl -n beta get deployment dragontts -o jsonpath='{.spec.template.spec.nodeSelector}'; echo
kubectl -n beta get deployment dragontts -o jsonpath='{.spec.template.spec.containers[0].resources}'; echo

# 5.3 current PVC size + StorageClass
kubectl -n beta get pvc dragontts-data

# 5.4 CRITICAL — find the PVC disk's ZONE (new pool must live here)
PV=$(kubectl -n beta get pvc dragontts-data -o jsonpath='{.spec.volumeName}')
kubectl get pv "$PV" -o yaml | grep -iE "zone|nodeAffinity|failure-domain"
#   look for: topology.kubernetes.io/zone: asia-south1-a   (or -b / -c)
ZONE=asia-south1-a   # <- set this to whatever the line above shows

# 5.5 confirm beta-pool autoscaling is off (so it can't scale away under clairvoyance/redis)
gcloud container node-pools describe beta-pool --cluster breeze-automatic-mum-01 --region asia-south1 --format="yaml(autoscaling)"
```

Write down `$ZONE` — you'll use it in Step 1.

---

## 6. The migration (safe order — do NOT reorder)

The principle: **expand the PVC while the pod is still stable on `beta-pool`
(no migration risk), then do the node move as the only downtime event.** That
gives you exactly **one** brief downtime window (the pod move).

### Step 1 — Create the dedicated pool (`e2-standard-4`, in the PVC's zone)

```bash
gcloud container node-pools create dragontts-pool \
  --cluster breeze-automatic-mum-01 \
  --region asia-south1 \
  --node-locations "$ZONE" \
  --machine-type e2-standard-4 \
  --disk-type pd-balanced \
  --disk-size 100 \
  --image-type COS_CONTAINERD \
  --scopes gke-default \
  --num-nodes 1
```

- `--node-locations "$ZONE"` is **non-negotiable** — it pins the node to the
  PVC's zone so the disk can attach (see §4).
- Same disk/image/scopes as `beta-pool`; default SA + default network are
  inherited automatically. (If you'd rather have N2 for deterministic CPU instead
  of E2 — see §10 — swap `--machine-type e2-standard-4` for `n2-standard-4`.)

**Verify:**
```bash
gcloud container node-pools describe dragontts-pool --cluster breeze-automatic-mum-01 \
  --region asia-south1 --format="yaml(config.machineType, config.locations, status)"
kubectl get nodes -l cloud.google.com/gke-nodepool=dragontts-pool -o wide
#   expect: 1 node, STATUS Ready, in $ZONE, machine-type e2-standard-4
```
Do **not** proceed until the node is `Ready`.

### Step 2 — Expand the cache PVC (30 Gi → 250 Gi), pod still on beta-pool

```bash
kubectl -n beta patch pvc dragontts-data \
  --patch '{"spec":{"resources":{"requests":{"storage":"250Gi"}}}}'
kubectl -n beta get pvc dragontts-data -w
#   watch CAPACITY climb 30Gi -> 250Gi; STATUS stays Bound. Ctrl-C when settled.
```

- The disk grows in place; **data is untouched**. This is node-independent.
- If you see `CONDITION = FileSystemResizePending`, that's fine — the filesystem
  will resize when the pod restarts in Step 4 (online resize may already do it).

**Verify the disk grew:**
```bash
kubectl -n beta get pvc dragontts-data
#   expect: CAPACITY 250Gi
```
> Do **not** delete/recreate the PVC to resize — `premium-rwo` PVs default to
> `reclaimPolicy: Delete`, which would **delete the disk and all cache data**.

### Step 3 — Edit `deployment.yaml` (nodeSelector + resources)

Two changes in `deploy/k8s/deployment.yaml`:

**(a) nodeSelector** — point at the new pool:
```yaml
      nodeSelector:
        cloud.google.com/gke-nodepool: dragontts-pool   # was: beta-pool
```

**(b) resources** — raise CPU (and a little memory) to use the bigger node:
```yaml
          resources:
            requests:
              cpu: 2000m          # was 1000m — reserves 2 of 4 cores for TTS
              memory: 3072Mi      # was 1900Mi
              ephemeral-storage: 1Gi
            limits:
              cpu: 3000m          # was 1000m — allows burst up to 3 cores
              memory: 4096Mi      # was 2300Mi
              ephemeral-storage: 2Gi
```
(Both are tunable. CPU is the latency lever; memory headroom is for the frequency
tracker + SQLite caches + numpy buffers. Leave headroom on the 4-core node for the
kubelet/system.)

### Step 4 — Apply and move the pod (the one downtime event)

```bash
kubectl -n beta apply -f deploy/k8s/deployment.yaml
kubectl -n beta rollout status deployment/dragontts --timeout=180s
```

What happens: `Recreate` terminates the old pod on `beta-pool` (releasing the
PVC), the scheduler places the new pod on the `dragontts-pool` node in `$ZONE`,
the PVC **detaches from beta-pool and re-attaches to dragontts-pool**. Expect
**~30 s to ~2 min** of downtime during the detach/attach.

**Verify the move:**
```bash
# pod is now on a dragontts-pool node
kubectl -n beta get pod -l app=dragontts -o wide
NODE=$(kubectl -n beta get pod -l app=dragontts -o jsonpath='{.items[0].spec.nodeName}')
kubectl get node "$NODE" -o jsonpath='{.metadata.labels.cloud\.google\.com/gke-nodepool}'; echo
#   expect: dragontts-pool

# PVC reattached, filesystem now sees 250 Gi, cache data intact
kubectl -n beta exec deployment/dragontts -- df -h /app/data
kubectl -n beta exec deployment/dragontts -- sh -c 'du -sh /app/data /app/data/blobs /app/data/dragontts.db'
```

### Step 5 — Cache sanity check (port-forward + curl)

```bash
kubectl -n beta port-forward deployment/dragontts 18000:8000 &   # bg, or in another tab
curl -s http://localhost:18000/health
curl -s http://localhost:18000/stats | jq '{entries, total_bytes, providers:.providers_configured}'
#   expect: same entries + total_bytes as before the move (cache fully intact)
```

If `entries`/`total_bytes` match the pre-move numbers, the migration succeeded
with **zero cache loss**.

---

## 7. Consolidated verification checklist

- [ ] `kubectl get nodes -l cloud.google.com/gke-nodepool=dragontts-pool` → 1 Ready node in `$ZONE`
- [ ] `kubectl -n beta get pvc dragontts-data` → CAPACITY 250 Gi, Bound
- [ ] `kubectl -n beta get pod -l app=dragontts -o wide` → NODE label = `dragontts-pool`
- [ ] `kubectl -n beta exec deployment/dragontts -- df -h /app/data` → ~250 Gi
- [ ] `curl /stats` → entries + total_bytes unchanged (cache intact)
- [ ] `curl /health` → ok
- [ ] Clairvoyance + redis still healthy on `beta-pool`:
      `kubectl -n beta get pods -o wide` (their NODEs unchanged)

---

## 8. Rollback (if anything looks wrong)

The cache PVC is the **same** PVC throughout — it follows the pod wherever it
goes, so rolling back costs you nothing in data. To send DragonTTS back to
`beta-pool`:

```bash
# revert nodeSelector live (fast), then fix deployment.yaml to match
kubectl -n beta patch deployment dragontts --type=merge \
  -p '{"spec":{"template":{"spec":{"nodeSelector":{"cloud.google.com/gke-nodepool":"beta-pool"}}}}}'
kubectl -n beta rollout status deployment/dragontts --timeout=180s
```

- The pod moves back to `beta-pool`; the 250 Gi PVC re-attaches there (the
  expansion is permanent — you can't shrink, but that's fine).
- Revert the `resources` block in `deployment.yaml` too if you want it 1:1.
- Delete `dragontts-pool` only after confirming DragonTTS is healthy on
  `beta-pool`: `gcloud container node-pools delete dragontts-pool --cluster breeze-automatic-mum-01 --region asia-south1`

---

## 9. Risks & mitigations

| Risk | Mitigation |
|---|---|
| **Wrong zone** → pod `Pending` (zonal PVC can't attach cross-zone) | §5.4 finds the zone; §Step 1 pins `--node-locations` to it. If still Pending, check `kubectl describe pod` events for `Multi-Attach`/`node(s) had volume node affinity`. |
| **Deleting the PVC/PV** → `reclaimPolicy: Delete` wipes the disk + all cache | Never delete PVC/PV. Only `patch`/`apply` to resize. |
| **`nodeSelector` before pool exists** → pod `Pending` | Pool is created + Ready in Step 1 *before* the selector changes in Step 4. |
| **Downtime during move** (single replica + Recreate + PD detach/attach) | Expected ~30 s–2 min, one-time. Acceptable for this maintenance. |
| **`beta-pool` autoscaling away** under clairvoyance/redis | §5.5 confirms it's off; if it were on, set `--min-nodes 1`. |
| **Cost surprise** | §2 table; verify in pricing calculator before Step 1. |

---

## 10. Post-migration: what to watch

1. **Latency (the whole point).** Over the next 1–2 days compare to pre-move:
   ```bash
   kubectl -n beta port-forward deployment/dragontts 18000:8000 &
   curl -s 'http://localhost:18000/stats?from=2026-07-15&to=2026-07-16' \
     | jq '.latency | map_values({avg_ms:(.avg_us/1000), p95_ms:(.p95_us/1000), count})'
   ```
   Expect `ttfb` and `total` to drop vs the shared-node baseline.
2. **E2 CPU variability.** `e2-standard-4` still has *variable/burst* vCPUs. If
   synth latency is still spiky after isolation, the next lever is a
   **deterministic** machine type — recreate the pool as **`n2-standard-4`**
   (~$140/mo) instead. E2 throttling under sustained load is a known latency
   culprit; N2 removes it.
3. **Cache growth vs 250 Gi.** Weekly:
   ```bash
   kubectl -n beta exec deployment/dragontts -- sh -c 'du -sh /app/data; df -h /app/data'
   ```
   250 Gi is months of headroom at current growth; revisit if it crosses ~70%.
4. **CPU utilization.** If DragonTTS rarely uses >2 cores, you can drop the limit
   back (and consider `e2-standard-2` for the dedicated pool). If it's pegged at
   3000m, raise it (headroom to ~3800m allocatable).

---

## Appendix — quick command reference

```bash
# create dedicated pool (in PVC zone)
gcloud container node-pools create dragontts-pool --cluster breeze-automatic-mum-01 \
  --region asia-south1 --node-locations "$ZONE" --machine-type e2-standard-4 \
  --disk-type pd-balanced --disk-size 100 --image-type COS_CONTAINERD \
  --scopes gke-default --num-nodes 1

# expand PVC
kubectl -n beta patch pvc dragontts-data --patch '{"spec":{"resources":{"requests":{"storage":"250Gi"}}}}'

# apply nodeSelector + resources change, then watch the move
kubectl -n beta apply -f deploy/k8s/deployment.yaml
kubectl -n beta rollout status deployment/dragontts --timeout=180s

# verify
kubectl -n beta get pod -l app=dragontts -o wide
kubectl -n beta exec deployment/dragontts -- df -h /app/data
curl -s http://localhost:18000/stats | jq '{entries,total_bytes}'

# rollback (live)
kubectl -n beta patch deployment dragontts --type=merge \
  -p '{"spec":{"template":{"spec":{"nodeSelector":{"cloud.google.com/gke-nodepool":"beta-pool"}}}}}'
```
