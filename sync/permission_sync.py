import json
from pathlib import Path
import os
import requests
from dotenv import load_dotenv

load_dotenv()
SEARCH_URL = f"{os.getenv('AZURE_SEARCH_ENDPOINT')}/indexes/{os.getenv('AZURE_SEARCH_INDEX')}/docs"
SEARCH_HEADERS = {"Content-Type": "application/json", "api-key": os.getenv("AZURE_SEARCH_API_KEY")}
API_VERSION = "2025-09-01"

ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = ROOT / "access_control.json"
GROUPS_PATH = ROOT / "groups.json"
STATE_PATH = ROOT / "sync_state.json"


def load_json(path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def resolve_policy(policy, groups):
    """Turn group names into Entra group IDs for every document."""
    resolved = {}
    for doc, names in policy.items():
        ids = []
        for name in names:
            if name not in groups:
                raise ValueError(f"Unknown group '{name}' for document '{doc}'")
            ids.append(groups[name])
        resolved[doc] = sorted(set(ids))
    return resolved

def diff_policies(old, new):
    """Return only the documents whose allowed groups changed."""
    changes = {}
    for doc in set(old) | set(new):
        before = sorted(old.get(doc, []))
        after = sorted(new.get(doc, []))
        if before != after:
            changes[doc] = after
    return changes

def chunks_by_document():
    """Build a lookup: document title -> list of its chunk IDs."""
    lookup, skip = {}, 0
    while True:
        response = requests.post(
            f"{SEARCH_URL}/search?api-version={API_VERSION}",
            headers=SEARCH_HEADERS,
            json={"search": "*", "select": "chunk_id,title", "top": 1000, "skip": skip},
        )
        response.raise_for_status()
        page = response.json()["value"]
        for chunk in page:
            lookup.setdefault(chunk["title"], []).append(chunk["chunk_id"])
        if len(page) < 1000:
            return lookup
        skip += 1000

def set_access_groups(chunk_ids, group_ids):
    """Update only access_groups on these chunks."""
    actions = [
        {"@search.action": "merge", "chunk_id": cid, "access_groups": group_ids}
        for cid in chunk_ids
    ]
    response = requests.post(
        f"{SEARCH_URL}/index?api-version={API_VERSION}",
        headers=SEARCH_HEADERS,
        json={"value": actions},
    )
    response.raise_for_status()

def run(dry_run=False):
    new = resolve_policy(load_json(POLICY_PATH, {}), load_json(GROUPS_PATH, {}))
    old = load_json(STATE_PATH, {})
    changes = diff_policies(old, new)

    if not changes:
        print("No permission changes.")
        return

    chunks = chunks_by_document()
    for doc, group_ids in sorted(changes.items()):
        if dry_run:
            print(f"[dry run] {doc}: {old.get(doc, [])} -> {group_ids}")
            continue
        if doc not in chunks:
            print(f"WARNING {doc}: no chunks in the index")
            continue
        set_access_groups(chunks[doc], group_ids)
        print(f"{doc}: updated {len(chunks[doc])} chunk(s) -> {group_ids}")

    if not dry_run:
        STATE_PATH.write_text(json.dumps(new, indent=2, sort_keys=True))


if __name__ == "__main__":
    import sys
    run(dry_run="--dry-run" in sys.argv)