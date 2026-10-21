# GitHub repository setup

This source tree contains the workflows and policy files needed for a public
repository, but GitHub-owned settings cannot be committed as files. A repository
administrator applies and keeps these settings:

1. Keep `pineforge-4pass/pineforge-hpo` **public** with `main` as its default
   branch.
2. Under **Settings → Actions → General**, allow the official `actions/*`
   workflows used in `.github/workflows/`.
3. Under **Settings → Pages → Build and deployment**, select **GitHub Actions**
   as the source. Each successful `API documentation` run on `main` publishes
   `https://pineforge-4pass.github.io/pineforge-hpo/`.
4. Under **Settings → Security → Code security**, enable private vulnerability
   reporting so the link in `SECURITY.md` accepts reports.
5. Create the `bug`, `enhancement`, and `benchmark` labels referenced by the
   issue forms, or update those forms to match the organization label policy.
6. Protect `main` with required pull requests and the `CI / build` aggregate
   (`build-gate`, check name `build`) and `API documentation / Build API reference`
   checks after their first runs. The aggregate includes the HPO memory growth
   gate and refuses skips outside a classified docs-only PR; see [CI](ci.md).
7. Make the existing org App's `PINEFORGE_APP_ID` and
   `PINEFORGE_APP_PRIVATE_KEY` Actions secrets available to this repository.
   Its installation must include `pineforge-release` with `Contents: write`.
   The notification job mints a short-lived token scoped only to that target.
   Allow the pinned official `actions/create-github-app-token` action under
   Actions policy. See [release notification and replay](release-chain.md).
8. Add `Release notification contract` to the required checks on `main` after
   its first CI run. Keep the existing native, Python and documentation checks.

Pages uses GitHub's OIDC-backed deployment action and the repository-scoped
`GITHUB_TOKEN` permissions declared in the workflow. It needs no additional
credential. Release-hub notification uses the existing org App credentials above;
no PAT, package-registry credential, cloud credential or new variable is required.
