from __future__ import annotations

import json
import re
import hashlib
from typing import Any

from kubernetes import client, config
from kubernetes.client import ApiException

from app.config import Settings
from app.services.audit import AuditLogger


class KubeService:
    def __init__(self, settings: Settings, audit: AuditLogger) -> None:
        self.settings = settings
        self.audit = audit
        kube = settings.section("kubernetes")
        if kube.get("in_cluster", True):
            config.load_incluster_config()
        else:
            kwargs: dict[str, Any] = {}
            if kube.get("kubeconfig"):
                kwargs["config_file"] = kube["kubeconfig"]
            if kube.get("context"):
                kwargs["context"] = kube["context"]
            config.load_kube_config(**kwargs)

        timeout = int(kube.get("request_timeout_seconds", 15))
        self.core = client.CoreV1Api()
        self.rbac = client.RbacAuthorizationV1Api()
        self.storage = client.StorageV1Api()
        self.custom = client.CustomObjectsApi()
        self.timeout = timeout
        self.rbac_cfg = settings.section("rbac")
        self.storage_cfg = settings.section("storage")

    @staticmethod
    def _safe_name(value: str) -> str:
        base = re.sub(r"[^a-z0-9-]", "-", value.lower()).strip("-")[:28] or "user"
        suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
        return f"{base}-{suffix}"

    def namespaces(self) -> list[dict[str, Any]]:
        selector = self.settings.get("namespace_filter_label", "")
        result = self.core.list_namespace(label_selector=selector or None, _request_timeout=self.timeout)
        rows = []
        for ns in result.items:
            labels = ns.metadata.labels or {}
            annotations = ns.metadata.annotations or {}
            raw = annotations.get(self.storage_cfg.get("allowed_classes_annotation", "hpc.aic.gov.eg/allowed-storageclasses"), "[]")
            try:
                allowed = json.loads(raw)
                if not isinstance(allowed, list) or not all(isinstance(x, str) for x in allowed):
                    allowed = []
            except (TypeError, json.JSONDecodeError):
                allowed = []
            rows.append({
                "name": ns.metadata.name,
                "status": ns.status.phase if ns.status else "Unknown",
                "account": labels.get("hpc.aic.gov.eg/slurm-account", ""),
                "managed_by": labels.get(self.rbac_cfg.get("namespace_managed_by_label", "hpc.aic.gov.eg/managed-by"), ""),
                "storage_classes": sorted(set(allowed)),
            })
        return sorted(rows, key=lambda x: x["name"])

    def storage_classes(self) -> list[dict[str, Any]]:
        result = self.storage.list_storage_class(_request_timeout=self.timeout)
        rows = []
        for sc in result.items:
            rows.append({
                "name": sc.metadata.name,
                "provisioner": sc.provisioner,
                "reclaim_policy": sc.reclaim_policy or "Delete (default)",
                "binding_mode": sc.volume_binding_mode or "Immediate (default)",
                "allow_expansion": bool(sc.allow_volume_expansion),
                "parameters": sc.parameters or {},
                "mount_options": sc.mount_options or [],
            })
        return sorted(rows, key=lambda x: x["name"])

    def users_for_namespace(self, namespace: str) -> list[dict[str, str]]:
        result = self.rbac.list_namespaced_role_binding(namespace, _request_timeout=self.timeout)
        rows: list[dict[str, str]] = []
        owner_label = self.rbac_cfg.get("managed_by_label", "hpc.aic.gov.eg/managed-by")
        app_owner = self.rbac_cfg.get("managed_by_value", "hpc-manager")
        admin_role = self.rbac_cfg.get("admin_cluster_role", "slurm-project-admin")
        for binding in result.items:
            labels = binding.metadata.labels or {}
            owner = labels.get(owner_label, "unmanaged")
            for subject in binding.subjects or []:
                if subject.kind == "User":
                    role = "admin" if binding.role_ref and binding.role_ref.name == admin_role else "user"
                    rows.append({
                        "username": subject.name,
                        "role": role,
                        "binding": binding.metadata.name,
                        "owner": owner,
                        "can_manage": "yes" if owner == app_owner else "no",
                    })
        return sorted(rows, key=lambda x: (x["username"], x["binding"]))

    def all_users(self) -> list[dict[str, Any]]:
        users: dict[str, dict[str, Any]] = {}
        for ns in self.namespaces():
            try:
                assignments = self.users_for_namespace(ns["name"])
            except ApiException as exc:
                if exc.status == 404:
                    continue
                raise
            for assignment in assignments:
                user = users.setdefault(assignment["username"], {"username": assignment["username"], "namespaces": []})
                user["namespaces"].append(f"{ns['name']} ({assignment['role']}, {assignment['owner']})")
        return sorted(users.values(), key=lambda x: x["username"])

    def assign_user(self, namespace: str, username: str, role: str) -> None:
        if role not in {"user", "admin"}:
            raise ValueError("Role must be 'user' or 'admin'.")
        self.core.read_namespace(namespace, _request_timeout=self.timeout)
        role_name = self.rbac_cfg.get("admin_cluster_role" if role == "admin" else "user_cluster_role")
        # Verify the target ClusterRole exists before attempting to bind it.
        self.rbac.read_cluster_role(role_name, _request_timeout=self.timeout)
        prefix = self.rbac_cfg.get("rolebinding_prefix", "hpcm")
        binding_name = f"{prefix}-{self._safe_name(username)}"
        labels = {
            self.rbac_cfg.get("managed_by_label", "hpc.aic.gov.eg/managed-by"): self.rbac_cfg.get("managed_by_value", "hpc-manager"),
            self.rbac_cfg.get("user_label", "hpc.aic.gov.eg/user"): self._safe_name(username),
            self.rbac_cfg.get("role_label", "hpc.aic.gov.eg/role"): role,
        }
        body = client.V1RoleBinding(
            metadata=client.V1ObjectMeta(name=binding_name, labels=labels),
            subjects=[
               client.RbacV1Subject(
               kind="User",
               api_group="rbac.authorization.k8s.io",
               name=username,
             )
            ],    
            role_ref=client.V1RoleRef(api_group="rbac.authorization.k8s.io", kind="ClusterRole", name=role_name),
        )
        try:
            existing = self.rbac.read_namespaced_role_binding(binding_name, namespace, _request_timeout=self.timeout)
            existing_labels = existing.metadata.labels or {}
            if existing_labels.get(self.rbac_cfg.get("managed_by_label", "hpc.aic.gov.eg/managed-by")) != self.rbac_cfg.get("managed_by_value", "hpc-manager"):
                raise PermissionError(f"RoleBinding '{binding_name}' already exists and is not owned by AIC HPC Manager.")
            if existing.role_ref.name != role_name:
                self.rbac.delete_namespaced_role_binding(binding_name, namespace, _request_timeout=self.timeout)
                self.rbac.create_namespaced_role_binding(namespace, body, _request_timeout=self.timeout)
            else:
                body.metadata.resource_version = existing.metadata.resource_version
                self.rbac.replace_namespaced_role_binding(binding_name, namespace, body, _request_timeout=self.timeout)
        except ApiException as exc:
            if exc.status != 404:
                raise
            self.rbac.create_namespaced_role_binding(namespace, body, _request_timeout=self.timeout)
        self.audit.record("assign_user", f"{username}@{namespace}", {"role": role, "cluster_role": role_name})

    def remove_user(self, namespace: str, username: str) -> None:
        prefix = self.rbac_cfg.get("rolebinding_prefix", "hpcm")
        binding_name = f"{prefix}-{self._safe_name(username)}"
        existing = self.rbac.read_namespaced_role_binding(binding_name, namespace, _request_timeout=self.timeout)
        labels = existing.metadata.labels or {}
        if labels.get(self.rbac_cfg.get("managed_by_label", "hpc.aic.gov.eg/managed-by")) != self.rbac_cfg.get("managed_by_value", "hpc-manager"):
            raise PermissionError("Refusing to delete a RoleBinding not owned by AIC HPC Manager.")
        self.rbac.delete_namespaced_role_binding(binding_name, namespace, _request_timeout=self.timeout)
        self.audit.record("remove_user", f"{username}@{namespace}")

    def set_namespace_storage_classes(self, namespace: str, classes: list[str]) -> None:
        self.core.read_namespace(namespace, _request_timeout=self.timeout)
        available = {item["name"] for item in self.storage_classes()}
        unknown = sorted(set(classes) - available)
        if unknown:
            raise ValueError(f"Unknown StorageClass(es): {', '.join(unknown)}")
        annotation_key = self.storage_cfg.get("allowed_classes_annotation", "hpc.aic.gov.eg/allowed-storageclasses")
        label_key = self.storage_cfg.get("enforcement_label", "hpc.aic.gov.eg/storage-policy")
        label_value = self.storage_cfg.get("enforcement_label_value", "enforced")
        patch = {
            "metadata": {
                "annotations": {annotation_key: json.dumps(sorted(set(classes)))},
                "labels": {label_key: label_value},
            }
        }
        self.core.patch_namespace(namespace, patch, _request_timeout=self.timeout)
        self.audit.record("set_namespace_storage_classes", namespace, {"allowed_storage_classes": sorted(set(classes))})

    def pvc_summary(self, namespace: str) -> list[dict[str, str]]:
        result = self.core.list_namespaced_persistent_volume_claim(namespace, _request_timeout=self.timeout)
        return [{"name": pvc.metadata.name, "status": pvc.status.phase if pvc.status else "Unknown", "storage_class": pvc.spec.storage_class_name or "(default)", "capacity": (pvc.status.capacity or {}).get("storage", "Pending") if pvc.status else "Pending"} for pvc in result.items]

    def pv_summary(self) -> list[dict[str, str]]:
        result = self.core.list_persistent_volume(_request_timeout=self.timeout)
        return [{"name": pv.metadata.name, "status": pv.status.phase if pv.status else "Unknown", "claim": f"{pv.spec.claim_ref.namespace}/{pv.spec.claim_ref.name}" if pv.spec.claim_ref else "-", "storage_class": pv.spec.storage_class_name or "-"} for pv in result.items]
