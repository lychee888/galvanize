# Changelog

## 0.2.4 — wake sessions get real tool access

Hermes wake sessions previously always ran on the gateway's restricted
webhook toolset (4 tools, no terminal/file), so any trigger prompt with
shell steps silently failed. Routes now carry an explicit `toolsets`
override:

- **Parity by default.** `register_route` writes the gateway `default`
  distribution's toolset names (`browser, file, image_gen, terminal,
  vision, web`) into every newly registered hermes route — the same tool
  surface a cron/main Hermes session gets.
- **`--wake-tools` (CLI) / `wake_tools` (plugin + MCP trigger_add).**
  Comma-separated names grant exactly that subset; `none` opts back into
  the restricted alert-only posture (the security knob for high-volume or
  untrusted inbound feeds). Rejected for non-hermes wakes. The explicit
  choice is recorded in the trigger's wake cfg so re-adds keep it.
- **doctor** reports each hermes route's effective state: parity /
  `toolsets override: …` / restricted (explicit), and WARNs on key-less
  routes registered by pre-0.2.4 galvanize.
- **Migration.** `galvanize doctor --fix` (and `daemon install`) refresh
  managed key-less routes to parity, honoring recorded choices and
  leaving manual grants untouched. Only the `toolsets` key is rewritten.

Earlier releases: see git history (`v0.2.3` = himalaya secret import +
doctor drift warnings; `v0.2.2` = secrets/health hardening).
