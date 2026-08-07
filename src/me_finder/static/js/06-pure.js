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
