# Supply-chain checks and release provenance

PostFader treats package integrity as a release concern without pretending that
an automated check proves the application is safe on every workstation. The
repository has no hosted PostFader service, and a release does not bundle FL
Studio, a virtual MIDI provider, an AI client, or a user's plug-ins.

## Pull-request and scheduled checks

`.github/workflows/security.yml` contains the current automated checks:

- CodeQL analyzes the Python source on pushes to `main`, pull requests, and a
  weekly schedule. Its findings are uploaded to GitHub code scanning.
- GitHub Dependency Review runs on pull requests and blocks newly introduced
  high or critical dependency vulnerabilities. It is intentionally scoped to
  dependency changes rather than being a release gate for every historical
  advisory.
- `pip-audit` runs against the installed project dependency set and publishes a
  short-lived JSON artifact. Its finding step is non-blocking while maintainers
  evaluate advisory quality and false positives. A warning is not a waiver:
  maintainers should open or update a tracked issue, check the affected code
  path, upgrade where practical, and record any accepted risk before release.

Dependabot is configured for both the `pip` and `github-actions` ecosystems.
Action references in workflows use full commit SHAs; the version comment beside
each SHA is only an audit hint, not the thing GitHub resolves. Review action
updates as code, and verify that the new SHA is the intended upstream release.

The checks use read-only repository permissions except where GitHub's code
scanning or artifact-attestation APIs require a narrowly scoped write
permission. They do not upload project files, audio, presets, private logs, or
telemetry.

## Release SBOM

Future tagged releases generate
`PostFader-vX.Y.Z-SBOM.cdx.json` in CycloneDX 1.6 format. The build first installs
the wheel into an isolated temporary environment, runs `pip inspect --local`,
and follows the installed project's runtime dependency closure. Build tools,
test extras, and the audit tool itself are not treated as shipped runtime
dependencies.

The SBOM also records each wheel, source archive, MCPB, and platform ZIP as a
file component with its SHA-256 digest. This makes the SBOM useful alongside
`PostFader-vX.Y.Z-SHA256SUMS.txt`; it does not replace the checksum file.

The SBOM deliberately identifies these package-boundary exclusions:

- FL Studio and its license are not bundled;
- virtual MIDI software, drivers, and endpoints are not bundled;
- Claude, Codex, Cursor, Grok, T3 Code, and other AI clients are not bundled;
- user-installed FL Studio plug-ins are not bundled; and
- user projects, audio, presets, credentials, and private paths are not release
  inputs.

The SBOM describes the exact build inputs observed by the release job. It is not
a claim that Python wheels or all release artifacts are byte-for-byte
reproducible. The setup ZIP builder has separate tests for its fixed timestamps,
file modes, ordering, and repeated-output digest; that narrower deterministic
ZIP result should not be generalized to the whole release.

## Attestations

Future tagged release assets are attested before PyPI and GitHub publication:

- the wheel, source archive, four platform setup ZIPs, MCPB, and SBOM receive a
  signed build-provenance attestation; and
- the release packages receive an SBOM attestation that points to the generated
  CycloneDX document.

The attestation is stored by GitHub's attestations service and is not a
replacement for the downloaded files or their checksums. The workflow uses
OIDC only for the short-lived signing identity and grants only
`id-token: write` and `attestations: write` to the dedicated tag-only
attestation job. The job
does not receive release-write credentials or the optional
`artifact-metadata: write` permission because it does not push artifacts to a
registry's linked-artifacts page.

After a future release, a maintainer can verify a downloaded artifact with the
GitHub CLI (the repository must be supplied so GitHub can locate its
attestation):

```bash
gh attestation verify PostFader-vX.Y.Z-macOS.zip \
  --repo synopsys0/postfader-fl-studio-mcp
```

Use the exact downloaded filename for a wheel, source archive, MCPB, ZIP, or
SBOM. Then compare its SHA-256 value with the matching line in the release
checksum file. `gh attestation verify` validates GitHub's signed attestation;
the checksum comparison validates that the local bytes match the published
release asset.

The authoritative references are GitHub's [artifact-attestation
guide](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations),
the [`actions/attest` documentation](https://github.com/actions/attest), the
[CodeQL action](https://github.com/github/codeql-action), and the
[Dependency Review action](https://github.com/actions/dependency-review-action).

## Triage and retry

When a security job reports a finding:

1. Record the workflow run, commit, package, advisory ID, and affected scope.
2. Distinguish a runtime vulnerability from a development-only or unreachable
   dependency and confirm the installed version independently.
3. Prefer a compatible fixed release, then rerun the hermetic suite and package
   verification. Do not silently suppress an advisory in workflow YAML.
4. If the finding is a false positive or cannot be fixed without a behavioral
   or compatibility change, document the rationale and owner in the issue or
   release review.
5. If an attestation or release upload fails before external publication,
   correct the branch and rerun it. If an external artifact was already
   published, never move or recreate the public tag; use the retry guidance in
   [`RELEASING.md`](../RELEASING.md) or publish a new patch version.

The published `v0.20.0` assets were created before these future-release SBOM
and attestation steps. This pass does not rewrite, replace, or retroactively
claim provenance for that release.
