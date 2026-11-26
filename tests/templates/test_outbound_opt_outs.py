#!/usr/bin/env python3
"""Cover outbound opt-out templates and their conditional deployment."""

from pathlib import Path

import yaml

from _render_env import ansible_env

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "roles/splunk_docker"
TEMPLATE_DIR = ROLE / "templates"
TASKS_DIR = ROLE / "tasks"
errors = []
env = ansible_env(TEMPLATE_DIR)

telemetry = env.get_template("telemetry.conf.j2")
telemetry_off = telemetry.render(splunk_docker_allow_internet_access=False)
telemetry_on = telemetry.render(splunk_docker_allow_internet_access=True)
telemetry_options = (
    "sendAnonymizedUsage",
    "sendLicenseUsage",
    "sendSupportUsage",
    "sendAnonymizedWebAnalytics",
    "precheckSendAnonymizedUsage",
    "precheckSendLicenseUsage",
    "precheckSendSupportUsage",
    "showOptInModal",
    "deprecatedConfig",
)
if "[general]" not in telemetry_off:
    errors.append("FAIL: telemetry.conf is missing [general] when internet access is disabled")
for option in telemetry_options:
    if f"{option} = false" not in telemetry_off:
        errors.append(f"FAIL: telemetry.conf is missing {option} = false")
if any(line.strip() for line in telemetry_on.splitlines() if not line.lstrip().startswith("#")):
    errors.append("FAIL: telemetry.conf renders settings when internet access is enabled")

app_conf = env.get_template("disabled_app.conf.j2")
app_off = app_conf.render(splunk_docker_allow_internet_access=False)
app_on = app_conf.render(splunk_docker_allow_internet_access=True)
if "[install]" not in app_off or "state = disabled" not in app_off:
    errors.append("FAIL: app.conf does not disable the app when internet access is disabled")
if any(line.strip() for line in app_on.splitlines() if not line.lstrip().startswith("#")):
    errors.append("FAIL: app.conf renders a disable stanza when internet access is enabled")


def tasks(path):
    return yaml.safe_load((TASKS_DIR / path).read_text())


def named(items, name):
    return next((item for item in items if item.get("name") == name), {})


outbound_tasks = tasks("configure_outbound_opt_outs.yml")
telemetry_deploy = named(outbound_tasks, "Disable Splunk usage telemetry")
telemetry_remove = named(outbound_tasks, "Remove Splunk telemetry opt-out when internet access is enabled")
app_deploy = named(outbound_tasks, "Disable apps that can initiate internet requests")
app_remove = named(outbound_tasks, "Remove offline app-disable overlays when internet access is enabled")
if telemetry_deploy.get("when") != "not (splunk_docker_allow_internet_access | bool)":
    errors.append("FAIL: telemetry.conf must deploy only with internet access disabled")
if telemetry_remove.get("ansible.builtin.file", {}).get("state") != "absent":
    errors.append("FAIL: telemetry.conf must be removed when internet access is enabled")
if telemetry_remove.get("when") != "splunk_docker_allow_internet_access | bool":
    errors.append("FAIL: telemetry.conf removal must run when internet access is enabled")
if app_deploy.get("loop") != ["splunk_secure_gateway", "Splunk_AI_Assistant_Cloud"]:
    errors.append("FAIL: app.conf overlays must cover both requested apps")
if app_deploy.get("when") != "not (splunk_docker_allow_internet_access | bool)":
    errors.append("FAIL: app.conf overlays must deploy only when internet access is disabled")
if app_remove.get("ansible.builtin.file", {}).get("state") != "absent":
    errors.append("FAIL: app.conf overlays must be removed when internet access is enabled")
if app_remove.get("when") != "splunk_docker_allow_internet_access | bool":
    errors.append("FAIL: app.conf removal must run when internet access is enabled")

launch = named(outbound_tasks, "Verify TLS for Splunk's Python runtime")
launch_line = launch.get("ansible.builtin.lineinfile", {})
if launch_line.get("regexp") != "^PYTHONHTTPSVERIFY=":
    errors.append("FAIL: lineinfile must replace the existing PYTHONHTTPSVERIFY setting")
if launch_line.get("line") != "PYTHONHTTPSVERIFY=1":
    errors.append("FAIL: Splunk's Python runtime must verify HTTPS certificates")
if "when" in launch:
    errors.append("FAIL: Python TLS verification must remain enabled in both internet modes")
if "Restart Splunk container" not in launch.get("notify", []):
    errors.append("FAIL: changing splunk-launch.conf must notify the restart handler")

registry = yaml.safe_load((ROLE / "vars/addons.yml").read_text())["splunk_docker_addons"]
assistant = next((item for item in registry if item.get("app_dir") == "Splunk_AI_Assistant_Cloud"), {})
if assistant.get("splunkbase_id") != 7245 or assistant.get("official_ai") is not True:
    errors.append("FAIL: cloud assistant must remain in the official AI mirror registry")
if "splunk_docker_allow_internet_access" not in assistant.get("install", ""):
    errors.append("FAIL: cloud assistant install must follow the internet-access setting")

main_tasks = tasks("main.yml")
install_index = next((i for i, task in enumerate(main_tasks) if task.get("name") == "Install apps and add-ons"), -1)
config_index = next((i for i, task in enumerate(main_tasks) if task.get("name") == "Enforce Splunk outbound opt-outs"), -1)
if install_index < 0 or config_index <= install_index:
    errors.append("FAIL: outbound overlays must run after app installation")

if errors:
    print("\n".join(errors))
    raise SystemExit(1)
print("PASS: telemetry, app-disable and Python TLS settings follow the outbound access mode")
