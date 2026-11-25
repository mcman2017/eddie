# k8s: keyless read-only web pod (not applied yet)

These manifests are a skeleton. They are rendered and validated client-side only:

```bash
kubectl kustomize k8s | kubectl apply --dry-run=client -f -
```

## Design rules
- **No Ember key in the cluster, ever.** The pod runs `EDDIE_BACKEND=mock` and has no Secret references. Paid calls run only from the operator CLI on the host against the single anchored ledger, so nothing reachable from the internet can spend the contest budget.
- The pod serves precomputed artifacts (index, atlas, telemetry snapshot) **read-only**.
- PodSecurity `restricted`: non-root uid 10001, read-only root filesystem, drop ALL capabilities, seccomp RuntimeDefault, no service-account token.
- NetworkPolicy is default deny, with ingress only from ingress-nginx and egress DNS only.

## Before the first deploy (a later night, each step needs an explicit OK)
1. Build and push `registry.theclamlife.com/eddie/eddie:<tag>` (arm64, built natively on the DGX).
2. Copy the registry pull secret `registry-secret` into namespace `eddie`.
3. Decide how the data reaches the pod: bake a compact atlas JSON into the image, or use a PVC. The full 1.2 GB index is a host artifact.
4. Pick the public hostname. `eddie.theclamlife.com` is a placeholder. Check DNS.
5. Verify the NetworkPolicy against how ingress-nginx and kubelet probes are sourced on this cluster.
6. Then `kubectl apply -k k8s/`.
