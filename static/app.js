const state = {
  chart: null,
};

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

function formatAxisTime(value) {
  if (!value) return "";
  return new Intl.DateTimeFormat("zh-CN", {
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

function pickColor(index) {
  const palette = [
    "#ff6b35",
    "#0d3b66",
    "#7c9885",
    "#c1121f",
    "#3a86ff",
    "#588157",
    "#8338ec",
    "#fb5607",
    "#2a9d8f",
    "#6d597a",
    "#ef476f",
    "#457b9d",
  ];
  return palette[index % palette.length];
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

function renderTopMovers(data) {
  const container = document.getElementById("topMovers");
  container.innerHTML = "";

  if (!data.recent_top_movers.length) {
    container.innerHTML = '<p class="empty-state">当前还没有最近一轮涨幅榜数据。</p>';
    return;
  }

  for (const mover of data.recent_top_movers) {
    const card = document.createElement("article");
    card.className = "mover-card";
    card.innerHTML = `
      <div class="mover-head">
        <strong>${mover.symbol.toUpperCase()}</strong>
        <span>#${mover.rank}</span>
      </div>
      <div class="mover-metrics">
        <span>${formatPercent(mover.change_pct)}</span>
        <small>最新价 ${formatNumber(mover.last_price, 4)}</small>
      </div>
    `;
    container.appendChild(card);
  }
}

function renderTable(data) {
  const tbody = document.getElementById("candidateTableBody");
  tbody.innerHTML = "";

  if (!data.candidate_pool.length) {
    tbody.innerHTML = `
      <tr>
        <td colspan="8" class="empty-state">候选池为空，服务会在第一次运行后填充。</td>
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
      <td>${row.last_filter_status ? `${row.last_filter_status} / ${formatDateTime(row.last_filter_at)}` : "-"}</td>
    `;
    tbody.appendChild(tr);
  }
}

function renderChart(data) {
  const ctx = document.getElementById("normalizedChart");
  const datasets = data.normalized_series
    .filter((item) => item.points.length > 0)
    .map((item, index) => ({
      label: item.symbol.toUpperCase(),
      data: item.points.map((point) => ({ x: new Date(point.t).getTime(), y: point.y, t: point.t, price: point.price })),
      borderColor: pickColor(index),
      backgroundColor: pickColor(index),
      borderWidth: 2,
      pointRadius: 0,
      tension: 0.22,
    }));

  if (state.chart) {
    state.chart.destroy();
  }

  state.chart = new Chart(ctx, {
    type: "line",
    data: { datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: {
        mode: "nearest",
        intersect: false,
      },
      plugins: {
        legend: {
          labels: {
            usePointStyle: true,
            boxWidth: 10,
            font: {
              family: "Space Grotesk, Noto Sans SC, sans-serif",
            },
          },
        },
        tooltip: {
          callbacks: {
            title(items) {
              if (!items.length) return "";
              return formatDateTime(items[0].raw.t);
            },
            label(context) {
              const raw = context.raw;
              return `${context.dataset.label}: ${formatNumber(raw.y, 2)} | 价格 ${formatNumber(raw.price, 4)}`;
            },
            afterLabel(context) {
              return formatDateTime(context.raw.t);
            },
          },
        },
      },
      scales: {
        x: {
          type: "linear",
          title: {
            display: true,
            text: "时间",
          },
          ticks: {
            callback(value) {
              return formatAxisTime(Number(value));
            },
            maxTicksLimit: 8,
          },
          grid: {
            color: "rgba(13, 59, 102, 0.08)",
          },
        },
        y: {
          title: {
            display: true,
            text: "归一化值（首点 = 100）",
          },
          grid: {
            color: "rgba(13, 59, 102, 0.08)",
          },
        },
      },
    },
  });
}

async function loadDashboard() {
  const data = await fetchState();
  renderStats(data);
  renderTopMovers(data);
  renderTable(data);
  renderChart(data);
}

document.getElementById("refreshButton").addEventListener("click", triggerUpdate);
document.getElementById("exportButton").addEventListener("click", triggerExport);
loadDashboard().catch((error) => {
  console.error(error);
  document.getElementById("refreshHint").textContent = "页面初始化失败，请检查服务是否已启动。";
});
