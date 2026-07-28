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
        names: intel.dataset.names || '',
        filters: seed ? JSON.parse(seed.textContent) : {}
    };

    function params(extra) {
        var p = ['window=' + encodeURIComponent(state.window)];
        if (state.names) {
            p.push('names=' + encodeURIComponent(state.names));
        }
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
            var chip = document.createElement('div');
            chip.className = 'tags has-addons chip';
            chip.innerHTML = '<span class="tag">' + k + ': <strong></strong></span>' +
                '<a class="tag is-delete chip-x" data-key="' + k + '"></a>';
            chip.querySelector('strong').textContent = state.filters[k];
            filterBar.appendChild(chip);
        });
        var clear = document.createElement('button');
        clear.type = 'button';
        clear.id = 'clear-filters';
        clear.className = 'button is-small is-ghost chip-clear';
        clear.textContent = 'clear all';
        filterBar.appendChild(clear);
    }

    function syncWindowButtons() {
        document.querySelectorAll('.win-btn').forEach(function (b) {
            var on = b.dataset.window === state.window;
            b.classList.toggle('is-link', on);
            b.classList.toggle('is-selected', on);
        });
    }

    // A filter click re-renders #char-blocks, which throws away every expanded card. Without
    // remembering what was open you never see the effect of your own click - worst of all
    // when the thing you clicked lives *inside* an expanded card.
    function openState() {
        return Array.prototype.map.call(
            blocks.querySelectorAll('.char-block.expanded .char-detail'),
            function (d) {
                var openTarget = d.querySelector('.tgt-detail.open');
                return {
                    id: d.dataset.charId,
                    bucket: openTarget ? openTarget.previousElementSibling.dataset.bucket : null
                };
            }
        );
    }

    function restoreOpen(open) {
        open.forEach(function (s) {
            var detail = blocks.querySelector('.char-detail[data-char-id="' + s.id + '"]');
            if (!detail) {
                return;                   // that pilot is no longer in the result set
            }
            var block = detail.closest('.char-block');
            var summary = block.querySelector('.char-summary');
            block.classList.add('expanded');
            if (summary) { summary.setAttribute('aria-expanded', 'true'); }
            loadDetail(detail).then(function () {
                if (!s.bucket) { return; }
                var row = detail.querySelector('.tgt-row[data-bucket="' + s.bucket + '"]');
                if (row && !row.classList.contains('is-empty')) { toggleTarget(row); }
            });
        });
    }

    function charIdSet() {
        var ids = {};
        blocks.querySelectorAll('.char-detail[data-char-id]').forEach(function (d) {
            ids[d.dataset.charId] = true;
        });
        return ids;
    }

    function markNewSince(known) {
        blocks.querySelectorAll('.char-detail[data-char-id]').forEach(function (d) {
            if (!known[d.dataset.charId]) { d.closest('.char-block').classList.add('is-new'); }
        });
    }

    function refresh(highlightNew) {
        var open = openState();
        var known = highlightNew ? charIdSet() : null;
        blocks.classList.add('loading');
        fetch(path + '?fragment=blocks&' + params())
            .then(function (r) { return r.text(); })
            .then(function (html) {
                blocks.innerHTML = html;
                blocks.classList.remove('loading');
                if (known) { markNewSince(known); }
                restoreOpen(open);
                renderChips();
                syncWindowButtons();
                history.replaceState(null, '', path + '?' + params());
            })
            .catch(function () { blocks.classList.remove('loading'); });
    }

    function loadDetail(detail) {
        detail.dataset.loaded = 'loading';
        detail.innerHTML = '<div class="empty-state"><span class="css-spinner"></span> loading…</div>';
        return fetch(path + '?' + params({ fragment: 'detail', char: detail.dataset.charId }))
            .then(function (r) { return r.text(); })
            .then(function (html) { detail.innerHTML = html; detail.dataset.loaded = '1'; })
            .catch(function () { detail.dataset.loaded = '0'; detail.innerHTML = ''; });
    }

    // Drill-down inside an already-swapped detail card: exact hulls behind one target
    // category, fetched on first expand so the detail payload stays small.
    function toggleTarget(row) {
        var detail = row.nextElementSibling;
        var wasOpen = detail.classList.contains('open');
        var card = row.closest('.char-detail');

        // Only one group open at a time, so the card's height stays predictable.
        card.querySelectorAll('.tgt-detail.open').forEach(function (d) {
            d.classList.remove('open');
            d.previousElementSibling.classList.remove('expanded');
            d.previousElementSibling.setAttribute('aria-expanded', 'false');
        });
        if (wasOpen) {
            return;                       // clicking the open group just closes it
        }
        detail.classList.add('open');
        row.classList.add('expanded');
        row.setAttribute('aria-expanded', 'true');
        if (detail.dataset.loaded !== '0') {
            return;
        }
        var cell = detail.querySelector('td');
        detail.dataset.loaded = 'loading';
        cell.innerHTML = '<div class="empty-state"><span class="css-spinner"></span> loading…</div>';
        fetch(path + '?' + params({ fragment: 'targets', char: card.dataset.charId, bucket: row.dataset.bucket }))
            .then(function (r) { return r.text(); })
            .then(function (html) { cell.innerHTML = html; detail.dataset.loaded = '1'; })
            .catch(function () { detail.dataset.loaded = '0'; cell.innerHTML = ''; });
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

    function namesFrom(lines) {
        return lines.map(function (n) { return n.trim(); }).filter(Boolean);
    }

    // `append` keeps what is already listed and adds only names not already there, so a
    // second paste grows the scan instead of replacing it. Case-insensitive: EVE names are
    // unique regardless of case, and the in-game copy preserves the player's own casing.
    function applyNames(lines, append) {
        var incoming = namesFrom(lines);
        if (!incoming.length) { return false; }
        var merged = append && state.names ? state.names.split(',').concat(incoming) : incoming;
        var seen = {};
        var out = [];
        merged.forEach(function (n) {
            var key = n.toLowerCase();
            if (!seen[key]) { seen[key] = true; out.push(n); }
        });
        state.names = out.join(',');
        var box = document.getElementById('names');
        if (box) { box.value = out.join('\n'); }
        state.filters = {};
        refresh(append);
        return true;
    }

    // Paste anywhere on the page to add pilots, the way localthreat does it. A paste aimed
    // at the textarea (or any field) is left alone so normal editing still works.
    document.addEventListener('paste', function (e) {
        var t = e.target;
        if (t && (t.tagName === 'TEXTAREA' || t.tagName === 'INPUT' || t.isContentEditable)) {
            return;
        }
        var data = e.clipboardData || window.clipboardData;
        if (!data) { return; }
        var text = data.getData('text');
        if (text && applyNames(text.split('\n'), true)) { e.preventDefault(); }
    });

    document.addEventListener('click', function (e) {
        var x = e.target.closest('.chip-x');
        if (x) { delete state.filters[x.dataset.key]; refresh(); return; }
        if (e.target.closest('#clear-filters')) { state.filters = {}; refresh(); return; }
        if (e.target.closest('#analyze')) {
            var box = document.getElementById('names');
            // one pilot per line, as the in-game member-list paste produces
            applyNames(box.value.split('\n'), false);
            return;
        }
        var win = e.target.closest('.win-btn');
        if (win) { state.window = win.dataset.window; refresh(); return; }
        var f = e.target.closest('.filterable');
        if (f) { state.filters[f.dataset.filter] = f.dataset.value; refresh(); return; }
        var tgt = e.target.closest('.tgt-row');
        if (tgt) { if (!tgt.classList.contains('is-empty')) { toggleTarget(tgt); } return; }
        var sum = e.target.closest('.char-summary');
        if (sum) { toggleExpand(sum); }
    });

    document.addEventListener('keydown', function (e) {
        if (e.key !== 'Enter' && e.key !== ' ') {
            return;
        }
        var tgtRow = e.target.closest('.tgt-row');
        if (tgtRow) {
            e.preventDefault();
            if (!tgtRow.classList.contains('is-empty')) { toggleTarget(tgtRow); }
            return;
        }
        var sum = e.target.closest('.char-summary');
        if (sum) { e.preventDefault(); toggleExpand(sum); }
    });

    renderChips();
    syncWindowButtons();
})();
