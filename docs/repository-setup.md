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
6. Protect `main` with required pull requests and the `CI` and
   `API documentation / Build API reference` checks after their first runs.

No Pages, deployment, package-registry, or cloud credential is required. Pages
uses GitHub's OIDC-backed deployment action and the repository-scoped
`GITHUB_TOKEN` permissions declared in the workflow.
