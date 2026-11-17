# AIC HPC Manager

A server-side interactive terminal application for managing Kubernetes user access, namespaces, StorageClasses, and namespace storage permissions.

## Selected design

- Textual TUI.
- Kubernetes is the access-management API/source of truth for application-managed assignments.
- Multiple StorageClasses can be allowed per namespace.
- Runs on a trusted Kubernetes login/admin node using a dedicated ServiceAccount.
- Slurm synchronization remains installed and continues managing its own RoleBindings.

## Important integration boundary with the existing Slurm synchronizer

The supplied Slurm sync script labels its RoleBindings with:

`hpc.aic.gov.eg/managed-by: slurm`

and removes stale bindings only when that label is `slurm`. AIC HPC Manager creates separate RoleBindings with:

`hpc.aic.gov.eg/managed-by: hpc-manager`

and names them with the `hpcm-` prefix. Do not change the app's ownership label to `slurm`. This separation prevents the existing sync job from deleting app-managed bindings.

The application displays user subjects found in namespace RoleBindings. It can remove only its own `hpc-manager` RoleBindings. Slurm-owned RoleBindings are visible but intentionally read-only in this app. If an administrator needs to change a Slurm-managed assignment, change the Slurm association source and run the existing synchronizer; otherwise the next sync can restore the binding.

## Storage permission model

A namespace stores its allowed StorageClasses in this annotation:

`hpc.aic.gov.eg/allowed-storageclasses: '["nfs-team-a","nfs-shared"]'`

The app also adds this label:

`hpc.aic.gov.eg/storage-policy: enforced`

Gatekeeper matches namespaces carrying that label and rejects PVCs whose `spec.storageClassName` is not in the annotation array. A missing `storageClassName` is rejected too. This avoids an implicit default StorageClass bypass.

The app validates that every assigned StorageClass exists before patching a namespace. The Gatekeeper inventory must sync Namespace objects. Apply the Gatekeeper config and policy files in `k8s/` and `policies/` before enabling enforcement.

## Requirements

- Python 3.10+ (Python 3.12 recommended).
- Kubernetes cluster access from the host where the TUI runs.
- Gatekeeper installed.
- Existing ClusterRoles `slurm-project-user` and `slurm-project-admin`, or update the names in `/etc/aic-hpc-manager/config.yaml` and the ServiceAccount RBAC manifest to your actual roles.
- Namespace names follow Kubernetes DNS-1123 rules.

## Recommended server deployment

The application is deployed as a long-running Pod with a dedicated ServiceAccount. Administrators use `kubectl exec -it` from the Kubernetes login/admin node to open the TUI. This keeps the ServiceAccount credential inside Kubernetes and avoids copying a privileged token to a shared login node.

See [k8s/README-deploy.md](k8s/README-deploy.md). The default manifests use your internal registry path `docker.aic.gov.eg/hpc/aic-hpc-manager:0.1.0`; change it if your registry repository differs.

For direct process execution on a host instead, set `kubernetes.in_cluster: false` and supply a dedicated kubeconfig. Do not use an administrator's personal kubeconfig as the application's permanent identity.

## Deploy Kubernetes RBAC

Edit `k8s/serviceaccount-rbac.yaml` if your ClusterRole names differ. Then:

```bash
kubectl apply -f k8s/serviceaccount-rbac.yaml
```

For a server-side app running on a node outside the cluster, use a short-lived projected token or a securely rotated ServiceAccount token mechanism supported by your cluster. Do not create a long-lived token Secret and leave it on a shared login node. The provided manifest is for the in-cluster ServiceAccount identity; the current config defaults to in-cluster authentication. If running directly on the login node, use a dedicated kubeconfig with a narrowly scoped identity and set `kubernetes.in_cluster: false`.

## Configure the app

Copy `config.yaml.example` to `/etc/aic-hpc-manager/config.yaml` and verify:

- `kubernetes.in_cluster`: true only when the app runs in a Pod; false for a login-node process using kubeconfig.
- `rbac.user_cluster_role` and `rbac.admin_cluster_role` match existing ClusterRoles.
- `namespace_filter_label` matches the labels on your managed team namespaces.
- audit log path is writable by the application operator.

Run:

```bash
.venv/bin/python -m app.main --config /etc/aic-hpc-manager/config.yaml
```

## Deploy Gatekeeper policy for multiple StorageClasses

First enable Namespace inventory sync. If `gatekeeper-system/config` already exists, inspect it and merge the Namespace entry into its existing `spec.sync.syncOnly` list; do not blindly replace existing sync configuration:

```bash
kubectl get config config -n gatekeeper-system -o yaml
# Merge the Namespace entry from k8s/namespace-inventory-config.yaml into the existing Config.
kubectl apply -f k8s/namespace-inventory-config.yaml
```

Then apply the template and constraint:

```bash
kubectl apply -f policies/namespace-storageclass-template.yaml
kubectl wait --for=condition=established crd/k8snamespaceallowedstorageclasses.constraints.gatekeeper.sh --timeout=90s
kubectl apply -f policies/namespace-storageclass-constraint.yaml
```

The constraint applies only to namespaces labelled `hpc.aic.gov.eg/storage-policy: enforced`. The application adds that label when saving storage permissions. Start with a test namespace and test PVC before enabling this on production team namespaces.

Example annotation managed by the app:

```yaml
metadata:
  annotations:
    hpc.aic.gov.eg/allowed-storageclasses: '["nfs-ibn-al-haytham","nfs-shared"]'
  labels:
    hpc.aic.gov.eg/storage-policy: enforced
```

## TUI usage

- **Dashboard**: counts namespaces, users, and StorageClasses.
- **Users**: displays Kubernetes User subjects and namespace assignments from RoleBindings. Use the search field to filter the current table by any displayed value.
- **Namespaces**: displays managed namespace inventory and currently allowed StorageClasses.
- **StorageClasses**: displays provisioner, reclaim policy, binding mode, and expansion setting.
- **Storage Permissions**: choose a namespace, load its current policy, enter comma-separated StorageClass names, and save.
- **Persistent Volumes**: displays PV, status, PVC claim, and StorageClass.
- **Search**: the search field filters the currently displayed table case-insensitively across all columns. Clear it to restore the full view.
- **Assign user**: enter the exact Kubernetes authentication username (for OIDC this is normally the `preferred_username` claim), choose user/admin, and either use the selected namespace or enter multiple comma-separated namespace names. All namespace names are validated against the managed namespace inventory before any assignment starts. Each namespace receives its own RoleBinding. If an API operation fails for one namespace, the app reports successful and failed namespaces separately.
- **Remove selected user**: removes the selected user's `hpc-manager` RoleBinding in the selected namespace only. It will not delete Slurm-owned bindings.

Use `r` to refresh and `q` to quit.

## Security notes

1. The ServiceAccount can patch namespace metadata, create/delete RoleBindings, read StorageClasses/PVs/PVCs, and bind only the configured project ClusterRoles. Review the role names in the RBAC manifest before applying.
2. Only trusted HPC administrators should be able to run the TUI or use its kubeconfig/ServiceAccount identity.
3. Restrict who can patch namespace labels/annotations, especially `hpc.aic.gov.eg/storage-policy` and `hpc.aic.gov.eg/allowed-storageclasses`. Otherwise a namespace owner could change the policy selector or allowed list.
4. Gatekeeper restricts PVC StorageClass selection; it does not secure NFS data by itself. Enforce UID/GID and export/filesystem permissions on FlashBlade.
5. Existing PVCs are not automatically migrated when a namespace's allowed StorageClasses change. The policy affects admission of new PVC requests and PVC updates that are evaluated by Gatekeeper.
6. Changing namespace storage permissions does not change existing PVs or mounted volumes.
7. `Retain` protects backend data from automatic reclamation; it is not a backup. Keep Pure snapshots/backup policies.
8. The UI currently manages direct Kubernetes RoleBindings. It does not edit FreeIPA or Slurm associations.

## Existing Slurm synchronizer

No changes are required to its ownership labels. Keep:

```python
MANAGED_BY_VALUE = "slurm"
```

AIC HPC Manager uses a distinct owner value (`hpc-manager`). This is intentional. The synchronizer can continue creating and pruning its own RoleBindings without touching application-owned bindings.

## Operational limitations in this initial version

- The TUI is an administrator tool, not a multi-user web service.
- Namespace creation/deletion and StorageClass creation/deletion are intentionally not included; those operations have larger blast radii and should remain platform-admin workflows.
- User identity must exactly match the username Kubernetes sees after OIDC authentication.
- The application does not infer Slurm group membership or edit Slurm accounting records.
- The app uses the configured existing ClusterRoles for access grants. Review their permissions before using the admin assignment option.
