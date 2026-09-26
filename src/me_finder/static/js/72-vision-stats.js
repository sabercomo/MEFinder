/* 解析统计与备份恢复。 */
(function (global) {  // module: 72-vision-stats.js
  function statsNode(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = String(text);
    return node;
  }

  function statsCount(parent, value, suffix) {
    parent.appendChild(statsNode('b', null, Number(value || 0).toLocaleString()));
    parent.appendChild(document.createTextNode(' ' + suffix));
  }
  function mineruPageRangesLabel(ranges) {
    if (!Array.isArray(ranges) || !ranges.length) return '—';
    return ranges.map(function(range) {
      if (!Array.isArray(range) || range.length < 2) return '';
      return Number(range[0]).toLocaleString() + '–' + Number(range[1]).toLocaleString();
    }).filter(Boolean).join('、');
  }

  function parserDateLabel(value) {
    if (!value) return '—';
    var date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleDateString('zh-CN', {year:'numeric', month:'2-digit', day:'2-digit'});
  }

  function renderParserProviderBooks(provider) {
    var fragment = document.createDocumentFragment();
    fragment.appendChild(statsNode('div', 'parser-detail-label', '解析文献'));
    var scroll = statsNode('div', 'parser-book-table-scroll');
    var table = statsNode('table', 'parser-book-table');
    var head = document.createElement('thead');
    var headerRow = document.createElement('tr');
    ['文献', '模型', '完成日期', '页数'].forEach(function(label) { headerRow.appendChild(statsNode('th', null, label)); });
    head.appendChild(headerRow);
    table.appendChild(head);
    var body = document.createElement('tbody');
    (Array.isArray(provider.books) ? provider.books : []).forEach(function(book) {
      var row = document.createElement('tr');
      var titleCell = document.createElement('td');
      titleCell.dataset.label = '文献';
      var title = statsNode('span', 'parser-book-title');
      title.appendChild(statsNode('strong', null, book.title || book.file_name || '未命名文献'));
      title.appendChild(statsNode('small', null, book.file_name || book.source_file_id || ''));
      titleCell.appendChild(title);
      row.appendChild(titleCell);
      [['模型', book.model || '—'], ['完成日期', parserDateLabel(book.completed_at)]].forEach(function(value) {
        var cell = statsNode('td', null, value[1]);
        cell.dataset.label = value[0];
        row.appendChild(cell);
      });
      var pages = document.createElement('td');
      pages.dataset.label = '页数';
      statsCount(pages, book.parsed_page_count, '页');
      row.appendChild(pages);
      body.appendChild(row);
    });
    table.appendChild(body);
    scroll.appendChild(table);
    fragment.appendChild(scroll);
    return fragment;
  }

  function renderMineruCredentialAttribution(credentials) {
    var fragment = document.createDocumentFragment();
    if (!Array.isArray(credentials) || !credentials.length) {
      fragment.appendChild(statsNode('div', 'parser-credential-empty', '这些 MinerU 文献没有可匹配的本地账号归属记录'));
      return fragment;
    }
    var heading = statsNode('div', 'parser-detail-label mineru-attribution-label', 'MinerU 账号归属 ');
    heading.appendChild(statsNode('small', null, '本地记录，不是官网用量或计费数据'));
    fragment.appendChild(heading);
    var list = statsNode('div', 'parser-credential-list');
    credentials.forEach(function(item) {
      var details = statsNode('details', 'parser-credential-account');
      var summary = document.createElement('summary');
      var identity = document.createElement('span');
      identity.appendChild(statsNode('strong', null, item.display_name || item.account_id));
      identity.appendChild(statsNode('small', null, Number(item.parsed_book_count || 0).toLocaleString() + ' 本文献'));
      summary.appendChild(identity);
      // 账号行的页数整体加粗（「N 页」同在一个 <b> 内），与书表的「<b>N</b> 页」不同。
      summary.appendChild(statsNode('b', null, Number(item.parsed_page_count || 0).toLocaleString() + ' 页'));
      var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      [['viewBox', '0 0 20 20'], ['fill', 'none'], ['stroke', 'currentColor'], ['stroke-width', '1.8'],
        ['stroke-linecap', 'round'], ['aria-hidden', 'true']].forEach(function(attribute) { svg.setAttribute(attribute[0], attribute[1]); });
      var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', 'm6 8 4 4 4-4');
      svg.appendChild(path);
      summary.appendChild(svg);
      details.appendChild(summary);
      var books = statsNode('div', 'parser-credential-books');
      (Array.isArray(item.books) ? item.books : []).forEach(function(book) {
        var row = statsNode('div', 'parser-credential-book');
        var name = document.createElement('span');
        name.appendChild(statsNode('strong', null, book.source_file_name || book.document_id || '未命名文献'));
        name.appendChild(statsNode('small', null, '原书页 ' + mineruPageRangesLabel(book.page_ranges)));
        row.appendChild(name);
        row.appendChild(statsNode('b', null, Number(book.parsed_page_count || 0).toLocaleString() + ' 页'));
        books.appendChild(row);
      });
      details.appendChild(books);
      list.appendChild(details);
    });
    fragment.appendChild(list);
    return fragment;
  }

  // 解析服务在构成条上的档位：按数据外流程度排，色跟服务走而不是跟 local/api
  // 二分走——新增一个本地服务不会把已有颜色重排。未知解析器不占档位，走中性色。
  var PARSER_CHART_STEPS = {
    'pymupdf': 1,
    'simple-pdf-text': 1,
    'mineru-local': 2,
    'ndlocr-lite': 3,
    'ndlkotenocr-lite': 3,
    'mineru-cloud': 4,
    'openai-compatible': 5,
    'qwen-ocr': 5
  };

  function parserChartStep(provider) {
    var mapped = PARSER_CHART_STEPS[provider.provider_id];
    if (mapped) return mapped;
    if (!provider.provider_id || provider.provider_id === 'unknown-parser') return 6;
    // 没登记过的解析器按本地/在线落到同类的末档，不新造颜色。
    return provider.provider_kind === 'local' ? 3 : 5;
  }

  function parserChartStepClass(provider) {
    var step = parserChartStep(provider);
    return step > 5 ? 'step-other' : 'step-' + step;
  }

  function renderParserStatistics() {
    var total = parserStore.parserStatistics.total || {};
    document.getElementById('parser-stat-books').textContent = Number(total.parsed_book_count || 0).toLocaleString();
    document.getElementById('parser-stat-pages').textContent = Number(total.parsed_page_count || 0).toLocaleString();
    document.getElementById('parser-stat-providers').textContent = Number(total.provider_count || 0).toLocaleString();
    var list = document.getElementById('parser-provider-list');
    var overview = document.getElementById('parser-share-overview');
    var providers = Array.isArray(parserStore.parserStatistics.providers) ? parserStore.parserStatistics.providers : [];
    if (!providers.length) {
      overview.replaceChildren();
      var empty = statsNode('div', 'parser-statistics-empty');
      empty.appendChild(statsNode('strong', null, '还没有解析统计'));
      empty.appendChild(statsNode('small', null, '导入并完成一本 PDF 的页级解析后，这里会按解析服务显示文献和页数'));
      list.replaceChildren(empty);
      return;
    }
    var totalPages = providers.reduce(function(sum, item) {
      return sum + Number(item.parsed_page_count || 0);
    }, 0);
    // 段序必须等于色阶序，相邻色对才是校验过的那几对；同档位的服务按页数降序。
    var orderedProviders = providers.slice().sort(function(a, b) {
      var stepDelta = parserChartStep(a) - parserChartStep(b);
      if (stepDelta) return stepDelta;
      return Number(b.parsed_page_count || 0) - Number(a.parsed_page_count || 0);
    });
    // 占比按页数算，与总览的「解析页」同口径；总页数为 0 时不编造比例。
    var shareOf = function(provider) {
      var pageCount = Number(provider.parsed_page_count || 0);
      var share = totalPages > 0 ? pageCount / totalPages : 0;
      var percent = totalPages > 0 ? Math.round(share * 100) : null;
      return {
        pageCount: pageCount,
        share: share,
        label: percent === null ? '—' : (percent === 0 && pageCount > 0 ? '<1%' : percent + '%')
      };
    };
    // 占比是部分-整体关系：顶部一根 100% 构成条 + 图例说明各段，
    // 行内不再重复画条，避免被误读成加载进度。
    if (totalPages > 0) {
      var breakdownLabel = orderedProviders.map(function(provider) {
        return (provider.provider_name || provider.provider_id) + '占' + shareOf(provider).label;
      }).join('，');
      var stack = statsNode('div', 'parser-share-stack');
      var bar = statsNode('div', 'parser-share-bar');
      bar.setAttribute('role', 'img');
      bar.setAttribute('aria-label', '按解析服务构成：' + breakdownLabel);
      orderedProviders.forEach(function(provider) {
        var segment = statsNode('span', 'parser-share-seg ' + parserChartStepClass(provider));
        segment.style.width = (shareOf(provider).share * 100).toFixed(1) + '%';
        bar.appendChild(segment);
      });
      stack.appendChild(bar);
      stack.appendChild(statsNode('div', 'parser-share-cap', '全库已解析 ' + totalPages.toLocaleString() + ' 页，按解析服务构成'));
      var legend = statsNode('div', 'parser-share-legend');
      orderedProviders.forEach(function(provider) {
        var row = document.createElement('div');
        var dot = statsNode('i', 'parser-share-dot ' + parserChartStepClass(provider));
        dot.setAttribute('aria-hidden', 'true');
        row.appendChild(dot);
        row.appendChild(statsNode('span', 'parser-share-name', provider.provider_name || provider.provider_id));
        row.appendChild(statsNode('small', null, provider.provider_kind === 'local' ? '本地' : 'API'));
        row.appendChild(statsNode('b', null, shareOf(provider).label));
        row.appendChild(statsNode('small', 'parser-share-pages', shareOf(provider).pageCount.toLocaleString() + ' 页'));
        legend.appendChild(row);
      });
      overview.replaceChildren(stack, legend);
    } else {
      overview.replaceChildren();
    }
    list.replaceChildren();
    orderedProviders.forEach(function(provider) {
      var isMineru = provider.provider_id === 'mineru-cloud' || provider.provider_id === 'mineru-local';
      var isCloudMineru = provider.provider_id === 'mineru-cloud';
      var kind = provider.provider_kind === 'local' ? '本地' : 'API';
      var group = statsNode('details', 'parser-provider-group');
      group.open = true;
      var summary = document.createElement('summary');
      var identity = statsNode('span', 'parser-provider-identity');
      var mark = statsNode('span', 'parser-provider-mark ' + (isMineru ? 'mineru' : ''));
      mark.setAttribute('aria-hidden', 'true');
      if (isMineru) mark.appendChild(statsNode('span', 'mineru-brand-glyph'));
      else mark.textContent = String(provider.provider_name || '?').charAt(0).toUpperCase();
      identity.appendChild(mark);
      var name = document.createElement('span');
      name.appendChild(statsNode('strong', null, provider.provider_name || provider.provider_id));
      name.appendChild(statsNode('small', null, kind));
      identity.appendChild(name);
      summary.appendChild(identity);
      var booksCount = statsNode('span', 'parser-provider-number');
      statsCount(booksCount, provider.parsed_book_count, '本');
      summary.appendChild(booksCount);
      var pagesCount = statsNode('span', 'parser-provider-number');
      statsCount(pagesCount, provider.parsed_page_count, '页');
      summary.appendChild(pagesCount);
      var chevron = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      [['class', 'parser-provider-chevron'], ['viewBox', '0 0 20 20'], ['fill', 'none'], ['stroke', 'currentColor'],
        ['stroke-width', '1.8'], ['stroke-linecap', 'round'], ['aria-hidden', 'true']].forEach(function(attribute) { chevron.setAttribute(attribute[0], attribute[1]); });
      var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', 'm6 8 4 4 4-4');
      chevron.appendChild(path);
      summary.appendChild(chevron);
      group.appendChild(summary);
      var detail = statsNode('div', 'parser-provider-detail');
      detail.appendChild(renderParserProviderBooks(provider));
      if (isCloudMineru) detail.appendChild(renderMineruCredentialAttribution(provider.credentials || []));
      group.appendChild(detail);
      list.appendChild(group);
    });
  }

  async function loadParserStatistics() {
    var status = document.getElementById('parser-statistics-status');
    if (status) {
      status.className = 'settings-status';
      status.textContent = '刷新中…';
    }
    try {
      var response = await MEFinderApi.fetch('/api/parser-statistics');
      var data = await response.json();
      if (!response.ok || data.error) throw new Error(data.error || '读取失败');
      parserStore.parserStatistics = data || {total:{parsed_book_count:0, parsed_page_count:0, provider_count:0}, providers:[]};
      renderParserStatistics();
      global.MEFinder.visionProviders.render();
      if (status) {
        status.className = 'settings-status ready';
        status.textContent = '已刷新';
      }
    } catch (error) {
      if (status) {
        status.className = 'settings-status warning';
        status.textContent = '读取失败';
      }
      showToast('读取本地解析统计失败：' + (error && error.message ? error.message : '未知错误'), 'danger');
    }
  }

  function loadMineruStatistics() {
    return loadParserStatistics();
  }

  function renderLastBackupExport(record) {
    var node = document.getElementById('backup-last-export');
    if (!node) return;
    if (!record || !record.exported_at) {
      node.textContent = '还没有导出过备份';
      return;
    }
    var when = new Date(Number(record.exported_at) * 1000);
    var stamp = when.getFullYear() + '-' + String(when.getMonth() + 1).padStart(2, '0')
      + '-' + String(when.getDate()).padStart(2, '0') + ' '
      + String(when.getHours()).padStart(2, '0') + ':' + String(when.getMinutes()).padStart(2, '0');
    var name = record.file_name || record.path || '';
    node.textContent = '上次导出：' + stamp + (name ? ' · ' + name : '');
    node.title = record.path || '';
  }

  async function exportBackup() {
    var hint = document.getElementById('backup-export-hint');
    try {
      var outputDirectory = await chooseDesktopExportDirectory();
      if (outputDirectory === null) return;
      if (hint) hint.textContent = '正在导出…';
      var payload = {};
      if (outputDirectory) payload.output_dir = outputDirectory;
      var resp = await MEFinderApi.fetch('/api/backup/export', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
      });
      var data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || '导出失败');
      if (hint) hint.textContent = '已导出到：' + data.path;
      renderLastBackupExport({
        path: data.path,
        file_name: String(data.path || '').split(/[\\/]/).pop(),
        exported_at: data.exported_at,
        size_bytes: data.size_bytes
      });
      showToast('备份已导出（' + formatFileSize(data.size_bytes) + '）');
    } catch (e) {
      if (hint) hint.textContent = '仅备份页码、书目和偏好，不含 PDF';
      showToast('导出备份失败：' + e.message);
    }
  }

  async function importBackup() {
    var button = document.getElementById('backup-import-choose');
    if (button && button.disabled) return;
    if (button) { button.disabled = true; button.textContent = '正在选择…'; }
    try {
      var chooseResp = await MEFinderApi.fetch('/api/backup/import/choose', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
      var chosen = await chooseResp.json();
      if (!chooseResp.ok || chosen.error) throw new Error(chosen.error || '选择备份失败');
      if (chosen.cancelled) return;
      if (!await showAppConfirm(
        '将从「' + (chosen.name || '所选备份') + '」恢复，并覆盖当前的页码映射与书目信息',
        {title:'导入并覆盖当前数据？', confirmText:'确认导入', tone:'danger'}
      )) return;
      if (button) button.textContent = '正在导入…';
      var resp = await MEFinderApi.fetch('/api/backup/import', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({path: chosen.path})});
      var data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || '导入失败');
      showToast('已恢复备份，正在重建索引…');
      pollBackupRestore(data.job_id);
    } catch (e) {
      showToast('导入备份失败：' + e.message);
    } finally {
      if (button) { button.disabled = false; button.textContent = '选择备份并恢复'; }
    }
  }

  function pollBackupRestore(jobId) {
    MEFinderApi.fetch('/api/import-status?job_id=' + encodeURIComponent(jobId))
      .then(function(resp) { return resp.json(); })
      .then(function(data) {
        if (data.status === 'completed') {
          showToast(data.message || '备份已恢复');
          invalidateLibraryCatalog();
          // 备份恢复整体替换索引库，作品与对齐数据一并失效。
          if (global.MEFinder && global.MEFinder.works) global.MEFinder.works.invalidate();
          loadMeta();
          return;
        }
        if (data.status === 'failed' || data.error) {
          showToast('恢复失败：' + (data.message || data.error || '未知错误'));
          return;
        }
        setTimeout(function() { pollBackupRestore(jobId); }, 2000);
      })
      .catch(function() { setTimeout(function() { pollBackupRestore(jobId); }, 4000); });
  }

  global.MEFinder = global.MEFinder || {};
  global.MEFinder.parserStats = {renderLastBackupExport: renderLastBackupExport};
  global.loadParserStatistics = loadParserStatistics;
  global.exportBackup = exportBackup;
  global.importBackup = importBackup;
}(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this)));
