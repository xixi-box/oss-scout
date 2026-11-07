"""Back up the four synced tables and migrate their existing rows to linked keys."""

import datetime as dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scout_v2 import feishu_app, feishu_bitable as bitable


def migrate(root: Path) -> dict:
    targets = {bitable.TABLE_NAME: "pull", bitable.REPO_TABLE_NAME: "repo",
               bitable.SCAN_TABLE_NAME: "issues", bitable.CAND_TABLE_NAME: "issues"}
    app = bitable._binding_app_token(root)
    token = feishu_app.tenant_token()
    if not app or not token:
        raise RuntimeError("Feishu Base binding and credentials are required")
    tables_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app}/tables"

    def request(method, url, payload=None):
        result = feishu_app._request(method, url, payload, token=token)
        if result.get("code") != 0:
            raise RuntimeError(f"Feishu {method}: {result.get('code')} {result.get('msg')}")
        return result.get("data") or {}

    matches = [t for t in request("GET", tables_url + "?page_size=100").get("items", [])
               if t["name"] in targets]
    if sorted(t["name"] for t in matches) != sorted(targets):
        raise RuntimeError("Expected exactly one existing table for each ledger")

    snapshot, updates = {}, {}
    for table in matches:
        name, table_id = table["name"], table["table_id"]
        fields = request("GET", f"{tables_url}/{table_id}/fields?page_size=100")
        columns = fields["items"]
        primary = [field for field in columns if field.get("is_primary")]
        if fields.get("has_more") or len(primary) != 1 or (
            primary[0]["field_name"], primary[0]["type"]
        ) != ("键", 1):
            raise RuntimeError(f"{name}: unexpected primary field or incomplete schema")
        rows = list(bitable.record_pages(app, table_id))
        snapshot[name] = {"table_id": table_id, "fields": columns, "records": rows}
        updates[name] = {}
        for row in rows:
            value = row.get("fields", {}).get("键")
            label = bitable._key_label(value)
            if label:
                updates[name][row["record_id"]] = bitable.link_key(label, targets[name])
            elif row.get("fields"):
                raise RuntimeError(f"{name}: nonempty row has no key")

    backup_dir = root / "state" / "feishu-backups"
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = backup_dir / (dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    with os.fdopen(os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600),
                   "w", encoding="utf-8") as file:
        json.dump(snapshot, file, ensure_ascii=False)
        file.flush()
        os.fsync(file.fileno())

    for name, table in snapshot.items():
        url = f"{tables_url}/{table['table_id']}/records"
        expected = updates[name]
        changes = [{"record_id": row["record_id"], "fields": {"键": expected[row["record_id"]]}}
                   for row in table["records"] if row["record_id"] in expected
                   and row["fields"].get("键") != expected[row["record_id"]]]
        for start in range(0, len(changes), 500):
            request("POST", url + "/batch_update", {"records": changes[start:start + 500]})
        observed = {row["record_id"]: row.get("fields", {}).get("键")
                    for row in bitable.record_pages(app, table["table_id"])}
        if not all(observed.get(rid) == link for rid, link in expected.items()):
            raise RuntimeError(f"{name}: linked keys did not match on read-back")

    report = {}
    for name, table in snapshot.items():
        url = f"{tables_url}/{table['table_id']}/fields"
        dropped = 0
        for field in table["fields"]:
            if not field.get("is_primary"):
                request("DELETE", f"{url}/{field['field_id']}")
                dropped += 1
        fields = request("GET", url + "?page_size=100")
        if [field["field_name"] for field in fields["items"]] != ["键"]:
            raise RuntimeError(f"{name}: unexpected remaining columns")
        report[name] = {"linked_rows": len(updates[name]), "removed_columns": dropped,
                        "url": f"https://my.feishu.cn/base/{app}?table={table['table_id']}"}
    return {"backup": backup.name, "tables": report}


if __name__ == "__main__":
    root = Path(os.environ.get("OSSCOUT_DASHBOARD_ROOT", "/data"))
    print(json.dumps(migrate(root), ensure_ascii=False))
