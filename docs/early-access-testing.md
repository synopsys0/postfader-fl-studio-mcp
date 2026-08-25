# Early-user activation guide

This guide is for the first external users and maintainers helping them
activate PostFader v0.20. It is a manual, privacy-preserving checklist. The
project has no telemetry or automatic analytics: activation results must be
shared only when a user chooses to report them.

## Activation path

Record each checkpoint locally, or in a sanitized issue description. Do not
paste a full doctor report if it contains paths or endpoint names.

1. **Downloaded the correct package.** Choose the Windows or macOS package for
   the host platform, or the matching Codex package when using Codex. The
   Claude Desktop MCPB is an optional client package, not a replacement for
   platform setup.
2. **Extracted it to a stable writable folder.** Keep the complete extracted
   folder in place because generated configuration points to it.
3. **Installer launched.** Confirm the platform installer starts without
   enabling writes or changing a project.
4. **Guided setup completed.** Let setup identify the FL Studio user-data
   directory, deploy the matching Universal Bridge, generate the selected
   client configuration, and finish its resumable flow.
5. **Virtual MIDI endpoint selected.** Choose one endpoint visible for both
   input and output. PostFader does not install or configure the provider.
6. **Universal Bridge loaded.** In FL Studio, assign the same endpoint and
   port for input and output, select Universal Bridge, and reload the script.
7. **Doctor passed.** Confirm `overall: pass`, a connected live status, a
   current bridge deployment, and the expected bridge/controller version.
8. **AI client connected.** Restart or reconnect the local MCP host and confirm
   it can call PostFader's read-only tools and resources.
9. **PostFader successfully read the project.** Begin with a non-mutating
   project or capability read. A healthy session remains
   `bridge_mode: read_only` with `verified_writes_enabled: false`.
10. **User enabled writes in a blank/disposable project.** Only after the
    read-only path works, explicitly request session write mode in a project
    that can be closed without saving. Never use a valuable project for the
    first write test.
11. **One supported change returned `verified: true`.** Use a narrow supported
    setter and inspect its receipt. Verification means the requested control
    state was observed after a later FL Studio update; it is not an artistic
    quality judgment or a rollback guarantee.
12. **Writes were disabled again.** Ask the client to disable write mode and
    confirm the session reports it disabled. Do not save the project as part of
    this test; PostFader never saves automatically.
13. **User successfully returned for a second session.** Quit and reconnect a
    fresh session, verify that startup is read-only again, and repeat a
    read-only inspection before considering activation complete.

If a mutation result is ambiguous, stop and inspect the current project state.
PostFader does not automatically replay an ambiguous mutation and does not
promise rollback or an undo point.

## What is safe to share

For a setup report, share only the smallest useful, sanitized facts:

- operating system and architecture;
- FL Studio edition, version, and build;
- PostFader package and version;
- Python version, when using a Python/source installation;
- AI client/host and version;
- virtual MIDI provider (not a private endpoint name);
- doctor status values: `overall`, `bridge_deployment.status`,
  `midi.enumeration_status`, `live.status`, `bridge_mode`, and
  `verified_writes_enabled`;
- whether the bridge appeared, whether Script Output reached
  `ready: MIDI SysEx`, whether a project read worked, and whether a write was
  attempted; and
- a short error message after removing paths, usernames, endpoint names,
  project names, credentials, and client transcripts.

Status fields are useful because they preserve the evidence boundary without
requiring a raw diagnostic dump. If a maintainer needs more information, ask
for one additional sanitized field rather than uploading a complete log.

## What must not be shared publicly

Do not attach or paste:

- FL Studio project files or backups;
- audio, stems, bounces, samples, presets, or generated MIDI from a private
  project;
- raw doctor output, raw logs, client transcripts, or crash dumps;
- credentials, tokens, cookies, license data, or private keys;
- personal filesystem paths, usernames, machine identifiers, or private
  endpoint names; or
- screenshots that expose project names, tracks, plug-in settings, paths, or
  other private session information.

The issue form's privacy confirmation is a reminder, not a substitute for
reviewing the text you are about to post. Do not use a public issue for an
unreleased security vulnerability; follow [SECURITY.md](../SECURITY.md).

## Reporting an activation problem

Use the [setup-help issue form](https://github.com/synopsys0/postfader-fl-studio-mcp/issues/new?template=setup-help.yml)
for installation, bridge, MIDI, doctor, or first-read failures. Include the
smallest reproducible sequence and the sanitized status values above. Do not
ask maintainers to infer a missing step from a private project or raw log.

For client or model tool-selection problems, report:

- the host and model family/version, if known;
- the user goal in one sentence;
- the tool or resource PostFader expected to be selected;
- the tool actually selected, if visible;
- whether the call was read-only, a plan, a direct setter, or a batch; and
- the sanitized result and whether the user stopped before any write.

Do not characterize one model's selection as a server capability failure until
the same request has been tried with an explicit tool instruction or a simple
read-only reproduction.

For a false-positive verification or misleading evidence report, stop making
further writes, preserve the project state without saving, and describe:

- the requested operation and target class, without project identity;
- the before-state guard, if one was supplied;
- the returned `verified` value and evidence basis;
- what a later manual read showed; and
- whether the response was a refusal, timeout, ambiguous transport result, or
  an observed-but-different control state.

PostFader should never claim stronger evidence than it observed. Do not replay
an ambiguous mutation to obtain a cleaner report.

For plug-in compatibility, follow the [plug-in matrix campaign](plugin-matrix.md)
and submit the generated privacy-safe report through the plug-in validation
form. State whether the observation was detected, read-profiled, or
write-validated. A representative write must use a disposable project and
may create undo history or dirty it; close without saving. Do not attach raw
scans, settings, presets, audio, paths, or screenshots.

## No telemetry

PostFader does not collect activation events, project data, audio, client
transcripts, tool-selection metrics, or plug-in reports automatically. There is
no hosted PostFader service and no background analytics opt-in hidden in setup.
Users and maintainers may exchange only the sanitized facts they deliberately
include in an issue or discussion.
