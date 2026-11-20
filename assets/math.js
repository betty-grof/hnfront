(function () {
  const KATEX_BASE = 'https://cdn.jsdelivr.net/npm/katex@0.19.1/dist/';

  const INTEGRITY = {
    'katex.min.css': 'sha384-WwGtaC6ywCHxA5cstpj/3+xIZvTez8dq2IcBQaXRkenHZ7FVVq1ZROtURwfbtTlG',
    'katex.min.js': 'sha384-a1WQdjRj162i159OYq041X6I4K8NenkrI09LlPmiHg0YOb7CR+wJOraXwK9abpnE',
    'contrib/auto-render.min.js': 'sha384-bjyGPfbij8/NDKJhSGZNP/khQVgtHUE5exjm4Ydllo42FwIgYsdLO2lXGmRBf5Mz'
  };

  // A single $ stays literal text: Hacker News stories are full of $100 and $5M.
  const DELIMITERS = [
    { left: '$$', right: '$$', display: true },
    { left: '\\[', right: '\\]', display: true },
    { left: '\\(', right: '\\)', display: false }
  ];

  // These elements already show their TeX source as written.
  const IGNORED_TAGS = ['script', 'noscript', 'style', 'textarea', 'pre', 'code', 'option'];

  const MATH_MARKERS = ['$$', '\\[', '\\('];

  function hasMath(text) {
    return MATH_MARKERS.some(function (marker) {
      return text.includes(marker);
    });
  }

  function appendToHead(tag, attributes) {
    const element = document.createElement(tag);

    Object.keys(attributes).forEach(function (name) {
      element.setAttribute(name, attributes[name]);
    });

    document.head.appendChild(element);

    return element;
  }

  function loadStyleSheet(path) {
    appendToHead('link', {
      rel: 'stylesheet',
      href: KATEX_BASE + path,
      integrity: INTEGRITY[path],
      crossorigin: 'anonymous'
    });
  }

  // Resolves once the browser has run the script; rejects when the CDN fails.
  function loadScript(path) {
    return new Promise(function (resolve, reject) {
      const script = appendToHead('script', {
        src: KATEX_BASE + path,
        integrity: INTEGRITY[path],
        crossorigin: 'anonymous'
      });

      script.addEventListener('load', resolve);
      script.addEventListener('error', reject);
    });
  }

  async function init() {
    const feed = document.querySelector('.feed');

    if (!feed) {
      return;
    }

    if (!hasMath(feed.textContent)) {
      return;
    }

    loadStyleSheet('katex.min.css');

    try {
      await loadScript('katex.min.js');
      await loadScript('contrib/auto-render.min.js');
    } catch (error) {
      // A blocked CDN leaves the raw TeX on screen instead of breaking the page.
      return;
    }

    if (typeof window.renderMathInElement !== 'function') {
      return;
    }

    window.renderMathInElement(feed, {
      delimiters: DELIMITERS,
      throwOnError: false,
      strict: 'ignore',
      ignoredTags: IGNORED_TAGS
    });
  }

  init();
})();
