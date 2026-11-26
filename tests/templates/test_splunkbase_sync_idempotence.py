#!/usr/bin/env python3
"""Keep object tags and the published manifest idempotent between syncs."""

import sys
from pathlib import Path

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT / "playbooks/tasks/sync-splunkbase/resolve-and-sync.yml"
REGISTRY = ROOT / "roles/splunk_docker/vars/addons.yml"
# The resolve/sync flow spans two task files (resolve-and-sync.yml imports
# sync-and-publish.yml); Ansible resolves tasks across that boundary, so the
# lookups below do too.
all_tasks = yaml.safe_load(TASKS.read_text()) + yaml.safe_load(
    (TASKS.parent / "sync-and-publish.yml").read_text()
)
by_name = {task["name"]: task for task in all_tasks}
errors = []

expected_unifi_apps = {
    9831: "TA_unifi_ng",
    9856: "unifi_app_for_splunk",
    10216: "TA-unifi_security",
    10217: "unifi_security",
}
registry_entries = yaml.safe_load(REGISTRY.read_text())["splunk_docker_addons"]
registry_by_id = {
    int(entry["splunkbase_id"]): entry
    for entry in registry_entries
    if "splunkbase_id" in entry
}
for app_id, app_dir in expected_unifi_apps.items():
    entry = registry_by_id.get(app_id)
    if entry is None:
        errors.append(f"FAIL: Splunkbase app {app_id} is missing from the registry")
        continue
    if entry.get("app_dir") != app_dir or entry.get("install") is not False:
        errors.append(f"FAIL: Splunkbase app {app_id} must parse with app_dir={app_dir} and install=false")
    if "." in Path(entry["filename"]).stem:
        errors.append(f"FAIL: Splunkbase app {app_id} filename must not contain a version")

key_builder = by_name["Build versioned release object keys"]
key_template = key_builder.get("vars", {}).get("versioned_history_key", "")
rendered_key = jinja2.Template(key_template).render(
    item={"app_dir": "sample_addon", "version": "1.2.3", "filename": "package.tar"}
)
if rendered_key != "sample_addon/1.2.3/package.tar":
    errors.append(f"FAIL: versioned object key rendered as {rendered_key!r}")

nvidia_entries = [
    entry for entry in yaml.safe_load(REGISTRY.read_text())["splunk_docker_addons"]
    if str(entry.get("splunkbase_id")) == "10131"
]
if len(nvidia_entries) != 1:
    errors.append("FAIL: TA-Nvidia must have one Splunkbase registry entry")
elif (
    nvidia_entries[0].get("app_dir") != "TA-Nvidia"
    or nvidia_entries[0].get("install") is not False
    or "pin_version" in nvidia_entries[0]
):
    errors.append("FAIL: TA-Nvidia must be mirrored at latest (no pin) with install: false")

query = by_name["Query current object-storage version tag for each entry"]
if query["ansible.builtin.command"]["argv"][-3:] != ["TagSet", "--output", "json"]:
    errors.append("FAIL: the tag probe must return the complete TagSet as JSON")
if query.get("check_mode") is not False:
    errors.append("FAIL: the complete tag probe must still run in check mode")

decisions = by_name["Compute sync decisions (bucket tag vs resolved latest)"]
if "items2dict(key_name='Key', value_name='Value')" not in decisions["vars"]["current_object_tags"]:
    errors.append("FAIL: current tags must be compared as a key/value mapping")
if "needs_tag" not in decisions["ansible.builtin.set_fact"]["splunkbase_plan"]:
    errors.append("FAIL: sync decisions must mark only objects with mismatched tags")
if any(key not in decisions["vars"]["desired_object_tags"]
       for key in ("channel", "version", "splunkbase_id")):
    errors.append("FAIL: desired tags must include channel, version, and Splunkbase ID")

tag = by_name["Tag object-storage objects as the current latest release"]
if tag.get("loop") != "{{ splunkbase_plan }}" or tag.get("when") != "item.needs_tag":
    errors.append("FAIL: tag writes must be limited to plan entries with changed tags")
if tag.get("changed_when") != "item.needs_tag":
    errors.append("FAIL: tag writes must report change only when their tags differ")

latest_download = by_name["Download drifted entries from Splunkbase"]
if "ansible.builtin.get_url" not in latest_download:
    errors.append("FAIL: the current package download must keep its existing get_url behavior")
latest_upload = by_name["Upload synced archives to object storage"]
if latest_upload.get("loop") != "{{ splunkbase_to_sync }}":
    errors.append("FAIL: current packages must continue to upload to their stable keys")

manifest_read = by_name["Read existing manifest for scoped updates and change detection"]
if manifest_read.get("when") != "splunkbase_manifest_work_required | bool":
    errors.append("FAIL: one published-manifest read must serve latest and history updates")
if manifest_read.get("changed_when") is not False or manifest_read.get("no_log") is not True:
    errors.append("FAIL: the published-manifest read must remain read-only and quiet")

publish_decision = by_name["Decide whether the published manifest needs an update"]
expression = publish_decision["ansible.builtin.set_fact"]["splunkbase_manifest_needs_publish"]
if "splunkbase_published_manifest != splunkbase_manifest" not in expression:
    errors.append("FAIL: manifest publication must compare parsed JSON values")
if "splunkbase_published_manifest_readable" not in expression:
    errors.append("FAIL: an absent or unreadable manifest must be republished")
parse_manifest = by_name["Parse published manifest when readable"]
if not parse_manifest.get("rescue"):
    errors.append("FAIL: an unreadable published manifest must fall through to a publish")

publish = by_name["Publish manifest.json to object storage"]
if publish.get("changed_when") != "splunkbase_manifest_needs_publish | bool":
    errors.append("FAIL: manifest publication must report its actual update decision")
if not any("splunkbase_manifest_needs_publish" in condition
           for condition in publish.get("when", [])):
    errors.append("FAIL: an unchanged manifest must skip its S3 publish")
if "splunkbase_manifest_work_required | bool" not in publish.get("when", []):
    errors.append("FAIL: manifest publication must run for release-history updates")


def desired_tags(version, splunkbase_id):
    return {
        "channel": "latest",
        "version": version,
        "splunkbase_id": splunkbase_id,
    }


def sync_actions(entries, current_tags, current_manifest):
    """Model the task contract for two resolved entries."""
    tags_to_write = []
    versions_to_upload = []
    next_manifest = {}
    for entry in entries:
        desired = desired_tags(entry["version"], entry["id"])
        existing = current_tags.get(entry["filename"], {})
        if existing.get("version", "") != entry["version"]:
            versions_to_upload.append(entry["filename"])
        if existing != desired:
            tags_to_write.append(entry["filename"])
        next_manifest[entry["app_dir"]] = {
            "version": entry["version"],
            "filename": entry["filename"],
        }
    return {
        "uploads": versions_to_upload,
        "tags": tags_to_write,
        "publish": current_manifest != next_manifest,
    }


entries = [
    {"app_dir": "app_one", "filename": "one.tgz", "id": "101", "version": "1.0"},
    {"app_dir": "app_two", "filename": "two.tgz", "id": "202", "version": "2.0"},
]
unchanged_tags = {entry["filename"]: desired_tags(entry["version"], entry["id"])
                  for entry in entries}
unchanged_manifest = {entry["app_dir"]: {
    "version": entry["version"], "filename": entry["filename"]
} for entry in entries}
if sync_actions(entries, unchanged_tags, unchanged_manifest) != {
    "uploads": [], "tags": [], "publish": False,
}:
    errors.append("FAIL: an unchanged run must skip uploads, tag writes, and manifest publish")

changed_entries = [dict(entry) for entry in entries]
changed_entries[1]["version"] = "2.1"
changed = sync_actions(changed_entries, unchanged_tags, unchanged_manifest)
if changed != {"uploads": ["two.tgz"], "tags": ["two.tgz"], "publish": True}:
    errors.append("FAIL: one version change must upload and tag only its object and publish once")

if errors:
    print("\n".join(errors))
    sys.exit(1)

print("OK: unchanged syncs skip tag writes and publish; one changed version affects one object")
