# Public release privacy checks

Before every publication or patch, run:

```powershell
python -B tools/check_public_release.py --git
gitleaks dir . --redact=100 --ignore-gitleaks-allow --max-decode-depth=2 --max-archive-depth=2
gitleaks git . --log-opts="--all --full-history" --redact=100 --ignore-gitleaks-allow --max-decode-depth=2 --max-archive-depth=2
```

The Python guard checks private file paths, environment templates, common credential/identity patterns and every locally stored Git object, including unreachable blobs. It rejects unreviewed binary formats; the small included calibrator is pinned to the schema-reviewed byte hash. It reports locations and rule names, not matched secret values.

`.public-privacy-fixtures.json` records only exact path/rule/matched-byte fingerprints of clearly fictitious offline tests. A different value or file remains blocked. Do not add broad file/directory exemptions, remove scanner rules, or copy private configuration to make a check pass. A match must be inspected before it can be classified as a test fixture.

Gitleaks 8.30.1 provides a separate scanner. The workflow downloads its official release archive and verifies the pinned SHA256 before execution, uses full Git history, enables decoding, and ignores inline allow comments. Upstream action commits are pinned. The job has read-only repository permissions and does not persist checkout credentials.

The working system's credential values and private email are never embedded in this guard or in CI. Before the initial publication, a separate local check compares the complete payload/Git objects against those known values and common encodings in memory. Generic CI scanning supplements that check; it cannot know every future credential or personal fact. All checks being green is not an absolute guarantee of zero disclosure risk.

Keep `.env`, LLM profiles, raw audio/transcripts, account ledgers, databases, private notes, audit folders and original development Git history out of the publication repository. Commit using the GitHub-provided noreply identity. A credential that has already been exposed should be revoked/rotated; deleting its current file does not remove old Git objects.

The public snapshot is intentionally a clean history. Its initial commit date is the actual publication-preparation date. Past development is described only with verifiable, sanitized milestones, not fabricated backdated commits.

Sources: [Gitleaks usage](https://github.com/gitleaks/gitleaks), [pinned release](https://github.com/gitleaks/gitleaks/releases/tag/v8.30.1), [GitHub sensitive-data guidance](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository).


## Reviewed UI screenshots

The three JPEGs under `docs/images/ui-v3-*.jpg` are captured from the synthetic public UI, not the private workbench. Each was visually inspected and is pinned to its exact reviewed SHA256 in `REVIEWED_BINARY`. Changing an image requires a new review; no generic image or directory exemption is added. Real account/news/credential screenshots remain outside the release.
