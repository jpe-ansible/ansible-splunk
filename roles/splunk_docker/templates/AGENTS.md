# Templates in this role

This repository is for Ansible configuration of the Splunk deployment only.

**Never add user-facing Splunk content here.** That means reports, saved searches, alerts, dashboards,
macros and lookups that a person reads or that page someone.

- Put such content in a Splunk app repository in the `jpe-splunk` organization: the existing app for that
  subject, or a new app when none fits.
- Do not add a file under `savedsearches/`, and do not extend one that is there. The files there now are
  legacy; a change to one of them is a move into its Splunk app.
- Templates here configure the deployment itself: indexes, inputs, server, limits, the container and its
  firewall.
- A pull request that adds or changes user-facing content in this repository is in the wrong repository.
  Close it and open the change against the Splunk app.
