# Plugins (contract version 1)

Plugins extend gis-stack without forking it. They are ordinary Python packages that publish **entry points**; gis-stack finds them in the
environment it runs in. They run third-party code, so they are **off unless `GISSTACK_FEATURES=plugins`** (or `all`).

| Kind | Entry-point group | Base class (`gisstack.plugins.contract`) | Used by |
|---|---|---|---|
| ServiceProvider | `gisstack.service_providers` | `ServiceProvider.provider()` -> a `providers.base.Provider` | `providers.get(<name>)` - an extra deployment target (never replaces native/compose/k8s) |
| MonitoringProvider | `gisstack.monitoring_providers` | `MonitoringProvider.notifiers()` -> objects with `name` and `send(note)` | extra alert channels next to webhook/e-mail |
| BackupProvider | `gisstack.backup_providers` | `BackupProvider.store(archive, backup_id)` -> location | `POST /api/v1/backups/offsite` (admin): download, verify, hand to every provider |
| AuthProvider | `gisstack.auth_providers` | `AuthProvider.authenticate(token)` -> `{"name", "scope"}` or `None` | Bearer tokens on `/api/v1` only; the scope must be `read`/`operate`/`admin` |
| DataSource | `gisstack.data_sources` | `DataSource.layers()` -> `[{"id","title","kind"}]` | `GET /api/v1/datasources` |

## Writing one

```toml
# pyproject.toml of your package
[project.entry-points."gisstack.backup_providers"]
vault = "my_pkg.vault:VaultBackup"
```

```python
from pathlib import Path
from gisstack.plugins.contract import BackupProvider

class VaultBackup(BackupProvider):
    name = "vault"                       # [a-z0-9][a-z0-9_-]{0,39}, unique per kind
    description = "copies backups to the company vault"
    api_version = 1                      # the contract you wrote against

    def store(self, archive: Path, backup_id: str) -> str:
        ...                              # raise on failure; never modify or delete `archive`
        return f"vault://{backup_id}"
```

## Rules the loader enforces

* The class must derive from the kind's base class, declare `api_version == 1`, and have a valid unique `name`; otherwise it is **reported, not loaded**
  (`GET /api/v1/plugins` lists `failures`). A broken plugin never stops gis-stack or other plugins.
* Every call into a plugin is guarded: an exception is logged (type only) and treated as "no result". An AuthProvider that crashes cannot lock out
  the built-in tokens; a failing notifier becomes a `notify.failed` event; a failing BackupProvider fails only its own copy.
* Plugin tokens are accepted on `/api/v1` only, never on the legacy routes. Plugins receive secrets only if you pass them (the contract passes none).
* Adding optional methods keeps version 1; changing or removing one bumps `API_VERSION` - old plugins are then refused with a clear message.

## Conformance kit

```python
from gisstack.plugins.kit import ConformanceTests

class TestVault(ConformanceTests):
    kind = "backup_provider"
    plugin = staticmethod(VaultBackup)
```

or `gisstack.plugins.kit.verify("backup_provider", VaultBackup)` -> list of problems (empty = conforms). The kit exercises the plugin (an AuthProvider is
probed with hostile token strings; a BackupProvider gets a dummy archive and must leave it untouched).
