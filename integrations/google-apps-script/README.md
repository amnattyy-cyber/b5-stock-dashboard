# Google Sheet event trigger

This integration starts the existing GitHub Actions workflow when the **Data Stock**
sheet is edited. It avoids relying on GitHub's scheduled-workflow queue, which can
run later than its cron expression.

## One-time setup

1. In GitHub, create a **fine-grained personal access token**:
   - Resource owner: `amnattyy-cyber`
   - Repository access: only `b5-stock-dashboard`
   - Repository permission: **Contents — Read and write**
   - Use an appropriate expiration date and rotate the token before it expires.
2. Open the source Google Sheet.
3. Select **Extensions > Apps Script**.
4. Replace the editor contents with `Code.gs` from this directory and save.
5. Open **Project Settings > Script Properties**.
6. Add a property:
   - Property: `GITHUB_TOKEN`
   - Value: the fine-grained token
7. In the function selector, run `installStockTriggers` once and approve the
   requested Google permissions.
8. Run `testStockDispatch` once.
9. In GitHub Actions, confirm that **Frequent stock refresh** starts with event
   `repository_dispatch` and completes successfully.

## What it does

- Manual edits and pasted cell ranges trigger an Edit event.
- Row, column, or sheet structure changes trigger a Change event.
- Events are debounced for two minutes to prevent duplicate workflow runs.
- Only a notification is sent to GitHub. Sheet contents are not included in the
  dispatch request.
- The existing GitHub schedule remains a fallback.

## Security

Do not paste `GITHUB_TOKEN` into `Code.gs`, the Sheet, a GitHub file, an issue,
or chat. Store it only in Apps Script **Script Properties**.

## Limitation

Google installable triggers do not run when another script or API changes cells.
If the source is updated by an automated importer rather than a user edit, call
`notifyStockDashboard(null)` at the end of that importer, or keep the scheduled
GitHub workflow as a fallback.
