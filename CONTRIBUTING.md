# Contributing

Bug reports and pull requests are welcome.

- **Report a bug:** [open an issue](https://github.com/100yenadmin/glasses-platform/issues/new) with your Hermes
  version, the plugin version, the device and what happened. Never paste a connect URL, QR code, `.env` line or log
  line that contains a device token; write `<token>` instead.
- **Change code:** branch from `next` and open the pull request against `next`. Releases go from `next` to `main`.
- **Run the tests** against a Hermes source tree (0.22.2 or later) with its virtual environment:

  ```bash
  HERMES_SOURCE=<hermes-agent checkout> HERMES_DISABLE_LAZY_INSTALLS=1 <hermes venv>/bin/python -m pytest -q
  node --test clients/js/test/ clients/even-g2/test/
  ```

  CI runs the same suites on Hermes v0.22.2, v0.23.0 and `main`.
- **Device protocol changes** need a matching update to [docs/PROTOCOL.md](docs/PROTOCOL.md) and the
  [conformance kit](conformance/README.md).

By contributing you agree that your work is released under the [MIT license](LICENSE).
