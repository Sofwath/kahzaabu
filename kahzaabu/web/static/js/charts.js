// Chart.js helpers — palette matches the dark theme.

const PALETTE = {
    lie:        "#e94560",
    deadline:   "#e9a045",
    credit:     "#9b59b6",
    shifting:   "#f7c948",
    context:    "#3498db",
    good:       "#2ecc71",
    dim:        "#9090b0",
    grid:       "rgba(255,255,255,0.06)",
    fg:         "#e4e4f0",
    fgDim:      "#9090b0",
};

const CATEGORY_COLORS = {
    "LIE":              PALETTE.lie,
    "CONTRADICTION":    PALETTE.lie,
    "MISLEADING":       PALETTE.deadline,
    "BROKEN DEADLINE":  PALETTE.deadline,
    "SHIFTING NUMBERS": PALETTE.shifting,
    "CREDIT THEFT":     PALETTE.credit,
};

function commonOpts(extra) {
    return Object.assign({
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
            legend: { labels: { color: PALETTE.fg, font: { size: 12 } } },
            tooltip: { backgroundColor: "#16162a", borderColor: "#2a2a40", borderWidth: 1 },
        },
        scales: {
            x: { ticks: { color: PALETTE.fgDim }, grid: { color: PALETTE.grid } },
            y: { ticks: { color: PALETTE.fgDim }, grid: { color: PALETTE.grid }, beginAtZero: true },
        },
    }, extra || {});
}

function lineChart(ctx, data, options) {
    return new Chart(ctx, {
        type: "line",
        data,
        options: commonOpts(options),
    });
}

function barChart(ctx, data, options) {
    return new Chart(ctx, {
        type: "bar",
        data,
        options: commonOpts(options),
    });
}

function doughnutChart(ctx, labels, values, colors) {
    return new Chart(ctx, {
        type: "doughnut",
        data: {
            labels,
            datasets: [{ data: values, backgroundColor: colors, borderColor: "#0a0a14", borderWidth: 2 }],
        },
        options: {
            responsive: true, maintainAspectRatio: false,
            plugins: {
                legend: { position: "right", labels: { color: PALETTE.fg, font: { size: 11 } } },
                tooltip: { backgroundColor: "#16162a" },
            },
        },
    });
}
