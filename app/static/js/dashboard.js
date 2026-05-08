// --- Sortable table ---
function makeSortable(tableId) {
    var table = document.getElementById(tableId);
    if (!table) return;
    var thead = table.querySelector('thead');
    var tbody = table.querySelector('tbody');
    var headers = thead.querySelectorAll('th');
    var currentCol = 0;
    var ascending = true;

    function sortTable(col, asc) {
        var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr'));
        rows.sort(function (a, b) {
            var aCell = a.cells[col];
            var bCell = b.cells[col];
            var aVal = aCell.getAttribute('data-sort') || aCell.textContent.trim().toLowerCase();
            var bVal = bCell.getAttribute('data-sort') || bCell.textContent.trim().toLowerCase();
            if (aVal < bVal) return asc ? -1 : 1;
            if (aVal > bVal) return asc ? 1 : -1;
            return 0;
        });
        rows.forEach(function (row) { tbody.appendChild(row); });
    }

    function updateArrows(col, asc) {
        headers.forEach(function (th, i) {
            var arrow = th.querySelector('.sort-arrow');
            if (i === col) {
                th.classList.add('sort-active');
                arrow.innerHTML = asc ? '&#9650;' : '&#9660;';
            } else {
                th.classList.remove('sort-active');
                arrow.innerHTML = '&#9650;';
            }
        });
    }

    headers.forEach(function (th) {
        th.addEventListener('click', function () {
            var col = parseInt(th.getAttribute('data-col'));
            if (col === currentCol) {
                ascending = !ascending;
            } else {
                currentCol = col;
                ascending = true;
            }
            sortTable(col, ascending);
            updateArrows(col, ascending);
        });
    });
}

makeSortable('pending-table');
makeSortable('resolved-table');

// --- Async scan ---
var scanBtn = document.getElementById('scan-btn');
var scanForm = document.getElementById('scan-form');

if (scanForm && scanBtn) {
    scanForm.addEventListener('submit', function (e) {
        e.preventDefault();
        scanBtn.disabled = true;
        scanBtn.textContent = 'Scanning...';
        fetch('/api/scan', { method: 'POST' })
            .then(function () {
                scanBtn.textContent = 'Scan triggered';
            })
            .catch(function () {
                scanBtn.disabled = false;
                scanBtn.textContent = 'Scan Now';
            });
    });
}

// --- Poll for new scan results ---
var lastCheck = document.body.dataset.lastCheck;
var banner = document.getElementById('update-banner');

function pollForScanUpdate() {
    fetch('/api/last-scan')
        .then(function (r) { return r.json(); })
        .then(function (data) {
            var serverCheck = data.last_check || '';
            if (serverCheck && serverCheck !== lastCheck) {
                banner.classList.add('visible');
                if (scanBtn) {
                    scanBtn.disabled = false;
                    scanBtn.textContent = 'Scan Now';
                }
            }
        })
        .catch(function () { });
}

setInterval(pollForScanUpdate, 10000);
