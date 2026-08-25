# GitHub Discussions enablement

GitHub Discussions is enabled for the repository at
<https://github.com/synopsys0/postfader-fl-studio-mcp/discussions>. The default
GitHub categories remain in place while the maintainer evaluates the proposed
category names below. The suggested welcome posts are drafts only: no post is
published or pinned by this document.

## Maintainer enablement checklist

1. Confirm that the repository's **Settings → General → Features** page shows
   **Discussions** enabled, and that the public `/discussions` page loads while
   signed out (or in a private browser window).
2. If the maintainer approves the proposed taxonomy, rename or create the
   following categories with the exact names and descriptions. Until then,
   keep the existing default categories; do not describe the proposed names as
   live.
   Use the suggested category type where GitHub asks for one.

   | Name | Suggested type | Description |
   | --- | --- | --- |
   | Announcements | Announcement | Maintainer-published release, maintenance, compatibility, and safety notices. Keep questions and troubleshooting in Help & Setup. |
   | Help & Setup | Q&A | Installation, guided setup, doctor output, Universal Bridge, virtual MIDI, and local MCP-client questions. Share status fields only; redact private paths and session material. |
   | Showcase | Show and tell | Community PostFader workflows and learning notes. Share generic techniques and privacy-safe observations, never private projects, audio, presets, or credentials. |
   | Ideas | Ideas | Workflow proposals and improvement ideas for PostFader. This is a place to discuss needs, not a promise that a feature will be built or scheduled. |
   | Plug-in Compatibility | Q&A | Privacy-safe evidence about detected, read-profiled, and write-validated plug-in behavior. Reports are observations, not support guarantees; do not post raw scans or private session material. |

3. Make **Announcements** the announcement category and **Help & Setup** the
   default category if the repository settings offer those choices, after the
   taxonomy is approved and available.
4. After the categories are configured, publish the reviewed welcome posts
   below and pin the relevant post in each category. Until that separate
   maintainer action occurs, these remain drafts. Do not imply that they are
   public or pinned.
   If moderation policy requires it, lock only the announcement post. Keep
   replies open on Help & Setup, Showcase, Ideas, and Plug-in Compatibility.
5. The live Discussions URL is already linked from the repository README.
   Keep that link because Discussions is enabled; do not add links to a
   category or post until its URL is actually verified.
6. Review the first few posts for project privacy, credentials, raw logs,
   unsupported compatibility claims, and unsafe write instructions. Point
   security reports to the private advisory flow in [SECURITY.md](../SECURITY.md).

## Suggested pinned welcome posts

These drafts are intentionally ready to paste, but they should be reviewed
against the repository settings before publishing.

### Help & Setup — “Start here: safe setup help”

> Welcome to PostFader setup help. Start with the [setup and troubleshooting
> guide](setup.md), then run the doctor in read-only mode. When asking for help,
> include your operating system and architecture, FL Studio edition/version/build,
> PostFader package, AI client, virtual MIDI provider, and the doctor's status
> categories. Share only redacted status values such as `overall`, bridge
> deployment status, live status, `bridge_mode`, and
> `verified_writes_enabled`.
>
> Do not attach an FLP/project, audio, preset, raw log, credential, personal
> filesystem path, endpoint name, or screenshot containing private project
> information. PostFader starts read-only, and a healthy first connection should
> report `bridge_mode: read_only` and `verified_writes_enabled: false`.

### Showcase — “Share a PostFader workflow”

> Share a generic workflow that helped you inspect, diagnose, compose, or
> arrange with PostFader. Explain the goal, the tools or resources used, and
> what evidence you could observe. Please remove project titles, artist names,
> audio, presets, private paths, and client transcripts. A workflow story is
> not a compatibility or quality guarantee for another project.

### Plug-in Compatibility — “How to submit privacy-safe evidence”

> Use the [plug-in matrix](plugin-matrix.md) and its validation reporter before
> posting. `detected` means FL exposed the effect; `read-profiled` means the
> reported parameter surface was examined; `write-validated` means one
> representative write and an independent restore read passed in a blank,
> disposable project. None of these labels proves every parameter, preset,
> option, plug-in version, or audible result.
>
> Review the generated report before posting. Do not attach raw JSON or scans,
> logs, screenshots, presets, project files, audio, paths, or account data.
> Write validation may create undo history and dirty the project, so close the
> disposable project without saving. Maintainers review provenance and evidence
> before adding any row to the matrix.

### Ideas — “Tell us what would make the workflow better”

> Describe the producer task, the current safe workaround, and the smallest
> improvement that would help. Keep proposals compatible with PostFader's
> read-only startup, explicit session-only writes, later-idle-tick readback,
> honest partial evidence, bounded transport/filesystem behavior, and no
> automatic project save. An idea is discussion, not a commitment or a request
> to weaken those safety properties.

### Announcements — “PostFader Discussions: welcome and safe write testing”

> This space is for release, maintenance, compatibility, and safety notices.
> For setup questions use Help & Setup; for workflow examples use Showcase; for
> plug-in evidence use Plug-in Compatibility; and for proposals use Ideas.
> PostFader is a local MCP server with no hosted PostFader service or telemetry.
> Keep project files, audio, presets, credentials, private paths, raw logs, and
> unverified support claims out of public discussions. When testing a write,
> use a blank or disposable project, enable session writes only after a healthy
> read-only check, inspect later readback, stop after an ambiguous result, and
> disable writes again. PostFader never saves the project automatically; close
> the disposable project without saving. Report unreleased security
> vulnerabilities through the private process in SECURITY.md.
