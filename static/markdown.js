// 위키봇 답변 마크다운 렌더러 + 로봇 아바타 — 대화 화면(index.html)과 관리자 화면(admin.html)이 같이 쓴다.
function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ───────────────────────── 마크다운 렌더링 ─────────────────────────
function renderInline(text) {
  let t = escapeHtml(text);
  t = t.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, '<img src="$2" alt="$1" loading="lazy">');
  // 정상 마크다운 링크([텍스트](url))와, 모델이 가끔 깨뜨려 쓰는 형태
  // (여는 대괄호 누락 등)에서도 살아남는 URL만이라도 클릭 가능하게 만든다
  t = t.replace(
    /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<)\]]+)/g,
    (match, linkText, linkUrl, bareUrl) => {
      const url = bareUrl || linkUrl;
      const label = bareUrl || linkText;
      return `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`;
    }
  );
  t = t.replace(/`([^`]+)`/g, '<code>$1</code>');
  t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  return t;
}

const FENCE_RE = /```(\w+)?\n([\s\S]*?)```/g;
// 모델이 가끔 코드펜스를 홀수개(닫는 ``` 하나 누락)로 생성하면 "mermaid"라는 단어까지 포함해
// 통째로 일반 텍스트로 노출된다(실측). 펜스 없이 맨 줄로 등장한 "mermaid" 다음에 다이어그램
// 문법이 바로 이어지면 렌더링 전에 펜스를 복구한다.
const BARE_MERMAID_RE = /(^|\n)[ \t]*mermaid[ \t]*\n((?:sequenceDiagram|graph\s|flowchart\s|classDiagram|stateDiagram|erDiagram|gantt|pie|journey|gitGraph|mindmap|timeline)[\s\S]*?)(?=\n[ \t]*\n|$)/gi;
function repairUnfencedMermaid(text) {
  return text.replace(BARE_MERMAID_RE, (match, lead, body) => `${lead}\`\`\`mermaid\n${body}\n\`\`\``);
}
function appendCodeBlock(bubble, code) {
  const pre = document.createElement('pre');
  const codeEl = document.createElement('code');
  codeEl.textContent = code;
  pre.appendChild(codeEl);
  bubble.appendChild(pre);
}
// 모델이 생성한 mermaid 문법이 틀리면 mermaid.js가 자체 에러 박스를 그려버린다 — parse()로 먼저
// 검증하고, placeholder를 동기적으로 먼저 붙여 텍스트 블록과의 순서를 보장한다.
function appendMermaidBlock(bubble, code) {
  const pre = document.createElement('pre');
  pre.className = 'mermaid';
  pre.textContent = code;
  bubble.appendChild(pre);
  return mermaid.parse(code).then(
    () => pre,
    () => {
      const note = document.createElement('div');
      note.className = 'mermaid-error-note';
      note.textContent = '⚠️ 다이어그램 생성에 실패했습니다 (문법 오류) — 원본 코드만 표시합니다';
      const codePre = document.createElement('pre');
      const codeEl = document.createElement('code');
      codeEl.textContent = code;
      codePre.appendChild(codeEl);
      pre.replaceWith(note, codePre);
      return null;
    }
  );
}

const TABLE_ROW_RE = /^\s*\|(.+)\|\s*$/;
const TABLE_SEP_RE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
const HEADING_RE = /^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/;
function parseTableRow(line) {
  let inner = line.trim();
  if (inner.startsWith('|')) inner = inner.slice(1);
  if (inner.endsWith('|')) inner = inner.slice(0, -1);
  return inner.split('|').map(c => c.trim());
}
function renderTextBlock(container, text) {
  const lines = text.split('\n');
  let i = 0;
  let buffer = [];
  function flushParagraph() {
    if (!buffer.length) return;
    const div = document.createElement('div');
    div.innerHTML = renderInline(buffer.join('\n'));
    container.appendChild(div);
    buffer = [];
  }
  while (i < lines.length) {
    const line = lines[i];
    const next = lines[i + 1];
    const headingMatch = line.match(HEADING_RE);
    if (headingMatch) {
      flushParagraph();
      const h = document.createElement(`h${headingMatch[1].length}`);
      h.innerHTML = renderInline(headingMatch[2]);
      container.appendChild(h);
      i++;
      continue;
    }
    if (TABLE_ROW_RE.test(line) && next !== undefined && TABLE_SEP_RE.test(next)) {
      flushParagraph();
      const headerCells = parseTableRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && TABLE_ROW_RE.test(lines[i])) { rows.push(parseTableRow(lines[i])); i++; }
      const wrap = document.createElement('div');
      wrap.className = 'table-wrap';
      const table = document.createElement('table');
      table.className = 'md-table';
      const thead = document.createElement('thead');
      const headRow = document.createElement('tr');
      headerCells.forEach(c => { const th = document.createElement('th'); th.innerHTML = renderInline(c); headRow.appendChild(th); });
      thead.appendChild(headRow);
      table.appendChild(thead);
      const tbody = document.createElement('tbody');
      rows.forEach(r => {
        const tr = document.createElement('tr');
        r.forEach(c => { const td = document.createElement('td'); td.innerHTML = renderInline(c); tr.appendChild(td); });
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      wrap.appendChild(table);
      container.appendChild(wrap);
      continue;
    }
    buffer.push(line);
    i++;
  }
  flushParagraph();
}
async function renderMessage(bubble, text) {
  bubble.innerHTML = '';
  text = repairUnfencedMermaid(text || '');
  let lastIndex = 0;
  let m;
  FENCE_RE.lastIndex = 0;
  const mermaidPending = [];
  while ((m = FENCE_RE.exec(text)) !== null) {
    if (m.index > lastIndex) renderTextBlock(bubble, text.slice(lastIndex, m.index));
    const lang = (m[1] || '').toLowerCase();
    if (lang === 'mermaid' && window.mermaid) mermaidPending.push(appendMermaidBlock(bubble, m[2]));
    else appendCodeBlock(bubble, m[2]);
    lastIndex = FENCE_RE.lastIndex;
  }
  if (lastIndex < text.length) renderTextBlock(bubble, text.slice(lastIndex));
  if (mermaidPending.length) {
    const nodes = (await Promise.all(mermaidPending)).filter(Boolean);
    if (nodes.length) mermaid.run({ nodes }).catch(() => {});
    return nodes.length < mermaidPending.length; // true면 하나 이상 파싱 실패
  }
  return false;
}


// 타이핑 효과용 — 답변을 앞에서부터 잘라 여러 번 그리는 동안에는 mermaid 파싱(비동기·무거움)을
// 하지 않고 코드블록으로만 보여준다. 다 쓰고 나면 renderMessage()로 한 번 제대로 그린다.
function renderPreview(bubble, text) {
  bubble.innerHTML = '';
  text = repairUnfencedMermaid(text || '');
  let lastIndex = 0;
  let m;
  FENCE_RE.lastIndex = 0;
  while ((m = FENCE_RE.exec(text)) !== null) {
    if (m.index > lastIndex) renderTextBlock(bubble, text.slice(lastIndex, m.index));
    appendCodeBlock(bubble, m[2]);
    lastIndex = FENCE_RE.lastIndex;
  }
  if (lastIndex < text.length) renderTextBlock(bubble, text.slice(lastIndex));
}

// 위키봇 로봇 캐릭터(expharness의 ML 엔지니어 캐릭터 형태를 벤치마킹).
// state: working(답변 만드는 중) / ready(답변 완료) / attention(오류) / idle
function robotHtml(state, small) {
  const mouth = state === 'attention' ? 'M16 26c2.3-1.5 5.7-1.5 8 0' : 'M16 25c2.3 1.5 5.7 1.5 8 0';
  return `<span class="engineer-character is-${state || 'idle'}${small ? ' sm' : ''}" aria-hidden="true">` +
    (state === 'working' ? '<span class="amicro-breathe-ring"></span>' : '') +
    `<svg viewBox="0 0 40 40" focusable="false"><path class="engineer-antenna" d="M20 8V5m0 0 3-2"/>` +
    `<rect class="engineer-face" x="8.5" y="9.5" width="23" height="21" rx="7"/>` +
    `<circle class="engineer-eye" cx="16" cy="19" r="1.7"/><circle class="engineer-eye" cx="24" cy="19" r="1.7"/>` +
    `<path class="engineer-mouth" d="${mouth}"/></svg><i></i></span>`;
}
