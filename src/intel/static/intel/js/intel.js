// Threat-profile page interactions.
//
//  * window selector + click-to-filter re-aggregate server-side and swap the
//    lightweight #char-blocks fragment (compact rows only) — fast, no reload.
//  * each character's heavy detail (chart + tables) is loaded lazily the first
//    time its row is expanded, and reset whenever the filter/window changes.
//  * window + filters live in the URL so the view is shareable.
(function () {
    var intel = document.querySelector('.intel');
    if (!intel) {
        return;
    }
    var blocks = document.getElementById('char-blocks');
    var filterBar = document.getElementById('filter-bar');
    var path = window.location.pathname;

    var seed = document.getElementById('intel-filters');
    var state = {
        window: intel.dataset.window,
        filters: seed ? JSON.parse(seed.textContent) : {}
    };

    var CHIP = {
        region: 'bi-globe2', const: 'bi-diagram-3', system: 'bi-pin-map',
        ship: 'bi-rocket-takeoff', target: 'bi-bullseye'
    };

    function params(extra) {
        var p = ['window=' + encodeURIComponent(state.window)];
        Object.keys(state.filters).forEach(function (k) {
            p.push(k + '=' + encodeURIComponent(state.filters[k]));
        });
        Object.keys(extra || {}).forEach(function (k) { p.push(k + '=' + encodeURIComponent(extra[k])); });
        return p.join('&');
    }

    function renderChips() {
        filterBar.innerHTML = '';
        var keys = Object.keys(state.filters);
        if (keys.length === 0) {
            return;
        }
        keys.forEach(function (k) {
            var chip = document.createElement('span');
            chip.className = 'chip';
            chip.innerHTML = '<i class="bi ' + (CHIP[k] || 'bi-funnel') + '"></i> ' +
                k + ': <strong></strong> <span class="chip-x" data-key="' + k + '">&times;</span>';
            chip.querySelector('strong').textContent = state.filters[k];
            filterBar.appendChild(chip);
        });
        var clear = document.createElement('button');
        clear.type = 'button';
        clear.id = 'clear-filters';
        clear.className = 'chip-clear';
        clear.textContent = 'clear all';
        filterBar.appendChild(clear);
    }

    function syncWindowButtons() {
        document.querySelectorAll('.win-btn').forEach(function (b) {
            b.classList.toggle('active', b.dataset.window === state.window);
        });
    }

    function refresh() {
        blocks.classList.add('loading');
        fetch(path + '?fragment=blocks&' + params())
            .then(function (r) { return r.text(); })
            .then(function (html) {
                blocks.innerHTML = html;          // fresh compact rows, all details reset
                blocks.classList.remove('loading');
                renderChips();
                syncWindowButtons();
                history.replaceState(null, '', path + '?' + params());
            })
            .catch(function () { blocks.classList.remove('loading'); });
    }

    function loadDetail(detail) {
        detail.dataset.loaded = 'loading';
        detail.innerHTML = '<div class="empty-state"><span class="css-spinner"></span> loading…</div>';
        fetch(path + '?' + params({ fragment: 'detail', char: detail.dataset.charId }))
            .then(function (r) { return r.text(); })
            .then(function (html) { detail.innerHTML = html; detail.dataset.loaded = '1'; })
            .catch(function () { detail.dataset.loaded = '0'; detail.innerHTML = ''; });
    }

    function toggleExpand(summary) {
        var block = summary.closest('.char-block');
        var detail = block.querySelector('.char-detail');
        var expanded = block.classList.toggle('expanded');
        summary.setAttribute('aria-expanded', expanded ? 'true' : 'false');
        if (expanded && detail.dataset.loaded === '0') {
            loadDetail(detail);
        }
    }

    document.addEventListener('click', function (e) {
        var x = e.target.closest('.chip-x');
        if (x) { delete state.filters[x.dataset.key]; refresh(); return; }
        if (e.target.closest('#clear-filters')) { state.filters = {}; refresh(); return; }
        var win = e.target.closest('.win-btn');
        if (win) { state.window = win.dataset.window; refresh(); return; }
        var f = e.target.closest('.filterable');
        if (f) { state.filters[f.dataset.filter] = f.dataset.value; refresh(); return; }
        var sum = e.target.closest('.char-summary');
        if (sum) { toggleExpand(sum); }
    });

    document.addEventListener('keydown', function (e) {
        if (e.key !== 'Enter' && e.key !== ' ') {
            return;
        }
        var sum = e.target.closest('.char-summary');
        if (sum) { e.preventDefault(); toggleExpand(sum); }
    });

    renderChips();
    syncWindowButtons();
})();
