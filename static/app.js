function formatDateTime(value) {
  if (!value) return "-";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function formatNumber(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return Number(value).toLocaleString("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function formatPercent(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  const num = Number(value);
  const sign = num > 0 ? "+" : "";
  return `${sign}${formatNumber(num, 2)}%`;
}

async function fetchState() {
  const response = await fetch("/api/state");
  if (!response.ok) {
    throw new Error(`state request failed: ${response.status}`);
  }
  return response.json();
}

async function triggerUpdate() {
  const button = document.getElementById("refreshButton");
  const hint = document.getElementById("refreshHint");
  button.disabled = true;
  hint.textContent = "正在执行更新...";
  try {
    const response = await fetch("/api/run-update", { method: "POST" });
    if (!response.ok) {
      throw new Error(`update request failed: ${response.status}`);
    }
    hint.textContent = "更新完成，页面数据已刷新。";
    await loadDashboard();
  } catch (error) {
    console.error(error);
    hint.textContent = "更新失败，请查看服务端日志。";
  } finally {
    button.disabled = false;
  }
}

function triggerExport() {
  window.location.href = "/api/export/tradingview";
}

function renderStats(data) {
  document.getElementById("poolCount").textContent = data.candidate_pool.length;
  document.getElementById("lastRunAt").textContent = formatDateTime(data.schedule.last_run_at);
  document.getElementById("nextRunAt").textContent = formatDateTime(data.schedule.next_run_at);
  document.getElementById("lastRunTrigger").textContent = data.schedule.last_run_trigger || "-";
}

function renderTable(data) {
  const tbody = document.getElementById("candidateTableBody");
  tbody.innerHTML = "";

  if (!data.candidate_pool.length) {
    tbody.innerHTML = `
      <tr>
        <td colspan="9" class="empty-state">候选池为空，服务会在第一次运行后填充。</td>
      </tr>
    `;
    return;
  }

  for (const row of data.candidate_pool) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><strong>${row.symbol.toUpperCase()}</strong></td>
      <td>${formatDateTime(row.added_at)}</td>
      <td>${formatNumber(row.days_in_pool, 1)}</td>
      <td>${formatNumber(row.entry_price, 4)}</td>
      <td>${formatNumber(row.latest_price, 4)}</td>
      <td class="${(row.change_from_entry_pct || 0) >= 0 ? "positive" : "negative"}">${formatPercent(row.change_from_entry_pct)}</td>
      <td class="${(row.last_change_pct || 0) >= 0 ? "positive" : "negative"}">${formatPercent(row.last_change_pct)}</td>
      <td>${row.consecutive_failure_count ?? 0}</td>
      <td>${row.last_filter_status ? `${row.last_filter_status} / ${formatDateTime(row.last_filter_at)}` : "-"}</td>
    `;
    tbody.appendChild(tr);
  }
}

function renderDailyPools(data) {
  const container = document.getElementById("dailyPools");
  container.innerHTML = "";

  if (!data.daily_candidate_pools.length) {
    container.innerHTML = '<p class="empty-state">当前还没有可展示的每日候选池快照。</p>';
    return;
  }

  for (const item of data.daily_candidate_pools) {
    const card = document.createElement("article");
    card.className = "daily-pool-card";
    const addedSet = new Set(item.added_symbols);
    const symbols = item.symbols
      .map((symbol) => {
        const isAdded = addedSet.has(symbol);
        return `<span class="symbol-pill ${isAdded ? "symbol-pill-added" : ""}">${symbol.toUpperCase()}</span>`;
      })
      .join("");
    const added = item.added_symbols.length ? item.added_symbols.map((symbol) => symbol.toUpperCase()).join(", ") : "-";
    const removed = item.removed_symbols.length ? item.removed_symbols.map((symbol) => symbol.toUpperCase()).join(", ") : "-";
    card.innerHTML = `
      <div class="daily-pool-head">
        <div>
          <p class="daily-date">${item.date}</p>
          <strong>${item.pool_size} 个币种</strong>
        </div>
        <span class="daily-trigger">${item.trigger || "-"}</span>
      </div>
      <p class="daily-meta">运行时间 ${formatDateTime(item.run_at)}</p>
      <p class="daily-meta">新增 ${added}</p>
      <p class="daily-meta">移除 ${removed}</p>
      <div class="symbol-pills">${symbols}</div>
    `;
    container.appendChild(card);
  }
}

async function loadDashboard() {
  const data = await fetchState();
  renderStats(data);
  renderTable(data);
  renderDailyPools(data);
}

document.getElementById("refreshButton").addEventListener("click", triggerUpdate);
document.getElementById("exportButton").addEventListener("click", triggerExport);
loadDashboard().catch((error) => {
  console.error(error);
  document.getElementById("refreshHint").textContent = "页面初始化失败，请检查服务是否已启动。";
});
