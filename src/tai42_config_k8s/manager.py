"""Kubernetes-based configuration manager.

Implements :class:`~tai42_contract.config.manager.ConfigManager` for the ``k8s``
config mode.  Reads and writes environment configuration via K8s Secrets and
manifest configuration via K8s ConfigMaps.

Requires the ``kubernetes`` package (install with ``pip install tai42-config-k8s[k8s]``).

The module exposes a :func:`build_config_manager` factory — the selection
convention every config provider follows. The skeleton's config factory loads
this provider by dynamic import (its ``k8s`` map entry is the string
``"tai42_config_k8s.manager"``), so there is no static skeleton dependency on this
plugin and no plugin dependency on the skeleton.
"""

from __future__ import annotations

import base64
import copy
import logging
from functools import cached_property
from typing import TYPE_CHECKING, Any, cast

import yaml
from pyaml_env import parse_config
from ruamel.yaml.comments import CommentedMap
from tai42_contract.config.manager import ConfigManager
from tai42_kit.utils.data import load_manifest, merge_and_dump_manifest

from tai42_config_k8s.settings import K8sConfigSettings, k8s_config_settings

if TYPE_CHECKING:
    from collections.abc import Callable

    from kubernetes.client import CoreV1Api, V1ConfigMap, V1Secret

logger = logging.getLogger(__name__)

# A manifest read-modify-write patches the ConfigMap under an optimistic-concurrency
# resourceVersion precondition; a concurrent writer makes the patch 409 and the
# operation re-reads and re-runs on the fresh document. This bounds those retries so
# a pathological conflict storm fails loudly instead of looping forever.
_MAX_CONFLICT_ATTEMPTS = 5


class K8sConfigError(Exception):
    """Raised when a Kubernetes API operation fails."""


class _ConfigMapConflict(Exception):
    """Internal signal that a ConfigMap patch hit a 409 resourceVersion conflict.

    Raised by the precondition patch helper and consumed by the retry loop; it
    never escapes the manager.
    """


class K8sConfigManager(ConfigManager):
    """Config backend that reads/writes K8s Secrets (env) and ConfigMaps (manifest).

    Raises:
        ImportError: If the ``kubernetes`` package is not installed.
    """

    def __init__(self) -> None:
        from tai42_config_k8s._kubernetes_optional import require_kubernetes

        require_kubernetes()

        self._settings: K8sConfigSettings = k8s_config_settings()

    # -- Internal helpers ----------------------------------------------------

    @cached_property
    def _core_api(self) -> CoreV1Api:
        """Return a lazily-built :class:`kubernetes.client.CoreV1Api`.

        The client is built on first access and cached for the lifetime of
        this manager instance, so each manager loads cluster config and
        constructs its API client at most once.
        """
        from kubernetes import client
        from kubernetes import config as k8s_config

        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException as exc:
            logger.info("in-cluster config unavailable (%s); falling back to kubeconfig", exc)
            k8s_config.load_kube_config()
        return client.CoreV1Api()

    # -- Environment configuration (Secret) ----------------------------------

    def read_env(self) -> dict[str, str]:
        """Read env config key-value pairs from a K8s Secret."""
        from kubernetes.client.exceptions import ApiException

        api = self._core_api
        try:
            secret = cast(
                "V1Secret",
                api.read_namespaced_secret(self._settings.secret_name, self._settings.namespace),
            )
        except ApiException as exc:
            if exc.status == 404:
                raise FileNotFoundError(
                    f"Secret '{self._settings.secret_name}' not found in namespace '{self._settings.namespace}'"
                ) from exc
            raise K8sConfigError(f"Failed to read Secret: {exc.reason}") from exc

        if not secret.data:
            return {}
        # validate=True makes a corrupt Secret value (non-base64 characters) raise
        # loudly instead of being silently dropped from the decoded payload.
        return {k: base64.b64decode(v, validate=True).decode("utf-8") for k, v in secret.data.items()}

    def write_env(self, config: dict[str, str]) -> None:
        """Patch a K8s Secret with env config key-value pairs.

        Uses ``string_data`` so K8s handles base64 encoding.
        Merges with existing keys; filters out empty values.

        Concurrency: each entry is an independent Secret key, so the
        server-side strategic-merge patch keeps concurrent writers to
        different keys independent (same key is last-writer-wins). This
        differs from ``write_manifest``, which needs a ``resourceVersion``
        precondition because it rewrites a single ConfigMap key as one blob.
        """
        from kubernetes import client
        from kubernetes.client.exceptions import ApiException

        api = self._core_api

        # Read existing secret to merge
        try:
            existing = cast(
                "V1Secret",
                api.read_namespaced_secret(self._settings.secret_name, self._settings.namespace),
            )
        except ApiException as exc:
            if exc.status == 404:
                raise K8sConfigError(
                    f"Secret '{self._settings.secret_name}' not found "
                    f"in namespace '{self._settings.namespace}'. "
                    "Create it before writing configuration."
                ) from exc
            raise K8sConfigError(f"Failed to read Secret: {exc.reason}") from exc

        existing_data: dict[str, str] = {}
        if existing.data:
            # validate=True: a corrupt existing Secret value must abort the write
            # loudly, never be silently mangled and re-written.
            existing_data = {k: base64.b64decode(v, validate=True).decode("utf-8") for k, v in existing.data.items()}

        preserved = {k: v for k, v in existing_data.items() if k not in config}
        merged = {**config, **preserved}
        filtered = {k: v for k, v in merged.items() if v != ""}

        body = client.V1Secret(
            string_data=filtered,
            metadata=client.V1ObjectMeta(name=self._settings.secret_name),
        )
        try:
            api.patch_namespaced_secret(
                self._settings.secret_name,
                self._settings.namespace,
                body,
            )
        except ApiException as exc:
            if exc.status == 403:
                raise K8sConfigError(
                    f"Permission denied updating Secret '{self._settings.secret_name}': {exc.reason}"
                ) from exc
            raise K8sConfigError(f"Failed to update Secret: {exc.reason}") from exc

        logger.info(
            "Updated K8s Secret '%s' in namespace '%s'",
            self._settings.secret_name,
            self._settings.namespace,
        )

    # -- Manifest configuration (ConfigMap) ----------------------------------

    def read_manifest(self) -> dict[str, Any]:
        """Read manifest YAML from a K8s ConfigMap key."""
        from kubernetes.client.exceptions import ApiException

        api = self._core_api
        try:
            cm = cast(
                "V1ConfigMap",
                api.read_namespaced_config_map(self._settings.configmap_name, self._settings.namespace),
            )
        except ApiException as exc:
            if exc.status == 404:
                raise FileNotFoundError(
                    f"ConfigMap '{self._settings.configmap_name}' not found in namespace '{self._settings.namespace}'"
                ) from exc
            raise K8sConfigError(f"Failed to read ConfigMap: {exc.reason}") from exc

        if not cm.data or self._settings.manifest_key not in cm.data:
            raise FileNotFoundError(
                f"Manifest key '{self._settings.manifest_key}' not found in ConfigMap '{self._settings.configmap_name}'"
            )

        parsed = parse_config(data=cm.data[self._settings.manifest_key]) or {}
        if not isinstance(parsed, dict):
            # Valid YAML that isn't a mapping (a list or bare scalar) can't be
            # returned through the dict[str, Any] contract — fail loudly.
            raise K8sConfigError(
                f"Manifest key '{self._settings.manifest_key}' in ConfigMap "
                f"'{self._settings.configmap_name}' parsed to a {type(parsed).__name__}, "
                "expected a mapping"
            )
        return parsed

    def read_manifest_preserved(self) -> dict[str, Any]:
        """Read manifest YAML from a K8s ConfigMap key with ``!ENV`` tags preserved.

        Loads the same ConfigMap key as :meth:`read_manifest`, but with the
        tag-preserving loader so each ``!ENV <expr>`` node is kept as its
        literal ``"!ENV <expr>"`` marker string rather than resolved to its
        environment value.
        """
        from kubernetes.client.exceptions import ApiException

        api = self._core_api
        try:
            cm = cast(
                "V1ConfigMap",
                api.read_namespaced_config_map(self._settings.configmap_name, self._settings.namespace),
            )
        except ApiException as exc:
            if exc.status == 404:
                raise FileNotFoundError(
                    f"ConfigMap '{self._settings.configmap_name}' not found in namespace '{self._settings.namespace}'"
                ) from exc
            raise K8sConfigError(f"Failed to read ConfigMap: {exc.reason}") from exc

        if not cm.data or self._settings.manifest_key not in cm.data:
            raise FileNotFoundError(
                f"Manifest key '{self._settings.manifest_key}' not found in ConfigMap '{self._settings.configmap_name}'"
            )

        return self._load_manifest_document(cm.data[self._settings.manifest_key])

    def read_defaults_manifest(self) -> dict[str, Any]:
        """Read defaults manifest YAML from a K8s ConfigMap key."""
        from kubernetes.client.exceptions import ApiException

        api = self._core_api
        try:
            cm = cast(
                "V1ConfigMap",
                api.read_namespaced_config_map(self._settings.configmap_name, self._settings.namespace),
            )
        except ApiException as exc:
            if exc.status == 404:
                return {}
            raise K8sConfigError(f"Failed to read ConfigMap: {exc.reason}") from exc

        return self._parse_defaults(cm.data)

    def _parse_defaults(self, data: dict[str, str] | None) -> dict[str, Any]:
        """Parse the defaults-manifest key out of ConfigMap ``data`` (empty if absent).

        A malformed defaults manifest is a loud error, never a silent empty config.
        """
        if not data or self._settings.defaults_manifest_key not in data:
            return {}
        try:
            parsed = parse_config(data=data[self._settings.defaults_manifest_key]) or {}
        except yaml.YAMLError:
            logger.error(
                "Error parsing defaults manifest YAML from ConfigMap '%s' key '%s'",
                self._settings.configmap_name,
                self._settings.defaults_manifest_key,
                exc_info=True,
            )
            raise
        if not isinstance(parsed, dict):
            # Valid YAML that isn't a mapping (a list or bare scalar) can't be
            # returned through the dict[str, Any] contract — fail loudly.
            raise K8sConfigError(
                f"Defaults manifest key '{self._settings.defaults_manifest_key}' in ConfigMap "
                f"'{self._settings.configmap_name}' parsed to a {type(parsed).__name__}, "
                "expected a mapping"
            )
        return parsed

    def _load_defaults_preserved(self, data: dict[str, str] | None) -> CommentedMap:
        """Load the defaults-manifest key as the PRESERVED view for the write merge.

        The three-way write merge backfills every default key missing from the
        working document, so the defaults must be loaded with ``!ENV`` tags kept as
        ``"!ENV <expr>"`` marker strings — a resolved secret string is not a marker
        and would be dumped into the ConfigMap as plaintext. Mirrors
        :meth:`read_defaults_manifest`'s loud-error contract: an absent key yields
        an empty document; a malformed one raises loudly; a non-mapping one raises,
        naming the ConfigMap and the defaults key. The runtime/expanded defaults
        view stays with :meth:`_parse_defaults` for ``read_defaults_manifest``.
        """
        if not data or self._settings.defaults_manifest_key not in data:
            return CommentedMap()
        try:
            return load_manifest(data[self._settings.defaults_manifest_key])
        except TypeError as exc:
            raise K8sConfigError(
                f"Defaults manifest key '{self._settings.defaults_manifest_key}' in ConfigMap "
                f"'{self._settings.configmap_name}' parsed to a non-mapping document, "
                "expected a mapping"
            ) from exc

    def _load_manifest_document(self, text: str) -> CommentedMap:
        """Round-trip-load a stored manifest string, keeping ``!ENV`` markers and comments.

        Every ``!ENV <expr>`` node becomes its literal ``"!ENV <expr>"`` marker
        string and comments/ordering survive for a later round-trip write. A
        document that is valid YAML but not a top-level mapping cannot satisfy
        the ``dict[str, Any]`` contract, and would feed the merge helper a
        non-mapping — it fails loudly, naming the key.
        """
        try:
            return load_manifest(text)
        except TypeError as exc:
            raise K8sConfigError(
                f"Manifest key '{self._settings.manifest_key}' in ConfigMap "
                f"'{self._settings.configmap_name}' parsed to a non-mapping document, "
                "expected a mapping"
            ) from exc

    def _read_configmap_for_write(self) -> V1ConfigMap:
        """Read the ConfigMap that a manifest write patches (must already exist).

        A write patches an existing ConfigMap under a ``resourceVersion``
        precondition, so an absent ConfigMap (404) is a loud error rather than a
        create.
        """
        from kubernetes.client.exceptions import ApiException

        try:
            return cast(
                "V1ConfigMap",
                self._core_api.read_namespaced_config_map(self._settings.configmap_name, self._settings.namespace),
            )
        except ApiException as exc:
            if exc.status == 404:
                raise K8sConfigError(
                    f"ConfigMap '{self._settings.configmap_name}' not found "
                    f"in namespace '{self._settings.namespace}'. "
                    "Create it before writing configuration."
                ) from exc
            raise K8sConfigError(f"Failed to read ConfigMap: {exc.reason}") from exc

    def _require_resource_version(self, existing: V1ConfigMap) -> str:
        """Return the ConfigMap ``resourceVersion``, refusing the write if it is absent.

        A ConfigMap returned by the API always carries metadata with a
        ``resourceVersion``; without it the patch body would serialize without
        the optimistic-concurrency precondition, silently allowing a lost update
        — the exact hole the precondition guards against.
        """
        if existing.metadata is None:
            raise K8sConfigError(
                f"ConfigMap '{self._settings.configmap_name}' was returned without metadata; "
                "cannot apply the optimistic-concurrency precondition"
            )
        if not existing.metadata.resource_version:
            raise K8sConfigError(
                f"ConfigMap '{self._settings.configmap_name}' was returned without a resourceVersion; "
                "cannot apply the optimistic-concurrency precondition"
            )
        return existing.metadata.resource_version

    def _manifest_patch_body(self, existing: V1ConfigMap, content: str, resource_version: str) -> V1ConfigMap:
        """Build the ConfigMap patch body carrying the manifest content and precondition.

        Other ConfigMap keys ride along unchanged; the ``resourceVersion`` on the
        metadata is the optimistic-concurrency precondition.
        """
        from kubernetes import client

        cm_data = dict(existing.data or {})
        cm_data[self._settings.manifest_key] = content
        return client.V1ConfigMap(
            data=cm_data,
            metadata=client.V1ObjectMeta(
                name=self._settings.configmap_name,
                resource_version=resource_version,
            ),
        )

    def _patch_manifest_precondition(self, existing: V1ConfigMap, content: str) -> None:
        """Patch the manifest key under the ``resourceVersion`` precondition.

        A 409 conflict is surfaced as :class:`_ConfigMapConflict` so the retry
        loop can re-read and re-run; a 403 and any other API error fail loudly.
        """
        from kubernetes.client.exceptions import ApiException

        resource_version = self._require_resource_version(existing)
        body = self._manifest_patch_body(existing, content, resource_version)
        try:
            self._core_api.patch_namespaced_config_map(
                self._settings.configmap_name,
                self._settings.namespace,
                body,
            )
        except ApiException as exc:
            if exc.status == 409:
                raise _ConfigMapConflict from exc
            if exc.status == 403:
                raise K8sConfigError(
                    f"Permission denied updating ConfigMap '{self._settings.configmap_name}': {exc.reason}"
                ) from exc
            raise K8sConfigError(f"Failed to update ConfigMap: {exc.reason}") from exc

    def _commit_with_retry(
        self, render: Callable[[V1ConfigMap, dict[str, Any]], tuple[str, dict[str, Any]]]
    ) -> dict[str, Any]:
        """Run the optimistic-concurrency retry loop for a manifest write.

        Each attempt freshly reads the ConfigMap, calls ``render`` with the read
        ConfigMap and its preserved-view defaults (``!ENV`` kept as marker strings,
        never resolved) to build the YAML content and the persisted document, then
        patches under the ``resourceVersion``
        precondition. On a 409 conflict the loop re-reads and re-invokes
        ``render`` on the fresh state (so a re-runnable mutator sees the latest
        document). Attempts are bounded; exhaustion fails loudly, naming the
        ConfigMap and the attempt count. Any exception raised by ``render``
        (e.g. a mutator error) aborts with nothing patched and propagates.
        """
        for attempt in range(1, _MAX_CONFLICT_ATTEMPTS + 1):
            existing = self._read_configmap_for_write()
            defaults = self._load_defaults_preserved(existing.data)
            content, document = render(existing, defaults)
            try:
                self._patch_manifest_precondition(existing, content)
            except _ConfigMapConflict:
                logger.info(
                    "resourceVersion conflict updating ConfigMap '%s' (attempt %d/%d); re-reading",
                    self._settings.configmap_name,
                    attempt,
                    _MAX_CONFLICT_ATTEMPTS,
                )
                continue
            logger.info(
                "Updated K8s ConfigMap '%s' key '%s' in namespace '%s'",
                self._settings.configmap_name,
                self._settings.manifest_key,
                self._settings.namespace,
            )
            return document

        raise K8sConfigError(
            f"Failed to update ConfigMap '{self._settings.configmap_name}' after "
            f"{_MAX_CONFLICT_ATTEMPTS} attempts due to repeated resourceVersion conflicts"
        )

    def write_manifest(self, manifest: dict[str, Any]) -> None:
        """Write manifest YAML to a K8s ConfigMap as a single key.

        Performs a three-way merge (defaults + current + new) and patches
        the ConfigMap. The patch carries the read ``resourceVersion`` as an
        optimistic-concurrency precondition, so a concurrent modification fails
        loudly with a 409 conflict rather than silently clobbering it.
        """
        from kubernetes.client.exceptions import ApiException

        existing = self._read_configmap_for_write()

        # Reuse the already-fetched ConfigMap for the defaults — no redundant GET.
        # Load defaults in the PRESERVED view so an `!ENV` default backfilled into a
        # key the working document omits round-trips as its marker, never a resolved
        # secret baked to disk as plaintext.
        defaults = self._load_defaults_preserved(existing.data)
        current: CommentedMap | dict[str, Any] = {}
        if existing.data and self._settings.manifest_key in existing.data:
            # A malformed existing manifest must abort the write — never discard
            # and overwrite it. Let the YAMLError propagate (matching read_manifest).
            current = self._load_manifest_document(existing.data[self._settings.manifest_key])

        content = merge_and_dump_manifest(defaults, cast("CommentedMap", current), manifest)
        resource_version = self._require_resource_version(existing)
        body = self._manifest_patch_body(existing, content, resource_version)
        try:
            self._core_api.patch_namespaced_config_map(
                self._settings.configmap_name,
                self._settings.namespace,
                body,
            )
        except ApiException as exc:
            if exc.status == 403:
                raise K8sConfigError(
                    f"Permission denied updating ConfigMap '{self._settings.configmap_name}': {exc.reason}"
                ) from exc
            raise K8sConfigError(f"Failed to update ConfigMap: {exc.reason}") from exc

        logger.info(
            "Updated K8s ConfigMap '%s' key '%s' in namespace '%s'",
            self._settings.configmap_name,
            self._settings.manifest_key,
            self._settings.namespace,
        )

    def mutate_manifest(self, mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        """Atomically read-modify-write the manifest under a resourceVersion precondition.

        Round-trip reads the ConfigMap manifest as the preserved view (``!ENV``
        markers kept, comments preserved), runs ``mutator`` to edit that document
        in place, and patches the ConfigMap carrying the read ``resourceVersion``
        as an optimistic-concurrency precondition. On a 409 conflict it re-reads
        the fresh document and re-runs ``mutator`` on it, so ``mutator`` MUST be
        re-runnable / pure. Attempts are bounded; exhaustion fails loudly. A
        ``mutator`` exception aborts with nothing patched and propagates. Returns
        the persisted document.
        """

        def render(existing: V1ConfigMap, defaults: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            text = (existing.data or {}).get(self._settings.manifest_key, "")
            document = self._load_manifest_document(text)
            mutator(document)
            content = merge_and_dump_manifest(defaults, document, {})
            return content, document

        return self._commit_with_retry(render)

    def replace_manifest(self, document: dict[str, Any]) -> dict[str, Any]:
        """Atomically replace the whole stored manifest under a resourceVersion precondition.

        ``document`` becomes the entire stored manifest — a key absent from it is
        deleted, nothing from the old document survives uninvited (defaults still
        backfill missing keys). Shares the precondition/retry machinery of
        :meth:`mutate_manifest`; on a 409 conflict it re-reads for a fresh
        ``resourceVersion`` and re-applies the same ``document``. The caller owns
        the preserved view: ``document`` must carry ``!ENV`` marker strings, never
        resolved secret values, since it is persisted verbatim. Returns the
        persisted document.
        """

        def render(existing: V1ConfigMap, defaults: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            # Work on a copy so retries do not accumulate defaults into — and the
            # backfill never mutates — the caller's document.
            working = cast("CommentedMap", copy.deepcopy(document))
            content = merge_and_dump_manifest(defaults, working, {})
            return content, working

        return self._commit_with_retry(render)


def build_config_manager() -> ConfigManager:
    """Provider entry point for the ``k8s`` config mode (the factory convention)."""
    return K8sConfigManager()
