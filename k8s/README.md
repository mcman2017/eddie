# k8s: the keyless, read-only explorer

Eddie normally runs natively on the host (`127.0.0.1:8765` API, `127.0.0.1:5173` UI). These manifests run the
same keyless, read-only web pod on a cluster.

| Path | What |
|---|---|
| `base/` | Namespace (PodSecurity `restricted`), Deployment, Service, Ingress, NetworkPolicies, the data PVC. Placeholders only (`eddie.example.local`, `ghcr.io/mcman2017/eddie`). |
| `overlays/prod.example/` | What `scripts/deploy_prod.sh render --example` writes from `deploy.env.example`: the shape of a real overlay, with placeholders. |
| `overlays/prod/` | The real overlay (host, registry, kube context). **Git-ignored**; rendered from its own `deploy.env`. |

```bash
make k8s-validate                       # render + client-side validation of base/ and the example; applies nothing
scripts/deploy_prod.sh init             # k8s/overlays/prod/deploy.env from the example; then edit it
scripts/deploy_prod.sh check            # render, tools, kube context, dry run; applies nothing
scripts/set_prod_auth.sh                # (in your own terminal) the Basic Auth credential; locks the ingress
scripts/deploy_prod.sh image            # build here and push; records IMAGE_TAG
scripts/deploy_prod.sh apply            # diff, confirm, server-side apply
scripts/deploy_prod.sh sync             # ship a data release into the volume, swapped atomically
scripts/deploy_prod.sh smoke            # in-pod checks; 401 from outside while locked
scripts/unlock_public_submission.sh     # submission day: remove the wall (after `budget trip`)
```

## Design rules
- **No Ember key in any cluster, ever.** The pod runs `EDDIE_BACKEND=mock`, has no Secret references and never mounts
  the ledger. Paid calls run only from the operator CLI against the single anchored ledger.
- **Read-only data.** The pod mounts the `eddie-data` volume read-only and reads through `/data/current`, a relative
  symlink to `/data/releases/<id>/`. A sync builds the next release beside it (unchanged files hard-linked, the rest
  streamed with `tar | kubectl exec`), verifies its `MANIFEST.sha256`, and swaps the symlink atomically: no restart,
  no downtime. Every database in a release is a rollback-journal file with no sidecars, so `mode=ro` opens need nothing.
- PodSecurity `restricted`: non-root uid 10001, read-only root filesystem, no capabilities, seccomp RuntimeDefault, no
  service-account token.
- NetworkPolicy: default deny, ingress only from the ingress controller's namespace, egress DNS only.
- Basic Auth lives at the ingress while the site is private; `unlock_public_submission.sh` removes it and turns on
  public cache headers (`EDDIE_PUBLIC_CACHE=1`), which are never sent behind the wall.
