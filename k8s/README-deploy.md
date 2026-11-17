# Deploy the terminal application in Kubernetes

This is the recommended server-side deployment for the selected ServiceAccount model. The TUI runs in a long-lived pod with in-cluster authentication. Administrators connect from the login node using `kubectl exec -it`; no ServiceAccount token needs to be copied onto the login node.

1. Build and push the image to your internal registry:

```bash
docker build -t docker.aic.gov.eg/hpc/aic-hpc-manager:0.1.0 .
docker push docker.aic.gov.eg/hpc/aic-hpc-manager:0.1.0
```

2. Review `serviceaccount-rbac.yaml`, especially the two ClusterRole names, then apply:

```bash
kubectl apply -f k8s/serviceaccount-rbac.yaml
kubectl apply -f k8s/app-configmap.yaml
kubectl apply -f k8s/deployment.yaml
kubectl -n aic-hpc-manager rollout status deployment/aic-hpc-manager
```

3. Open an interactive terminal from the login/admin node:

```bash
kubectl exec -it -n aic-hpc-manager deployment/aic-hpc-manager -- \
  python -m app.main --config /etc/aic-hpc-manager/config.yaml
```

The deployment uses `emptyDir` for the audit file and temporary files. For production retention, ship container logs to your central logging stack or replace the audit `emptyDir` with a dedicated PVC and assign an approved StorageClass to the `aic-hpc-manager` namespace. The app also prints each audit event as JSON to stdout.

The pod is not exposed through a Service or Ingress. Access is through Kubernetes RBAC and `kubectl exec` from authorized administrators.
