/**
 * B5 Stock Dashboard - Google Sheet -> GitHub Actions bridge
 *
 * Setup:
 * 1. Add a fine-grained GitHub token to Apps Script > Project Settings
 *    > Script Properties with the key GITHUB_TOKEN.
 * 2. Run installStockTriggers() once and approve the requested permissions.
 * 3. Run testStockDispatch() once to verify the connection.
 *
 * Never paste the token into this source file.
 */

const GITHUB_OWNER = 'amnattyy-cyber';
const GITHUB_REPO = 'b5-stock-dashboard';
const DISPATCH_EVENT = 'stock_sheet_updated';
const WATCHED_SHEET = 'Data Stock';
const MIN_DISPATCH_INTERVAL_MS = 2 * 60 * 1000;

/**
 * Handler for both installable Edit and Change triggers.
 * The debounce prevents a paste/row operation from starting duplicate workflows.
 */
function notifyStockDashboard(event) {
  if (event && event.range) {
    const editedSheet = event.range.getSheet().getName();
    if (editedSheet !== WATCHED_SHEET) {
      return;
    }
  }

  const lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) {
    return;
  }

  try {
    const properties = PropertiesService.getScriptProperties();
    const token = properties.getProperty('GITHUB_TOKEN');
    if (!token) {
      throw new Error('Missing GITHUB_TOKEN in Apps Script Project Settings > Script Properties.');
    }

    const now = Date.now();
    const lastDispatchAt = Number(properties.getProperty('LAST_DISPATCH_AT') || 0);
    if (now - lastDispatchAt < MIN_DISPATCH_INTERVAL_MS) {
      return;
    }

    const url =
      'https://api.github.com/repos/' +
      encodeURIComponent(GITHUB_OWNER) +
      '/' +
      encodeURIComponent(GITHUB_REPO) +
      '/dispatches';

    const response = UrlFetchApp.fetch(url, {
      method: 'post',
      contentType: 'application/json',
      muteHttpExceptions: true,
      headers: {
        Authorization: 'Bearer ' + token,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
      },
      payload: JSON.stringify({
        event_type: DISPATCH_EVENT,
        client_payload: {
          spreadsheet_id: SpreadsheetApp.getActive().getId(),
          sheet_name: WATCHED_SHEET,
          triggered_at: new Date(now).toISOString(),
        },
      }),
    });

    const status = response.getResponseCode();
    if (status !== 204) {
      throw new Error(
        'GitHub repository_dispatch failed (' +
          status +
          '): ' +
          response.getContentText()
      );
    }

    properties.setProperty('LAST_DISPATCH_AT', String(now));
  } finally {
    lock.releaseLock();
  }
}

/**
 * Installs both triggers:
 * - Edit: cell edits and pasted data.
 * - Change: inserted/deleted rows, columns, sheets, and other structural changes.
 */
function installStockTriggers() {
  const spreadsheet = SpreadsheetApp.getActive();

  ScriptApp.getProjectTriggers()
    .filter(function (trigger) {
      return trigger.getHandlerFunction() === 'notifyStockDashboard';
    })
    .forEach(function (trigger) {
      ScriptApp.deleteTrigger(trigger);
    });

  ScriptApp.newTrigger('notifyStockDashboard')
    .forSpreadsheet(spreadsheet)
    .onEdit()
    .create();

  ScriptApp.newTrigger('notifyStockDashboard')
    .forSpreadsheet(spreadsheet)
    .onChange()
    .create();
}

/**
 * Sends one test event. Run this after installing the token and triggers.
 */
function testStockDispatch() {
  PropertiesService.getScriptProperties().deleteProperty('LAST_DISPATCH_AT');
  notifyStockDashboard(null);
}
