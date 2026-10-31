# Releasing Ask AI

Releases are built, signed and published by `.github/workflows/release.yml` when a tag starting with `v` is pushed.

## 0. One-time setup: the release environment and the signing key

Installed copies of Ask AI only install an update whose `SHA256SUMS.txt` carries a valid Ed25519 signature
(`SHA256SUMS.txt.sig`) by the release key. The public half is embedded in
`addon/globalPlugins/askAI/updater.py` (`SIGNING_PUBLIC_KEY`); the private half never enters the repository.

* **The private key** is at `~/.config/askai-release/ed25519.key` on the maintainer's machine (PKCS#8 PEM,
  file mode 600 in a mode 700 folder). Back it up offline (for example an encrypted USB stick or a password
  manager). Never paste it into an issue, a log or a chat.
* **The `release` environment**: in the GitHub repository open Settings, Environments, New environment, name it
  `release`, and limit it to tags matching `v*` under "Deployment branches and tags". Optionally tick **Required reviewers** and add
  yourself, so every release run waits for your approval (stronger protection against a stolen token; off by default).
* **The secret**: add the key as the environment secret `ASKAI_SIGNING_KEY` of `release`, either in
  Settings, Environments, release, Environment secrets, or with

  ```
  gh secret set ASKAI_SIGNING_KEY --env release --repo shea786/nvda-ask-ai < ~/.config/askai-release/ed25519.key
  ```

  The workflow fails with a clear error if the secret is missing, writes it only to a private temporary file
  (`umask 077`, deleted right after signing), signs with `openssl pkeyutl -sign -rawin`, and then checks the
  signature with the add-on's own pure-Python verifier (`tools/release/verify_sums.py`) before publishing.
* **Key rotation**: generate a new key, put its public half into `SIGNING_PUBLIC_KEY`, and publish that version
  signed with the **old** key (installed copies only trust the old key until they have updated). Then replace
  the `ASKAI_SIGNING_KEY` secret with the new key for later releases. If the old key is lost, users must install
  the next version by hand.
* A release without `SHA256SUMS.txt.sig`, or with a signature that does not match, is refused by installed copies
  ("the downloaded file could not be verified"), so never publish one by hand.

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
* signs it into `SHA256SUMS.txt.sig` (64-byte Ed25519 signature) and verifies that signature,
* creates the GitHub release with `askAI-<version>.nvda-addon`, `SHA256SUMS.txt`, `SHA256SUMS.txt.sig` and
  `TESTING.txt` (marked as a prerelease when the tag contains `-`).

The job runs in the `release` environment, which holds the signing key (and, if you turned it on, waits for the
required reviewer first). Actions are pinned to full commit SHAs (with the version in a comment) and the checkout does not keep the
token (`persist-credentials: false`); each job only gets the permissions it needs.

Always keep `SHA256SUMS.txt` and `SHA256SUMS.txt.sig` in the release, and never replace release assets. The add-on's
update checker only accepts files under `https://github.com/shea786/nvda-ask-ai/releases/download/<tag>/` for the
release's own tag, verifies the signature before trusting any checksum, checks the add-on's SHA-256, and requires
the add-on's `manifest.ini` to say `name = askAI` and the release's version.

## 3. Submit to the NVDA Add-on Store

Open <https://github.com/nvaccess/addon-datastore/issues/new?template=registerAddon.yml> and fill in the form.
Its fields (from `.github/ISSUE_TEMPLATE/registerAddon.yml` in nvaccess/addon-datastore) are:

| Field label | Value |
| - | - |
| Download URL | `https://github.com/shea786/nvda-ask-ai/releases/download/v<version>/askAI-<version>.nvda-addon` |
| Source URL | `https://github.com/shea786/nvda-ask-ai` |
| Publisher | `Asif Ali` |
| Channel | `beta` (the channel chosen for the first submission, 1.2.0; use `stable` once the add-on has been through beta) |
| License Name | `GPL v2` |
| License URL | `https://www.gnu.org/licenses/old-licenses/gpl-2.0.html` (the form's prefilled `https://www.gnu.org/licenses/gpl-2.0.html` is also fine) |

For 1.2.0 (first submission, **beta** channel) the Download URL is
`https://github.com/shea786/nvda-ask-ai/releases/download/v1.2.0/askAI-1.2.0.nvda-addon`.
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
  `https://api.github.com/repos/shea786/nvda-ask-ai/releases/latest` at most once a day (setting
  "Check for updates automatically"), or when the user chooses NVDA menu, Tools, "Check for Ask AI updates".
  Prereleases are only offered to users who run a prerelease. The update dialog offers Install, What's new (the
  release notes, from the release's description), Skip this version (not offered again automatically) and Later.
  The download (at most 120 seconds) goes through NVDA's own add-on installation dialogs, and the temporary
  download folder is deleted afterwards. If the installed version cannot be determined, no update is offered.
