# Releasing Ask AI

Releases are built and published by `.github/workflows/release.yml` when a tag starting with `v` is pushed.

## 1. Bump the version

1. Set `version` in `addon/manifest.ini`, for example `1.2.0`. For a beta use a version with a dash, for example `1.2.0-beta1`.
2. Update the places that name the version or the file name `askAI-<version>.nvda-addon`:
   `README.md`, `addon/doc/en/readme.html`, `tester/TESTING.txt`, and the expected version in `tests/test_packaging.py`.
3. Check locally:

   ```
   python3 -m unittest discover -s tests -v
   python3 build.py        # writes askAI-<version>.nvda-addon
   ```

4. Commit.

## 2. Tag and push

```
git tag v1.2.0
git push origin v1.2.0
```

For a prerelease, the manifest says `1.2.0-beta1` and the tag is `v1.2.0-beta1`.

The workflow then:

* runs the tests,
* fails if the tag (without the leading `v`) is not exactly the manifest version,
* builds `askAI-<version>.nvda-addon`,
* writes `SHA256SUMS.txt` (`sha256sum` format: `<hex>  <file name>`),
* creates the GitHub release with `askAI-<version>.nvda-addon`, `SHA256SUMS.txt` and `TESTING.txt`
  (marked as a prerelease when the tag contains `-`).

Always keep `SHA256SUMS.txt` in the release. The add-on's own update checker downloads it from the same
release and refuses to install an update whose checksum is missing or does not match.

## 3. Submit to the NVDA Add-on Store

Open <https://github.com/nvaccess/addon-datastore/issues/new?template=registerAddon.yml> and fill in the form.
Its fields (from `.github/ISSUE_TEMPLATE/registerAddon.yml` in nvaccess/addon-datastore) are:

| Field label | Value |
| - | - |
| Download URL | `https://github.com/shea786/nvda-ask-gpt/releases/download/v<version>/askAI-<version>.nvda-addon` |
| Source URL | `https://github.com/shea786/nvda-ask-gpt` |
| Publisher | `Asif Ali` |
| Channel | `beta` (the channel chosen for the first submission, 1.2.0; use `stable` once the add-on has been through beta) |
| License Name | `GPL v2` |
| License URL | `https://www.gnu.org/licenses/old-licenses/gpl-2.0.html` (the form's prefilled `https://www.gnu.org/licenses/gpl-2.0.html` is also fine) |

For 1.2.0 (first submission, **beta** channel) the Download URL is
`https://github.com/shea786/nvda-ask-gpt/releases/download/v1.2.0/askAI-1.2.0.nvda-addon`.
The Download URL must start with `https`, end with `nvda-addon` and always serve the same file, so never replace
a release asset after submitting it; release a new version instead.

The store keys submissions by a numeric version (`addonVersionNumber`, major.minor.patch, parsed from the
manifest version by nvaccess/addon-datastore-validation). `1.2.0-beta1` and `1.2.0` therefore both become 1.2.0.
If you submit betas to the store, give them a lower number than the final release (for example `1.1.91-beta1`),
or only publish betas on GitHub.

## Updates for users

* Copies installed from the NVDA Add-on Store are updated by the store (NVDA menu, Tools, Add-on Store,
  Updatable add-ons). The add-on detects this and skips its own GitHub update check for them.
* Copies installed from a `.nvda-addon` file check
  `https://api.github.com/repos/shea786/nvda-ask-gpt/releases/latest` at most once a day (setting
  "Check for updates automatically"), or when the user chooses "Check for updates". Prereleases are only offered to
  users who run a prerelease. The download goes through NVDA's own add-on installation dialogs.
