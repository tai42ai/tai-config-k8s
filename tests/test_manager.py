"""Tests for ``K8sConfigManager`` Secret / ConfigMap read-write + merge logic.

The Kubernetes client is faked: real ``kubernetes.client`` model classes
(``V1Secret`` / ``V1ConfigMap`` / ``V1ObjectMeta``) and the real ``ApiException``
are used, but the ``CoreV1Api`` is replaced by an in-memory recorder injected over
the manager's per-instance ``_core_api`` cached property — no cluster, no network.
The lazy ``_core_api`` builder itself is covered by stubbing ``kubernetes.config``
and ``kubernetes.client``.
"""

from __future__ import annotations

import base64

import pytest
import yaml
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from tai42_config_k8s import settings as settings_mod
from tai42_config_k8s.manager import K8sConfigError, K8sConfigManager

NAMESPACE = "default"
SECRET_NAME = "tai-env"
CONFIGMAP_NAME = "tai-manifest"
MANIFEST_KEY = "manifest.yml"
DEFAULTS_KEY = "defaults.manifest.yml"


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("utf-8")


class FakeCoreV1Api:
    """In-memory stand-in for ``kubernetes.client.CoreV1Api``.

    Each ``*_result`` is returned by the matching read; setting a ``*_exc`` makes
    that call raise instead. Patches are recorded for assertions.
    """

    def __init__(self) -> None:
        self.secret_result: client.V1Secret | None = None
        self.secret_exc: Exception | None = None
        self.configmap_result: client.V1ConfigMap | None = None
        self.configmap_exc: Exception | None = None
        self.patch_secret_exc: Exception | None = None
        self.patch_configmap_exc: Exception | None = None

        self.read_secret_calls: list[tuple[str, str]] = []
        self.read_configmap_calls: list[tuple[str, str]] = []
        self.patched_secret: client.V1Secret | None = None
        self.patched_secret_call: tuple[str, str] | None = None
        self.patched_configmap: client.V1ConfigMap | None = None
        self.patched_configmap_call: tuple[str, str] | None = None

    def read_namespaced_secret(self, name: str, namespace: str) -> client.V1Secret:
        self.read_secret_calls.append((name, namespace))
        if self.secret_exc is not None:
            raise self.secret_exc
        assert self.secret_result is not None
        return self.secret_result

    def patch_namespaced_secret(self, name: str, namespace: str, body: client.V1Secret) -> client.V1Secret:
        self.patched_secret_call = (name, namespace)
        if self.patch_secret_exc is not None:
            raise self.patch_secret_exc
        self.patched_secret = body
        return body

    def read_namespaced_config_map(self, name: str, namespace: str) -> client.V1ConfigMap:
        self.read_configmap_calls.append((name, namespace))
        if self.configmap_exc is not None:
            raise self.configmap_exc
        assert self.configmap_result is not None
        return self.configmap_result

    def patch_namespaced_config_map(self, name: str, namespace: str, body: client.V1ConfigMap) -> client.V1ConfigMap:
        self.patched_configmap_call = (name, namespace)
        if self.patch_configmap_exc is not None:
            raise self.patch_configmap_exc
        self.patched_configmap = body
        return body


@pytest.fixture
def manager_and_api(monkeypatch: pytest.MonkeyPatch, tmp_path) -> tuple[K8sConfigManager, FakeCoreV1Api]:
    # Deterministic settings: no service-account file -> namespace "default".
    # The hermetic TAI_K8S_* environment and the settings-cache reset come from
    # the shared conftest fixture.
    monkeypatch.setattr(settings_mod, "_SA_NAMESPACE_PATH", tmp_path / "missing")

    manager = K8sConfigManager()
    api = FakeCoreV1Api()
    # Inject over the per-instance cached_property so no real client is built.
    manager._core_api = api  # type: ignore[attr-defined]
    return manager, api


# -- read_env ----------------------------------------------------------------


def test_read_env_decodes_secret_data(manager_and_api) -> None:
    manager, api = manager_and_api
    api.secret_result = client.V1Secret(data={"API_KEY": _b64("s3cret"), "HOST": _b64("db")})
    assert manager.read_env() == {"API_KEY": "s3cret", "HOST": "db"}
    assert api.read_secret_calls == [(SECRET_NAME, NAMESPACE)]


def test_read_env_empty_when_no_data(manager_and_api) -> None:
    manager, api = manager_and_api
    api.secret_result = client.V1Secret(data=None)
    assert manager.read_env() == {}


def test_read_env_missing_secret_raises_filenotfound(manager_and_api) -> None:
    # The ConfigManager contract documents FileNotFoundError when the env source
    # does not exist; the absent-Secret (404) case honors it.
    manager, api = manager_and_api
    api.secret_exc = ApiException(status=404, reason="Not Found")
    with pytest.raises(FileNotFoundError, match="not found"):
        manager.read_env()


def test_read_env_other_api_error_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    api.secret_exc = ApiException(status=500, reason="Server Error")
    with pytest.raises(K8sConfigError, match="Failed to read Secret"):
        manager.read_env()


def test_read_env_invalid_base64_raises(manager_and_api) -> None:
    import binascii

    manager, api = manager_and_api
    # A Secret value that is not valid base64 is a corrupt source — decode raises
    # loudly rather than returning a bogus / silently-mangled string.
    api.secret_result = client.V1Secret(data={"BAD": "a"})
    with pytest.raises(binascii.Error):
        manager.read_env()


def test_read_env_non_alphabet_base64_raises(manager_and_api) -> None:
    import binascii

    manager, api = manager_and_api
    # A non-alphabet character inside otherwise-decodable base64 is corruption:
    # validate=True raises instead of dropping the character and returning a
    # silently-mangled value.
    api.secret_result = client.V1Secret(data={"BAD": "aGVs!bG8="})
    with pytest.raises(binascii.Error):
        manager.read_env()


# -- write_env ---------------------------------------------------------------


def test_write_env_merges_and_filters(manager_and_api) -> None:
    manager, api = manager_and_api
    api.secret_result = client.V1Secret(data={"API_KEY": _b64("old"), "KEEP": _b64("preserved")})
    manager.write_env({"API_KEY": "new", "BLANK": "", "EXTRA": "added"})

    body = api.patched_secret
    assert body is not None
    # API_KEY overwritten, KEEP preserved (not in new config), BLANK filtered out.
    assert body.string_data == {"API_KEY": "new", "EXTRA": "added", "KEEP": "preserved"}
    assert body.metadata.name == SECRET_NAME
    assert api.patched_secret_call == (SECRET_NAME, NAMESPACE)


def test_write_env_empty_value_removes_key(manager_and_api) -> None:
    manager, api = manager_and_api
    api.secret_result = client.V1Secret(data={"KEEP": _b64("old"), "OTHER": _b64("keep-me")})
    # Writing an explicit empty value blanks the key: it is in *config* (so it is
    # not preserved) and then filtered out for being empty — the documented
    # empty-value semantics of write_env.
    manager.write_env({"KEEP": ""})
    body = api.patched_secret
    assert body is not None
    assert body.string_data == {"OTHER": "keep-me"}


def test_write_env_handles_empty_existing(manager_and_api) -> None:
    manager, api = manager_and_api
    api.secret_result = client.V1Secret(data=None)
    manager.write_env({"A": "1"})
    assert api.patched_secret is not None
    assert api.patched_secret.string_data == {"A": "1"}


def test_write_env_corrupt_existing_base64_raises(manager_and_api) -> None:
    import binascii

    manager, api = manager_and_api
    # A corrupt existing Secret value must abort the write loudly — never be
    # silently mangled, merged, and re-written. Nothing is patched.
    api.secret_result = client.V1Secret(data={"KEEP": "aGVs!bG8="})
    with pytest.raises(binascii.Error):
        manager.write_env({"A": "1"})
    assert api.patched_secret is None


def test_write_env_missing_secret_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    api.secret_exc = ApiException(status=404, reason="Not Found")
    with pytest.raises(K8sConfigError, match="Create it before writing"):
        manager.write_env({"A": "1"})


def test_write_env_read_other_error_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    api.secret_exc = ApiException(status=500, reason="Server Error")
    with pytest.raises(K8sConfigError, match="Failed to read Secret"):
        manager.write_env({"A": "1"})


def test_write_env_permission_denied_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    api.secret_result = client.V1Secret(data={})
    api.patch_secret_exc = ApiException(status=403, reason="Forbidden")
    with pytest.raises(K8sConfigError, match="Permission denied updating Secret"):
        manager.write_env({"A": "1"})


def test_write_env_patch_other_error_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    api.secret_result = client.V1Secret(data={})
    api.patch_secret_exc = ApiException(status=500, reason="Server Error")
    with pytest.raises(K8sConfigError, match="Failed to update Secret"):
        manager.write_env({"A": "1"})


# -- read_manifest -----------------------------------------------------------


def test_read_manifest_parses_yaml(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_result = client.V1ConfigMap(data={MANIFEST_KEY: "a: 1\nb: two"})
    assert manager.read_manifest() == {"a": 1, "b": "two"}
    assert api.read_configmap_calls == [(CONFIGMAP_NAME, NAMESPACE)]


def test_read_manifest_empty_document_returns_empty(manager_and_api) -> None:
    manager, api = manager_and_api
    # A truthy document that parses to None (e.g. "null") -> {} via `or {}`.
    api.configmap_result = client.V1ConfigMap(data={MANIFEST_KEY: "null"})
    assert manager.read_manifest() == {}


def test_read_manifest_resolves_env_tag(manager_and_api, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, api = manager_and_api
    # An !ENV ${VAR} tag in the ConfigMap is interpolated by parse_config on read.
    monkeypatch.setenv("TAI_MANIFEST_TOKEN", "resolved-secret")
    api.configmap_result = client.V1ConfigMap(data={MANIFEST_KEY: "token: !ENV ${TAI_MANIFEST_TOKEN}\nname: app"})
    assert manager.read_manifest() == {"token": "resolved-secret", "name": "app"}


def test_read_manifest_invalid_yaml_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_result = client.V1ConfigMap(data={MANIFEST_KEY: "a: : :"})
    # Malformed manifest YAML is a loud error, never a silent empty config.
    with pytest.raises(yaml.YAMLError):
        manager.read_manifest()


def test_read_manifest_non_mapping_document_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    # A valid-YAML but non-mapping document (a list) cannot satisfy the
    # dict[str, Any] contract — the mapping guard raises, naming the key.
    api.configmap_result = client.V1ConfigMap(data={MANIFEST_KEY: "- one\n- two"})
    with pytest.raises(K8sConfigError, match="expected a mapping"):
        manager.read_manifest()


def test_read_manifest_missing_configmap_raises_filenotfound(manager_and_api) -> None:
    # The ConfigManager contract documents FileNotFoundError when the manifest
    # does not exist; the absent-ConfigMap (404) case honors it, matching the
    # missing-key case below and the file provider's behavior.
    manager, api = manager_and_api
    api.configmap_exc = ApiException(status=404, reason="Not Found")
    with pytest.raises(FileNotFoundError, match="not found"):
        manager.read_manifest()


def test_read_manifest_other_api_error_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_exc = ApiException(status=500, reason="Server Error")
    with pytest.raises(K8sConfigError, match="Failed to read ConfigMap"):
        manager.read_manifest()


def test_read_manifest_missing_key_raises_filenotfound(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_result = client.V1ConfigMap(data={"other.yml": "a: 1"})
    with pytest.raises(FileNotFoundError, match="Manifest key"):
        manager.read_manifest()


def test_read_manifest_no_data_raises_filenotfound(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_result = client.V1ConfigMap(data=None)
    with pytest.raises(FileNotFoundError, match="Manifest key"):
        manager.read_manifest()


# -- read_manifest_preserved -------------------------------------------------


def test_read_manifest_preserved_keeps_env_tag_marker(manager_and_api, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, api = manager_and_api
    # The preserving view keeps `!ENV ${VAR}` as its literal marker string and
    # never reads the environment, even when the variable is set.
    monkeypatch.setenv("TAI_MANIFEST_TOKEN", "resolved-secret")
    api.configmap_result = client.V1ConfigMap(data={MANIFEST_KEY: "token: !ENV ${TAI_MANIFEST_TOKEN}\nname: app"})
    assert manager.read_manifest_preserved() == {"token": "!ENV ${TAI_MANIFEST_TOKEN}", "name": "app"}


def test_read_manifest_preserved_non_mapping_document_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    # A valid-YAML but non-mapping document (a list) cannot satisfy the
    # dict[str, Any] contract — the mapping guard raises, naming the key.
    api.configmap_result = client.V1ConfigMap(data={MANIFEST_KEY: "- one\n- two"})
    with pytest.raises(K8sConfigError, match="expected a mapping"):
        manager.read_manifest_preserved()


def test_read_manifest_preserved_missing_configmap_raises_filenotfound(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_exc = ApiException(status=404, reason="Not Found")
    with pytest.raises(FileNotFoundError, match="not found"):
        manager.read_manifest_preserved()


def test_read_manifest_preserved_other_api_error_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_exc = ApiException(status=500, reason="Server Error")
    with pytest.raises(K8sConfigError, match="Failed to read ConfigMap"):
        manager.read_manifest_preserved()


def test_read_manifest_preserved_missing_key_raises_filenotfound(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_result = client.V1ConfigMap(data={"other.yml": "a: 1"})
    with pytest.raises(FileNotFoundError, match="Manifest key"):
        manager.read_manifest_preserved()


# -- read_defaults_manifest --------------------------------------------------


def test_read_defaults_manifest_parses_yaml(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_result = client.V1ConfigMap(data={DEFAULTS_KEY: "x: 9"})
    assert manager.read_defaults_manifest() == {"x": 9}


def test_read_defaults_manifest_resolves_env_tag(manager_and_api, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, api = manager_and_api
    # An !ENV ${VAR} tag in the defaults ConfigMap is interpolated on read too.
    monkeypatch.setenv("TAI_DEFAULTS_TOKEN", "default-secret")
    api.configmap_result = client.V1ConfigMap(data={DEFAULTS_KEY: "token: !ENV ${TAI_DEFAULTS_TOKEN}"})
    assert manager.read_defaults_manifest() == {"token": "default-secret"}


def test_read_defaults_manifest_missing_configmap_returns_empty(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_exc = ApiException(status=404, reason="Not Found")
    assert manager.read_defaults_manifest() == {}


def test_read_defaults_manifest_other_api_error_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_exc = ApiException(status=500, reason="Server Error")
    with pytest.raises(K8sConfigError, match="Failed to read ConfigMap"):
        manager.read_defaults_manifest()


def test_read_defaults_manifest_missing_key_returns_empty(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_result = client.V1ConfigMap(data={"other.yml": "x: 1"})
    assert manager.read_defaults_manifest() == {}


def test_read_defaults_manifest_invalid_yaml_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_result = client.V1ConfigMap(data={DEFAULTS_KEY: "a: : :"})
    # A malformed defaults manifest is a loud error, not a silent empty config.
    with pytest.raises(yaml.YAMLError):
        manager.read_defaults_manifest()


def test_read_defaults_manifest_non_mapping_document_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    # A valid-YAML but non-mapping defaults document (a list) cannot satisfy the
    # dict[str, Any] contract — the mapping guard raises, naming the key.
    api.configmap_result = client.V1ConfigMap(data={DEFAULTS_KEY: "- one\n- two"})
    with pytest.raises(K8sConfigError, match="expected a mapping"):
        manager.read_defaults_manifest()


# -- write_manifest ----------------------------------------------------------


def test_write_manifest_three_way_merge(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_result = client.V1ConfigMap(
        data={MANIFEST_KEY: "b: 3\nc: 4", DEFAULTS_KEY: "a: 1\nb: 2"},
        metadata=client.V1ObjectMeta(name=CONFIGMAP_NAME, resource_version="rv-42"),
    )
    manager.write_manifest({"c": 5, "d": 6})

    body = api.patched_configmap
    assert body is not None
    written = yaml.safe_load(body.data[MANIFEST_KEY])
    # defaults {a:1,b:2} | current {b:3,c:4} | new {c:5,d:6}
    assert written == {"a": 1, "b": 3, "c": 5, "d": 6}
    # Other ConfigMap keys are preserved alongside the rewritten manifest.
    assert body.data[DEFAULTS_KEY] == "a: 1\nb: 2"
    assert body.metadata.name == CONFIGMAP_NAME
    # The defaults are read from the single fetched ConfigMap — no redundant GET.
    assert len(api.read_configmap_calls) == 1
    # The read resourceVersion rides on the patch as an optimistic-concurrency
    # precondition, so a concurrent write 409s instead of being clobbered.
    assert body.metadata.resource_version == "rv-42"
    assert api.patched_configmap_call == (CONFIGMAP_NAME, NAMESPACE)


def test_write_manifest_no_current_manifest_key(manager_and_api) -> None:
    manager, api = manager_and_api
    # ConfigMap exists but holds no manifest key yet -> current stays empty.
    api.configmap_result = client.V1ConfigMap(
        data={DEFAULTS_KEY: "a: 1"},
        metadata=client.V1ObjectMeta(name=CONFIGMAP_NAME, resource_version="rv-7"),
    )
    manager.write_manifest({"b": 2})

    body = api.patched_configmap
    assert body is not None
    written = yaml.safe_load(body.data[MANIFEST_KEY])
    # defaults {a:1} | current {} | new {b:2}
    assert written == {"a": 1, "b": 2}


def test_write_manifest_missing_metadata_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    # A ConfigMap with no metadata cannot carry a resourceVersion precondition;
    # the write refuses rather than patching without optimistic concurrency.
    api.configmap_result = client.V1ConfigMap(data={DEFAULTS_KEY: "a: 1"})
    with pytest.raises(K8sConfigError, match="without metadata"):
        manager.write_manifest({"b": 2})
    assert api.patched_configmap is None


def test_write_manifest_invalid_current_raises(manager_and_api) -> None:
    import ruamel.yaml

    manager, api = manager_and_api
    api.configmap_result = client.V1ConfigMap(
        data={MANIFEST_KEY: "a: : :", DEFAULTS_KEY: "x: 9"},
    )
    # A malformed existing manifest must abort the write, never be discarded and
    # clobbered (data loss). The round-trip loader's YAMLError propagates and
    # nothing is patched.
    with pytest.raises(ruamel.yaml.YAMLError):
        manager.write_manifest({"y": 1})
    assert api.patched_configmap is None


def test_write_manifest_non_mapping_current_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    # An existing manifest that is valid YAML but a non-mapping (a list) would feed
    # merge_and_dump_manifest a non-dict; the mapping guard raises loudly instead,
    # naming the key, and nothing is patched.
    api.configmap_result = client.V1ConfigMap(
        data={MANIFEST_KEY: "- one\n- two", DEFAULTS_KEY: "x: 9"},
        metadata=client.V1ObjectMeta(name=CONFIGMAP_NAME, resource_version="rv-1"),
    )
    with pytest.raises(K8sConfigError, match="expected a mapping"):
        manager.write_manifest({"y": 1})
    assert api.patched_configmap is None


def test_write_manifest_missing_resource_version_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    # Metadata present but no resourceVersion cannot carry the optimistic-concurrency
    # precondition; the write refuses rather than patching without it (lost-update hole).
    api.configmap_result = client.V1ConfigMap(
        data={DEFAULTS_KEY: "a: 1"},
        metadata=client.V1ObjectMeta(name=CONFIGMAP_NAME, resource_version=None),
    )
    with pytest.raises(K8sConfigError, match="without a resourceVersion"):
        manager.write_manifest({"b": 2})
    assert api.patched_configmap is None


def test_write_manifest_missing_configmap_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_exc = ApiException(status=404, reason="Not Found")
    with pytest.raises(K8sConfigError, match="Create it before writing"):
        manager.write_manifest({"a": 1})


def test_write_manifest_read_other_error_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_exc = ApiException(status=500, reason="Server Error")
    with pytest.raises(K8sConfigError, match="Failed to read ConfigMap"):
        manager.write_manifest({"a": 1})


def test_write_manifest_permission_denied_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_result = client.V1ConfigMap(
        data={MANIFEST_KEY: "a: 1", DEFAULTS_KEY: "b: 2"},
        metadata=client.V1ObjectMeta(name=CONFIGMAP_NAME, resource_version="rv-1"),
    )
    api.patch_configmap_exc = ApiException(status=403, reason="Forbidden")
    with pytest.raises(K8sConfigError, match="Permission denied updating ConfigMap"):
        manager.write_manifest({"c": 3})


def test_write_manifest_patch_other_error_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_result = client.V1ConfigMap(
        data={MANIFEST_KEY: "a: 1", DEFAULTS_KEY: "b: 2"},
        metadata=client.V1ObjectMeta(name=CONFIGMAP_NAME, resource_version="rv-1"),
    )
    api.patch_configmap_exc = ApiException(status=500, reason="Server Error")
    with pytest.raises(K8sConfigError, match="Failed to update ConfigMap"):
        manager.write_manifest({"c": 3})


# -- mutate_manifest / replace_manifest --------------------------------------


class RaceCoreV1Api:
    """Fake CoreV1Api modeling a resourceVersion race on the manifest ConfigMap.

    A concurrent writer is simulated on the FIRST patch attempt: before the
    precondition is checked, the stored document advances (a new key ``c: 3`` and
    a bumped ``resourceVersion``), so the caller's read-stale precondition
    mismatches and the patch 409s. The subsequent re-read returns the advanced
    document; the second patch matches the bumped version and lands.
    """

    def __init__(self, initial_manifest: str, initial_rv: str) -> None:
        self._data: dict[str, str] = {MANIFEST_KEY: initial_manifest}
        self._rv = initial_rv
        self._concurrent_applied = False
        self.read_calls = 0
        self.patch_calls = 0

    def read_namespaced_config_map(self, name: str, namespace: str) -> client.V1ConfigMap:
        self.read_calls += 1
        return client.V1ConfigMap(
            data=dict(self._data),
            metadata=client.V1ObjectMeta(name=name, resource_version=self._rv),
        )

    def patch_namespaced_config_map(self, name: str, namespace: str, body: client.V1ConfigMap) -> client.V1ConfigMap:
        self.patch_calls += 1
        if not self._concurrent_applied:
            # A concurrent writer lands between the caller's read and this patch.
            self._data[MANIFEST_KEY] = "a: 1\nc: 3\n"
            self._rv = "rv-2"
            self._concurrent_applied = True
        assert body.metadata is not None
        if body.metadata.resource_version != self._rv:
            raise ApiException(status=409, reason="Conflict")
        self._data = dict(body.data or {})
        self._rv = "rv-final"
        return body

    def stored_manifest(self) -> dict:
        return yaml.safe_load(self._data[MANIFEST_KEY])


def test_mutate_manifest_retries_on_conflict_and_reruns(manager_and_api) -> None:
    manager, _ = manager_and_api
    api = RaceCoreV1Api("a: 1\n", "rv-1")
    manager._core_api = api  # type: ignore[attr-defined]

    runs: list[dict] = []

    def mutator(document: dict) -> None:
        runs.append(dict(document))
        document["b"] = 2

    result = manager.mutate_manifest(mutator)

    # The mutator runs once per attempt: first on the stale {a:1}, then re-run on
    # the freshly re-read {a:1,c:3} after the 409 — proof of re-read + re-run.
    assert len(runs) == 2
    assert runs[0] == {"a": 1}
    assert runs[1] == {"a": 1, "c": 3}
    assert api.patch_calls == 2
    # The persisted ConfigMap carries BOTH the concurrent writer's `c` and the
    # mutator's `b`; the first attempt's write was discarded by the conflict.
    assert api.stored_manifest() == {"a": 1, "c": 3, "b": 2}
    assert result == {"a": 1, "c": 3, "b": 2}


def test_mutate_manifest_preserves_comments(manager_and_api) -> None:
    manager, api = manager_and_api
    source = "# top-level note\nname: app  # the app name\ntoken: !ENV ${TAI_MANIFEST_SECRET}\n"
    api.configmap_result = client.V1ConfigMap(
        data={MANIFEST_KEY: source},
        metadata=client.V1ObjectMeta(name=CONFIGMAP_NAME, resource_version="rv-1"),
    )

    def mutator(document: dict) -> None:
        document["name"] = "renamed"

    manager.mutate_manifest(mutator)

    body = api.patched_configmap
    assert body is not None
    written = body.data[MANIFEST_KEY]
    # Untouched comments survive the edit; the edited value changes; the !ENV
    # marker round-trips back to a genuine tag (never a resolved secret value).
    assert "# top-level note" in written
    assert "# the app name" in written
    assert "name: renamed" in written
    assert "token: !ENV ${TAI_MANIFEST_SECRET}" in written


def test_mutate_manifest_conflict_exhaustion_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_result = client.V1ConfigMap(
        data={MANIFEST_KEY: "a: 1\n"},
        metadata=client.V1ObjectMeta(name=CONFIGMAP_NAME, resource_version="rv-1"),
    )
    # Every patch conflicts: the loop exhausts its bounded attempts and fails
    # loudly, naming the ConfigMap and the attempt count.
    api.patch_configmap_exc = ApiException(status=409, reason="Conflict")
    runs: list[int] = []
    with pytest.raises(K8sConfigError, match="after 5 attempts"):
        manager.mutate_manifest(lambda document: runs.append(1))
    assert len(runs) == 5


def test_mutate_manifest_mutator_exception_aborts(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_result = client.V1ConfigMap(
        data={MANIFEST_KEY: "a: 1\n"},
        metadata=client.V1ObjectMeta(name=CONFIGMAP_NAME, resource_version="rv-1"),
    )

    def boom(document: dict) -> None:
        raise ValueError("bad mutation")

    # A mutator exception aborts the transaction with nothing patched.
    with pytest.raises(ValueError, match="bad mutation"):
        manager.mutate_manifest(boom)
    assert api.patched_configmap is None


def test_mutate_manifest_permission_denied_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_result = client.V1ConfigMap(
        data={MANIFEST_KEY: "a: 1\n"},
        metadata=client.V1ObjectMeta(name=CONFIGMAP_NAME, resource_version="rv-1"),
    )
    # The seam path (mutate/replace via `_commit_with_retry` -> the precondition
    # patch helper) surfaces a 403 as a loud permission error — distinct from the
    # 409 the retry loop swallows and from `write_manifest`'s separate inline path.
    api.patch_configmap_exc = ApiException(status=403, reason="Forbidden")
    with pytest.raises(K8sConfigError, match="Permission denied updating ConfigMap"):
        manager.mutate_manifest(lambda document: document.__setitem__("b", 2))


def test_mutate_manifest_patch_other_error_raises(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_result = client.V1ConfigMap(
        data={MANIFEST_KEY: "a: 1\n"},
        metadata=client.V1ObjectMeta(name=CONFIGMAP_NAME, resource_version="rv-1"),
    )
    # A generic non-409 API error on the seam path's precondition patch fails loudly
    # rather than being retried like a conflict or swallowed.
    api.patch_configmap_exc = ApiException(status=500, reason="Server Error")
    with pytest.raises(K8sConfigError, match="Failed to update ConfigMap"):
        manager.mutate_manifest(lambda document: document.__setitem__("b", 2))


def test_replace_manifest_deletes_uninvited_keys(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_result = client.V1ConfigMap(
        data={MANIFEST_KEY: "a: 1\nb: 2\n", DEFAULTS_KEY: "d: 9\n"},
        metadata=client.V1ObjectMeta(name=CONFIGMAP_NAME, resource_version="rv-1"),
    )
    result = manager.replace_manifest({"a": 100})

    body = api.patched_configmap
    assert body is not None
    written = yaml.safe_load(body.data[MANIFEST_KEY])
    # `b` is gone — nothing from the old document survives uninvited — while the
    # defaults still backfill the missing `d`.
    assert written == {"a": 100, "d": 9}
    assert result == {"a": 100, "d": 9}
    assert body.metadata.resource_version == "rv-1"


def test_replace_manifest_retries_on_conflict(manager_and_api) -> None:
    manager, _ = manager_and_api
    api = RaceCoreV1Api("a: 1\n", "rv-1")
    manager._core_api = api  # type: ignore[attr-defined]

    result = manager.replace_manifest({"a": 1, "x": 9})

    # The first patch 409s against the stale rv-1; the loop re-reads the bumped
    # rv-2 and re-applies the same document, which lands on the second patch.
    assert api.patch_calls == 2
    # replace wins wholesale: the concurrent writer's `c` is uninvited and gone —
    # the replacement document is exactly what persists against the bumped version.
    assert api.stored_manifest() == {"a": 1, "x": 9}
    assert result == {"a": 1, "x": 9}


def test_replace_manifest_does_not_mutate_caller_document(manager_and_api) -> None:
    manager, api = manager_and_api
    api.configmap_result = client.V1ConfigMap(
        data={MANIFEST_KEY: "a: 1\n", DEFAULTS_KEY: "d: 9\n"},
        metadata=client.V1ObjectMeta(name=CONFIGMAP_NAME, resource_version="rv-1"),
    )
    document = {"a": 100}
    result = manager.replace_manifest(document)

    # The backfilled default `d` lands in the persisted document, but the deep copy
    # in the replace render closure keeps it from leaking back into the caller's dict.
    assert result == {"a": 100, "d": 9}
    assert document == {"a": 100}


def test_replace_manifest_preserves_comments_from_round_trip_document(manager_and_api) -> None:
    manager, api = manager_and_api
    source = "# top-level note\nname: app  # the app name\ntoken: !ENV ${TAI_MANIFEST_SECRET}\n"
    api.configmap_result = client.V1ConfigMap(
        data={MANIFEST_KEY: source},
        metadata=client.V1ObjectMeta(name=CONFIGMAP_NAME, resource_version="rv-1"),
    )
    # Read the manifest as its comment-bearing preserved view, then replace the
    # whole manifest with that same round-trip document.
    document = manager.read_manifest_preserved()
    manager.replace_manifest(document)

    body = api.patched_configmap
    assert body is not None
    written = body.data[MANIFEST_KEY]
    # The deep copy in replace's render closure keeps the ruamel comment/`.ca`
    # metadata, so the leading comment and the inline comment survive the replace
    # round-trip; the !ENV marker round-trips back to a genuine tag, never a
    # resolved secret value.
    assert "# top-level note" in written
    assert "# the app name" in written
    assert "token: !ENV ${TAI_MANIFEST_SECRET}" in written


def test_write_manifest_defaults_env_marker_never_leaks_plaintext(
    manager_and_api, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, api = manager_and_api
    # write_manifest reaches `_load_defaults_preserved` through its own inline patch
    # path: a defaults key `foo: !ENV ${SOME_SECRET}` with `foo` ABSENT from the
    # working manifest is backfilled by the three-way merge. The preserved-view
    # default lands as its `!ENV` marker, never the resolved secret as plaintext.
    monkeypatch.setenv("SOME_SECRET", "super-secret-plaintext")
    api.configmap_result = client.V1ConfigMap(
        data={MANIFEST_KEY: "a: 1\n", DEFAULTS_KEY: "foo: !ENV ${SOME_SECRET}\n"},
        metadata=client.V1ObjectMeta(name=CONFIGMAP_NAME, resource_version="rv-1"),
    )

    manager.write_manifest({"a": 2})

    body = api.patched_configmap
    assert body is not None
    written = body.data[MANIFEST_KEY]
    assert "foo: !ENV ${SOME_SECRET}" in written
    assert "super-secret-plaintext" not in written


def test_mutate_manifest_defaults_env_marker_never_leaks_plaintext(
    manager_and_api, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, api = manager_and_api
    # A defaults key authored `foo: !ENV ${SOME_SECRET}` with `foo` ABSENT from the
    # working manifest: the three-way merge backfills `foo` from the defaults. Loaded
    # in the preserved view, that backfill is the `!ENV` marker — never the resolved
    # secret dumped to the ConfigMap as plaintext.
    monkeypatch.setenv("SOME_SECRET", "super-secret-plaintext")
    api.configmap_result = client.V1ConfigMap(
        data={MANIFEST_KEY: "a: 1\n", DEFAULTS_KEY: "foo: !ENV ${SOME_SECRET}\n"},
        metadata=client.V1ObjectMeta(name=CONFIGMAP_NAME, resource_version="rv-1"),
    )

    def mutator(document: dict) -> None:
        document["a"] = 2

    manager.mutate_manifest(mutator)

    body = api.patched_configmap
    assert body is not None
    written = body.data[MANIFEST_KEY]
    # The backfilled default round-trips to a genuine `!ENV` tag; the resolved
    # secret value never reaches the stored YAML.
    assert "foo: !ENV ${SOME_SECRET}" in written
    assert "super-secret-plaintext" not in written


def test_replace_manifest_defaults_env_marker_never_leaks_plaintext(
    manager_and_api, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, api = manager_and_api
    # replace deletes uninvited keys, so `foo` — absent from the replacement
    # document — is backfilled from the defaults on every write. The preserved-view
    # default lands as its `!ENV` marker, not the plaintext secret.
    monkeypatch.setenv("SOME_SECRET", "super-secret-plaintext")
    api.configmap_result = client.V1ConfigMap(
        data={MANIFEST_KEY: "a: 1\n", DEFAULTS_KEY: "foo: !ENV ${SOME_SECRET}\n"},
        metadata=client.V1ObjectMeta(name=CONFIGMAP_NAME, resource_version="rv-1"),
    )

    manager.replace_manifest({"a": 1})

    body = api.patched_configmap
    assert body is not None
    written = body.data[MANIFEST_KEY]
    assert "foo: !ENV ${SOME_SECRET}" in written
    assert "super-secret-plaintext" not in written


# -- lazy CoreV1Api builder --------------------------------------------------


def _build_manager(monkeypatch: pytest.MonkeyPatch, tmp_path) -> K8sConfigManager:
    # The hermetic environment and settings-cache reset come from the shared
    # conftest fixture; only the service-account path needs pointing away.
    monkeypatch.setattr(settings_mod, "_SA_NAMESPACE_PATH", tmp_path / "missing")
    return K8sConfigManager()


def test_core_api_uses_incluster_config(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import kubernetes

    calls: list[str] = []
    sentinel = object()
    monkeypatch.setattr(kubernetes.config, "load_incluster_config", lambda: calls.append("incluster"))
    monkeypatch.setattr(kubernetes.config, "load_kube_config", lambda: calls.append("kube"))
    monkeypatch.setattr(kubernetes.client, "CoreV1Api", lambda: sentinel)

    manager = _build_manager(monkeypatch, tmp_path)
    assert manager._core_api is sentinel  # type: ignore[attr-defined]
    # In-cluster config succeeded, so the kubeconfig fallback is not used.
    assert calls == ["incluster"]


def test_core_api_falls_back_to_kubeconfig(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import kubernetes
    from kubernetes.config import ConfigException

    calls: list[str] = []
    sentinel = object()

    def _raise_incluster() -> None:
        raise ConfigException("no in-cluster config")

    monkeypatch.setattr(kubernetes.config, "load_incluster_config", _raise_incluster)
    monkeypatch.setattr(kubernetes.config, "load_kube_config", lambda: calls.append("kube"))
    monkeypatch.setattr(kubernetes.client, "CoreV1Api", lambda: sentinel)

    manager = _build_manager(monkeypatch, tmp_path)
    assert manager._core_api is sentinel  # type: ignore[attr-defined]
    assert calls == ["kube"]
