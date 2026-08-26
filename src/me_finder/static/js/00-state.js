/* ═══════════════════════════════════════════════════════════════
   App State
   ═══════════════════════════════════════════════════════════════ */
let currentPage = 'search';
const searchStore = {
  currentMode: 'auto',
  results: [],
  selectedIndex: -1,
  sequence: 0,
  sourceType: 'all',
  limit: 10,
  documentId: '',
  groupId: '',
  sourceFiles: [],
  volumes: [],
  documentsLoaded: false,
  libraryCatalog: null,
  libraryCatalogPromise: null
};
const CITATION_STYLE_OPTIONS = [
  {id:'chinese', label:'中文脚注'},
  {id:'gb', label:'GB/T 7714'},
  {id:'chicago', label:'Chicago'},
  {id:'apa', label:'APA'},
  {id:'mla', label:'MLA'}
];
const CITATION_STYLE_IDS = new Set(CITATION_STYLE_OPTIONS.map(function(option) { return option.id; }));
const DEFAULT_CITATION_STYLES = ['chinese', 'gb'];
const CITATION_STYLES_STORAGE_KEY = 'meFinderEnabledCitationStylesV1';
const ONLINE_METADATA_AUTO_MATCH_THRESHOLD_DEFAULT = 0.90;  // 联网书目补全自动采用唯一高匹配候选的分数下限
const ONLINE_METADATA_AUTO_MATCH_MIN_PERCENT = 80;  // 低于此值只弹候选让人工确认，避免误采
let onlineMetadataAutoMatchThreshold = loadOnlineAutoMatchThreshold();
let enabledCitationStyles = loadLocalCitationStyles() || DEFAULT_CITATION_STYLES.slice();
let citationStyle = loadLocalSelectedCitationStyle() || 'chinese';
// 全文检索范围：单篇文献(searchDocumentId)与作品组(searchGroupId)互斥；两者皆空=全部文献。
// 文献库摘要在搜索下拉与文献库页之间共用一份：两处并发打开时只发一次请求。

let detailContextResizeObserver = null;

const libraryStore = {
  sources: [],
  volumes: [],
  volumeBySource: new Map(),
  works: [],
  stats: null,
  loaded: false,
  documentGroups: [],
  groupScopeId: '',
  detailLoaded: {},
  detailPending: {},
  filterTimer: null,
  renderToken: 0,
  typeFilter: 'all',
  languageFilter: 'all',
  documentTypeFilter: 'all',
  defaultLanguage: loadLibDefaultLanguage(),
  statusFilter: 'all',
  selectedId: null,
  deleteSelection: new Set(),
  exportRunning: false,
  dragSelection: null,
  suppressSelectionClick: false,
  viewMode: localStorage.getItem('meFinderLibraryView') === 'grid' ? 'grid' : 'list',
  sortField: ['imported_at','title','author','modified_at','source_type','status'].indexOf(localStorage.getItem('meFinderLibrarySortField')) >= 0 ? localStorage.getItem('meFinderLibrarySortField') : 'imported_at',
  sortDirection: localStorage.getItem('meFinderLibrarySortDirection') === 'asc' ? 'asc' : 'desc'
};
// 作品组：只限定 source_file 集合，不引入 folder/root scope。空串 = 全部文献。
const LIBRARY_RENDER_BATCH = 50;
const DRAG_SELECT_EDGE_ZONE = 56;
const DRAG_SELECT_MAX_SCROLL_SPEED = 26;
// 本国语言视角：'chinese'（中文为本国）或 'foreign'（西文为本国，此时中文计入外文）。
// 只改变文献库的语言分类标签与「本国/外文」归属，不改变联网数据源路由——
// 数据源永远按文字系统事实选择（中文→知网，其余→Crossref / 图书目录）。

let calSegments = [];
let calSelectedDoc = null;
let calSelectedSourceId = null;
let calAutoResult = null;
let calTransientStatus = {};
let removeDocumentTarget = null;
let removeDocumentTargets = [];
let removeSecondStage = false;
let removeRequestController = null;
let appDialogResolve = null;
let appDialogPreviousFocus = null;
const parserStore = {
  mineruConfigLoaded: false,
  visionConfigLoaded: false,
  visionConfig: {providers: [], default_provider_id: null, auto_fallback_from_mineru: false},
  visionTestResults: {},
  visionModelOptions: [],
  visionModelRequestSerial: 0,
  mineruAccounts: [],
  mineruStatistics: {parsed_book_count:0, parsed_page_count:0, credentials:[]},
  parserStatistics: {total:{parsed_book_count:0, parsed_page_count:0, provider_count:0}, providers:[]},
  mineruSelectedAccountId: '',
  localOCRConfig: null,
  localOCRPollTimer: null,
  managedMineruPollTimer: null,
  managedMineruWasBusy: false,
  mineruLocalConfig: {}
};
// Per-provider live connectivity, keyed by id: {sig, ok}. Kept in memory only —
// a reload honestly resets接口到“待测试”，因为“可用”须以真实测连为准。
const initialTheme = document.documentElement.dataset.theme || 'frost-blue';
const settingsStore = {
  preferencesLoaded: false,
  currentTheme: initialTheme,
  persistedTheme: initialTheme,
  themeRevision: 0,
  themeSaveQueue: Promise.resolve(),
  appearanceState: { mode: 'system', light: 'frost-blue', dark: 'midnight', customThemes: {} },
  appearanceEditMode: 'light',
  appearanceReady: false,
  currentPdfOpenMode: 'native',
  currentPdfParseMode: 'auto',
  pdfParseModeSaving: false,
  currentDocumentExportMode: 'data_only',
  // Markdown / EPUB 共用页码锚点策略；三项页面噪声固定清理。
  exportPageCleanup: {
    page_marker_mode: 'full',
    remove_visible_page_numbers: true,
    remove_running_headers: true,
    remove_running_footers: true
  },
  exportPageCleanupSaving: false,
  autoUpdateEnabled: false,
  updateAutoStarted: false,
  updateState: {status: 'idle', can_self_update: false},
  dataLocationLoaded: false,
  pendingDataLocation: '',
  preferencesLoadPromise: null,
  pdfOpenModeSaving: false,
  documentExportModeSaving: false,
  citationStylesSaving: false,
  scanDirectories: []
};
// 可扩展主题引擎（阶段 3-6）：外观模式 + 浅/深各自独立的主题选择 + 自定义主题。
// 首帧仍由服务端注入的 data-theme 决定；载入偏好后 applyAppearance() 校正。
const desktopShell = document.documentElement.dataset.desktopShell || '';
const importStore = { queue: [] };
