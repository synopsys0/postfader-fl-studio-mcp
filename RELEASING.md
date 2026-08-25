# Releasing PostFader

This is the maintainer checklist for a PostFader release. Keep release work
small, reviewable, and reproducible. The release workflow is the final gate:
it validates the public tree, runs the hermetic suite on Windows and macOS,
builds and inspects the Python distributions, validates the MCPB and platform
bundles, publishes to PyPI and the MCP Registry, and attaches the verified
assets and SHA-256 checksums to GitHub.

## Before preparing a version

- Confirm the change is on the intended `main` commit and the worktree is
  clean.
- Review the current [setup guide](docs/setup.md), [security policy](SECURITY.md),
  tool contracts, plug-in evidence, and the previous release page. Keep the
  public claims aligned with observed evidence.
- Prepare `docs/releases/vX.Y.Z.md` with user-facing notes. Explain package
  selection, upgrade steps, qualified environments, limitations, and checksums;
  do not rely on GitHub's generated pull-request list as the final page.
- Use a blank or disposable project for every live qualification and do not
  commit projects, audio, presets, screenshots, paths, logs, or private
  session evidence.

## Synchronize the version

Use `pyproject.toml` as the package version source. Before tagging, synchronize
the same `X.Y.Z` value in every public version declaration, at minimum:

- `[project].version` in `pyproject.toml`;
- `server.json` (MCP Registry metadata and package version);
- `manifest.json` (MCPB metadata); and
- README badges, release download links, supported-version text, and the
  release page where a version is intentionally named.

Search before committing and inspect generated metadata rather than assuming a
replacement was complete:

```bash
rg -n '0\.20\.0|v0\.20\.0|version' pyproject.toml server.json manifest.json README.md docs
python -m build
python scripts/verify_distribution.py --dist-dir dist
```

The release workflow refuses a tag that does not equal `v` plus
`pyproject.toml`'s version, or public registry/MCPB versions that disagree.
Do not change the package name, entry points, registry name, or compatibility
identifiers merely to change public capitalization: the product name is
`PostFader`, while those identifiers remain compatibility surfaces.

## Required checks and qualification

Run the hardware-free checks from a clean environment before pushing a tag:

```bash
python -m pip install --upgrade build twine
python scripts/check_public_tree.py
python scripts/run_safe_tests.py
python -m build
python -m twine check dist/*
python scripts/verify_distribution.py --dist-dir dist
python scripts/clean_wheel_smoke.py --dist-dir dist
python scripts/build_mcpb.py --output-dir mcpb-dist
python scripts/build_release_bundles.py --output-dir release-bundles
```

The release CI must be green for the SDK compatibility checks, the safe suite,
and package builds on Windows and macOS with Python 3.10 and 3.14. Review the
extracted generic and Codex launchers on both platforms. The release workflow
also validates `server.json` with the pinned MCP Registry publisher before any
external publication. A manual `workflow_dispatch` run is a rehearsal only,
even when it is selected against a tag; external publication jobs require a
tag `push`. Review the Security workflow's CodeQL result and any
Dependency Review or `pip-audit` advisories before tagging; the advisory report
is intentionally non-blocking while its signal is triaged.

For v0.20, the live qualification environments were macOS 27.0 arm64 with FL
Studio Producer Edition 26.1.3 build 5336 and the built-in IAC bus, and Windows
11 x64 with FL Studio Producer Edition 26.1.4 build 5589. Record only the
generic result and tested revision in public documentation; keep operator logs
outside the repository. Confirm the normal launch is read-only and use later
readback for supported writes. Never describe hermetic CI as live FL Studio
coverage.

## Build and publish

1. Merge the reviewed version-sync and release-note changes.
2. Confirm the exact commit, version, generated package metadata, and CI status.
3. Create the release tag using the signed-tag procedure below and push it.
4. Let the tag workflow complete. It builds the wheel and source archive,
   validates the MCPB, builds the generic and Codex Windows/macOS ZIPs, runs
   native smoke tests, publishes the Python distributions to [PyPI](https://pypi.org/),
   generates a CycloneDX SBOM, creates GitHub artifact attestations, publishes
   `server.json` to the [MCP Registry](https://registry.modelcontextprotocol.io/),
   and creates or updates the GitHub release.
5. Verify PyPI shows the intended version and that the MCP Registry entry
   `io.github.synopsys0/postfader-fl-studio-mcp@X.Y.Z` is present.
6. Verify the GitHub release has these assets: generic Windows/macOS ZIPs,
   Codex Windows/macOS ZIPs, the `.mcpb`, one wheel, one source archive, and
   `PostFader-vX.Y.Z-SBOM.cdx.json`, plus
   `PostFader-vX.Y.Z-SHA256SUMS.txt`.

Useful checks after publication:

```bash
gh release view vX.Y.Z --repo synopsys0/postfader-fl-studio-mcp
python -m pip index versions postfader-fl-studio-mcp
gh attestation verify PostFader-vX.Y.Z-macOS.zip \
  --repo synopsys0/postfader-fl-studio-mcp
```

If GitHub CLI authentication and repository permissions are available, replace
the generated release body with the reviewed page:

```bash
gh release edit vX.Y.Z \
  --repo synopsys0/postfader-fl-studio-mcp \
  --title 'PostFader vX.Y.Z' \
  --notes-file docs/releases/vX.Y.Z.md
```

## GitHub download-link and installation check

Before calling a release complete, check every README download URL against the
actual release assets. The `latest/download` links must resolve to the intended
generic and Codex package names. Also test one fresh installation path on each
qualified platform:

- download the matching ZIP into a stable writable folder and run its dry-run
  installer before accepting changes;
- complete guided setup with the matching Universal Bridge and exact endpoint;
- reload the FL Studio script, run the doctor, and confirm `read_only` and
  `verified_writes_enabled: false`;
- connect the client and perform a read-only project inspection; and
- only in a blank/disposable project, enable writes, test one supported change
  with `verified: true`, inspect the later readback, then disable writes again.

For the Python path, install the published wheel into a clean temporary
environment and run the package verification/smoke command. For the MCPB path,
confirm that the extension opens only after the matching platform setup; the
MCPB does not install Universal Bridge or configure MIDI.

## Checksums and release notes

The GitHub job computes SHA-256 values for every downloadable artifact,
including the CycloneDX SBOM, and publishes them in
`PostFader-vX.Y.Z-SHA256SUMS.txt`. Do not hand-edit a digest. The release job
also attests the wheel, source archive, MCPB, platform ZIPs, and SBOM before
publication. See [supply-chain.md](docs/supply-chain.md) for the verification
and triage workflow.

If an asset is rebuilt, rebuild and verify the complete set before publishing a
replacement asset, then ensure the checksum file is regenerated from the same
directory. The release page should tell users how to compare the digest on
Windows PowerShell and macOS/Linux.

Release notes should be written for users, not copied from generated commits.
At minimum, cover:

- what the release is and its read-only/no-automatic-save safety posture;
- which package each platform/client should use;
- major user-visible additions and evidence boundaries;
- the upgrade path from the previous supported release;
- exact qualified environments;
- limitations and unsupported claims; and
- package checksums and links to setup, contracts, security, and issues.

## Retry, rollback, and failure handling

Treat PyPI, the MCP Registry, and a public GitHub release as durable external
state. If a pre-publication CI job fails, fix the branch and rerun the checks;
do not tag an unverified commit. If a tag workflow fails after an artifact has
been published, inspect the exact failure and retry only the failed workflow
step when the inputs are unchanged. A GitHub release upload can be retried with
the same immutable tag, but every replaced asset must be rebuilt and its
checksum regenerated together.

Never delete, force-move, recreate, or rewrite a published tag to repair a
release. Publish a new patch version when code or package contents need to
change. Moving a public tag is worse for user trust and provenance than leaving
an existing unsigned tag intact. Do not claim a release is complete until the
GitHub assets, PyPI version, MCP Registry version, checksum file, and one clean
installation check all agree.

## Signed annotated tags for future releases

`v0.20.0` is already public and unsigned. Leave that tag exactly as published:
do not delete it, force-push it, move it, or recreate it. Starting with the next
release, use a signed annotated tag after all version synchronization and CI
checks pass.

For a GPG signing key:

```bash
git config user.signingkey <GPG_KEY_ID>
git config tag.gpgSign true
git tag --sign --annotate vX.Y.Z --message 'PostFader vX.Y.Z'
git show --show-signature vX.Y.Z
git push origin vX.Y.Z
```

For an SSH signing key, configure Git's SSH signing format and allowed signer
file first, then use the same `git tag --sign --annotate` and verification flow:

```bash
git config gpg.format ssh
git config user.signingkey ~/.ssh/release_signing_key
git config gpg.ssh.allowedSignersFile ~/.config/git/allowed_signers
git tag --sign --annotate vX.Y.Z --message 'PostFader vX.Y.Z'
git show --show-signature vX.Y.Z
git push origin vX.Y.Z
```

Confirm on GitHub that the tag is shown as verified before relying on the tag
as a release input. A signed tag improves provenance for future releases; it is
not a reason to rewrite the already-public v0.20.0 history.
