# k8s: keyless read-only web pod (optional; not applied)

Eddie normally runs natively on the host (`127.0.0.1:8765` API, `127.0.0.1:5173` UI) and is reached over an SSH port-forward. These manifests are a portable skeleton for running the same keyless, read-only web pod on any cluster. They are rendered and validated client-side only:

```bash
kubectl kustomize k8s | kubectl apply --dry-run=client -f -
```

## Design rules
- **No Ember key in any cluster, ever.** The pod runs `EDDIE_BACKEND=mock` and has no Secret references. Paid calls run only from the operator CLI against the single anchored ledger, so nothing reachable over the network can spend the contest budget.
- The pod serves precomputed artifacts (index, atlas, telemetry snapshot) **read-only**.
- PodSecurity `restricted`: non-root uid 10001, read-only root filesystem, drop ALL capabilities, seccomp RuntimeDefault, no service-account token.
- NetworkPolicy is default deny, with ingress only from the ingress controller's namespace and egress DNS only.

## Adapting it
1. Build and push the image (`ghcr.io/mcman2017/eddie:latest` is the placeholder name), or point `images:` in `kustomization.yaml` at your registry.
2. Decide how the data reaches the pod: bake a compact atlas JSON into the image, or use a PVC.
3. Replace the placeholder host `eddie.example.local`. Add your TLS issuer annotation and `tls` block (see the comment in `ingress.yaml`).
4. Check that the NetworkPolicy matches how your ingress controller and kubelet probes reach pods.
