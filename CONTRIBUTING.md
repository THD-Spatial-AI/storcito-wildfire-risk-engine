# Contributing

Thank you for taking the time to contribute to the **STORCITO Wildfire Risk Engine**.

This project welcomes contributions such as bug reports, feature requests, documentation improvements, code changes, and general feedback.

Please read this guide before opening an issue or submitting a pull request.

## Code of Conduct

By participating in this project, you agree to follow the rules and expectations described in the [Code of Conduct](CODE_OF_CONDUCT.md).

## Ways to Contribute

You can contribute in different ways, including:

- Reporting bugs
- Requesting features or improvements
- Improving documentation
- Fixing issues
- Reviewing pull requests
- Asking and answering questions

## Before You Start

Before creating a new issue or pull request, please:

- Read the `README.md` to understand the project purpose and setup
- Check existing issues and pull requests to avoid duplicates
- Make sure your idea/request is relevant to the project scope
- Use the issue templates (if available)

## Reporting Bugs and Requesting Changes

Use the project issue tracker for bug reports, feature requests, and documentation issues.

- **Issue tracker:** <https://github.com/THD-Spatial-AI/storcito-wildfire-risk-engine/issues>

When reporting an issue, please include:

- What you expected to happen
- What actually happened
- Steps to reproduce the issue
- Screenshots/logs/error messages (if applicable)
- Environment details (OS, Docker version, model version, request payload without credentials, if relevant)

## Development Workflow

Setup and data-pipeline instructions are in the [README](README.md).

### 1) Fork and clone the repository (if applicable)

If you do not have direct write access, fork the repository first, then clone your fork:

```bash
git clone https://github.com/THD-Spatial-AI/storcito-wildfire-risk-engine.git
cd storcito-wildfire-risk-engine
```

If you have direct write access, clone the main repository instead.

### 2) Create a branch for your change

Create a dedicated branch for your bugfix, feature, or documentation update. Branch names are checked in CI and must follow `<type>/<description>`:

```bash
git checkout -b type/short-description
```

- **type:** `feat`, `feature`, `fix`, `bugfix`, `hotfix`, `release`, `chore`, `docs`, `refactor`, `test`, `style` or `perf`
- **description:** lowercase letters, digits, hyphens and dots, with no leading, trailing or doubled separators

Examples:

- `fix/ndmi-nodata`
- `feat/lst-regional-breaks`
- `docs/readme-setup`

`main`, `dev`, `develop` and `staging` are exempt.

### 3) Make your changes

Keep changes focused and small where possible. If your change is large, consider splitting it into multiple pull requests.

### 4) Test your changes (if applicable)

Before submitting a pull request:

- Run relevant tests
- Check linting/formatting tools (if used)
- Verify the project still builds/runs locally
- Update documentation if your change affects usage or behaviour

### 5) Commit your changes

Commit messages are checked in CI and must follow [Conventional Commits](https://www.conventionalcommits.org):

```bash
git add <files>
git commit -m "fix(fwi): keep rainfall accumulation noon to noon"
```

For larger changes, add a body that explains why the change was made.

### 6) Push your branch

```bash
git push -u origin <your-branch-name>
```

### 7) Open a pull request

Create a pull request against the appropriate branch (usually `main` unless the project uses a different workflow).

In your pull request description, include:

- What changed
- Why it changed
- Testing notes
- For model changes: the effect on outputs and the new `STORCITO_MODEL_VERSION`
- Related issue(s), if applicable (e.g. `Closes #123`)

## Pull Request Checklist

Before submitting a pull request, check:

- [ ] The change is relevant and scoped appropriately
- [ ] I tested my changes (if applicable)
- [ ] I updated documentation (if applicable)
- [ ] I followed the project coding/style conventions (if applicable)
- [ ] I checked for sensitive information (keys, credentials, private data)
- [ ] I linked related issues (if applicable)

## Commit Message Rules

Format: `<type>(<scope>): <subject>` or `<type>: <subject>`.

- **Allowed types:** `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `chore`
- The subject starts with a lowercase letter, uses the imperative mood and does not end with a period
- The full subject line is at most 100 characters
- Merge, revert and initial commits are skipped

Good examples:

- `fix(ndmi): preserve source nodata in both entry points`
- `feat(lst): add regional percentile breaks per assessment date`
- `docs: update the data-source table in README`

Avoid vague messages such as `fix`, `changes` or `update stuff`.

## Documentation Contributions

Documentation improvements are welcome and valuable.

If you are updating docs:

- Keep wording clear and practical
- Prefer short examples where useful
- Check links and commands
- Match the style used in existing documentation

## Project-Specific Notes

- **Setup:** follow the [README](README.md); `make build` and `make up` start the API stack, and the `make <layer>` targets fetch and seed input data.
- **Tests:** `docker compose exec storcito-api-1 pytest`
- **Model changes:** changes to scoring rules, AHP weights, FWI conventions or masks change the outputs. Record them in [CHANGELOG.md](CHANGELOG.md), bump `STORCITO_MODEL_VERSION`, and note that existing results and caches must be regenerated.
- **Data and credentials:** never commit `.env` files, Copernicus/FIRMS/CLMS credentials or downloaded source data. New data sources must be listed with their licence in [ATTRIBUTIONS.md](ATTRIBUTIONS.md).

## Licensing of Contributions

By contributing to this project, you confirm that:

- your contribution is your own work (or you have the right to submit it), and
- you agree that your contribution will be licensed under the [MIT License](LICENSE) of this repository.

## Need Help?

If you are unsure where to start, open an issue and ask. Maintainers can help point you in the right direction.
