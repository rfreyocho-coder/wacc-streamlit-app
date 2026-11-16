"""Refresh backup_snapshot.json from the live sources: python build_backup.py"""
import json
from datetime import datetime, timezone

import app

snap = {"created": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
for name, fn in app.LIVE_FETCHERS.items():
    snap[name] = app.to_jsonable(fn())
    print(name, "ok", snap[name].get("as_of"))
app.BACKUP_PATH.write_text(json.dumps(snap, indent=1, default=str))
print("saved", app.BACKUP_PATH)
