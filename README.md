# bvt-triage-test

Sandbox for testing the **BVT Triage GitHub Action** before deploying it to `ibm-webmethods/esb`.

## How to test

1. Open a PR against `main` — the fake CI workflow (`Action - Integration Server CI-CD`) will run and **deliberately fail**, uploading a fake JUnit artifact.
2. The `BVT Triage Report` workflow fires automatically via `workflow_run`.
3. Check the PR comments — a triage report should appear within ~1 minute of the CI failure.

## Files

| File | Purpose |
|---|---|
| `.github/workflows/fake-ci.yml` | Fake CI that always fails + uploads a test artifact |
| `.github/workflows/bvt-triage.yml` | Triage orchestrator (workflow_run + manual dispatch) |
| `.github/actions/bvt-triage/action.yml` | Composite action |
| `.github/actions/bvt-triage/triage.py` | Python classifier |
