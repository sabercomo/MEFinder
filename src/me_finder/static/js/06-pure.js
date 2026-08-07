/* ── Pure logic ────────────────────────────────────────────────
   不碰 DOM、不读全局状态的纯函数：输入完全由参数决定，输出可预测。
   把它们从渲染函数里抽出来，页码映射这类核心算术第一次可以被单元测试
   覆盖（见 tests/test_frontend_pure_logic.py）。原渲染函数保留，改成
   “读 DOM → 调这里的纯函数 → 写 DOM”，行为逐位不变。
   ────────────────────────────────────────────────────────────── */

// 阿拉伯数字转罗马数字（校准页码用）。num<=0 时原样返回，避免罗马数字无零。
function intToRoman(num, upper) {
  if (num <= 0) return String(num);
  var vals = [[1000,'m'],[900,'cm'],[500,'d'],[400,'cd'],[100,'c'],[90,'xc'],[50,'l'],[40,'xl'],[10,'x'],[9,'ix'],[5,'v'],[4,'iv'],[1,'i']];
  var out = '';
  for (var i = 0; i < vals.length; i++) {
    while (num >= vals[i][0]) { out += vals[i][1]; num -= vals[i][0]; }
  }
  return upper ? out.toUpperCase() : out;
}

// 给定校准分段与 0 基的 PDF 页序号，算出该页对应的引用页码标签与映射方式。
// 返回 {mapped, mappedEnd, method}：mapped 为 null 表示未校准/命中空引用段。
// 逐位照搬自 updateCalPreview 的原内联算术，不改任何规则。
function calibrateCitationForIndex(segments, pageIndex) {
  var mapped = null;
  var mappedEnd = null;
  var method = 'uncalibrated';
  for (var i = 0; i < segments.length; i++) {
    var seg = segments[i];
    var start = seg.pdf_page_start != null ? seg.pdf_page_start : -1;
    var end = seg.pdf_page_end != null ? seg.pdf_page_end : start;
    if (pageIndex >= start && pageIndex <= end) {
      if (seg.citation === null && !seg.citation_page_start) {
        method = seg.method || 'uncalibrated';
        mapped = null;
        break;
      }
      if (seg.citation_page_start != null && seg.citation_page_start !== '') {
        var logicalPageCount = seg.layout_mode === 'spread' ? 2 : 1;
        var offset = (pageIndex - start) * logicalPageCount;
        var style = seg.number_style || 'arabic';
        var baseNumber = parseInt(seg.citation_page_start, 10);
        if (isNaN(baseNumber)) baseNumber = 1;
        var citNum = baseNumber + offset;
        var citEndNum = citNum + logicalPageCount - 1;
        if (style === 'roman_lower' || style === 'roman_upper') {
          mapped = intToRoman(citNum, style === 'roman_upper');
          mappedEnd = intToRoman(citEndNum, style === 'roman_upper');
        } else {
          mapped = String(citNum);
          mappedEnd = String(citEndNum);
        }
        method = seg.method || 'manual_segment';
        break;
      }
    }
  }
  return { mapped: mapped, mappedEnd: mappedEnd, method: method };
}

// ── 书目字段判定 ───────────────────────────────────────────────
// 引文必需字段的中文名（ISBN/ISSN/DOI 不计入必需，故不在此表触发缺失）。
const bibliographicFieldLabels = {author:'作者',title:'书名',translator:'译者',publisher:'出版社',publish_place:'出版地',publish_year:'出版年份',journal_name:'出版刊物',volume:'卷次',issue:'期号',page_range:'页码',doi:'DOI',issn:'ISSN'};

// 归一化文献类型：未知值一律回落为 book。
function bibliographicDocType(meta) {
  var value = String((meta && meta.document_type) || '');
  return ['book','translated_book','journal_article','thesis'].indexOf(value) >= 0 ? value : 'book';
}

// 编辑器只区分 期刊/学位论文/图书 三档（译著在图书档内再由译者细分）。
function bibliographicEditorDocType(docType) {
  return docType === 'journal_article' || docType === 'thesis' ? docType : 'book';
}

// 编辑器选定类型 + 是否填了译者 → 落地文献类型（collectBibliographicForm 用）。
function bibliographicFormDocType(editorDocType, translator) {
  return editorDocType === 'journal_article' || editorDocType === 'thesis'
    ? editorDocType
    : (translator ? 'translated_book' : 'book');
}

// 按文献类型算出仍缺的引文必需字段。后端给了 metadata_missing_fields 就以它为准，
// 否则按类型的必需字段清单逐个查空；ISBN 及未登记字段永不计入。
function bibliographicMissingFields(meta) {
  meta = meta || {};
  var docType = bibliographicDocType(meta);
  var listed = Array.isArray(meta.metadata_missing_fields) ? meta.metadata_missing_fields.slice() : null;
  var required = listed || (docType === 'thesis'
    ? ['author','title','publisher','publish_year']
    : docType === 'journal_article'
      ? ['author','title','journal_name','publish_year','issue']
      : ['author','title','publisher','publish_place','publish_year']);
  if (!listed && docType === 'translated_book') required.splice(2, 0, 'translator');
  return required.filter(function(field, index, values) {
    if (field === 'isbn' || !bibliographicFieldLabels[field] || values.indexOf(field) !== index) return false;
    if (listed) return true;
    return !String(meta[field] == null ? '' : meta[field]).trim();
  });
}

// 联网补全的等价判定：已填值与联网值“实质相同”时视为无冲突、不覆盖。
// 卷期按数值比较、DOI 去掉解析前缀、ISSN 去连字符/大小写归一。
function bibliographicValuesEquivalent(field, left, right) {
  left = String(left || '').trim();
  right = String(right || '').trim();
  if (left === right) return true;
  if (field === 'issue' || field === 'volume') {
    return /^\d+$/.test(left) && /^\d+$/.test(right) && Number(left) === Number(right);
  }
  if (field === 'doi') {
    return left.replace(/^https?:\/\/(?:dx\.)?doi\.org\//i, '').toLowerCase()
      === right.replace(/^https?:\/\/(?:dx\.)?doi\.org\//i, '').toLowerCase();
  }
  if (field === 'issn') return left.replace(/[-\s]/g, '').toUpperCase() === right.replace(/[-\s]/g, '').toUpperCase();
  return false;
}

// ── 批量联网源路由 ─────────────────────────────────────────────
// 无中日韩汉字的题名视为外文；据此把每条缺信息文献分流到合适的联网源。
function isForeignTitle(title) {
  return !!String(title || '').trim() && !/[㐀-鿿]/.test(String(title));
}

// 中文期刊→知网，外文期刊→Crossref，外文图书→图书目录（Open Library / K10plus / LoC，
// Google 兜底），中文图书→本地 CIP（不联网，返回 null）。
function batchLookupSourceFor(meta) {
  var docType = bibliographicDocType(meta);
  var foreign = isForeignTitle(meta.title);
  if (docType === 'journal_article') return foreign ? 'crossref' : 'cnki';
  if (docType === 'book' || docType === 'translated_book') return foreign ? 'google_books' : null;
  return null;
}

// ── 映射与页码标签 ─────────────────────────────────────────────
// 枚举值转中文显示名的查表函数：未登记的值原样回吐，不猜不兜底。
function mappingMethodLabel(m) {
  const labels = {manual_segment:'人工分段',manual:'人工',manual_override:'人工覆盖',fixed_offset:'固定偏移',manual_page:'逐页校准',pdf_page_label:'PDF标签',numeric_bookmark_sequence:'PDF数字书签',native_pdf_edge_sequence:'页边数字序列',ocr_sequence:'OCR序列',ocr_sequence_with_structure:'OCR序列+结构',combined_sequence:'多来源序列',uncalibrated:'未校准',mixed:'混合'};
  return labels[m] || m || '';
}
function mappingStatusLabel(status) {
  const labels = {manual_mapped:'人工映射',auto_mapped_high:'自动映射 · 高可信',auto_mapped_medium:'自动映射 · 待确认',needs_review:'待确认',unmapped:'未映射',auto_mapping_failed:'页码自动检测失败',source_missing:'原文件缺失'};
  return labels[status] || status || '未映射';
}
function mappingConfidenceLabel(level, score) {
  const labels = {high:'高', medium:'中', low:'低', mixed:'混合'};
  const pct = score != null ? '（' + Math.round(Number(score) * 100) + '%）' : '';
  return (labels[level] || level || '') + pct;
}
function pageScopeLabel(scope) {
  const labels = {body:'正文', preface:'序言', front_matter:'前置页', appendix:'附录', mixed:'混合'};
  return labels[scope] || scope || '';
}
function logicalPageSideLabel(side, precision) {
  if (side === 'left') return '左页';
  if (side === 'right') return '右页';
  if (side === 'both') return '跨左右页';
  return precision === 'range_fallback' ? '坐标不足，显示页码范围' : '—';
}

// 自动映射证据摘要：对象按已知字段拼中文短句，未知结构回落为截断的 JSON。
function mappingEvidenceSummary(evidence) {
  if (!evidence) return '';
  if (typeof evidence === 'string') return evidence;
  const parts = [];
  if (evidence.observed_page_numbers != null) parts.push('识别页码 ' + evidence.observed_page_numbers + ' 个');
  if (evidence.sequence_consistency != null) parts.push('连续性 ' + Math.round(Number(evidence.sequence_consistency) * 100) + '%');
  if (evidence.inferred_offset != null) parts.push('offset ' + evidence.inferred_offset);
  if (evidence.structure_evidence) parts.push('结构：' + pageScopeLabel(evidence.structure_evidence));
  return parts.join('；') || JSON.stringify(evidence).slice(0, 120);
}

// 自动映射分段的一行摘要。PDF 页序号在库里是 0 基，展示时统一 +1 变成人读页号。
function autoMappingSegmentText(seg) {
  if (!seg) return '';
  const pdfStart = Number(seg.pdf_page_start) + 1;
  const pdfEnd = Number(seg.pdf_page_end) + 1;
  const citation = formatCitationPageLabel({
    source_type: 'pdf',
    citation_page_label_start: seg.citation_page_label_start,
    citation_page_label_end: seg.citation_page_label_end,
    citation_page_start: seg.citation_page_start,
    citation_page_end: seg.citation_page_end
  });
  const layout = seg.layout_mode === 'spread'
    ? ' · 双开页' + (seg.reading_direction === 'rtl' ? '（右→左）' : '（左→右）')
    : '';
  return pageScopeLabel(seg.page_scope) + ' PDF ' + pdfStart + '–' + pdfEnd + ' → ' + citation + layout + ' ' + mappingConfidenceLabel(seg.confidence_level, seg.mapping_confidence);
}

// 按优先级取第一个非空值（trim 后判空），全空返回 ''。
function firstPageValue(values) {
  for (var i = 0; i < values.length; i++) {
    if (values[i] !== undefined && values[i] !== null && String(values[i]).trim() !== '') {
      return String(values[i]).trim();
    }
  }
  return '';
}

// 识别「尚未校准」这类占位文案，避免把它当成真页码去拼范围。
function isUncalibratedPageLabel(value) {
  return /(?:页码尚未校准|引用页码尚未校准|页码未验证|未校准)/.test(String(value || ''));
}

// 把起止页码拼成中文页码范围。起止同前缀时合并为「第 X—Y 页」，
// 起页已含「页」字则直接连接，避免出现「第第…页页」。
function formatChinesePageRange(start, end) {
  start = String(start || '').trim();
  end = String(end || '').trim();
  if (!start || isUncalibratedPageLabel(start)) return '页码尚未校准';
  if (!end || end === start || isUncalibratedPageLabel(end)) end = '';

  var startMatch = start.match(/^(.*?第)([^页]+)页$/);
  var endMatch = end.match(/^(.*?第)([^页]+)页$/);
  if (startMatch && !end) return start;
  if (startMatch && endMatch && startMatch[1] === endMatch[1]) {
    return startMatch[1] + startMatch[2] + '—' + endMatch[2] + '页';
  }
  if (startMatch && end && end.indexOf('页') < 0) {
    return startMatch[1] + startMatch[2] + '—' + end + '页';
  }
  if (start.indexOf('页') >= 0) return end ? start + '—' + end : start;
  if (endMatch && endMatch[1] === '第') return '第' + start + '—' + endMatch[2] + '页';
  return '第' + start + (end ? '—' + end : '') + '页';
}

// 检索结果 → 引用页码标签。PDF 走校准后的 citation_* 字段，
// 其余来源用原始页码；两条路径的字段优先级不同，故分支保留。
function formatCitationPageLabel(item) {
  item = item || {};
  var sourceType = String(item.source_type || '').toLowerCase();
  var start;
  var end;
  if (sourceType === 'pdf') {
    start = firstPageValue([
      item.citation_page_label,
      item.citation_page_label_start,
      item.citation_page,
      item.citation_page_start
    ]);
    end = firstPageValue([item.citation_page_label_end, item.citation_page_end]);
  } else {
    start = firstPageValue([
      item.citation_page_label,
      item.citation_page_label_start,
      item.original_page_start,
      item.page
    ]);
    end = firstPageValue([
      item.citation_page_label_end,
      item.original_page_end,
      item.end_page
    ]);
  }
  return formatChinesePageRange(start, end);
}

// 枚举值 → 中文显示名。原在 40-bibliography.js，因 06-pure.js 需自包含而前移。
function pdfTypeLabel(type) {
  var labels = {native_text:'原生文本',scanned:'扫描版',broken_text:'文本损坏',complex_layout:'复杂排版',mineru_structured:'MinerU 结构化',api_structured:'视觉 API 结构化'};
  return labels[type] || type || '未知';
}

function structureLabel(s) {
  var labels = {article_collection:'文集',complete_works:'全集',selected_works:'选集',monograph:'专著',whole_pdf:'整本',pdf_document:'PDF 文献',manuscript_selection:'手稿选编',mixed:'混合',letters:'书信集'};
  return labels[s] || s || '';
}

function formatFileSize(bytes) {
  if (!bytes) return '—';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / 1048576).toFixed(1) + ' MB';
}

// 导入队列的处理步骤文案。原在 80-import.js，纯映射，前移以便单测。
function importStepsFor(q) {
  if (q.type !== 'pdf') return ['读取文件', '文本入库', '建立索引'];
  if (q.route === 'mineru') return ['读取文件', '类型检测', 'MinerU 解析', '文本入库', '建立索引'];
  if (q.route === 'vision') return ['读取文件', '类型检测', (q.providerName || '其他 API') + ' 解析', '文本入库', '建立索引'];
  return ['读取文件', '类型检测', '本地解析', '建立索引'];
}

function importRouteBadge(q) {
  if (q.type !== 'pdf' || !q.detectedType) return '';
  var mineru = q.route === 'mineru';
  var vision = q.route === 'vision';
  return '<span class="import-route-badge ' + (mineru ? 'mineru' : vision ? 'vision' : 'native') + '">'
    + esc(pdfTypeLabel(q.detectedType))
    + (mineru ? ' · 提交 MinerU' : vision ? ' · ' + esc(q.providerName || '其他视觉 API') : ' · 本地解析')
    + '</span>';
}

// 联网补全的查询字段裁剪：不同数据源接受的字段集不同。原在 80-import.js。
function batchQueryFor(source, meta) {
  if (source === 'cnki') return {title:meta.title||'', author:meta.author||'', publish_year:meta.publish_year||'', journal_name:meta.journal_name||'', doi:meta.doi||'', issn:meta.issn||''};
  if (source === 'crossref') return {title:meta.title||'', author:meta.author||'', publish_year:meta.publish_year||'', doi:meta.doi||''};
  return {title:meta.title||'', author:meta.author||'', publish_year:meta.publish_year||'', isbn:meta.isbn||''};
}

// 框选自动滚动的边缘速度曲线。原在 80-import.js，纯算术。
function dragSelectionEdgeSpeed(depth) {
  return Math.min(DRAG_SELECT_MAX_SCROLL_SPEED, Math.max(4, Math.round(depth / 2)));
}

// ── 阶段3 续：从渲染模块整簇前移的自包含纯函数 ──

// 原在 50-calibration.js，纯函数，前移以便单测。
function calibrationStatusGroup(status) {
  if (status === 'manual_mapped' || status === 'auto_mapped_high') return 'calibrated';
  if (status === 'needs_review') return 'review';
  if (status === 'auto_mapping_failed' || status === 'source_missing') return 'failed';
  if (status === 'mapping') return 'mapping';
  return 'pending';
}

// 原在 50-calibration.js，纯函数，前移以便单测。
function statusSemanticVariant(group) {
  var variants = {calibrated:'success',pending:'neutral',review:'warning',failed:'danger',mapping:'info'};
  return variants[group] || 'neutral';
}

// 原在 50-calibration.js，纯函数，前移以便单测。
function calibrationStatusLabel(status) {
  var labels = {manual_mapped:'页码已校准',auto_mapped_high:'页码已校准',needs_review:'页码待确认',unmapped:'页码尚未检测',auto_mapping_failed:'页码自动检测失败',mapping:'正在检测页码',source_missing:'原文件缺失'};
  return labels[status] || '页码尚未检测';
}

// 原在 50-calibration.js，纯函数，前移以便单测。
function formatCalDate(value) {
  if (!value) return '未知';
  var date = new Date(value);
  if (isNaN(date.getTime())) return '未知';
  return date.getFullYear() + '-' + String(date.getMonth()+1).padStart(2,'0') + '-' + String(date.getDate()).padStart(2,'0');
}

// 原在 50-calibration.js，纯函数，前移以便单测。
function segmentNumberStyleLabel(style) {
  return ({arabic:'阿拉伯数字',roman_lower:'罗马数字（小写）',roman_upper:'罗马数字（大写）',none:'无编号'})[style] || '阿拉伯数字';
}

// 原在 50-calibration.js，纯函数，前移以便单测。
function segmentLayoutLabel(layout) {
  return layout === 'spread' ? '双开页' : '单页';
}

// 原在 50-calibration.js，纯函数，前移以便单测。
function spreadGutterPercent(seg) {
  var gutter = Number(seg.gutter_x);
  if (!isFinite(gutter) || gutter < 0.3 || gutter > 0.7) gutter = 0.5;
  return Math.round(gutter * 100);
}

// 原在 50-calibration.js，纯函数，前移以便单测。
function spreadCitationPair(seg) {
  if (seg.number_style === 'none') return { mapped: false };
  if (seg.citation === null && !seg.citation_page_start) return { mapped: false };
  if (seg.citation_page_start == null || seg.citation_page_start === '') return { mapped: false };
  var base = parseInt(seg.citation_page_start, 10);
  if (isNaN(base)) return { mapped: false };
  var style = seg.number_style || 'arabic';
  function fmt(n) {
    if (style === 'roman_lower' || style === 'roman_upper') return intToRoman(n, style === 'roman_upper');
    return String(n);
  }
  var direction = seg.reading_direction === 'rtl' ? 'rtl' : 'ltr';
  var first = fmt(base), second = fmt(base + 1);
  return direction === 'rtl'
    ? { mapped: true, left: second, right: first, firstSide: 'right' }
    : { mapped: true, left: first, right: second, firstSide: 'left' };
}

// 原在 30-library.js，纯函数，前移以便单测。
function libLangChipLabel(scriptLang) {
  return scriptLang === 'chinese' ? '中文' : '外文';
}

// 原在 30-library.js，纯函数，前移以便单测。
function libraryDocType(source) {
  var value = String((source && source.document_type) || '');
  return value === 'journal_article' || value === 'thesis' ? value : 'book';
}

// 原在 30-library.js，纯函数，前移以便单测。
function librarySortProjection(source) {
  return {
    title: source.title || source.file_name || source.source_file_id,
    author: source.author || '',
    imported_at: source.imported_at || source.last_modified || '',
    modified_at: source.modified_at || source.last_modified || '',
    source_type: source.source_type === 'word' ? 'Word' : 'PDF'
  };
}

// 原在 70-vision.js，纯函数，前移以便单测。
function visionHash(text) {
  var h = 0;
  for (var i = 0; i < text.length; i++) h = (h * 31 + text.charCodeAt(i)) >>> 0;
  return h;
}

// 原在 70-vision.js，纯函数，前移以便单测。
function visionHostLabel(apiBase) {
  try {
    return new URL(apiBase.indexOf('://') >= 0 ? apiBase : 'https://' + apiBase).hostname || apiBase;
  } catch (e) {
    return apiBase || '';
  }
}


// 联网补全按钮主文案。原在 40-bibliography.js，纯映射。
function bibPrimaryLabel(source) {
  if (source === 'cnki') return '知网补全';
  if (source === 'crossref') return 'Crossref 补全';
  return '补全期刊信息';
}

// 书目元数据状态中文名。原在 40-bibliography.js，纯映射。
function metadataStatusLabel(status) {
  return ({complete:'完整',partial:'部分缺失',missing:'缺失',needs_review:'书目待确认',recognition_failed:'识别失败'})[status] || status || '未识别';
}

// 书目元数据来源中文名。原在 40-bibliography.js，纯映射。
function metadataSourceLabel(source) {
  return ({manual:'人工维护',auto:'自动识别',automatic_recognition:'自动识别',pdf_metadata:'PDF 元数据'})[source] || source || '未知';
}

// 合并书源的书目字段：优先取顶层非空值覆盖嵌套元数据。原在 40-bibliography.js。
function sourceBibliographicMetadata(src) {
  var nested = src && src.bibliographic_metadata ? src.bibliographic_metadata : {};
  var meta = Object.assign({}, nested);
  ['title','author','country','translator','publisher','publish_place','publish_year','isbn','journal_name','volume','issue','page_range','doi','issn','document_type','metadata_status','metadata_source','metadata_confidence','metadata_evidence','metadata_conflicts','metadata_missing_fields'].forEach(function(key) {
    if (src && src[key] != null && src[key] !== '') meta[key] = src[key];
  });
  return meta;
}

// 书目缺失字段的中文提示串。原在 40-bibliography.js，依赖本文件内的判定函数。
function bibliographicMissingText(meta) {
  var fields = bibliographicMissingFields(meta);
  var docType = bibliographicDocType(meta);
  return fields.length ? '书目缺失：' + fields.map(function(field) {
    return docType === 'thesis' && field === 'publisher' ? '学校' : bibliographicFieldLabels[field];
  }).join('、') : '';
}

// 主题预览缩略图标记。原在 60-settings.js，纯字符串拼接。
function themePreviewMarkup(themeId) {
  return '<span class="theme-preview" data-preview-theme="' + themeId + '" aria-hidden="true">'
    + '<span class="theme-mini-sidebar">'
    + '<span class="theme-mini-brand"><span class="theme-mini-brand-mark"></span><span class="theme-mini-brand-line"></span></span>'
    + '<span class="theme-mini-nav">'
    + '<span class="theme-mini-nav-item"><span class="theme-mini-nav-icon"></span><span class="theme-mini-nav-line"></span></span>'
    + '<span class="theme-mini-nav-item is-selected"><span class="theme-mini-nav-icon"></span><span class="theme-mini-nav-line"></span></span>'
    + '<span class="theme-mini-nav-item"><span class="theme-mini-nav-icon"></span><span class="theme-mini-nav-line"></span></span>'
    + '</span></span>'
    + '<span class="theme-mini-main">'
    + '<span class="theme-mini-header"><span class="theme-mini-heading"><i class="theme-mini-title-line"></i><i class="theme-mini-subtitle-line"></i></span><span class="theme-mini-header-status"><i></i><b></b></span></span>'
    + '<span class="theme-mini-search"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg><span class="theme-mini-search-line"></span><span class="theme-mini-search-action"></span></span>'
    + '<span class="theme-mini-cards">'
    + '<span class="theme-mini-doc-card"><span class="theme-mini-card-top"><i class="theme-mini-source"></i><i class="theme-mini-state is-success"></i></span><i class="theme-mini-doc-title"></i><i class="theme-mini-doc-title is-short"></i><i class="theme-mini-doc-meta"></i></span>'
    + '<span class="theme-mini-doc-card"><span class="theme-mini-card-top"><i class="theme-mini-source"></i><i class="theme-mini-state is-danger"></i></span><i class="theme-mini-doc-title"></i><i class="theme-mini-doc-title is-short"></i><i class="theme-mini-match"></i></span>'
    + '<span class="theme-mini-doc-card"><span class="theme-mini-card-top"><i class="theme-mini-source"></i><i class="theme-mini-state is-success"></i></span><i class="theme-mini-doc-title"></i><i class="theme-mini-doc-title is-short"></i><i class="theme-mini-doc-meta"></i></span>'
    + '</span></span></span>';
}

// 主题选项按钮标记。原在 60-settings.js，依赖 themePreviewMarkup。
function themeOptionMarkup(theme) {
  return '<button class="theme-option" type="button" data-theme-choice="' + theme.id + '" role="radio" aria-checked="false" onclick="setTheme(\'' + theme.id + '\')">'
    + '<span class="theme-option-head"><span class="theme-option-identity"><span class="theme-option-name">' + theme.name + '</span><span class="theme-option-tone">' + theme.tone + '</span></span>'
    + '<span class="theme-option-check" aria-hidden="true"><svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12 4 4L19 6"/></svg></span></span>'
    + themePreviewMarkup(theme.id)
    + '<span class="theme-option-description">' + theme.description + '</span></button>';
}

// 卷册索引：source_file_id → volume。原在 20-search.js，纯。
function buildVolumeIndex(volumes) {
  var index = new Map();
  (volumes || []).forEach(function(volume) {
    if (volume && volume.source_file_id) index.set(volume.source_file_id, volume);
  });
  return index;
}

// 折叠空白后按长度截断加省略号。原在 20-search.js，纯。
function truncate(s, n) {
  s = String(s || '').replace(/\s+/g, ' ');
  return s.length > n ? s.slice(0, n) + '…' : s;
}

// 匹配类型中文名。原在 20-search.js，纯映射。
function matchTypeLabel(t) {
  const m = {exact:'精确',normalized_exact:'标准化',space_insensitive:'忽略空格',punctuation_insensitive:'忽略标点',ngram_fuzzy:'模糊'};
  return m[t] || t || '';
}

// 双开页分段的引用页码摘要文案。原在 50-calibration.js，纯字符串，依赖 spreadCitationPair。
function spreadSummaryHtml(seg) {
  var firstPdf = seg.pdf_page_start != null ? seg.pdf_page_start + 1 : 1;
  var pair = spreadCitationPair(seg);
  if (!pair.mapped) {
    return '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>'
      + ' PDF 第 ' + firstPdf + ' 页 → 该分段未设引用页码，仅按双开切分';
  }
  return '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>'
    + ' PDF 第 ' + firstPdf + ' 页 → 左半页 <b>引文 ' + pair.left + ' 页</b>，右半页 <b>引文 ' + pair.right + ' 页</b>';
}

// 双开页分段设置面板的 HTML。原在 50-calibration.js，纯字符串拼接（onclick 均为字面量）。
function segmentSpreadPanelRow(seg, index) {
  if ((seg.layout_mode || 'single') !== 'spread') return '';
  var direction = seg.reading_direction === 'rtl' ? 'rtl' : 'ltr';
  var gp = spreadGutterPercent(seg);
  var pair = spreadCitationPair(seg);
  var leftFirst = direction !== 'rtl';
  var leftLabel = pair.mapped ? '引文 ' + pair.left + ' 页' : '不映射';
  var rightLabel = pair.mapped ? '引文 ' + pair.right + ' 页' : '不映射';
  var diagram = '<div class="spread-diagram" id="spread-diagram-' + index + '">'
    + '<div class="spread-half left" id="spread-half-left-' + index + '" style="width:' + gp + '%">'
    + '<span class="spread-badge" id="spread-badge-left-' + index + '">' + (leftFirst ? '1' : '2') + '</span>'
    + '<span class="spread-half-name">左半页</span>'
    + '<span class="spread-half-page" id="spread-page-left-' + index + '">' + leftLabel + '</span>'
    + '</div>'
    + '<div class="spread-half right" id="spread-half-right-' + index + '" style="width:' + (100 - gp) + '%">'
    + '<span class="spread-badge alt" id="spread-badge-right-' + index + '">' + (leftFirst ? '2' : '1') + '</span>'
    + '<span class="spread-half-name">右半页</span>'
    + '<span class="spread-half-page" id="spread-page-right-' + index + '">' + rightLabel + '</span>'
    + '</div>'
    + '<div class="spread-gutter-line" id="spread-gutter-line-' + index + '" style="left:' + gp + '%"></div>'
    + '</div>';
  var controls = '<div class="spread-controls">'
    + '<div class="spread-field"><span class="spread-field-label">阅读方向</span>'
    + '<div class="segment-direction-control" role="group" aria-label="双开页阅读方向">'
    + '<button class="segment-direction-btn' + (direction === 'ltr' ? ' is-active' : '') + '" type="button" aria-pressed="' + (direction === 'ltr' ? 'true' : 'false') + '" onclick="setSegmentReadingDirection(' + index + ',\'ltr\')">左→右</button>'
    + '<button class="segment-direction-btn' + (direction === 'rtl' ? ' is-active' : '') + '" type="button" aria-pressed="' + (direction === 'rtl' ? 'true' : 'false') + '" onclick="setSegmentReadingDirection(' + index + ',\'rtl\')">右→左</button>'
    + '</div></div>'
    + '<div class="spread-field"><div class="spread-field-row"><span class="spread-field-label">中缝位置</span><span class="spread-gutter-out" id="spread-gutter-out-' + index + '">' + gp + '%</span></div>'
    + '<input class="spread-gutter-range" type="range" min="30" max="70" step="1" value="' + gp + '" aria-label="中缝横向位置" oninput="updateSegmentGutter(' + index + ',this.value)">'
    + '</div></div>';
  var summary = '<div class="spread-summary" id="spread-summary-' + index + '">' + spreadSummaryHtml(seg) + '</div>';
  return '<tr class="segment-spread-row"><td colspan="7">'
    + '<div class="segment-spread-panel">'
    + '<div class="spread-panel-main">' + diagram + controls + '</div>'
    + summary
    + '</td></tr>';
}

// 详情上下文条目拼成纯文本。原在 20-search.js，纯数组处理。
function detailContextText(items) {
  if (!Array.isArray(items)) return '';
  return items.map(function(item) {
    return item && item.text != null ? String(item.text) : '';
  }).filter(Boolean).join('\n');
}

// HTML 转义。原在 20-search.js，纯函数，前移以消除 06-pure 对外部符号的依赖。
function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// 校准状态统计的图标 SVG。原在 50-calibration.js，纯映射。
function statusStatIcon(icon) {
  var paths = {
    document:'<path d="M6 3h9l3 3v15H6z"/><path d="M15 3v4h4"/>',
    book:'<path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H11v16H6.5A2.5 2.5 0 0 0 4 21.5z"/><path d="M20 5.5A2.5 2.5 0 0 0 17.5 3H13v16h4.5a2.5 2.5 0 0 1 2.5 2.5z"/>',
    check:'<circle cx="12" cy="12" r="9"/><path d="m8 12 2.6 2.6L16.5 9"/>',
    clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    notice:'<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5"/><path d="M12 16.5h.01"/>',
    danger:'<path d="M12 3 2.8 20h18.4L12 3Z"/><path d="M12 9v5"/><path d="M12 17.5h.01"/>'
  };
  return '<span class="status-stat__icon" aria-hidden="true"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' + (paths[icon] || paths.notice) + '</svg></span>';
}

// 校准状态芯片的图标 SVG。原在 50-calibration.js，纯映射。
function statusChipIcon(group) {
  var icons = {
    calibrated:'<circle cx="12" cy="12" r="9"/><path d="m8 12 2.6 2.6L16.5 9"/>',
    pending:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    review:'<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5"/><path d="M12 16.5h.01"/>',
    failed:'<path d="M12 3 2.8 20h18.4L12 3Z"/><path d="M12 9v5"/><path d="M12 17.5h.01"/>',
    mapping:'<path d="M20 12a8 8 0 1 1-2.34-5.66"/><path d="M20 4v5h-5"/>'
  };
  var spinning = group === 'mapping' ? ' is-spinning' : '';
  return '<span class="status-chip__icon' + spinning + '" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' + (icons[group] || icons.pending) + '</svg></span>';
}

// 校准状态统计按钮。原在 50-calibration.js，纯字符串，依赖 statusStatIcon。
function statusStatButton(status, label, value, variant, icon, activeFilter, handlerName) {
  return '<button type="button" data-status="' + status + '" class="status-stat status-stat--' + variant + (activeFilter === status ? ' active' : '') + '" onclick="' + handlerName + '(\'' + status + '\')">'
    + statusStatIcon(icon)
    + '<span class="status-stat__label">' + label + '</span>'
    + '<span class="status-stat__count">' + value + '</span></button>';
}

// 书目补全来源菜单。原在 40-bibliography.js，纯字符串。
function bibSourceMenuHTML(sid, active) {
  function item(source, label, note) {
    return '<button class="bib-menu-item' + (active === source ? ' active' : '') + '" type="button" role="menuitem" onclick="bibSetSource(event,\'' + sid + '\',\'' + source + '\')">' + label
      + (note ? '<span class="bib-menu-note">' + note + '</span>' : '') + '</button>';
  }
  return item('auto', '智能补全', '推荐')
    + item('cnki', '知网补全', '中文')
    + item('crossref', 'Crossref 补全', '外文')
    + '<div class="bib-menu-sep"></div>'
    + '<button class="bib-menu-item" type="button" role="menuitem" onclick="bibMenuAction(event,\'paste\',\'' + sid + '\')">粘贴引文</button>'
    + '<button class="bib-menu-item" type="button" role="menuitem" onclick="bibMenuAction(event,\'opencnki\',\'' + sid + '\')">打开知网检索</button>';
}

// 抽屉信息行。原在 40-bibliography.js，仅依赖 esc。
function drawerInfoRow(label, value) {
  return '<div class="drawer-info-row"><span class="drawer-info-label">' + esc(label) + '</span><span class="drawer-info-value">' + esc(String(value || '—')) + '</span></div>';
}

// 书目缺失徽标。原在 40-bibliography.js，依赖 bibliographicMissingText + esc。
function bibliographicMissingBadge(meta) {
  var text = bibliographicMissingText(meta);
  if (!text) return '';
  return '<span class="bibliographic-missing" title="ISBN、ISSN 与 DOI 不计入引文必需字段"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5"/><path d="M12 16.5h.01"/></svg><span>' + esc(text) + '</span></span>';
}

// 依据文本长度计算 toast 停留时长（纯函数）
function toastDuration(text) {
  // 原来固定 1800ms，像「已停止等待。移除是一个整体事务…」这种长提示根本读不完。
  return Math.min(6500, Math.max(2400, 1100 + text.length * 110));
}

// 判断书库条目是否可勾选删除（纯函数）
function isLibraryDeleteSelectable(source) {
  return !!source && (source.source_type === 'pdf' || source.source_type === 'word');
}

// 自动标定失败原因转中文提示（纯函数）
function autoFailureReasons(reasons) {
  var labels = {no_page_labels:'没有 PDF Page Labels',no_bookmarks:'没有数字书签',no_mineru_candidates:'现有 MinerU 结果没有可靠页码候选',no_edge_candidates:'页边区域未发现页码候选',sequence_not_found:'未找到稳定递增页码序列',spread_sequence_not_found:'识别到双开布局，但未找到可靠的双页页码序列',source_missing:'原始 PDF 文件不存在'};
  return reasons.map(function(reason) { return '• ' + (labels[reason] || reason); }).join('<br>');
}

// 页面详情行渲染：拼接 label/value 的转义 HTML（纯函数）
function pdRow(label, value) {
  return '<div class="page-detail-row"><span class="page-detail-label">' + esc(label) + '</span><span>' + esc(String(value)) + '</span></div>';
}

// 纯函数：由滚动容器与指针事件算出框选锚点坐标（读参数，无副作用）
function dragSelectionAnchor(scroller, event) {
  var viewport = scroller.getBoundingClientRect();
  return {
    anchorX: event.clientX - viewport.left + scroller.scrollLeft,
    anchorY: event.clientY - viewport.top + scroller.scrollTop
  };
}

// 纯函数：由框选状态算出当前选框的边界矩形（读参数，无副作用）
function dragSelectionBox(state) {
  var scroller = state.scroller;
  var viewport = scroller.getBoundingClientRect();
  var pointerX = state.pointerX - viewport.left + scroller.scrollLeft;
  var pointerY = state.pointerY - viewport.top + scroller.scrollTop;
  return {
    viewport: viewport,
    left: Math.min(state.anchorX, pointerX),
    right: Math.max(state.anchorX, pointerX),
    top: Math.min(state.anchorY, pointerY),
    bottom: Math.max(state.anchorY, pointerY)
  };
}

// 纯函数：判断元素矩形是否落入框选区域（读参数，无副作用）
function dragSelectionHits(element, box, scroller) {
  var rect = element.getBoundingClientRect();
  var left = rect.left - box.viewport.left + scroller.scrollLeft;
  var top = rect.top - box.viewport.top + scroller.scrollTop;
  return left + rect.width >= box.left && left <= box.right
    && top + rect.height >= box.top && top <= box.bottom;
}

// 纯函数：给定页码样式与索引，渲染分段页码样式选择控件的 HTML（无副作用）
function segmentNumberStyleControl(style, index) {
  var values = ['arabic','roman_lower','roman_upper','none'];
  return '<div class="app-select segment-style-select" id="segment-style-select-' + index + '">'
    + '<button class="app-select-trigger" type="button" aria-haspopup="listbox" aria-expanded="false" onclick="toggleAppSelect(event,\'segment-style-select-' + index + '\')"><span class="app-select-value">' + segmentNumberStyleLabel(style) + '</span><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 8 4 4 4-4"/></svg></button>'
    + '<div class="app-select-menu" role="listbox">' + values.map(function(value) {
      return '<button class="app-select-option' + (style === value ? ' is-selected' : '') + '" type="button" data-value="' + value + '" onclick="setSegmentNumberStyle(event,' + index + ',\'' + value + '\')">' + segmentNumberStyleLabel(value) + '</button>';
    }).join('') + '</div></div>';
}

// 纯函数：给定版式与索引，渲染分段版式选择控件的 HTML（无副作用）
function segmentLayoutControl(layout, index) {
  var values = ['single','spread'];
  return '<div class="app-select segment-layout-select" id="segment-layout-select-' + index + '">'
    + '<button class="app-select-trigger" type="button" aria-haspopup="listbox" aria-expanded="false" onclick="toggleAppSelect(event,\'segment-layout-select-' + index + '\')"><span class="app-select-value">' + segmentLayoutLabel(layout) + '</span><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 8 4 4 4-4"/></svg></button>'
    + '<div class="app-select-menu" role="listbox">' + values.map(function(value) {
      return '<button class="app-select-option' + (layout === value ? ' is-selected' : '') + '" type="button" data-value="' + value + '" onclick="setSegmentLayout(event,' + index + ',\'' + value + '\')">' + segmentLayoutLabel(value) + '</button>';
    }).join('') + '</div></div>';
}

// 纯函数：给定扫描条目与勾选状态，渲染扫描列表行的 HTML（无副作用）
function scanEntryRow(entry, index, checkable, checked) {
  var typeCls = entry.file_type === 'pdf' ? 'pdf' : 'word';
  var note = '';
  if (entry.status === 'processing') note = '已提交，正在导入…';
  else if (entry.status === 'name_conflict') note = '与已导入文献同名但大小不同，请重命名后再导入';
  else if (entry.needs_ocr === true) note = '需 OCR';
  else if (entry.needs_ocr === null && entry.file_type === 'pdf' && entry.status === 'new') note = '未预检测，导入时自动判断；非原生文本将提交 MinerU';
  return '<div class="scan-row' + (entry.status === 'imported' ? ' is-imported' : '') + '">'
    + (checkable
      ? '<input type="checkbox" class="scan-check" id="scan-check-' + index + '" data-index="' + index + '"' + (checked ? ' checked' : '') + ' onchange="handleScanCheckChange(this)">'
      : '<span class="scan-check-placeholder"></span>')
    + '<span class="type-badge ' + typeCls + '">' + (entry.file_type === 'pdf' ? 'PDF' : 'DOCX') + '</span>'
    + '<label class="scan-row-name"' + (checkable ? ' for="scan-check-' + index + '"' : '') + ' title="' + esc(entry.path) + '">' + esc(entry.name) + '</label>'
    + '<span class="scan-row-size">' + formatFileSize(entry.size_bytes) + '</span>'
    + (note ? '<span class="scan-row-note">' + esc(note) + '</span>' : '')
    + '</div>';
}

// 纯常量：详情页上下文预览的最大可见字符数（原在 00-state.js，无副作用）
const DETAIL_CONTEXT_PREVIEW_CHARS = 180;

// 纯函数：按可见字符数截取上下文预览文本，超长两侧加省略号（读常量，无副作用）
function detailContextPreview(text, side) {
  const characters = Array.from(String(text || ''));
  if (characters.length <= DETAIL_CONTEXT_PREVIEW_CHARS) return characters.join('');
  if (side === 'before') {
    return '…' + characters.slice(-DETAIL_CONTEXT_PREVIEW_CHARS).join('');
  }
  return characters.slice(0, DETAIL_CONTEXT_PREVIEW_CHARS).join('') + '…';
}
