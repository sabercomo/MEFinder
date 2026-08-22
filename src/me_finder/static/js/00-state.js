/* ═══════════════════════════════════════════════════════════════
   App State
   ═══════════════════════════════════════════════════════════════ */
let currentPage = 'search';
let currentMode = 'auto';
let searchResults = [];
let selectedIndex = -1;
let searchSeq = 0;
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
let searchSourceType = 'all';
let searchLimit = 10;
let searchDocumentId = '';
// 全文检索范围：单篇文献(searchDocumentId)与作品组(searchGroupId)互斥；两者皆空=全部文献。
let searchGroupId = '';
let searchSourceFiles = [];
let searchVolumes = [];
let searchDocumentsLoaded = false;
// 文献库摘要在搜索下拉与文献库页之间共用一份：两处并发打开时只发一次请求。
let libraryCatalog = null;
let libraryCatalogPromise = null;

let detailContextResizeObserver = null;

let libSources = [];
let libVolumes = [];
let libVolumeBySource = new Map();
let libWorks = [];
let libStats = null;
let libLoaded = false;
// 作品组：只限定 source_file 集合，不引入 folder/root scope。空串 = 全部文献。
let libDocumentGroups = [];
let libGroupScopeId = '';
let libDetailLoaded = {};
let libDetailPending = {};
let libFilterTimer = null;
let libraryRenderToken = 0;
const LIBRARY_RENDER_BATCH = 50;
const DRAG_SELECT_EDGE_ZONE = 56;
const DRAG_SELECT_MAX_SCROLL_SPEED = 26;
let libTypeFilter = 'all';
let libLangFilter = 'all';
let libDocTypeFilter = 'all';
// 本国语言视角：'chinese'（中文为本国）或 'foreign'（西文为本国，此时中文计入外文）。
// 只改变文献库的语言分类标签与「本国/外文」归属，不改变联网数据源路由——
// 数据源永远按文字系统事实选择（中文→知网，其余→Crossref / 图书目录）。
let libDefaultLanguage = loadLibDefaultLanguage();
let libStatusFilter = 'all';
let libSelectedId = null;
let libDeleteSelection = new Set();
let libraryExportRunning = false;
let libraryDragSelection = null;
let suppressLibrarySelectionClick = false;
let libViewMode = localStorage.getItem('meFinderLibraryView') === 'grid' ? 'grid' : 'list';
let libSortField = ['imported_at','title','author','modified_at','source_type','status'].indexOf(localStorage.getItem('meFinderLibrarySortField')) >= 0 ? localStorage.getItem('meFinderLibrarySortField') : 'imported_at';
let libSortDirection = localStorage.getItem('meFinderLibrarySortDirection') === 'asc' ? 'asc' : 'desc';

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
let mineruConfigLoaded = false;
let visionConfigLoaded = false;
let visionConfig = {providers: [], default_provider_id: null, auto_fallback_from_mineru: false};
// Per-provider live connectivity, keyed by id: {sig, ok}. Kept in memory only —
// a reload honestly resets接口到“待测试”，因为“可用”须以真实测连为准。
let visionTestResults = {};
let visionModelOptions = [];
let visionModelRequestSerial = 0;
let preferencesLoaded = false;
let currentTheme = document.documentElement.dataset.theme || 'frost-blue';
let persistedTheme = currentTheme;
let themeRevision = 0;
let themeSaveQueue = Promise.resolve();
// 可扩展主题引擎（阶段 3-6）：外观模式 + 浅/深各自独立的主题选择 + 自定义主题。
// 首帧仍由服务端注入的 data-theme 决定；载入偏好后 applyAppearance() 校正。
let appearanceState = { mode: 'system', light: 'frost-blue', dark: 'midnight', customThemes: {} };
let appearanceEditMode = 'light';   // 设置页当前正在编辑哪一套（浅/深）
let appearanceReady = false;
let currentPdfOpenMode = 'native';
let currentDocumentExportMode = 'data_only';
let autoUpdateEnabled = false;
let updateAutoStarted = false;
let updateState = {status: 'idle', can_self_update: false};
const desktopShell = document.documentElement.dataset.desktopShell || '';
let dataLocationLoaded = false;
let pendingDataLocation = '';

