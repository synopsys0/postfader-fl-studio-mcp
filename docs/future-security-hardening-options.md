# Future security hardening options

Status: design backlog only. None of the options in this document is
implemented by the post-release documentation pass. They require a separate
design review, compatibility testing, and an explicit release decision. The
current v0.20 safety properties remain the baseline: read-only startup,
session-only write authorization, explicit Master protection, later-idle-tick
readback, no automatic replay after ambiguous mutation outcomes, no automatic
project save, honest partial/unverified evidence, bridge-source provenance,
strict contracts, and bounded MIDI/filesystem behavior.

PostFader has no hosted service and no telemetry. These options must not be
implemented by silently collecting usage data or by making a future security
control depend on a remote service. The local single-user workstation threat
model remains the current assumption unless an option is specifically designed
to narrow it.

## Decision rules

For each option, a design proposal must identify:

- the threat addressed and the attacker/precondition it assumes;
- the user-experience cost and support burden;
- compatibility and migration risks for Windows, macOS, FL Studio, MCP
  clients, installers, virtual MIDI providers, and existing projects;
- whether the control is opt-in, opt-out, or a new default, with a reason;
- minimum hermetic, package, and live-FL tests before release;
- how refusal, unavailable, partial, or unverified evidence is represented;
- how a user can recover safely without automatic replay, rollback, or save.

A hardening option is not a license to weaken current evidence boundaries. In
particular, an undo request is not a guaranteed undo point, a successful setter
is not artistic verification, and a user-present boolean is not identity
authentication.

## Option register

| Option | Threat addressed | UX cost | Compatibility risk | Default recommendation | Minimum test requirement |
| --- | --- | --- | --- | --- | --- |
| Out-of-band human confirmation for enabling writes | A compromised, misleading, or over-permissive MCP client sets the current client-supplied confirmation field without a real human decision | Adds a second UI/app step, possible context switching, and an unavailable path for headless clients | Requires a trusted local UI/channel and a clear client integration; may break terminal-only workflows and complicate macOS/Windows packaging | Opt-in experiment first; do not replace the current session gate until a supported confirmation channel is available on both platforms | Fake confirmation state-machine tests; refusal on missing/stale/forged confirmation; client disconnect/race tests; live disposable-project enable/disable qualification; verify no automatic enable |
| Configurable write-mode inactivity timeout | A user leaves a write-enabled bridge session unattended or a client/model retains capability longer than intended | May interrupt a long deliberate editing session and require re-enabling; timeout discovery must be clear | Timer semantics must survive FL idle scheduling, long scans, bridge reconnects, and clients with no lifecycle hooks | Opt-in per user/session before evidence supports a safe default; never extend a timeout silently | Deterministic clock tests; timeout during reads, batches, peak watches, and in-flight mutation; later handshake proof; ensure no mid-command replay or save; live timing qualification |
| Lock write mode after disconnect or prolonged inactivity | A disconnected MCP client, stale bridge, or abandoned workstation leaves the mutation allowlist open | Can surprise users during network/client restarts and require explicit reauthorization | Must distinguish temporary transport loss from a real client/session change; may conflict with reconnect workflows and process locks | Consider opt-in first; default only after reconnect semantics and clear UI are tested | Disconnect/reconnect state-machine tests; in-flight mutation ambiguity tests; session fingerprint invalidation; verify read-only recovery and no automatic mutation replay; live reconnect test |
| Stricter approved-root policies for audio files | A malicious or mistaken client names a sensitive absolute audio path outside an intended project/output area | Adds setup prompts and path configuration; can reject legitimate reference files and complicate external-drive workflows | Must handle Windows drive letters, macOS volumes, symlinks, existing configs, recent-bounce roots, and package upgrades | Opt-in approved roots initially; keep current explicit absolute-path behavior documented until migration exists | Traversal/symlink/canonicalization tests; Windows/macOS path cases; root allow/deny/refusal contracts; decoded-memory tests; verify hashes/paths remain honest and no unrestricted search appears |
| Signed native installers | A local or network attacker replaces a downloaded installer/package, or users cannot authenticate package origin | Requires signature tooling, key verification, trust UI, and recovery instructions; unsigned local builds need a clear path | Key rotation, platform notarization/signing, CI credentials, Windows/macOS policies, and existing ZIP/MCPB/PyPI distribution may diverge | Opt-in verification where supported, then make signed artifacts the release default once reproducibly operated; never falsely claim a signature for old v0.20 artifacts | Key generation/rotation and compromise-recovery drill; signature verification on clean Windows/macOS hosts; tampered artifact refusal; package content/hygiene tests; verify signature scope and release asset mapping |
| Embedded Python runtime | A compromised or incompatible system Python changes dependency resolution or executes a different runtime than qualification assumed | Larger download, slower updates, more disk space, and fewer opportunities for users to use their own Python/debugger | Native wheels, Python 3.10–3.14 support, platform architecture, licensing, plug-in/FL environment paths, and MCP client configuration may change | Evaluate as an optional managed package first; do not make it default without size, update, and support evidence | Reproducible clean-host installs on qualified Windows/macOS architectures; dependency isolation; no network at startup; package-hygiene and path tests; setup/doctor/upgrade tests; live read-only and disposable write qualification |
| Installation into an application-data directory | A writable/downloaded ZIP or install tree is replaced or modified in a broad user folder; reduce accidental execution from a project directory | Less visible install location, permission prompts, upgrade/uninstall complexity, and harder manual inspection | Windows/macOS app-data conventions, client config paths, FL user-data paths, portable installs, existing bridge deployment, and rollback/retry behavior | Offer as an explicit installation mode first; preserve current stable writable-folder setup until migration is proven | Path resolution and permissions tests per platform; upgrade atomicity and stale-version tests; client config/doctor discovery tests; package tree and bridge-stamp checks; no project files under the install root |
| Make the downloaded ZIP disposable after installation | A user later runs a stale or tampered copy from Downloads, or confuses an installer bundle with the installed package | Removing/archiving the ZIP can surprise users, hinder offline reinstall, and conflict with users who keep release archives | Must not remove the wrong file, alter release assets, or break repair/offline workflows; browser/download folder permissions vary | Opt-in cleanup prompt or documented manual cleanup; never automatic deletion by default | Exact-target/path and consent tests; interrupted install/retry tests; verify installed copy is complete and stamped; ensure no broad recursive deletion, no deletion of the public release archive, and no hidden telemetry |

## Detailed design notes

### Out-of-band human confirmation for enabling writes

The current confirm_user_present argument is deliberately limited: it is a
literal client-supplied assertion after an explicit present-user request, and
it is checked with provenance and the current session fingerprint. It is not
proof that a human is present or that the client displayed the request.

An out-of-band design could use a local OS dialog, a short-lived local
challenge displayed by the bridge, or a client API with a user-approved
capability prompt. The design must state which process owns the prompt, what
the user sees, how the challenge is bound to the exact bridge/session, and how
headless/terminal users refuse safely. It must never turn confirmation into
remote authorization or store a durable write token in a project or client
configuration.

### Configurable write-mode inactivity timeout

A timer should cover the session capability, not individual setter responses.
The design must define whether reads reset the timer, whether a long FL idle job
counts as activity, and what happens when a timeout races an expected-before
check or an in-flight mutation. The safe outcome for an ambiguous mutation is
unknown/unverified plus read-only recovery, never an automatic retry or
rollback. A timeout must be observable in capabilities/status and must not be
silently represented as a successful disable.

### Lock write mode after disconnect or prolonged inactivity

This option overlaps with a timeout but addresses lifecycle events. Define
“disconnect” for stdio client exit, bridge MIDI loss, process lock loss,
Windows/macOS sleep, and a client that remains connected but sends no calls.
The bridge and host must agree on session invalidation; a reconnect should not
inherit a previous write capability merely because a process ID or endpoint
name matches. Reads can recover with warnings where safe; mutations must wait
for a fresh handshake and explicit enablement.

### Stricter approved-root policies for audio files

The current policy intentionally permits caller-selected absolute audio files
with extension, regular-file, size, duration, path-shape, and decoded-memory
checks, while recent-bounce discovery is restricted to bounded known roots.
An approved-root option would add a user-configured allowlist and should
preserve a clear distinction between direct analysis and recent-bounce
discovery. Canonicalization, symlink handling, case sensitivity, drive/volume
mounts, and a root being removed must be specified. Do not imply that an
approved root makes the MCP client trustworthy or that a returned path/hash is
private.

### Signed native installers

Signatures prove artifact origin/integrity for a defined key and asset set;
they do not prove that FL Studio, a virtual MIDI provider, a plug-in, a model
provider, or an MCP client is safe. Document key custody, CI permissions,
rotation, revocation/compromise response, offline verification, and what a
user can do when a signature is unavailable. The unsigned v0.20 tag and
artifacts must not be rewritten merely to retrofit a claim. Future signed
artifacts should identify their exact release asset and checksum.

### Embedded Python runtime

An embedded runtime could remove system-Python drift but increases the trusted
package size and native dependency surface. Decide whether the runtime is
bundled in each platform ZIP, how licenses/notices are shipped, whether
advanced users can still use a wheel/source distribution, and how security
updates are delivered. The startup path must retain read-only behavior and no
network/telemetry. The bridge remains deployed into FL Studio's separate
embedded environment; bundling PostFader Python does not bundle FL Studio,
plugins, or a virtual MIDI provider.

### Installation into an application-data directory

The install root must be a narrow, platform-appropriate directory selected by
the installer, not a broad recursive target and not an FL Studio project
folder. Define ownership, permissions, update atomicity, stale bridge cleanup,
and how users inspect/remove the installation. Client configuration should point
to the selected version explicitly, and the doctor should report sanitized
locations without exposing personal paths in shareable diagnostics.

### Make the downloaded ZIP disposable after installation

A cleanup action must name one exact downloaded archive selected by the user or
installer. It must never infer a broad directory, recursively delete a home or
workspace, remove a release asset, or erase a project. The safest first
implementation is a visible “move to trash/archive” choice after a successful
installation check, with an offline repair path. The installed package must
still pass bridge-source stamping, package hygiene, and verification checks
before cleanup is offered.

## Deliberately deferred

Do not implement any option in this document in a patch merely because it is
listed. A separate proposal should select one option, define migration and
failure behavior, add tests, qualify both platforms, update setup/security
documentation, and explain how users recover. Until then, the current
read-only/session-gated model and explicit limitations remain the source of
truth.
