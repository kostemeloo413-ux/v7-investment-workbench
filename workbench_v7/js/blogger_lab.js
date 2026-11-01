/**
 * V7 博主量化实验室前端核心模块 (Blogger Lab Domain Module)
 * 封装在 window.BloggerLabDomain 闭包与独立命名空间内
 * 遵循 A股配色铁律: 红涨(#dc2626) 绿跌(#166534)
 * 规范生命周期钩子: onEnter() / onLeave() / switchStep()
 * 兼容标准 ES6/ES8 语法并通过 esprima AST 语法校验
 */
(function(window) {
  'use strict';

  // 内部状态
  var state = {
    initialized: false,
    isPageActive: false,
    pageGeneration: 0,
    personas: [],
    currentPersonaId: '',
    currentPersona: null,
    currentBatch: null,
    currentJobId: null,
    pollTimer: null,
    currentDraft: null,
    activeStrategy: null,
    latestScanRun: null,
    lastPositions: [],
    lastFills: [],
    activeStep: 1,
    personaSwitchToken: 0,
    lastRequestKeys: {
      step1: '',
      step2: '',
      step3: ''
    }
  };

  var STORAGE_KEY_STEP = 'v7_lab_active_step';
  var STORAGE_KEY_PERSONA = 'v7_lab_active_persona';

  // DOM 容器解析
  function getRoot() {
    return document.getElementById('page-blogger-lab') ||
           document.querySelector('.blogger-lab-root') ||
           document;
  }

  function $(selector) {
    var root = getRoot();
    return root.querySelector(selector) || document.querySelector(selector);
  }

  function $$(selector) {
    var root = getRoot();
    return root.querySelectorAll(selector);
  }

  // 提示信息提示框
  function showNotice(msg, type) {
    if (!type) type = 'info';
    var el = $('#globalNotice');
    if (!el) return;
    el.className = 'notice-box notice-' + type;
    el.textContent = msg;
    el.style.display = 'flex';
    if (type !== 'error') {
      setTimeout(function() {
        if (el.textContent === msg) el.style.display = 'none';
      }, 5000);
    }
  }

  // 步骤切换 (保持顶层路由 #page-blogger-lab，绝不使用 #step2 覆盖主路由)
  function switchStep(stepIndex) {
    state.activeStep = stepIndex;
    try {
      localStorage.setItem(STORAGE_KEY_STEP, String(stepIndex));
    } catch (e) {}

    $$('.nav-tab').forEach(function(t, idx) {
      t.classList.toggle('active', idx === stepIndex - 1);
    });
    $$('.step-panel').forEach(function(p, idx) {
      p.classList.toggle('active', idx === stepIndex - 1);
    });

    if (stepIndex === 3) loadDraftAndStrategies();
    if (stepIndex === 4) refreshScanView();
    if (stepIndex === 5) refreshAccount();
  }

  // ==========================================
  // 步骤 1: 分身管理与资料批次导入
  // ==========================================
  async function loadPersonas(gen) {
    if (!state.isPageActive) return;
    if (gen === undefined) gen = state.pageGeneration;
    var token = ++state.personaSwitchToken;
    try {
      var res = await fetch('/api/blogger-lab/personas');
      if (!state.isPageActive || gen !== state.pageGeneration || token !== state.personaSwitchToken) return;

      if (!res.ok) {
        if (res.status === 503) {
          showNotice('博主实验室服务未就绪或未开启实验接入 (503 生产安全隔离)', 'warning');
        } else {
          showNotice('加载分身列表失败 (HTTP ' + res.status + ')', 'error');
        }
        renderPersonaSelect();
        return;
      }

      var json = await res.json();
      if (!state.isPageActive || gen !== state.pageGeneration || token !== state.personaSwitchToken) return;

      if (json.status === 'ok') {
        state.personas = json.data || [];
        renderPersonaSelect();
      } else {
        var errs = json.errors || [];
        showNotice('获取分身失败: ' + errs.map(function(e) { return e.message; }).join('; '), 'error');
      }
    } catch (e) {
      if (!state.isPageActive || gen !== state.pageGeneration || token !== state.personaSwitchToken) return;
      showNotice('网络或服务异常: ' + e.message, 'error');
      renderPersonaSelect();
    }
  }

  function renderPersonaSelect() {
    var sel = $('#personaSelect');
    if (!sel) return;
    sel.innerHTML = '';

    if (!state.personas || state.personas.length === 0) {
      sel.innerHTML = '<option value="">(暂无可用分身，请注册)</option>';
      state.currentPersonaId = '';
      state.currentPersona = null;
      renderActiveStrategy(null);
      return;
    }

    state.personas.forEach(function(p) {
      var opt = document.createElement('option');
      opt.value = p.id;
      opt.textContent = p.name + ' (' + p.id + ')';
      sel.appendChild(opt);
    });

    // 优先恢复保存的分身，否则选中首个
    var savedPersonaId = null;
    try {
      savedPersonaId = localStorage.getItem(STORAGE_KEY_PERSONA);
    } catch (e) {}

    if (savedPersonaId && state.personas.some(function(p) { return p.id === savedPersonaId; })) {
      state.currentPersonaId = savedPersonaId;
    } else {
      state.currentPersonaId = state.personas[0].id;
    }

    sel.value = state.currentPersonaId;
    onPersonaChanged(false);
  }

  async function onPersonaChanged(userInitiated) {
    if (!state.isPageActive) return;
    var gen = state.pageGeneration;
    var sel = $('#personaSelect');
    if (sel && sel.value) {
      state.currentPersonaId = sel.value;
    }
    state.currentPersona = (state.personas || []).find(function(p) { return p.id === state.currentPersonaId; }) || null;

    if (state.currentPersonaId) {
      try {
        localStorage.setItem(STORAGE_KEY_PERSONA, state.currentPersonaId);
      } catch (e) {}
    }

    // 停止旧分身的轮询器，增加 token 丢弃旧异步响应
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
    var token = ++state.personaSwitchToken;

    // 清空旧分身候选选择器与回放结果
    var candSel = $('#replayCandidateSelect');
    if (candSel) {
      candSel.innerHTML = '<option value="">(分身切换中...)</option>';
    }

    await loadPersonaOverview(token, gen);
  }

  async function loadPersonaOverview(token, gen) {
    if (!state.isPageActive || !state.currentPersonaId) return;
    if (token === undefined) token = ++state.personaSwitchToken;
    if (gen === undefined) gen = state.pageGeneration;

    try {
      var res = await fetch('/api/blogger-lab/personas/' + state.currentPersonaId + '/overview');
      if (!state.isPageActive || gen !== state.pageGeneration || token !== state.personaSwitchToken) return;

      if (!res.ok) {
        if (res.status === 503) {
          showNotice('实验运行时不可用 (503)', 'warning');
        }
        return;
      }

      var json = await res.json();
      if (!state.isPageActive || gen !== state.pageGeneration || token !== state.personaSwitchToken) return;

      if (json.status === 'ok') {
        var ov = json.data;

        // 1. 加载批次列表
        await loadBatchesForExtraction(token, gen);
        if (!state.isPageActive || gen !== state.pageGeneration || token !== state.personaSwitchToken) return;

        // 2. 检查与恢复后台任务
        if (ov.latest_job) {
          state.currentJobId = ov.latest_job.id;
          var monitorBox = $('#jobMonitorBox');
          if (monitorBox) monitorBox.style.display = 'block';
          var lblJobId = $('#lblJobId');
          if (lblJobId) lblJobId.textContent = state.currentJobId;
          renderJobProgress(ov.latest_job);
          if (ov.latest_job.status === 'RUNNING') {
            startPollingJob();
          }
        } else {
          var monitorBox2 = $('#jobMonitorBox');
          if (monitorBox2) monitorBox2.style.display = 'none';
        }

        // 恢复知识条目预览
        if (ov.latest_knowledge && ov.latest_knowledge.items && ov.latest_knowledge.items.length > 0) {
          renderKnowledgeItems(ov.latest_knowledge.items);
        } else {
          renderKnowledgeItems([]);
        }

        // 3. 恢复草案与激活策略
        state.activeStrategy = ov.active_strategy || null;
        renderActiveStrategy(state.activeStrategy);
        state.currentDraft = ov.latest_draft || null;
        renderDraft(state.currentDraft);

        // 4. 恢复扫描记录与候选池
        if (ov.latest_scan) {
          state.latestScanRun = ov.latest_scan;
          renderScanResults(ov.latest_scan);
        } else {
          state.latestScanRun = null;
          var scanSummaryBox = $('#scanSummaryBox');
          if (scanSummaryBox) scanSummaryBox.style.display = 'none';
          var lblCandCount = $('#lblCandidatesCount');
          if (lblCandCount) lblCandCount.textContent = '0 只候选';
          var tbody = $('#tblCandidates tbody');
          if (tbody) tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: #94a3b8;">尚未执行扫描</td></tr>';
          var candSel = $('#replayCandidateSelect');
          if (candSel) candSel.innerHTML = '<option value="">(请先在步骤4执行机会扫描)</option>';
        }

        // 5. 恢复模拟研究账户
        if (ov.account) {
          renderAccount(ov.account);
        }
      }
    } catch (e) {
      if (token !== state.personaSwitchToken) return;
      console.warn('加载分身概况异常:', e);
    }
  }

  async function createPersona() {
    var nameEl = $('#newPersonaName');
    var idEl = $('#newPersonaId');
    var descEl = $('#newPersonaDesc');
    var name = nameEl ? nameEl.value.trim() : '';
    var pId = idEl ? idEl.value.trim() : '';
    var desc = descEl ? descEl.value.trim() : '';

    if (!name) {
      alert('请输入分身名称');
      return;
    }
    var btn = $('#btnCreatePersona');
    if (btn) btn.disabled = true;

    try {
      var res = await fetch('/api/blogger-lab/personas', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name, persona_id: pId || null, description: desc })
      });
      var json = await res.json();
      if (json.status === 'ok') {
        showNotice("分身 '" + json.data.name + "' 注册成功", 'info');
        if (nameEl) nameEl.value = '';
        if (idEl) idEl.value = '';
        if (descEl) descEl.value = '';
        state.currentPersonaId = json.data.id;
        try {
          localStorage.setItem(STORAGE_KEY_PERSONA, state.currentPersonaId);
        } catch (e) {}
        await loadPersonas();
      } else {
        var errs = json.errors || [];
        alert('注册失败: ' + errs.map(function(e) { return e.message; }).join('; '));
      }
    } catch (e) {
      alert('网络请求异常: ' + e.message);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function uploadBatch() {
    if (!state.currentPersonaId) {
      alert('请先选择或注册分身');
      return;
    }
    var fileInput = $('#corpusFiles');
    if (!fileInput || !fileInput.files || fileInput.files.length === 0) {
      alert('请至少选择一个 TXT / MD / DOCX 文稿文件');
      return;
    }
    var btn = $('#btnUploadBatch');
    if (btn) {
      btn.disabled = true;
      btn.textContent = '解析导入中...';
    }
    var formData = new FormData();
    for (var i = 0; i < fileInput.files.length; i++) {
      formData.append('files', fileInput.files[i]);
    }

    try {
      var res = await fetch('/api/blogger-lab/personas/' + state.currentPersonaId + '/batches', {
        method: 'POST',
        body: formData
      });
      var json = await res.json();
      if (json.status === 'ok') {
        state.currentBatch = json.data.batch;
        var reportBox = $('#batchReportBox');
        var reportContent = $('#batchReportContent');
        if (reportBox) reportBox.style.display = 'block';
        if (reportContent) {
          reportContent.innerHTML = 
            '<strong>批次 ID:</strong> <code>' + state.currentBatch.id + '</code> | ' +
            '<strong>状态:</strong> <span class="badge badge-' + (state.currentBatch.status || '').toLowerCase() + '">' + state.currentBatch.status + '</span><br>' +
            '<strong>文件:</strong> ' + state.currentBatch.total_files + ' (成功: ' + state.currentBatch.ready_files + ', 失败: ' + state.currentBatch.failed_files + ')<br>' +
            '<strong>字符数:</strong> ' + Number(state.currentBatch.total_chars || 0).toLocaleString() + ' 字 | ' +
            '<strong>分块数:</strong> ' + state.currentBatch.total_chunks + ' 块';
        }
        showNotice('文稿资料导入并分块完成', 'info');
        loadBatchesForExtraction();
      } else {
        var errs = json.errors || [];
        alert('导入失败: ' + errs.map(function(e) { return e.message; }).join('; '));
      }
    } catch (e) {
      alert('上传异常: ' + e.message);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = '开始解析并导入批次';
      }
    }
  }

  // ==========================================
  // 步骤 2: 全文知识提炼监控
  // ==========================================
  async function loadBatchesForExtraction(token, gen) {
    var sel = $('#extractBatchSelect');
    if (!sel) return;
    sel.innerHTML = '';
    if (!state.isPageActive || !state.currentPersonaId) return;
    if (token === undefined) token = state.personaSwitchToken;
    if (gen === undefined) gen = state.pageGeneration;

    try {
      var res = await fetch('/api/blogger-lab/personas/' + state.currentPersonaId + '/batches');
      if (!state.isPageActive || gen !== state.pageGeneration || token !== state.personaSwitchToken) return;

      var json = await res.json();
      if (!state.isPageActive || gen !== state.pageGeneration || token !== state.personaSwitchToken) return;

      var batches = json.status === 'ok' ? (json.data || []) : [];
      if (batches.length > 0) {
        batches.forEach(function(b) {
          var opt = document.createElement('option');
          opt.value = b.id;
          opt.textContent = b.id + ' (' + b.total_files + ' 文件, ' + b.total_chunks + ' 块, ' + b.status + ')';
          sel.appendChild(opt);
        });
        if (state.currentBatch && batches.some(function(b) { return b.id === state.currentBatch.id; })) {
          sel.value = state.currentBatch.id;
        } else {
          sel.value = batches[0].id;
          state.currentBatch = batches[0];
        }
      } else {
        var opt = document.createElement('option');
        opt.value = '';
        opt.textContent = '(暂无批次，请在步骤1导入)';
        sel.appendChild(opt);
      }
    } catch (e) {
      console.warn('获取批次列表失败:', e);
    }
  }

  async function startExtraction() {
    var sel = $('#extractBatchSelect');
    var batchId = sel ? sel.value : '';
    if (!batchId) {
      alert('请选择要提炼的资料批次');
      return;
    }
    var mockBox = $('#chkUseMock');
    var useMock = mockBox ? mockBox.checked : true;
    var btn = $('#btnStartExtract');
    if (btn) btn.disabled = true;

    try {
      var res = await fetch('/api/blogger-lab/batches/' + batchId + '/extract', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          persona_id: state.currentPersonaId,
          use_mock: useMock
        })
      });
      if (!state.isPageActive) return;
      var json = await res.json();
      if (!state.isPageActive) return;
      if (json.status === 'ok') {
        state.currentJobId = json.data.job_id;
        var monitorBox = $('#jobMonitorBox');
        if (monitorBox) monitorBox.style.display = 'block';
        var lblJobId = $('#lblJobId');
        if (lblJobId) lblJobId.textContent = state.currentJobId;
        startPollingJob();
      } else {
        var errs = json.errors || [];
        alert('提交提炼任务失败: ' + errs.map(function(e) { return e.message; }).join('; '));
      }
    } catch (e) {
      alert('网络异常: ' + e.message);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function startPollingJob() {
    if (!state.isPageActive) return;
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
    pollJobStatus();
    if (state.isPageActive) {
      state.pollTimer = setInterval(pollJobStatus, 2000);
    }
  }

  async function pollJobStatus() {
    if (!state.isPageActive) {
      if (state.pollTimer) {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
      }
      return;
    }
    if (!state.currentJobId || !state.currentPersonaId) return;
    var currentToken = state.personaSwitchToken;
    var currentGen = state.pageGeneration;

    try {
      var res = await fetch('/api/blogger-lab/jobs/' + state.currentJobId + '?persona_id=' + state.currentPersonaId);
      if (!state.isPageActive || currentGen !== state.pageGeneration || currentToken !== state.personaSwitchToken) return;

      var json = await res.json();
      if (!state.isPageActive || currentGen !== state.pageGeneration || currentToken !== state.personaSwitchToken) return;

      if (json.status === 'ok') {
        var job = json.data.job;
        renderJobProgress(job);
        if (job.status !== 'RUNNING') {
          if (state.pollTimer) {
            clearInterval(state.pollTimer);
            state.pollTimer = null;
          }
          if (job.knowledge_version_id && state.isPageActive) {
            await loadKnowledgeItems(job.knowledge_version_id);
          }
        }
      }
    } catch (e) {
      console.warn('轮询任务失败:', e);
    }
  }

  function renderJobProgress(job) {
    var lblStatus = $('#lblJobStatus');
    if (lblStatus) {
      lblStatus.textContent = job.status;
      lblStatus.className = 'badge badge-' + (job.status || '').toLowerCase();
    }

    var pct = job.total_chunks > 0 ? Math.round((job.processed_chunks / job.total_chunks) * 100) : 0;
    var bar = $('#progressBar');
    if (bar) bar.style.width = pct + '%';

    var lblStats = $('#lblProgressStats');
    if (lblStats) lblStats.textContent = '已处理 ' + job.processed_chunks + ' / ' + job.total_chunks + ' 块 (' + pct + '%)';

    var lblDetails = $('#lblChunkDetails');
    if (lblDetails) {
      lblDetails.textContent = '成功: ' + job.succeeded_chunks + ' | 部分: ' + (job.partial_chunks || 0) + ' | 失败: ' + job.failed_chunks;
    }

    var errBox = $('#lblJobError');
    var retryBtn = $('#btnRetryJob');
    if (job.status === 'FAILED' || job.status === 'INTERRUPTED' || job.status === 'PARTIAL') {
      if (errBox) {
        errBox.style.display = 'block';
        errBox.textContent = job.error_message || '任务部分分块失败，支持断点续跑';
      }
      if (retryBtn) retryBtn.style.display = 'inline-flex';
    } else {
      if (errBox) errBox.style.display = 'none';
      if (retryBtn) retryBtn.style.display = 'none';
    }
  }

  async function retryJob() {
    if (!state.currentJobId) return;
    var mockBox = $('#chkUseMock');
    var useMock = mockBox ? mockBox.checked : true;
    var btn = $('#btnRetryJob');
    if (btn) btn.disabled = true;

    try {
      var res = await fetch('/api/blogger-lab/jobs/' + state.currentJobId + '/retry', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          persona_id: state.currentPersonaId,
          use_mock: useMock
        })
      });
      if (!state.isPageActive) return;
      var json = await res.json();
      if (!state.isPageActive) return;
      if (json.status === 'ok') {
        showNotice('断点续跑任务已提交', 'info');
        startPollingJob();
      } else {
        var errs = json.errors || [];
        alert('重试失败: ' + errs.map(function(e) { return e.message; }).join('; '));
      }
    } catch (e) {
      alert('重试请求异常: ' + e.message);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function loadKnowledgeItems(versionId) {
    if (!state.currentPersonaId) return;
    var token = state.personaSwitchToken;
    try {
      var res = await fetch('/api/blogger-lab/personas/' + state.currentPersonaId + '/knowledge?version_id=' + versionId);
      if (token !== state.personaSwitchToken) return;

      var json = await res.json();
      if (token !== state.personaSwitchToken) return;

      if (json.status === 'ok' && json.data && json.data.items) {
        renderKnowledgeItems(json.data.items);
      }
    } catch (e) {
      console.warn('加载知识库失败:', e);
    }
  }

  function renderKnowledgeItems(items) {
    var lbl = $('#lblItemsCount');
    if (lbl) lbl.textContent = (items ? items.length : 0) + ' 条';

    var tbody = $('#tblKnowledgeItems tbody');
    if (!tbody) return;
    tbody.innerHTML = '';

    if (!items || items.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: #94a3b8;">未提取到有效条目</td></tr>';
      return;
    }

    items.forEach(function(it) {
      var tr = document.createElement('tr');
      var cit = (it.citations && it.citations.length > 0) ? it.citations[0] : null;
      var quoteText = cit ? cit.quote : '—';
      var srcId = cit ? cit.source_id : '';
      var start = cit ? ((cit.start !== undefined && cit.start !== null) ? cit.start : 0) : 0;
      var end = cit ? ((cit.end !== undefined && cit.end !== null) ? cit.end : 0) : 0;

      tr.innerHTML = 
        '<td><code>' + it.id + '</code></td>' +
        '<td><span class="badge badge-gray">' + it.kind + '</span></td>' +
        '<td style="max-width: 300px;">' + it.statement + '</td>' +
        '<td style="max-width: 260px; font-style: italic; color: #475569;">"' + quoteText + '"</td>' +
        '<td>' +
          (cit ? '<button class="btn btn-secondary btn-view-excerpt" style="padding: 3px 8px; font-size: 11px;" data-src="' + srcId + '" data-start="' + start + '" data-end="' + end + '">查看原文切片</button>' : '—') +
        '</td>';
      tbody.appendChild(tr);
    });

    // 绑定查看原文切片事件
    tbody.querySelectorAll('.btn-view-excerpt').forEach(function(b) {
      b.addEventListener('click', function() {
        viewExcerpt(b.dataset.src, parseInt(b.dataset.start, 10), parseInt(b.dataset.end, 10));
      });
    });
  }

  async function viewExcerpt(sourceId, start, end) {
    try {
      var res = await fetch('/api/blogger-lab/sources/' + sourceId + '/excerpt?persona_id=' + state.currentPersonaId + '&start_char=' + start + '&end_char=' + end);
      var json = await res.json();
      if (json.status === 'ok') {
        var titleEl = $('#modalSourceTitle');
        var srcEl = $('#modalSourceId');
        var rangeEl = $('#modalCharRange');
        var contentEl = $('#modalExcerptContent');
        var modal = $('#excerptModal');

        if (titleEl) titleEl.textContent = '来源切片: ' + json.data.filename;
        if (srcEl) srcEl.textContent = json.data.source_id;
        if (rangeEl) rangeEl.textContent = json.data.start_char + ' ~ ' + json.data.end_char + ' / ' + json.data.total_chars + '字';
        if (contentEl) contentEl.innerHTML = '<span class="excerpt-highlight">' + escapeHtml(json.data.excerpt) + '</span>';
        if (modal) modal.classList.add('active');
      } else {
        var errs = json.errors || [];
        alert('读取原文切片失败: ' + errs.map(function(e) { return e.message; }).join('; '));
      }
    } catch (e) {
      alert('读取原文切片异常: ' + e.message);
    }
  }

  function escapeHtml(str) {
    if (!str) return '';
    return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function closeModal() {
    var modal = $('#excerptModal');
    if (modal) modal.classList.remove('active');
  }

  // ==========================================
  // 步骤 3: 知识与策略草案确认
  // ==========================================
  async function loadDraftAndStrategies() {
    if (!state.currentPersonaId) return;
    var token = state.personaSwitchToken;

    try {
      var res = await fetch('/api/blogger-lab/personas/' + state.currentPersonaId + '/strategies');
      if (token !== state.personaSwitchToken) return;

      var json = await res.json();
      if (token !== state.personaSwitchToken) return;

      if (json.status === 'ok') {
        state.activeStrategy = json.data.active_strategy || null;
        renderActiveStrategy(state.activeStrategy);
        if (json.data.drafts && json.data.drafts.length > 0) {
          state.currentDraft = json.data.drafts[json.data.drafts.length - 1];
          renderDraft(state.currentDraft);
        }
      }
    } catch (e) {
      console.warn('加载策略与草案异常:', e);
    }
  }

  function renderActiveStrategy(strat) {
    var box = $('#activeStratContent');
    var badge = $('#activeStratBadge');
    var lblActiveId = $('#lblActiveStratId');

    if (!strat) {
      if (badge) {
        badge.textContent = '未激活策略';
        badge.className = 'badge badge-gray';
      }
      if (lblActiveId) lblActiveId.textContent = '无激活版本';
      if (box) box.innerHTML = '<p style="color: #94a3b8;">分身尚未确认任何策略版本。请先在左侧生成草案并确认。</p>';
      return;
    }

    if (badge) {
      badge.textContent = '已激活: ' + strat.id;
      badge.className = 'badge badge-ready';
    }
    if (lblActiveId) lblActiveId.textContent = strat.id;
    if (box) {
      var entryRules = strat.entry_rules || [];
      var entryDesc = entryRules.map(function(r) { return r.field + ' ' + r.op + ' ' + JSON.stringify(r.value); }).join(', ') || '—';
      var confirmedTime = strat.confirmed_at ? strat.confirmed_at.replace('T', ' ').slice(0, 19) : '—';
      var stopLoss = strat.exit_rules ? strat.exit_rules.stop_loss_pct : '—';
      var takeProfit = strat.exit_rules ? strat.exit_rules.take_profit_pct : '—';
      var posPct = strat.sizing_rules ? Math.round((strat.sizing_rules.position_fraction || 0) * 100) : 0;

      box.innerHTML = 
        '<div style="background: #f8fafc; padding: 12px; border-radius: 6px; border: 1px solid var(--lab-color-border); line-height: 1.8;">' +
          '<strong>版本标识:</strong> <code>' + strat.id + '</code><br>' +
          '<strong>草案来源:</strong> ' + strat.draft_id + ' (Rev: ' + strat.draft_revision + ')<br>' +
          '<strong>草案哈希:</strong> <code style="font-size: 11px;">' + (strat.draft_hash || '').slice(0, 16) + '...</code><br>' +
          '<strong>入场条件:</strong> ' + entryDesc + '<br>' +
          '<strong>退出规则:</strong> 止损 ' + stopLoss + '%, 止盈 ' + takeProfit + '%<br>' +
          '<strong>仓位管理:</strong> 占用 ' + posPct + '% 资金 (上限 1 只)<br>' +
          '<strong>确认时间:</strong> ' + confirmedTime +
        '</div>';
    }
  }

  function renderDraft(draft) {
    if (!draft) return;
    var lblRev = $('#lblDraftRev');
    if (lblRev) lblRev.textContent = 'Rev: ' + draft.revision;

    var entryBox = $('#entryRulesContainer');
    if (entryBox) {
      entryBox.innerHTML = '';
      if (draft.entry_rules && draft.entry_rules.length > 0) {
        draft.entry_rules.forEach(function(r) {
          var div = document.createElement('div');
          div.style.cssText = 'background: #f8fafc; border: 1px solid var(--lab-color-border); border-radius: 4px; padding: 6px 10px; margin-bottom: 6px; font-size: 12px; display: flex; justify-content: space-between; align-items: center;';
          var originBadge = r.parameter_origin === 'user' 
            ? '<span class="badge badge-primary">user</span>'
            : '<span class="badge badge-gray">' + (r.parameter_origin || 'source') + '</span>';
          div.innerHTML = 
            '<div>' +
              '<strong>' + r.field + '</strong> ' + r.op + ' <code>' + JSON.stringify(r.value) + '</code> (' + (r.unit || 'boolean') + ', ' + (r.basis || 'daily') + ')' +
            '</div>' +
            '<div>' + originBadge + '</div>';
          entryBox.appendChild(div);
        });
      } else {
        entryBox.innerHTML = '<div style="color: #94a3b8; font-size: 12px;">暂无入场规则</div>';
      }
    }

    var jsonArea = $('#draftEntryRulesJson');
    if (jsonArea) {
      jsonArea.value = JSON.stringify(draft.entry_rules || [], null, 2);
    }

    if (draft.exit_rules) {
      var sl = $('#draftStopLoss');
      var tp = $('#draftTakeProfit');
      if (sl) sl.value = (draft.exit_rules.stop_loss_pct !== undefined && draft.exit_rules.stop_loss_pct !== null) ? draft.exit_rules.stop_loss_pct : -3.5;
      if (tp) tp.value = (draft.exit_rules.take_profit_pct !== undefined && draft.exit_rules.take_profit_pct !== null) ? draft.exit_rules.take_profit_pct : 5.0;
    }
    if (draft.sizing_rules) {
      var pf = $('#draftPositionFraction');
      if (pf) pf.value = (draft.sizing_rules.position_fraction !== undefined && draft.sizing_rules.position_fraction !== null) ? draft.sizing_rules.position_fraction : 0.5;
    }

    var hasUnresolved = (draft.unresolved_fields && draft.unresolved_fields.length > 0) ||
                        (draft.unsupported_conditions && draft.unsupported_conditions.length > 0);
    var alertBox = $('#unresolvedAlert');
    if (alertBox) alertBox.style.display = hasUnresolved ? 'block' : 'none';
  }

  async function generateDraft() {
    if (!state.currentPersonaId) return;
    try {
      var kvRes = await fetch('/api/blogger-lab/personas/' + state.currentPersonaId + '/knowledge');
      var kvJson = await kvRes.json();
      var versions = kvJson.data ? (kvJson.data.versions || []) : [];
      if (versions.length === 0) {
        alert('请先在步骤2提炼至少一个知识版本');
        return;
      }
      var latestKv = versions[versions.length - 1];
      var res = await fetch('/api/blogger-lab/personas/' + state.currentPersonaId + '/strategy-drafts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ knowledge_version_id: latestKv.id })
      });
      var json = await res.json();
      if (json.status === 'ok') {
        state.currentDraft = json.data;
        renderDraft(state.currentDraft);
        showNotice('策略草案已生成', 'info');
      } else {
        var errs = json.errors || [];
        alert('生成草案失败: ' + errs.map(function(e) { return e.message; }).join('; '));
      }
    } catch (e) {
      alert('生成草案请求异常: ' + e.message);
    }
  }

  async function saveDraft() {
    if (!state.currentDraft) {
      alert('请先生成策略草案');
      return;
    }
    var slEl = $('#draftStopLoss');
    var tpEl = $('#draftTakeProfit');
    var pfEl = $('#draftPositionFraction');

    var stopLoss = slEl ? parseFloat(slEl.value) : -3.5;
    var takeProfit = tpEl ? parseFloat(tpEl.value) : 5.0;
    var fraction = pfEl ? parseFloat(pfEl.value) : 0.5;

    var entryRulesToSend = state.currentDraft.entry_rules || [];
    var jsonArea = $('#draftEntryRulesJson');
    if (jsonArea && jsonArea.value.trim()) {
      try {
        var parsed = JSON.parse(jsonArea.value.trim());
        if (Array.isArray(parsed)) {
          entryRulesToSend = parsed;
        }
      } catch (e) {
        alert('入场规则 JSON 格式解析失败: ' + e.message);
        return;
      }
    }

    var origExit = state.currentDraft.exit_rules || {};
    var origSizing = state.currentDraft.sizing_rules || {};

    var stopLossChanged = isNaN(stopLoss) || origExit.stop_loss_pct === undefined ? true : Math.abs(stopLoss - origExit.stop_loss_pct) > 1e-6;
    var takeProfitChanged = isNaN(takeProfit) || origExit.take_profit_pct === undefined ? true : Math.abs(takeProfit - origExit.take_profit_pct) > 1e-6;
    var fractionChanged = isNaN(fraction) || origSizing.position_fraction === undefined ? true : Math.abs(fraction - origSizing.position_fraction) > 1e-6;

    var exitRulesToSend = {
      stop_loss_pct: isNaN(stopLoss) ? null : stopLoss,
      take_profit_pct: isNaN(takeProfit) ? null : takeProfit,
      max_holding_sessions: (origExit.max_holding_sessions !== undefined && origExit.max_holding_sessions !== null) ? origExit.max_holding_sessions : null,
      stop_loss_source_refs: origExit.stop_loss_source_refs || [],
      take_profit_source_refs: origExit.take_profit_source_refs || [],
      max_holding_source_refs: origExit.max_holding_source_refs || [],
      stop_loss_origin: stopLossChanged ? 'user' : (origExit.stop_loss_origin || 'user'),
      take_profit_origin: takeProfitChanged ? 'user' : (origExit.take_profit_origin || 'user'),
      max_holding_origin: origExit.max_holding_origin || 'unresolved'
    };

    var sizingRulesToSend = {
      position_fraction: isNaN(fraction) ? null : fraction,
      max_positions: (origSizing.max_positions !== undefined && origSizing.max_positions !== null) ? origSizing.max_positions : 1,
      fraction_source_refs: origSizing.fraction_source_refs || [],
      fraction_origin: fractionChanged ? 'user' : (origSizing.fraction_origin || 'user')
    };

    try {
      var res = await fetch('/api/blogger-lab/strategy-drafts/' + state.currentDraft.id, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          persona_id: state.currentPersonaId,
          expected_revision: state.currentDraft.revision,
          entry_rules: entryRulesToSend,
          exit_rules: exitRulesToSend,
          sizing_rules: sizingRulesToSend
        })
      });
      var json = await res.json();
      if (json.status === 'ok') {
        state.currentDraft = json.data;
        renderDraft(state.currentDraft);
        showNotice('草案已保存 (Revision: ' + json.data.revision + ')', 'info');
      } else {
        var errs = json.errors || [];
        alert('保存草案失败: ' + errs.map(function(e) { return e.message; }).join('; '));
      }
    } catch (e) {
      alert('保存草案请求异常: ' + e.message);
    }
  }

  async function confirmDraft() {
    if (!state.currentDraft) {
      alert('请先生成并保存策略草案');
      return;
    }
    var btn = $('#btnConfirmDraft');
    if (btn) btn.disabled = true;

    try {
      var res = await fetch('/api/blogger-lab/strategy-drafts/' + state.currentDraft.id + '/confirm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          persona_id: state.currentPersonaId,
          expected_revision: state.currentDraft.revision,
          expected_hash: state.currentDraft.draft_hash
        })
      });
      var json = await res.json();
      if (json.status === 'ok') {
        state.activeStrategy = json.data;
        renderActiveStrategy(state.activeStrategy);
        showNotice("不可变策略版本 '" + json.data.id + "' 确认发布成功！", 'info');
      } else {
        var errs = json.errors || [];
        alert('确认策略失败: ' + errs.map(function(e) { return e.message; }).join('; '));
      }
    } catch (e) {
      alert('确认策略请求异常: ' + e.message);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // ==========================================
  // 步骤 4: 今日机会发现与证据扫描
  // ==========================================
  async function runScan() {
    if (!state.activeStrategy) {
      alert('分身尚未有已确认的生效策略版本，请先在步骤3生成并确认');
      return;
    }
    var dateEl = $('#scanAsOfDate');
    var asOfDate = dateEl ? dateEl.value.trim() : '20261030';
    var btn = $('#btnRunScan');
    if (btn) {
      btn.disabled = true;
      btn.textContent = '执行机会扫描中...';
    }

    try {
      var res = await fetch('/api/blogger-lab/personas/' + state.currentPersonaId + '/scans', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          strategy_version_id: state.activeStrategy.id,
          mode: 'REPLAY',
          as_of_date: asOfDate || '20261030',
          universe_limit: 50
        })
      });
      var json = await res.json();
      if (json.status === 'ok') {
        state.latestScanRun = json.data;
        renderScanResults(state.latestScanRun);
        showNotice('机会扫描完成，筛选出 ' + json.data.candidates_count + ' 只合格候选', 'info');
      } else {
        var errs = json.errors || [];
        alert('扫描失败: ' + errs.map(function(e) { return e.message; }).join('; '));
      }
    } catch (e) {
      alert('扫描请求异常: ' + e.message);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = '执行机会发现扫描';
      }
    }
  }

  function refreshScanView() {
    if (state.latestScanRun) {
      renderScanResults(state.latestScanRun);
    }
  }

  function renderScanResults(run) {
    var summaryBox = $('#scanSummaryBox');
    var summaryText = $('#lblScanSummaryText');
    var candCount = $('#lblCandidatesCount');

    if (summaryBox) summaryBox.style.display = 'block';
    if (summaryText) {
      summaryText.textContent = '扫描规模: 目标 ' + run.universe_target_count + ' 只 | 实际评估 ' + run.universe_evaluated_count + ' 只 | 排除 ' + run.universe_excluded_count + ' 只 | 合格候选 ' + run.candidates_count + ' 只 (耗时 ' + run.execution_ms + 'ms)';
    }
    if (candCount) candCount.textContent = run.candidates_count + ' 只候选';

    var tbody = $('#tblCandidates tbody');
    if (!tbody) return;
    tbody.innerHTML = '';

    if (!run.candidates || run.candidates.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: #94a3b8;">本次扫描无合格候选标的</td></tr>';
    } else {
      run.candidates.slice(0, 5).forEach(function(c) {
        var tr = document.createElement('tr');
        var checks = c.checks || [];
        var checkStr = checks.map(function(k) {
          var fieldName = k.field || (k.error ? 'error' : 'rule');
          var rawStatus = k.status ? String(k.status).toUpperCase() : (k.passed === true ? 'TRUE' : (k.passed === false ? 'FALSE' : 'UNKNOWN'));
          var statusClass = rawStatus === 'TRUE' ? 'color-up' : (rawStatus === 'FALSE' ? 'color-down' : 'color-unknown');
          return fieldName + ': <strong class="' + statusClass + '">' + rawStatus + '</strong>';
        }).join(', ');
        var snap = c.market_snapshot || {};
        var closePrice = snap.last_price || snap.close || '—';
        var snapStr = '现价: ' + closePrice + ' | 昨收: ' + (snap.prev_close || '—') + ' | MA20: ' + (snap.ma20 || '—');
        tr.innerHTML = 
          '<td><code>' + c.symbol + '</code></td>' +
          '<td><strong>' + c.name + '</strong></td>' +
          '<td>' + checkStr + '</td>' +
          '<td>' + snapStr + '</td>' +
          '<td>' + (c.data_cutoff || '—') + '</td>' +
          '<td><span class="badge badge-ready">QUALIFIED</span></td>';
        tbody.appendChild(tr);
      });
    }

    // 同步更新步骤 5 受控回放待建仓候选下拉框
    var candSel = $('#replayCandidateSelect');
    if (candSel) {
      candSel.innerHTML = '';
      var qual = (run.candidates || []).filter(function(c) { return c.decision === 'QUALIFIED'; });
      if (qual.length > 0) {
        qual.forEach(function(c) {
          var opt = document.createElement('option');
          opt.value = c.symbol;
          opt.dataset.cutoff = c.data_cutoff || '';
          var pVal = c.market_snapshot ? (c.market_snapshot.last_price || c.market_snapshot.close || '—') : '—';
          opt.textContent = c.symbol + ' ' + (c.name || '') + ' (基准价: ' + pVal + ', 数据截止: ' + (c.data_cutoff || '—') + ')';
          candSel.appendChild(opt);
        });
      } else {
        var opt = document.createElement('option');
        opt.value = '';
        opt.textContent = '(本次扫描无合格候选标的)';
        candSel.appendChild(opt);
      }
      updateReplayDates();
    }
  }

  // ==========================================
  // 步骤 5: 受控回放与模拟账户
  // ==========================================
  async function executeStep(stepIndex) {
    if (!state.currentPersonaId) {
      alert('请先选择分身');
      return;
    }
    var btn = $('#btnStep' + stepIndex);
    if (btn) btn.disabled = true;

    var candId = null;
    if (stepIndex === 1) {
      var candSel = $('#replayCandidateSelect');
      candId = candSel ? candSel.value : null;
      if (!candId) {
        alert('当前未选中任何合格候选标的，回放无法建仓！请在步骤4执行扫描并确保产生合格候选。');
        if (btn) btn.disabled = false;
        return;
      }
    }

    // 为防双击提供 request_key
    var keyProp = 'step' + stepIndex;
    if (!state.lastRequestKeys[keyProp]) {
      state.lastRequestKeys[keyProp] = 'req_step' + stepIndex + '_' + Date.now();
    }
    var reqKey = state.lastRequestKeys[keyProp];

    try {
      var res = await fetch('/api/blogger-lab/personas/' + state.currentPersonaId + '/replay-step', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          scenario_id: 'standard_t1_cycle',
          step_index: stepIndex,
          candidate_id: candId,
          request_key: reqKey
        })
      });
      var json = await res.json();
      var resBox = $('#stepResultBox');
      if (resBox) resBox.style.display = 'block';

      if (json.status === 'ok') {
        var cyc = json.data.cycle;
        var statusClass = cyc.status === 'COMPLETED' ? 'badge-completed' : (cyc.status === 'BLOCKED' ? 'badge-blocked' : 'badge-gray');
        if (resBox) {
          resBox.className = cyc.status === 'COMPLETED' ? 'notice-box notice-info' : 'notice-box notice-warning';
          resBox.innerHTML = 
            '<div>' +
              '<strong>第 ' + stepIndex + ' 步执行反馈:</strong> 动作 <span class="badge ' + statusClass + '">' + cyc.action + '</span> | 状态 <span class="badge ' + statusClass + '">' + cyc.status + '</span> | 成交 <span class="badge badge-gray">' + cyc.fills_count + ' 笔</span><br>' +
              '<strong>明细说明:</strong> ' + cyc.summary +
            '</div>';
        }
        refreshAccount();
      } else {
        var errs = json.errors || [];
        if (resBox) {
          resBox.className = 'notice-box notice-error';
          resBox.innerHTML = '<strong>执行被拦截:</strong> ' + errs.map(function(e) { return e.message; }).join('; ');
        }
      }
    } catch (e) {
      alert('回放单步执行异常: ' + e.message);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function renderAccount(acc) {
    if (!acc) return;
    var cashEl = $('#lblAccountCash');
    var totalEl = $('#lblAccountTotal');
    if (cashEl) cashEl.textContent = Number(acc.cash || 0).toLocaleString('zh-CN', { minimumFractionDigits: 2 });
    if (totalEl) totalEl.textContent = Number(acc.total_asset || acc.cash || 0).toLocaleString('zh-CN', { minimumFractionDigits: 2 });
    refreshAccount();
  }

  async function refreshAccount() {
    if (!state.currentPersonaId) return;
    var token = state.personaSwitchToken;

    try {
      var res = await fetch('/api/blogger-lab/personas/' + state.currentPersonaId + '/paper?mode=REPLAY');
      if (token !== state.personaSwitchToken) return;

      var json = await res.json();
      if (token !== state.personaSwitchToken) return;

      if (json.status === 'ok') {
        var acc = json.data.account;
        var cashEl = $('#lblAccountCash');
        var totalEl = $('#lblAccountTotal');
        if (cashEl) cashEl.textContent = Number(acc.cash).toLocaleString('zh-CN', { minimumFractionDigits: 2 });
        if (totalEl) totalEl.textContent = Number(acc.total_asset).toLocaleString('zh-CN', { minimumFractionDigits: 2 });

        // 渲染持仓
        var posBox = $('#positionsContainer');
        var positions = json.data.positions || [];
        state.lastPositions = positions;
        if (posBox) {
          if (positions.length === 0) {
            posBox.innerHTML = '<p style="color: #94a3b8; font-size: 13px;">当前空仓，无持有标的</p>';
          } else {
            posBox.innerHTML = positions.map(function(p) {
              return '<div style="background: #f8fafc; padding: 10px; border-radius: 6px; border: 1px solid var(--lab-color-border); font-size: 13px; margin-bottom: 6px;">' +
                '<strong>' + p.name + ' (' + p.symbol + ')</strong>: 持有 ' + p.shares + ' 股 | 成本: ' + p.entry_price + ' 元 | 建立: ' + p.entry_date +
              '</div>';
            }).join('');
          }
        }

        // 渲染成交流水
        var fills = json.data.fills || [];
        state.lastFills = fills;
        renderFills(fills);

        // 动态更新受控回放按钮说明
        updateReplayDates();
      }
    } catch (e) {
      console.warn('刷新账户失败:', e);
    }
  }

  function renderFills(fills) {
    var lblFills = $('#lblFillsCount');
    if (lblFills) lblFills.textContent = fills.length + ' 笔成交';

    var tbody = $('#tblFills tbody');
    if (!tbody) return;
    tbody.innerHTML = '';

    if (!fills || fills.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: #94a3b8;">暂无成交流水</td></tr>';
      return;
    }

    fills.forEach(function(f) {
      var tr = document.createElement('tr');
      var dirClass = f.direction === 'BUY' ? 'color-up' : 'color-down';
      var fee = (f.commission + f.stamp_tax + (f.slippage_cost || 0)).toFixed(2);
      tr.innerHTML = 
        '<td><strong class="' + dirClass + '">' + f.direction + '</strong></td>' +
        '<td>' + f.name + ' (' + f.symbol + ')</td>' +
        '<td>' + f.price.toFixed(2) + '</td>' +
        '<td>' + f.shares + '</td>' +
        '<td>' + fee + ' 元</td>' +
        '<td class="' + (f.cash_effect >= 0 ? 'color-up' : 'color-down') + '">' + (f.cash_effect >= 0 ? '+' : '') + f.cash_effect.toFixed(2) + '</td>' +
        '<td><small style="color: #64748b;">' + (f.trade_time ? f.trade_time.slice(0, 19).replace('T', ' ') : '—') + '</small></td>';
      tbody.appendChild(tr);
    });
  }

  function updateReplayDates() {
    var candSel = $('#replayCandidateSelect');
    var selectedOpt = candSel && candSel.selectedIndex >= 0 ? candSel.options[candSel.selectedIndex] : null;
    var cutoff = (selectedOpt && selectedOpt.dataset && selectedOpt.dataset.cutoff) ? selectedOpt.dataset.cutoff : '';

    var descEl = $('#replayScenarioDesc');
    if (descEl) {
      if (cutoff) {
        descEl.innerHTML = '本场景复现：<strong>前日收盘候选 (数据截止 ' + cutoff + ') → 下一交易日 10:00 BUY → 同日 14:30 T+1锁仓拦截 → 次一交易日 10:00 止盈 SELL</strong>。';
      } else {
        descEl.innerHTML = '本场景复现：<strong>前日收盘候选 → 下一交易日 10:00 BUY → 同日 14:30 T+1锁仓拦截 → 次一交易日 10:00 止盈 SELL</strong>。';
      }
    }

    var btn1 = $('#btnStep1');
    var btn2 = $('#btnStep2');
    var btn3 = $('#btnStep3');

    var fills = state.lastFills || [];
    var buyFill = fills.find(function(f) { return f.direction === 'BUY'; });
    var sellFill = fills.find(function(f) { return f.direction === 'SELL'; });
    var positions = state.lastPositions || [];
    var openPos = positions.length > 0 ? positions[0] : null;

    if (btn1) {
      if (buyFill && buyFill.trade_time) {
        var t = buyFill.trade_time.slice(0, 16).replace('T', ' ');
        btn1.innerHTML = '<span>第 1 步: 竞价建仓买入 (已成交: ' + t + ')</span>';
      } else {
        btn1.innerHTML = '<span>第 1 步: 竞价建仓买入 (下一交易日 10:00)</span>';
      }
    }

    if (btn2) {
      var entryDate = openPos ? openPos.entry_date : (buyFill && buyFill.trade_time ? buyFill.trade_time.slice(0, 10) : '');
      if (entryDate) {
        btn2.innerHTML = '<span>第 2 步: 测试同日 T+1 锁仓拦截 (' + entryDate + ' 14:30)</span>';
      } else {
        btn2.innerHTML = '<span>第 2 步: 测试同日 T+1 锁仓拦截 (同日 14:30)</span>';
      }
    }

    if (btn3) {
      if (sellFill && sellFill.trade_time) {
        var t2 = sellFill.trade_time.slice(0, 16).replace('T', ' ');
        btn3.innerHTML = '<span>第 3 步: 达成止盈全额平仓 (已平仓: ' + t2 + ')</span>';
      } else {
        btn3.innerHTML = '<span>第 3 步: 达成止盈全额平仓 (次一交易日 10:00)</span>';
      }
    }
  }

  // ==========================================
  // 事件绑定与生命周期钩子
  // ==========================================
  function bindEvents() {
    // 步骤导航点击
    $$('.nav-tab').forEach(function(tab, idx) {
      tab.addEventListener('click', function() { switchStep(idx + 1); });
    });

    // 分身切换
    var personaSel = $('#personaSelect');
    if (personaSel) {
      personaSel.addEventListener('change', function() { onPersonaChanged(true); });
    }

    // 步骤 1 按钮
    var btnCreatePersona = $('#btnCreatePersona');
    if (btnCreatePersona) btnCreatePersona.addEventListener('click', createPersona);
    var btnUploadBatch = $('#btnUploadBatch');
    if (btnUploadBatch) btnUploadBatch.addEventListener('click', uploadBatch);

    // 步骤 2 按钮
    var btnStartExtract = $('#btnStartExtract');
    if (btnStartExtract) btnStartExtract.addEventListener('click', startExtraction);
    var btnRetryJob = $('#btnRetryJob');
    if (btnRetryJob) btnRetryJob.addEventListener('click', retryJob);

    // 步骤 3 按钮
    var btnGenDraft = $('#btnGenDraft');
    if (btnGenDraft) btnGenDraft.addEventListener('click', generateDraft);
    var btnSaveDraft = $('#btnSaveDraft');
    if (btnSaveDraft) btnSaveDraft.addEventListener('click', saveDraft);
    var btnConfirmDraft = $('#btnConfirmDraft');
    if (btnConfirmDraft) btnConfirmDraft.addEventListener('click', confirmDraft);

    // 步骤 4 按钮
    var btnRunScan = $('#btnRunScan');
    if (btnRunScan) btnRunScan.addEventListener('click', runScan);

    // 步骤 5 按钮
    var replayCandSel = $('#replayCandidateSelect');
    if (replayCandSel) replayCandSel.addEventListener('change', updateReplayDates);
    var btnStep1 = $('#btnStep1');
    if (btnStep1) btnStep1.addEventListener('click', function() { executeStep(1); });
    var btnStep2 = $('#btnStep2');
    if (btnStep2) btnStep2.addEventListener('click', function() { executeStep(2); });
    var btnStep3 = $('#btnStep3');
    if (btnStep3) btnStep3.addEventListener('click', function() { executeStep(3); });
    var btnRefreshAcc = $('#btnRefreshAccount');
    if (btnRefreshAcc) btnRefreshAcc.addEventListener('click', refreshAccount);

    // 原文切片弹窗关闭
    var modalCloseX = $('#modalCloseX');
    if (modalCloseX) modalCloseX.addEventListener('click', closeModal);
    var btnModalClose = $('#btnModalClose');
    if (btnModalClose) btnModalClose.addEventListener('click', closeModal);
  }

  function init() {
    if (state.initialized) return;
    var root = getRoot();
    if (!root) return;

    bindEvents();
    state.initialized = true;

    // 恢复本地存储的活跃步骤
    var savedStep = 1;
    try {
      var st = localStorage.getItem(STORAGE_KEY_STEP);
      if (st && /^[1-5]$/.test(st)) savedStep = parseInt(st, 10);
    } catch (e) {}

    switchStep(savedStep);
  }

  // 生命周期：进入实验页面
  function onEnter() {
    state.isPageActive = true;
    var currentGen = ++state.pageGeneration;
    init();
    loadPersonas(currentGen);
  }

  // 生命周期：离开实验页面
  function onLeave() {
    state.isPageActive = false;
    ++state.pageGeneration;
    ++state.personaSwitchToken;
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  // 暴露模块命名空间
  window.BloggerLabDomain = {
    init: init,
    onEnter: onEnter,
    onLeave: onLeave,
    switchStep: switchStep,
    getState: function() { return state; },
    loadPersonas: loadPersonas,
    onPersonaChanged: onPersonaChanged,
    createPersona: createPersona,
    uploadBatch: uploadBatch,
    startExtraction: startExtraction,
    retryJob: retryJob,
    generateDraft: generateDraft,
    saveDraft: saveDraft,
    confirmDraft: confirmDraft,
    runScan: runScan,
    executeStep: executeStep,
    refreshAccount: refreshAccount,
    closeModal: closeModal,
    viewExcerpt: viewExcerpt
  };

  // 若处于独立薄壳页面，自动在 DOM 就绪后执行 onEnter
  if (document.querySelector('.blogger-lab-root.standalone-shell')) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', onEnter);
    } else {
      onEnter();
    }
  }

})(window);
