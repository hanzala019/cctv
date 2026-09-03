# Storage settings API

`core/storage/settings_store.py`. Instance available as self.settings_store in app.py, and recordings.py

## Read

**`get_settings_info()`**
All settings in one call. Always returns a full dict, never empty.
```python
{
    "recording_path": None,          # str, or None when unset
    "duration_minutes": 30.0,        # float
    "max_storage_gb": 50.0,          # float
    "max_recording_gb": 10.0,        # float
    "max_event_thumbnails_gb": 1.0,  # float
    "max_clip_gb": 2.5,              # float
    "retention_days": 14,            # int
    "last_retention_sweep": None,    # ISO 8601 str, or None
}
```

**`get_recording_path()`**
The user's explicit choice. Returns `str`, or `None` if unset.

**`get_effective_recording_path()`**
Where recordings actually go — the user's choice, or the default. Returns `str`, never `None`.

**`get_disk_space_bytes(path=None)`**
Volume totals for `path` (defaults to the effective recording path).
Returns `{"total": int, "used": int, "free": int}`. Raises `ValueError` if the path doesn't exist.

**`get_disk_space(path)`**
Same numbers as display strings: `{"total": "243.5GB", "used": "198.3GB", "free": "45.2GB"}`.
`path` is required here.

## Write

All raise `ValueError` on bad input. The message is safe to show the user directly.

**`set_recording_path(path)`**
`str`, or `None` to clear. Creates and write-tests the directory. Stored absolute.

**`set_duration_minutes(minutes)`**
Float, must be > 0.

**`set_retention_days(days)`**
Int, must be >= 1.

**`set_max_storage_gb(gb)`**
Float, must be > 0 and not exceed the volume's total capacity.

## Helpers

```python
from core.storage.settings_store import gb_to_bytes, format_bytes, GB
```

**`GB`** — `1024 ** 3`. Sizes are binary gigabytes.
**`gb_to_bytes(gb)`** → `int`
**`format_bytes(n)`** → `str`, e.g. `"243.5GB". this does the reverse of gb_to_bytes`


## Pending APIS:
**`Events se`**
**`Camera`**
