# Contributing

Bug reports and pull requests are welcome.

- **Report a bug:** [open an issue](https://github.com/100yenadmin/glasses-platform/issues/new/choose) with your Hermes
  version, the plugin version, the device and what happened. Never paste a connect URL, QR code, `.env` line or log
  line that contains a device token; write `<token>` instead.
- **Report a security problem** privately through a
  [security advisory](https://github.com/100yenadmin/glasses-platform/security/advisories/new), not a public issue.
  [SECURITY.md](SECURITY.md) lists what counts and the plugin's boundaries.
- **Change code:** branch from `next` and open the pull request against `next`. Releases go from `next` to `main`.
- **Run the tests** against a Hermes source tree (0.22.2 or later) with its virtual environment:

  ```bash
  HERMES_SOURCE=<hermes-agent checkout> HERMES_DISABLE_LAZY_INSTALLS=1 <hermes venv>/bin/python -m pytest -q
  PATH=<hermes venv>/bin:$PATH node --test clients/js/test/ clients/even-g2/test/
  ```

  CI runs the Python suites on Hermes v0.22.2, v0.23.0 and `main`; run the Node tests locally.
- **Working with an AI agent:** [AGENTS.md](AGENTS.md) has the layout and the rules a change must keep.
- **Device protocol changes** need a matching update to [docs/PROTOCOL.md](docs/PROTOCOL.md) and the
  [conformance kit](conformance/README.md).

By contributing you agree that your work is released under the [MIT license](LICENSE).
