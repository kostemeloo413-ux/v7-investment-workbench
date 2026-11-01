/**
 * V7 主工作台 - 博主实验室旧入口兼容垫片 (Blogger Lab Entry Shim)
 * 阶段2原生集成后，五步功能已直接内置于 #page-blogger-lab 容器。
 * 本垫片保留以兼容旧测试或独立调试场景，安全探测 DOM 节点，避免报错或多余探测。
 */
(function() {
  'use strict';

  function init() {
    var input = document.getElementById('blogger-lab-url-input');
    // 原生工作台下相关配置输入框已替换为原生工作域，直接安全返回
    if (!input) return;

    var DEFAULT_LAB_URL = 'http://127.0.0.1:8094/workbench_v7/blogger_lab.html';
    var STORAGE_KEY = 'v7_blogger_lab_target_url';

    function getTargetUrl() {
      try {
        return localStorage.getItem(STORAGE_KEY) || DEFAULT_LAB_URL;
      } catch (e) {
        return DEFAULT_LAB_URL;
      }
    }

    var btnSave = document.getElementById('btn-save-lab-url');
    var btnReset = document.getElementById('btn-reset-lab-url');
    var btnProbe = document.getElementById('btn-probe-lab');
    var btnLaunch = document.getElementById('btn-launch-blogger-lab');
    var btnToggleIframe = document.getElementById('btn-toggle-lab-iframe');
    var btnCloseIframe = document.getElementById('btn-close-lab-iframe');
    var iframeContainer = document.getElementById('blogger-lab-iframe-container');
    var iframe = document.getElementById('blogger-lab-iframe');

    input.value = getTargetUrl();

    if (btnSave) {
      btnSave.addEventListener('click', function() {
        var v = input.value ? input.value.trim() : DEFAULT_LAB_URL;
        try { localStorage.setItem(STORAGE_KEY, v); } catch (e) {}
      });
    }

    if (btnReset) {
      btnReset.addEventListener('click', function() {
        input.value = DEFAULT_LAB_URL;
        try { localStorage.setItem(STORAGE_KEY, DEFAULT_LAB_URL); } catch (e) {}
      });
    }

    if (btnLaunch) {
      btnLaunch.addEventListener('click', function() {
        var v = input.value ? input.value.trim() : DEFAULT_LAB_URL;
        window.open(v, '_blank');
      });
    }

    if (btnToggleIframe && iframeContainer) {
      btnToggleIframe.addEventListener('click', function() {
        var isHidden = iframeContainer.style.display === 'none';
        iframeContainer.style.display = isHidden ? 'block' : 'none';
        if (isHidden && iframe && (!iframe.src || iframe.src === 'about:blank')) {
          iframe.src = input.value ? input.value.trim() : DEFAULT_LAB_URL;
        }
      });
    }

    if (btnCloseIframe && iframeContainer) {
      btnCloseIframe.addEventListener('click', function() {
        iframeContainer.style.display = 'none';
      });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
