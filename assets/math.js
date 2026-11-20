(function () {
  const base = 'https://cdn.jsdelivr.net/npm/katex@0.19.1/dist/';
  const integrity = {
    'katex.min.css': 'sha384-WwGtaC6ywCHxA5cstpj/3+xIZvTez8dq2IcBQaXRkenHZ7FVVq1ZROtURwfbtTlG',
    'katex.min.js': 'sha384-a1WQdjRj162i159OYq041X6I4K8NenkrI09LlPmiHg0YOb7CR+wJOraXwK9abpnE',
    'contrib/auto-render.min.js': 'sha384-bjyGPfbij8/NDKJhSGZNP/khQVgtHUE5exjm4Ydllo42FwIgYsdLO2lXGmRBf5Mz'
  };
  // A single $ stays literal text: Hacker News stories are full of $100 and $5M.
  const delimiters = [
    { left: '$$', right: '$$', display: true },
    { left: '\\[', right: '\\]', display: true },
    { left: '\\(', right: '\\)', display: false }
  ];

  const feed = document.querySelector('.feed');
  if (!feed) return;

  const text = feed.textContent;
  if (!text.includes('\\(') && !text.includes('\\[') && !text.includes('$$')) return;

  function append(tag, attributes) {
    const element = document.createElement(tag);
    Object.keys(attributes).forEach(function (name) {
      element.setAttribute(name, attributes[name]);
    });
    document.head.appendChild(element);
    return element;
  }

  function load(path) {
    return append('script', {
      src: base + path,
      integrity: integrity[path],
      crossorigin: 'anonymous'
    });
  }

  append('link', {
    rel: 'stylesheet',
    href: base + 'katex.min.css',
    integrity: integrity['katex.min.css'],
    crossorigin: 'anonymous'
  });

  load('katex.min.js').addEventListener('load', function () {
    load('contrib/auto-render.min.js').addEventListener('load', function () {
      if (typeof window.renderMathInElement !== 'function') return;

      window.renderMathInElement(feed, {
        delimiters: delimiters,
        throwOnError: false,
        strict: 'ignore',
        ignoredTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code', 'option']
      });
    });
  });
})();
