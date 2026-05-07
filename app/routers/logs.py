"""Observability logs API and UI."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from app.observability import get_store

router = APIRouter()

_LOGS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>searchproxy logs</title>
  <style>
    :root { --bg: #0d1117; --fg: #c9d1d9; --muted: #8b949e; --panel: #161b22; --border: #30363d; --accent: #58a6ff; --ok: #3fb950; --warn: #f0883e; --err: #f85149; --danger: #da3633; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--fg); margin: 0; padding: 24px; line-height: 1.45; }
    h1 { margin: 0 0 18px; font-size: 20px; }
    .toolbar { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 18px; align-items: center; }
    .toolbar input, .toolbar select, .toolbar button { background: var(--panel); border: 1px solid var(--border); color: var(--fg); padding: 6px 10px; border-radius: 6px; font-size: 13px; }
    .toolbar button { cursor: pointer; }
    .toolbar button:hover { border-color: var(--muted); }
    .toolbar .spacer { flex: 1; }
    .toolbar .badge { color: var(--muted); font-size: 12px; }
    .btn-danger { border-color: var(--danger); color: var(--danger); }
    .btn-danger:hover { background: var(--danger); color: #fff; border-color: var(--danger); }
    table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
    th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--border); vertical-align: top; }
    th { color: var(--muted); font-weight: 500; position: sticky; top: 0; background: var(--bg); z-index: 1; }
    tr:hover { background: var(--panel); }
    .status-2xx { color: var(--ok); font-weight: 600; }
    .status-4xx { color: var(--warn); font-weight: 600; }
    .status-5xx { color: var(--err); font-weight: 600; }
    .detail { display: none; }
    .detail td { padding: 14px 10px; background: var(--panel); }
    .detail pre { background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 10px; overflow-x: auto; font-size: 11.5px; margin: 0 0 10px; white-space: pre-wrap; word-break: break-word; }
    .detail label { color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: 0.4px; display: block; margin-bottom: 3px; }
    .pagination { display: flex; gap: 8px; align-items: center; margin-top: 14px; }
    .muted { color: var(--muted); }
    .nowrap { white-space: nowrap; }
  </style>
</head>
<body>
  <h1>🔍 searchproxy request logs</h1>
  <div class="toolbar">
    <input id="search" placeholder="Fuzzy search…" />
    <select id="method"><option value="">Method</option><option>GET</option><option>POST</option></select>
    <input id="path" placeholder="Path filter" />
    <input id="status" placeholder="Status" type="number" />
    <input id="source" placeholder="Source" />
    <button onclick="fetchLogs()">Refresh</button>
    <button class="btn-danger" onclick="clearAll()">Clear All</button>
    <div class="spacer"></div>
    <span id="badge" class="badge"></span>
  </div>
  <table>
    <thead>
      <tr>
        <th class="nowrap">Time</th>
        <th>Method</th>
        <th>Path</th>
        <th>Status</th>
        <th>Source</th>
        <th class="nowrap">RT (ms)</th>
      </tr>
    </thead>
    <tbody id="tbody"></tbody>
  </table>
  <div class="pagination">
    <button onclick="prevPage()">← Prev</button>
    <span id="pageInfo" class="muted">Page 1</span>
    <button onclick="nextPage()">Next →</button>
  </div>

  <script>
    let offset = 0, limit = 100, total = 0;
    async function fetchLogs() {
      const p = new URLSearchParams({ limit: String(limit), offset: String(offset) });
      const v = id => document.getElementById(id).value;
      if (v('search')) p.set('search', v('search'));
      if (v('method')) p.set('method', v('method'));
      if (v('path')) p.set('path', v('path'));
      if (v('status')) p.set('status_code', v('status'));
      if (v('source')) p.set('source', v('source'));
      const r = await fetch('/api/logs?' + p);
      const j = await r.json();
      total = j.total;
      render(j.data);
      const page = Math.floor(offset / limit) + 1;
      const pages = Math.ceil(total / limit) || 1;
      document.getElementById('pageInfo').textContent = `Page ${page} of ${pages} (${total} total)`;
      document.getElementById('badge').textContent = `Updated ${new Date().toLocaleTimeString()}`;
    }
    function render(rows) {
      const tb = document.getElementById('tbody');
      tb.innerHTML = '';
      for (const row of rows) {
        const tr = document.createElement('tr');
        const ts = new Date(row.timestamp * 1000).toLocaleString();
        const stClass = row.status_code >= 500 ? 'status-5xx' : row.status_code >= 400 ? 'status-4xx' : 'status-2xx';
        tr.innerHTML = `<td class="nowrap">${ts}</td><td>${row.method}</td><td>${row.path}</td><td class="${stClass}">${row.status_code ?? '-'}</td><td>${row.source || '-'}</td><td class="nowrap">${Math.round(row.response_time_ms)}</td>`;
        tr.onclick = () => toggleDetail(row.id);
        tb.appendChild(tr);

        const d = document.createElement('tr');
        d.className = 'detail';
        d.id = 'd-' + row.id;
        d.innerHTML = `<td colspan="6">
          <label>Request ID</label><pre>${row.request_id}</pre>
          <label>Query Params</label><pre>${row.query_params || '-'}</pre>
          <label>Client IP</label><pre>${row.client_ip || '-'}</pre>
          <label>User Agent</label><pre>${row.user_agent || '-'}</pre>
          <label>Request Headers</label><pre>${row.request_headers}</pre>
          <label>Request Body</label><pre>${row.request_body || '-'}</pre>
          <label>Response Headers</label><pre>${row.response_headers}</pre>
          <label>Response Body</label><pre>${row.response_body || '-'}</pre>
          <label>Error</label><pre>${row.error || '-'}</pre>
        </td>`;
        tb.appendChild(d);
      }
    }
    function toggleDetail(id) {
      const el = document.getElementById('d-' + id);
      el.style.display = el.style.display === 'table-row' ? 'none' : 'table-row';
    }
    async function clearAll() {
      if (!confirm('Delete every log record? This cannot be undone.')) return;
      const r = await fetch('/api/logs', { method: 'DELETE' });
      if (!r.ok) { alert('Failed to clear logs: ' + r.status); return; }
      offset = 0;
      await fetchLogs();
    }
    function nextPage() { if (offset + limit < total) { offset += limit; fetchLogs(); } }
    function prevPage() { if (offset >= limit) { offset -= limit; fetchLogs(); } }
    ['search','path','status','source'].forEach(id => {
      document.getElementById(id).addEventListener('keydown', e => { if (e.key === 'Enter') { offset = 0; fetchLogs(); } });
    });
    document.getElementById('method').addEventListener('change', () => { offset = 0; fetchLogs(); });
    fetchLogs();
    setInterval(fetchLogs, 30000);
  </script>
</body>
</html>"""


@router.get("/logs", include_in_schema=False)
async def logs_page() -> HTMLResponse:
    return HTMLResponse(content=_LOGS_HTML)


@router.get("/api/logs", tags=["logs"])
async def list_logs(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    method: str | None = None,
    path: str | None = None,
    status_code: int | None = None,
    source: str | None = None,
    search: str | None = None,
    start_time: float | None = None,
    end_time: float | None = None,
) -> dict[str, Any]:
    store = get_store()
    if store is None:
        return {"total": 0, "limit": limit, "offset": offset, "data": []}
    rows, total = await store.query(
        limit=limit,
        offset=offset,
        method=method,
        path=path,
        status_code=status_code,
        source=source,
        search=search,
        start_time=start_time,
        end_time=end_time,
    )
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "data": rows,
    }


@router.delete("/api/logs", tags=["logs"])
async def delete_all_logs() -> dict[str, Any]:
    """Delete every observability record. Returns the number of rows removed."""
    store = get_store()
    if store is None:
        return {"deleted": 0}
    deleted = await store.delete_all()
    return {"deleted": deleted}
