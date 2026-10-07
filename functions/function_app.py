import json
import logging
import os

import azure.functions as func
import requests
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

app = func.FunctionApp()

SEARCH_URL = f"{os.environ['SEARCH_ENDPOINT']}/indexes/{os.environ['SEARCH_INDEX']}/docs"
API_VERSION = "2025-09-01"

# Sign in with the Function's managed identity: no keys anywhere
credential = DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID"))
config = BlobServiceClient(os.environ["STORAGE_ACCOUNT_URL"], credential=credential).get_container_client("config")



# ---- Blob helpers (new: files live in the "config" container, not on disk) ----

def read_json(name, default=None):
    try:
        return json.loads(config.download_blob(name).readall())
    except ResourceNotFoundError:
        return default


def write_json(name, data):
    config.upload_blob(name, json.dumps(data, indent=2, sort_keys=True), overwrite=True)


# ---- Search sign-in (new: a token from the managed identity instead of the API key) ----

def search_headers():
    token = credential.get_token("https://search.azure.com/.default").token
    return {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}


# ---- Same logic as your Mac script ----

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
            headers=search_headers(),
            json={"search": "*", "select": "chunk_id,title", "top": 1000, "skip": skip},
            timeout=30,
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
        headers=search_headers(),
        json={"value": actions},
        timeout=30,
    )
    response.raise_for_status()



# ---- The sync itself ----

def run():
    new = resolve_policy(read_json("access_control.json", {}), read_json("groups.json", {}))
    old = read_json("sync_state.json", {})
    changes = diff_policies(old, new)

    if not changes:
        logging.info("No permission changes.")
        return

    chunks = chunks_by_document()
    for doc, group_ids in sorted(changes.items()):
        if doc not in chunks:
            logging.warning("%s: no chunks in the index", doc)
            continue
        set_access_groups(chunks[doc], group_ids)
        logging.info("%s: updated %d chunk(s) -> %s", doc, len(chunks[doc]), group_ids)

    write_json("sync_state.json", new)


# ---- Event Grid wakes this up when a blob is uploaded ----

@app.event_grid_trigger(arg_name="event")
def permission_sync(event: func.EventGridEvent):
    # Only react to access_control.json. Saving sync_state.json into the same
    # container also fires an event, and this check stops it from looping.
    if not event.subject.endswith("/containers/config/blobs/access_control.json"):
        logging.info("Ignoring %s", event.subject)
        return
    logging.info("Permission file changed, syncing.")
    run()