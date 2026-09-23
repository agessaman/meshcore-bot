import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const templateUrl = new URL(
    '../../modules/web_viewer/templates/contacts.html',
    import.meta.url,
);
const templateSource = fs.readFileSync(templateUrl, 'utf8');
const classStart = templateSource.indexOf('class ModernContactsManager');
const classEnd = templateSource.indexOf('\nfunction exportData', classStart);
assert.ok(classStart >= 0 && classEnd > classStart, 'contacts manager class not found');
const classSource = templateSource.slice(classStart, classEnd);

function fakeElement(overrides = {}) {
    return {
        disabled: false,
        textContent: '',
        innerHTML: '',
        value: '',
        listeners: {},
        addEventListener(type, listener) {
            this.listeners[type] = listener;
        },
        replaceChildren() {
            this.innerHTML = '';
        },
        querySelectorAll() {
            return [];
        },
        ...overrides,
    };
}

function loadManagerClass() {
    const elements = new Map();
    const context = vm.createContext({
        AbortController,
        URLSearchParams,
        // Template injects IS_ADMIN for display; tests assume admin UI (legacy).
        IS_ADMIN: true,
        Chart: function Chart() {},
        bootstrap: {
            Dropdown: class Dropdown {},
            Modal: {
                getInstance() {
                    return null;
                },
            },
            Tooltip: class Tooltip {},
        },
        console,
        document: {
            body: fakeElement(),
            getElementById(id) {
                return elements.get(id) || null;
            },
            querySelector() {
                return null;
            },
            querySelectorAll() {
                return [];
            },
        },
        fetch: async () => {
            throw new Error('unexpected fetch');
        },
        window: {
            clearTimeout,
            localStorage: {
                getItem() {
                    return null;
                },
                setItem() {},
            },
            matchMedia() {
                return { matches: false, addEventListener() {} };
            },
            setTimeout,
        },
    });
    vm.runInContext(
        `${classSource}\nglobalThis.ModernContactsManager = ModernContactsManager;`,
        context,
    );
    return { context, elements, Manager: context.ModernContactsManager };
}

function bareManager(Manager) {
    const manager = Object.create(Manager.prototype);
    Object.assign(manager, {
        contactsData: { tracking_data: [], server_stats: {} },
        filteredData: [],
        selectedContactIds: new Set(),
        searchTerm: '',
        sortColumn: 'last_seen',
        sortDirection: 'desc',
        currentPage: 1,
        pageSize: 100,
        pagination: {
            page: 1,
            page_size: 100,
            total_items: 0,
            total_pages: 1,
            has_previous: false,
            has_next: false,
        },
        filteredStats: {},
        requestController: null,
        searchTimer: null,
        mobileLayout: { matches: false },
        activityChart: null,
    });
    return manager;
}

function successfulResponse(data) {
    return {
        ok: true,
        async json() {
            return data;
        },
    };
}

test('loadContactsData sends bounded server-side list parameters', async () => {
    const { context, elements, Manager } = loadManagerClass();
    elements.set('contacts-timespan', fakeElement({ value: '30d' }));
    elements.set('contacts-path-bytes', fakeElement({ value: '2' }));
    elements.set('contacts-device-role', fakeElement({ value: 'repeater' }));
    elements.set('contacts-hop-filter', fakeElement({ value: '2' }));
    elements.set('contacts-location-filter', fakeElement({ value: 'known' }));
    elements.set('contacts-starred-filter', fakeElement({ value: 'yes' }));
    const manager = bareManager(Manager);
    manager.currentPage = 3;
    manager.searchTerm = 'alpha';
    manager.sortColumn = 'distance';
    manager.sortDirection = 'asc';
    manager.setLoadingState = () => {};
    manager.updateSortIcons = () => {};
    manager.renderContactsData = () => {};
    manager.updatePagination = () => {};
    manager.updateStatistics = () => {};

    let request;
    context.fetch = async (url, options) => {
        request = { url, options };
        return successfulResponse({
            tracking_data: [{ user_id: 'a1' }],
            server_stats: {},
            filtered_stats: { contacts_total: 201 },
            pagination: {
                page: 3,
                page_size: 100,
                total_items: 201,
                total_pages: 3,
                has_previous: true,
                has_next: false,
            },
        });
    };

    await manager.loadContactsData();
    const url = new URL(request.url, 'http://viewer.local');
    assert.equal(url.pathname, '/api/contacts');
    assert.equal(url.searchParams.get('since'), '30d');
    assert.equal(url.searchParams.get('page'), '3');
    assert.equal(url.searchParams.get('page_size'), '100');
    assert.equal(url.searchParams.get('search'), 'alpha');
    assert.equal(url.searchParams.get('path_bytes'), '2');
    assert.equal(url.searchParams.get('device_role'), 'repeater');
    assert.equal(url.searchParams.get('hop_filter'), '2');
    assert.equal(url.searchParams.get('location_filter'), 'known');
    assert.equal(url.searchParams.get('starred'), 'yes');
    assert.equal(url.searchParams.get('sort'), 'distance');
    assert.equal(url.searchParams.get('direction'), 'asc');
    assert.equal(manager.filteredData.length, 1);
    assert.equal(manager.pagination.total_items, 201);
});

test('a newer contacts request aborts the stale request', async () => {
    const { context, elements, Manager } = loadManagerClass();
    elements.set('contacts-timespan', fakeElement({ value: '30d' }));
    elements.set('contacts-path-bytes', fakeElement({ value: '' }));
    elements.set('contacts-device-role', fakeElement({ value: '' }));
    elements.set('contacts-hop-filter', fakeElement({ value: '' }));
    elements.set('contacts-location-filter', fakeElement({ value: '' }));
    elements.set('contacts-starred-filter', fakeElement({ value: '' }));
    const manager = bareManager(Manager);
    manager.setLoadingState = () => {};
    manager.updateSortIcons = () => {};
    manager.renderContactsData = () => {};
    manager.updatePagination = () => {};
    manager.updateStatistics = () => {};

    const signals = [];
    let callCount = 0;
    context.fetch = (url, { signal }) => {
        signals.push(signal);
        callCount += 1;
        if (callCount === 1) {
            return new Promise((resolve, reject) => {
                signal.addEventListener('abort', () => {
                    const error = new Error('aborted');
                    error.name = 'AbortError';
                    reject(error);
                });
            });
        }
        return Promise.resolve(successfulResponse({
            tracking_data: [],
            server_stats: {},
            filtered_stats: {},
            pagination: manager.pagination,
        }));
    };

    const stale = manager.loadContactsData();
    await new Promise(resolve => setImmediate(resolve));
    const current = manager.loadContactsData();
    await Promise.all([stale, current]);
    assert.equal(signals.length, 2);
    assert.equal(signals[0].aborted, true);
    assert.equal(signals[1].aborted, false);
});

test('updateSortIcons leaves the hops info tip alone', () => {
    const { context, Manager } = loadManagerClass();
    const sortIcon = fakeElement({ className: 'fas fa-sort sort-icon' });
    const infoIcon = fakeElement({ className: 'fas fa-info-circle' });
    const hopsHeader = fakeElement({
        classList: {
            remove(name) {
                this._removed = name;
            },
            add(name) {
                this._added = name;
            },
        },
        querySelector(selector) {
            if (selector === '.sort-icon') return sortIcon;
            if (selector === 'i') return sortIcon;
            return null;
        },
        querySelectorAll(selector) {
            if (selector === '.sort-icon') return [sortIcon];
            if (selector === 'i') return [sortIcon, infoIcon];
            return [];
        },
    });
    const otherHeader = fakeElement({
        classList: {
            remove() {},
            add() {},
        },
        querySelector() {
            return null;
        },
        querySelectorAll() {
            return [];
        },
    });

    context.document.querySelectorAll = selector => {
        if (selector === '.sortable .sort-icon') return [sortIcon];
        if (selector === '.sortable i') return [sortIcon, infoIcon];
        if (selector === '.sortable') return [hopsHeader, otherHeader];
        return [];
    };
    context.document.querySelector = selector => {
        if (selector === '[data-sort="hop_count"]') return hopsHeader;
        return null;
    };

    const manager = bareManager(Manager);
    manager.sortColumn = 'hop_count';
    manager.sortDirection = 'asc';
    manager.syncMobileSortSelect = () => {};
    manager.updateSortIcons();

    assert.equal(sortIcon.className, 'fas fa-sort-up sort-icon');
    assert.equal(infoIcon.className, 'fas fa-info-circle');
    assert.equal(hopsHeader.classList._added, 'sort-active');
});

test('search is debounced before reloading the first page', () => {
    const { context, elements, Manager } = loadManagerClass();
    const requiredIds = [
        'contacts-list-multibyte-badge-info',
        'refresh-contacts',
        'search-contacts',
        'contacts-timespan',
        'contacts-path-bytes',
        'contacts-device-role',
        'contacts-hop-filter',
        'contacts-location-filter',
        'contacts-starred-filter',
        'contacts-clear-filters',
        'contacts-timespan-mobile-menu',
        'bulk-delete-contacts',
        'contacts-list-panel',
        'contacts-mobile-sort',
        'contacts-page-previous',
        'contacts-page-next',
    ];
    requiredIds.forEach(id => elements.set(id, fakeElement()));
    const manager = bareManager(Manager);
    const reloads = [];
    manager.loadContactsData = options => {
        reloads.push(options);
    };
    manager.syncContactsTimespanMobileUI = () => {};
    manager.showBulkDeleteConfirmation = () => {};

    let scheduled;
    context.window.clearTimeout = () => {};
    context.window.setTimeout = (callback, delay) => {
        scheduled = { callback, delay };
        return 1;
    };

    manager.setupEventHandlers();
    elements.get('search-contacts').listeners.input({
        target: { value: '  Alpha  ' },
    });
    assert.equal(manager.searchTerm, 'alpha');
    assert.equal(reloads.length, 0);
    assert.equal(scheduled.delay, 300);
    scheduled.callback();
    assert.equal(reloads.length, 1);
    assert.equal(reloads[0].resetPage, true);
    assert.equal(reloads[0].clearSelection, true);
});

test('renderContactsData builds only the active responsive layout', () => {
    const { elements, Manager } = loadManagerClass();
    const tbody = fakeElement();
    const mobile = fakeElement();
    elements.set('contacts-table-body', tbody);
    elements.set('contacts-mobile-stack', mobile);
    const manager = bareManager(Manager);
    manager.filteredData = [
        { user_id: 'a1', username: 'Alpha', advert_count: 1 },
        { user_id: 'b2', username: 'Bravo', advert_count: 2 },
    ];
    manager.escapeHtml = value => String(value);
    manager.formatDeviceType = () => '';
    manager.formatLocation = () => '';
    manager.formatDistance = () => '';
    manager.formatSignal = () => '';
    manager.formatHops = () => '';
    manager.formatPathEncodingBadge = () => '';
    manager.formatTimestamp = () => '';
    manager.formatTimeAgo = () => '';
    manager.setupPathTooltips = () => {};
    manager.updateBulkDeleteButton = () => {};
    manager.updateSelectAllCheckbox = () => {};
    manager.initContactsDropdowns = () => {};

    manager.mobileLayout = { matches: false };
    manager.renderContactsData();
    assert.equal((tbody.innerHTML.match(/<tr>/g) || []).length, 2);
    assert.equal(mobile.innerHTML, '');

    manager.mobileLayout = { matches: true };
    manager.renderContactsData();
    assert.equal(tbody.innerHTML, '');
    assert.equal(
        (mobile.innerHTML.match(/class="contact-mobile-card"/g) || []).length,
        2,
    );
});

test('successful delete reloads the current server page', async () => {
    const { context, elements, Manager } = loadManagerClass();
    elements.set(
        'confirmDeleteBtn',
        fakeElement({ innerHTML: '<i class="fas fa-trash"></i>' }),
    );
    const manager = bareManager(Manager);
    manager.selectedContactIds.add('deadbeef');
    let reloads = 0;
    manager.loadContactsData = async () => {
        reloads += 1;
    };
    manager.showSuccess = () => {};
    manager.showError = error => {
        throw new Error(error);
    };
    manager.escapeHtml = value => String(value);
    context.fetch = async () => successfulResponse({ success: true });
    let hidden = false;
    context.bootstrap.Modal.getInstance = () => ({
        hide() {
            hidden = true;
        },
    });

    await manager.deleteContact('deadbeef', 'Deleted Contact', fakeElement());
    assert.equal(hidden, true);
    assert.equal(manager.selectedContactIds.has('deadbeef'), false);
    assert.equal(reloads, 1);
});
