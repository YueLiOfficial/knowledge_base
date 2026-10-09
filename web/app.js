/* ==========================================================================
 * 统一知识库页面前端逻辑
 *   §0 配置与状态        §1 通用工具        §2 图片解析与渲染
 *   §3 AI 对话（API + SSE）                 §4 知识库导入（上传 + 轮询）
 *   §5 事件绑定与初始化
 *
 * 后端服务保持独立：
 *   query_server  -> http://127.0.0.1:8001   /health /query /stream/{sid}
 *                                            /history/{sid} (GET/DELETE)
 *   import_server -> http://127.0.0.1:8000   /upload /status/{task_id}
 * 本文件不修改任何 API 路径、请求参数与返回结构。
 * ========================================================================== */

/* ==========================================================================
 * §0 配置与全局状态
 * ========================================================================== */

/**
 * 解析服务地址：默认使用显式常量。
 * 兼容两种额外场景：
 *   1) 页面恰好由某个后端 /html 端点提供（端口 8000/8001）时，优先同源，保持原有行为；
 *   2) 其他端口（如本地静态服务器）打开页面时，回退到约定的 127.0.0.1 端口。
 */
function resolveApi(defaultUrl, servicePort){
  const { protocol, hostname, port } = location;
  if((protocol === 'http:' || protocol === 'https:')
     && (hostname === '127.0.0.1' || hostname === 'localhost')
     && (port === '8000' || port === '8001')){
    return `${protocol}//${hostname}:${servicePort}`;
  }
  return defaultUrl;
}

const QUERY_API  = resolveApi('http://127.0.0.1:8001', 8001);
const IMPORT_API = resolveApi('http://127.0.0.1:8000', 8000);

const HEALTH_INTERVAL_MS   = 5000;   // 健康检查间隔
const IMPORT_POLL_MS       = 2000;   // 导入任务轮询间隔
const SSE_RETRY_LIMIT      = 3;      // SSE 首事件前最大重连次数（修 create_sse_queue 竞态）
const SSE_RETRY_DELAY_MS   = 500;

const state = {
  sessionId: '',
  sending: false,
  tasks: new Map(),   // taskId -> 上传任务运行时信息
  files: new Map(),   // cardId -> { name, status, taskId }
};

/* ==========================================================================
 * §1 通用工具
 * ========================================================================== */

function $(id){ return document.getElementById(id); }

function escapeHtml(str){
  return String(str == null ? '' : str)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function nowTime(){
  const d = new Date();
  return d.toLocaleTimeString('zh-CN', { hour:'2-digit', minute:'2-digit' });
}

function formatTime(ts){
  if(!ts) return nowTime();
  const d = new Date(Number(ts) * 1000);
  if(Number.isNaN(d.getTime())) return nowTime();
  return d.toLocaleTimeString('zh-CN', { hour:'2-digit', minute:'2-digit' });
}

function dedupeKeepOrder(arr){
  const seen = new Set();
  const out = [];
  for(const x of (Array.isArray(arr) ? arr : [])){
    const v = String(x || '');
    if(!v) continue;
    if(seen.has(v)) continue;
    seen.add(v);
    out.push(v);
  }
  return out;
}

function genId(prefix){
  return `${prefix}-${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`;
}

function formatSize(bytes){
  const n = Number(bytes) || 0;
  if(n < 1024) return `${n} B`;
  if(n < 1024 * 1024) return `${(n / 1024).toFixed(2)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

/** 从后端错误响应中提取可读信息（FastAPI 通常返回 {"detail": "..."}）。 */
async function readErrorMessage(res){
  try{
    const text = await res.text();
    if(!text) return `HTTP ${res.status}`;
    try{
      const data = JSON.parse(text);
      if(typeof data?.detail === 'string') return data.detail;
      if(Array.isArray(data?.detail)) return JSON.stringify(data.detail);
      if(typeof data?.message === 'string') return data.message;
      return text;
    }catch(_){
      return text;
    }
  }catch(e){
    return `HTTP ${res.status}`;
  }
}

/* ==========================================================================
 * §2 图片解析与渲染（逻辑沿用原 chat.html）
 * ========================================================================== */

function isImageUrl(url){
  try{
    const u = new URL(url);
    return /\.(png|jpe?g|gif|webp|bmp|svg)$/i.test(u.pathname);
  }catch(e){
    // new URL 失败（相对路径/非法字符）时退回正则匹配
    return /\.(png|jpe?g|gif|webp|bmp|svg)(\?|#|$)/i.test(url || '');
  }
}

function normalizeUrl(rawUrl){
  const s = String(rawUrl || '').trim();
  if(!s) return '';
  // 尽量保留原始 URL，仅编码空格，避免 img src 失败
  return s.replace(/\s/g, '%20');
}

function extractUrlsLoose(text){
  const s = String(text || '');
  const regex = /(https?:\/\/[^\s]+)/g;
  const matches = s.match(regex) || [];
  const urls = [];

  const trimTailPunct = (u) => String(u || '').replace(/[)\]}'">，。,;；\]】）＞]+$/g, '');
  const trimHeadPunct = (u) => String(u || '').replace(/^[<([{'"]+|^[＜（【\[]+/g, '');

  for(const m of matches){
    const u = trimHeadPunct(trimTailPunct(m));
    if(u) urls.push(u);
  }
  return dedupeKeepOrder(urls);
}

function parseImagesFromTextLoosely(text){
  return dedupeKeepOrder(extractUrlsLoose(text).filter(isImageUrl));
}

function findLastImageMarkerIndex(raw){
  const s = String(raw || '');
  // 支持：【图片】、【 图片 】、[图片]、[ 图片 ]
  const re = /【\s*图片\s*】|\[\s*图片\s*\]/g;
  let m;
  let lastIdx = -1;
  let lastLen = 0;
  while((m = re.exec(s)) !== null){
    lastIdx = m.index;
    lastLen = m[0].length;
  }
  return { idx: lastIdx, len: lastLen };
}

function parseAnswerAndImages(text){
  const raw = String(text || '');
  const { idx, len } = findLastImageMarkerIndex(raw);
  if(idx === -1) return { text: raw, images: [] };

  const before = raw.slice(0, idx).trimEnd();
  const after = raw.slice(idx + len).trim();
  const urls = [];

  // 优先按“每行一个 URL”解析，允许 URL 中包含空格
  const lines = after.split(/\r?\n/).map(l => l.trim()).filter(Boolean);
  for(const line of lines){
    if(line.startsWith('http://') || line.startsWith('https://')){
      urls.push(line);
    }else{
      // 兼容同一行包含多个 URL 的情况
      for(const u of extractUrlsLoose(line)) urls.push(u);
    }
  }

  const seen = new Set();
  const images = [];
  for(const u of urls){
    const normalized = normalizeUrl(u);
    if(!isImageUrl(normalized)) continue;
    if(seen.has(normalized)) continue;
    seen.add(normalized);
    images.push(normalized);
  }
  return { text: before, images };
}

function shouldShowImagesByAnswer(answerText){
  const t = String(answerText || '');
  const keywords = [
    '如图', '如下图', '见图', '见下图', '下图', '上图',
    '图片', '示意图', '结构图', '外观', '接线图', '电路图', '原理图', '安装图', '尺寸图', '截图'
  ];
  return keywords.some(k => t.includes(k));
}

/**
 * 渲染答案文本 + 图片。
 * 图片来源取并集：Block 显式标记【图片】 > 后端候选 image_urls > 文本宽松提取。
 *
 * 注意：流式过程中反复调用本函数时传 markdown=false（纯 textContent），
 * 避免半截标记导致列表/表格结构反复重建；只有最终帧传 markdown=true。
 */
function renderAnswerWithImages(containerEl, answerText, candidateImageUrls, markdown){
  const { text, images: imagesFromBlock } = parseAnswerAndImages(answerText);
  const candidates = Array.isArray(candidateImageUrls)
    ? candidateImageUrls.map(normalizeUrl).filter(isImageUrl)
    : [];
  const hasBlockImages = imagesFromBlock && imagesFromBlock.length > 0;
  const looseImages = extractUrlsLoose(answerText).map(normalizeUrl).filter(isImageUrl);

  const allImages = new Set([
    ...(hasBlockImages ? imagesFromBlock : []),
    ...candidates,
    ...looseImages
  ]);
  const images = Array.from(allImages);
  const body = (text || '').trim().length > 0 ? text : '（已完成，但未返回答案）';

  containerEl.textContent = '';

  const textEl = document.createElement('div');
  textEl.className = 'answer-text';
  if(markdown){
    textEl.classList.add('md-body');
    textEl.innerHTML = renderMarkdown(body);
  }else{
    textEl.textContent = body;
  }
  containerEl.appendChild(textEl);

  if(images.length > 0){
    const imgWrap = document.createElement('div');
    imgWrap.className = 'answer-images';
    for(const url of images){
      const safeUrl = normalizeUrl(url);
      const img = document.createElement('img');
      img.loading = 'lazy';
      img.src = safeUrl;
      img.alt = '参考图片';
      img.referrerPolicy = 'no-referrer';
      img.addEventListener('error', () => { img.style.display = 'none'; });
      imgWrap.appendChild(img);

      const link = document.createElement('a');
      link.href = safeUrl;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = url;
      imgWrap.appendChild(link);
    }
    containerEl.appendChild(imgWrap);
  }
}

/* ==========================================================================
 * §3 主体确认选项卡片
 *
 * 后端在「无法确定唯一商品型号」时会给出候选列表：
 *   - 结构化字段 option_item_names（优先使用）
 *   - 同时保留 answer 文案 "...选择要咨询的产品: ['A', 'B']"（兜底解析）
 * 两路都识别不到时，本功能静默退化为普通文本，绝不影响聊天主流程。
 * ========================================================================== */

const OPTION_ANSWER_PREFIX = '请在以下选项中选择要咨询的产品';
const NO_ITEM_ANSWER_PREFIX = '无法识别您要咨询的产品';
/** 匹配「前缀 + 冒号 + 方括号清单」，用于从展示文本中去掉选项清单本身。 */
const OPTION_LIST_PATTERN = /请在以下选项中选择要咨询的产品\s*[:：]?\s*\[[\s\S]*?\]/;

/**
 * 候选集签名：排序后拼接，与顺序无关。
 *
 * 防死循环的判定以 DOM 中的卡片为唯一事实源（而不是内存里的 Set），
 * 这样「清空对话」「删除消息」等操作不会让记录与实际界面脱节。
 */
function optionSetSignature(names){
  return names.map(n => String(n).trim()).filter(Boolean).sort().join('\u0001');
}

/**
 * 当前界面里是否已存在「同一候选集且仍可点击」的卡片。
 * 命中说明该候选集已经问过用户而用户尚未做出选择，应避免重复给出。
 *
 * @param {string} signature 候选集签名
 * @returns {boolean}
 */
function hasLiveCardForSignature(signature){
  const cards = chatEl.querySelectorAll('.choice-card');
  for(const card of cards){
    if(card.classList.contains('is-disabled')) continue;
    if(card.getAttribute('data-opt-sig') === signature) return true;
  }
  return false;
}

/**
 * 从 answer 文案中兜底解析候选列表。
 * 仅用于兼容「后端未提供结构化字段」的场景（旧版服务或旧历史记录）。
 *
 * @param {string} text 形如 "请在以下选项中选择要咨询的产品: ['A', 'B']"
 * @returns {string[]} 解析出的候选名称；无法解析时返回空数组
 */
function parseOptionsFromAnswer(text){
  const raw = String(text == null ? '' : text);
  if(!raw) return [];
  // 必须完整锚定前缀：后端改了文案就自动退化为普通文本，而不是错误解析
  const at = raw.indexOf(OPTION_ANSWER_PREFIX);
  if(at === -1) return [];
  let rest = raw.slice(at + OPTION_ANSWER_PREFIX.length);
  const colon = rest.indexOf(':');
  const full = rest.indexOf('：');
  const cut = colon === -1 ? full : (full === -1 ? colon : Math.min(colon, full));
  if(cut !== -1) rest = rest.slice(cut + 1);
  rest = rest.trim().replace(/^\[/, '').replace(/\]\s*$/, '');
  if(!rest) return [];
  // 优先按引号切分，可正确处理名称内部含逗号的情况
  const quoted = rest.match(/'([^']*)'|"([^"]*)"/g);
  if(quoted && quoted.length > 0){
    return dedupeKeepOrder(quoted.map(q => q.slice(1, -1).trim()).filter(Boolean));
  }
  return dedupeKeepOrder(rest.split(/[,，]/).map(s => s.trim()).filter(Boolean));
}

/** 是否属于「无法识别产品」的兜底回答（此类回答应引导重新描述，而非给选项）。 */
function isNoItemAnswer(text){
  return String(text == null ? '' : text).includes(NO_ITEM_ANSWER_PREFIX);
}

/**
 * 统一获取候选列表：结构化字段优先，answer 文案兜底。
 *
 * @param {object|null} data
 * @param {string[]|null} data.structured       后端结构化候选字段（优先）
 * @param {string} data.answer                  答案文本（兜底解析来源）
 * @param {boolean} [data.skipTextFallback]     true 时不做文案兜底解析（用于防死循环）
 * @returns {string[]} 候选名称列表
 */
function extractOptions(data){
  const d = data || {};
  const raw = d.structured;
  if(Array.isArray(raw)){
    const structured = dedupeKeepOrder(raw.map(n => String(n == null ? '' : n).trim()).filter(Boolean));
    if(structured.length > 0) return structured;
  }
  if(d.skipTextFallback) return [];
  return parseOptionsFromAnswer(d.answer);
}

/** 构建候选卡片。 */
function makeChoiceCard(names, { disabled, selected, stale, question }){
  const wrap = document.createElement('div');
  wrap.className = 'choice-card';
  if(disabled) wrap.classList.add('is-disabled');
  if(selected) wrap.classList.add('is-selected');
  wrap.setAttribute('role', 'group');
  wrap.setAttribute('aria-label', '请选择要咨询的产品');
  // 记录本卡片对应的用户原问题：选中后仍要带着意图去检索
  if(question) wrap.setAttribute('data-question', String(question));

  const hint = document.createElement('div');
  hint.className = 'choice-hint';
  hint.textContent = selected
    ? '已选择：'
    : (stale ? '该选项已失效，请重新提问：' : '请选择要咨询的产品：');
  wrap.appendChild(hint);

  const list = document.createElement('div');
  list.className = 'choice-list';

  for(const name of names){
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'choice-btn';
    btn.textContent = name;
    if(selected && name === selected) btn.classList.add('is-chosen');
    if(disabled) btn.disabled = true;
    btn.addEventListener('click', (ev) => {
      ev.preventDefault();
      if(btn.disabled) return;
      onChoicePick(name, wrap);
    });
    list.appendChild(btn);
  }
  wrap.appendChild(list);
  return wrap;
}

/**
 * 在一条机器人消息里渲染候选卡片。返回是否真的渲染了卡片。
 *
 * @param {HTMLElement} botMsgEl 机器人消息元素
 * @param {string[]} names       候选名称
 * @param {object} [opts]        { stale:boolean }
 */
function renderChoiceCard(botMsgEl, names, opts){
  if(!botMsgEl || !Array.isArray(names) || names.length === 0) return false;
  const body = botMsgEl.querySelector('.msg-body');
  if(!body) return false;

  const old = body.querySelector('.choice-card');
  if(old) old.remove();

  const stale = !!(opts && opts.stale);
  const card = makeChoiceCard(names, {
    disabled: stale,
    selected: '',
    stale,
    question: opts && opts.question,
  });
  // 卡片挂在气泡之后：既保持与消息的关联，又不被气泡配色影响
  const bubble = body.querySelector('.bubble');
  if(bubble) body.insertBefore(card, bubble.nextSibling);
  else body.appendChild(card);
  scrollToBottom();
  return card;
}

/** 把聊天里除最后一条之外的所有候选卡片置为失效，避免点击过期选项。 */
function markStaleChoiceCards(){
  const cards = Array.from(chatEl.querySelectorAll('.choice-card'));
  if(cards.length <= 1) return;
  for(let i = 0; i < cards.length - 1; i++){
    const card = cards[i];
    if(card.classList.contains('is-disabled')) continue;
    card.classList.add('is-disabled');
    const hint = card.querySelector('.choice-hint');
    if(hint) hint.textContent = '该选项已失效，请重新提问：';
    card.querySelectorAll('.choice-btn').forEach(b => { b.disabled = true; });
  }
}

/** 用户点击某个候选：标记该卡片，并以选中型号「直选」重新发起查询。 */
function onChoicePick(name, cardEl){
  if(cardEl){
    cardEl.classList.add('is-disabled', 'is-selected');
    const hint = cardEl.querySelector('.choice-hint');
    if(hint) hint.textContent = '已选择：';
    cardEl.querySelectorAll('.choice-btn').forEach(b => {
      b.disabled = true;
      b.classList.toggle('is-chosen', b.textContent === name);
    });
  }
  // 关键：提问文本必须是「用户原问题」本身，型号只放在 forceItemNames 里。
  // 「型号 + 原问题」的拼接统一由后端完成（见 apply_forced_item_name）；
  // 前端若也拼一遍，就会得到「型号 型号 原问题」这种重复文本。
  const pendingQuestion = readPendingQuestion(cardEl);
  const queryText = pendingQuestion || name;   // 拿不到原问题时退回型号名
  onSend(queryText, { fromChoice: true, forceItemNames: [name], displayText: name });
}

/**
 * 读取候选卡片所属的「用户原问题」。
 *
 * 优先取卡片上记录的值（实时交互时由 presentChoiceCard 写入）；
 * 历史回放场景下卡片由 addBotMsgWithTime 重建，此时向上寻找最近的一条用户消息。
 *
 * @param {HTMLElement|null} cardEl 候选卡片元素
 * @returns {string} 用户原问题；找不到时返回空串
 */
function readPendingQuestion(cardEl){
  if(!cardEl) return '';
  const recorded = cardEl.getAttribute('data-question');
  if(recorded) return recorded.trim();

  // 兜底：向上遍历，找该卡片之前最近的一条用户消息
  let node = cardEl;
  while(node){
    let sib = node.previousElementSibling;
    while(sib){
      if(sib.classList && sib.classList.contains('msg') && sib.classList.contains('user')){
        const bubble = sib.querySelector('.bubble');
        return bubble ? bubble.textContent.trim() : '';
      }
      sib = sib.previousElementSibling;
    }
    node = node.parentElement;
  }
  return '';
}

/* ==========================================================================
 * §4 Markdown 渲染（自包含，不依赖第三方库）
 *
 * 后端 LLM 答案天然是 Markdown（**加粗**、- 列表、### 标题、表格、代码块等），
 * 直接 textContent 输出会把标记符号原样显示，因此这里做一次受控渲染。
 *
 * 安全约定（LLM 输出属于不可信内容）：
 *   1) 先整体转义 HTML，再做标记替换，因此原文中的 < > & 不可能变成标签；
 *   2) 链接协议白名单：仅 http / https / mailto 与站内相对路径，阻断 javascript: 等；
 *   3) 所有插入的标签都由本模块自己拼出，不使用任何未转义的用户输入。
 * ========================================================================== */

/** 提取代码块并替换为占位符，返回替换后的文本与占位符映射。 */
function extractFencedCode(src){
  const store = [];
  const text = String(src || '').replace(/^[ \t]*```([^\n`]*)\n?([\s\S]*?)^[ \t]*```[ \t]*$/gm, (m, lang, code) => {
    const cls = String(lang || '').trim().replace(/[^\w+#-]/g, '');
    const clsAttr = cls ? ` class="language-${cls}"` : '';
    store.push(`<pre class="md-pre"><code${clsAttr}>${escapeHtml(code.replace(/\n$/, ''))}</code></pre>`);
    return `\u0000CODE${store.length - 1}\u0000`;
  });
  return { text, store };
}

/** 去掉 Markdown 行内标记，用于 linkify 时避免误判（如 `[a](url)` 里的 url）。 */
function stripInlineMarkup(s){
  return String(s || '')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/!?\[([^\]]*)\]\(([^)\s]+)\)/g, '$1');
}

/** 链接协议白名单校验。 */
function isSafeHref(url){
  const u = String(url || '').trim();
  if(!u) return false;
  if(/^(https?:|mailto:)/i.test(u)) return true;
  // 站内相对路径（/foo、./foo、#anchor），排除 //host 形式的协议相对 URL
  if(/^[#./]/.test(u) && !/^\/\//.test(u)) return true;
  return false;
}

/** 行内标记：转义 → 代码 → 链接 → 裸链接 → 加粗 → 斜体 → 删除线。 */
function renderInline(raw){
  if(!raw) return '';
  // 关键：先转义，再拼装标签。因此块级标记（> # -）的识别必须在调用本函数之前完成，
  // 否则 > 会先变成 &gt; 而无法被引用块规则匹配。
  let s = escapeHtml(raw);
  const store = [];
  // 占位符以 \u0000 包裹，确保不会被后续 *_~ 等规则再次处理
  const keep = (html) => {
    store.push(html);
    return `\u0000INLINE${store.length - 1}\u0000`;
  };

  // 1) 行内代码：内容原样（含 HTML）呈现，避免被其他规则改写
  s = s.replace(/`([^`\n]+)`/g, (m, code) => keep(`<code>${escapeHtml(code)}</code>`));

  // 2) Markdown 链接
  s = s.replace(/\[([^\]\n]*)\]\(\s*([^)\s]+)\s*\)/g, (m, label, url) => {
    if(!isSafeHref(url)) return label;
    return keep(`<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${label || url}</a>`);
  });

  // 3) 裸链接自动成链
  //    字符类排除 & 与 < > " ）】 ：此时文本已转义，排除 & 可避免把 &quot; 这类
  //    实体吞进 URL；排除 < > 则保证不会跨越已有标签。
  s = s.replace(/https?:\/\/[^\s&<>"）】]+/g, (u) => keep(`<a href="${escapeHtml(u)}" target="_blank" rel="noopener noreferrer">${u}</a>`));

  // 4) 加粗 **x** / __x__
  s = s.replace(/\*\*([^\n]+?)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/__([^\n]+?)__/g, '<strong>$1</strong>');

  // 5) 斜体（下划线要求两侧非单词字符，避免误伤 file_name 这类标识符）
  s = s.replace(/(^|[^*])\*([^*\n]+?)\*(?!\*)/g, '$1<em>$2</em>');
  s = s.replace(/(^|[\s(（[【])\_([^_\n]+?)\_(?=[\s)）\]】.,!?;:、。，！？；：]|$)/g, '$1<em>$2</em>');

  // 6) 删除线
  s = s.replace(/~~([^\n]+?)~~/g, '<del>$1</del>');

  return s.replace(/\u0000INLINE(\d+)\u0000/g, (m, i) => store[Number(i)]);
}

/** 解析表格分隔行（|---|:--:|），非表格返回 null。 */
function parseTableAlign(sepLine){
  const s = String(sepLine || '').trim();
  if(!/^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$/.test(s)) return null;
  return s.replace(/^\||\|$/g, '').split('|').map((c) => {
    const t = c.trim();
    if(/^:-+:$/.test(t)) return 'center';
    if(/-+:$/.test(t)) return 'right';
    if(/^:-+/.test(t)) return 'left';
    return '';
  });
}

/** 拆表格行单元格，兼容首尾可选的竖线。 */
function splitTableRow(line){
  return String(line || '').trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
}

/** 块级渲染：标题 / 表格 / 引用 / 列表 / 分隔线 / 段落。 */
function renderBlocks(text){
  const out = [];
  const lines = String(text || '').split(/\r?\n/);
  let i = 0;

  while(i < lines.length){
    const line = lines[i];

    if(/^\s*$/.test(line)){ i++; continue; }

    // 代码块占位符
    const codeOnly = line.match(/^\u0000CODE(\d+)\u0000$/);
    if(codeOnly){ out.push(`\u0000CODE${codeOnly[1]}\u0000`); i++; continue; }

    // 分隔线
    if(/^\s*([-*_])\s*(\1\s*){2,}$/.test(line)){ out.push('<hr />'); i++; continue; }

    // 标题
    const heading = line.match(/^\s{0,3}(#{1,6})\s+(.*)$/);
    if(heading){
      const level = heading[1].length;
      out.push(`<h${level}>${renderInline(heading[2].trim().replace(/\s+#+\s*$/, ''))}</h${level}>`);
      i++; continue;
    }

    // 表格：当前行含 | 且下一行是分隔行
    if(line.includes('|') && i + 1 < lines.length){
      const align = parseTableAlign(lines[i + 1]);
      if(align){
        const head = splitTableRow(line);
        const body = [];
        i += 2;
        while(i < lines.length && lines[i].includes('|') && !/^\s*$/.test(lines[i])){
          body.push(splitTableRow(lines[i]));
          i++;
        }
        const th = head.map((c, k) => {
          const a = align[k] ? ` style="text-align:${align[k]}"` : '';
          return `<th${a}>${renderInline(c)}</th>`;
        }).join('');
        const trs = body.map((cells) => {
          const tds = head.map((_, k) => {
            const a = align[k] ? ` style="text-align:${align[k]}"` : '';
            return `<td${a}>${renderInline(cells[k] || '')}</td>`;
          }).join('');
          return `<tr>${tds}</tr>`;
        }).join('');
        out.push(`<div class="md-table-wrap"><table><thead><tr>${th}</tr></thead><tbody>${trs}</tbody></table></div>`);
        continue;
      }
    }

    // 引用块（可连续多行）
    if(/^\s{0,3}>\s?/.test(line)){
      const buf = [];
      while(i < lines.length && /^\s{0,3}>\s?/.test(lines[i])){
        buf.push(lines[i].replace(/^\s{0,3}>\s?/, ''));
        i++;
      }
      out.push(`<blockquote>${renderInline(buf.join('\n')).replace(/\n/g, '<br />')}</blockquote>`);
      continue;
    }

    // 列表（有序 / 无序，按缩进还原嵌套结构）
    const listMatch = line.match(/^([ \t]*)([-*+]|\d+[.)])\s+(.*)$/);
    if(listMatch){
      const ordered = /^\d/.test(listMatch[2]);
      const baseIndent = listMatch[1].replace(/\t/g, '  ').length;
      // 每层收集若干条目，每个条目记录自己的续行
      const items = [];
      let cur = null;
      while(i < lines.length){
        const m = lines[i].match(/^([ \t]*)([-*+]|\d+[.)])\s+(.*)$/);
        if(m){
          const indent = m[1].replace(/\t/g, '  ').length;
          const isOrdered = /^\d/.test(m[2]);
          if(indent > baseIndent){
            // 更深缩进：作为上一个条目的子内容
            if(cur === null) break;
            const sub = m[3];
            const subTag = isOrdered ? 'ol' : 'ul';
            const lastChild = cur.children[cur.children.length - 1];
            if(lastChild && lastChild.tag === subTag){
              lastChild.items.push(sub);
            }else{
              cur.children.push({ tag: subTag, items: [sub] });
            }
            i++;
            continue;
          }
          if(indent < baseIndent || isOrdered !== ordered) break;
          cur = { text: m[3], children: [] };
          items.push(cur);
          i++;
          continue;
        }
        // 普通续行归属上一个条目
        if(cur !== null && /^\s+\S/.test(lines[i]) && !/^\s*$/.test(lines[i])){
          cur.text += `\n${lines[i].trim()}`;
          i++;
          continue;
        }
        break;
      }
      const renderItem = (it) => {
        let inner = renderInline(it.text).replace(/\n/g, '<br />');
        for(const child of it.children){
          inner += `<${child.tag}>${child.items.map((t) => `<li>${renderInline(t)}</li>`).join('')}</${child.tag}>`;
        }
        return `<li>${inner}</li>`;
      };
      const tag = ordered ? 'ol' : 'ul';
      out.push(`<${tag}>${items.map(renderItem).join('')}</${tag}>`);
      continue;
    }

    // 段落（连续非空行合并）
    const para = [line];
    i++;
    while(i < lines.length && !/^\s*$/.test(lines[i])
          && !/^\s{0,3}(#{1,6})\s+/.test(lines[i])
          && !/^\s{0,3}>\s?/.test(lines[i])
          && !/^(\s*)([-*+]|\d+[.)])\s+/.test(lines[i])
          && !/^\u0000CODE\d+\u0000$/.test(lines[i])
          && !/^\s*([-*_])\s*(\1\s*){2,}$/.test(lines[i])){
      para.push(lines[i]);
      i++;
    }
    out.push(`<p>${renderInline(para.join('\n')).replace(/\n/g, '<br />')}</p>`);
  }

  return out.join('\n');
}

/**
 * 将 Markdown 文本渲染为安全 HTML。
 *
 * 处理顺序：抽出代码块 → 块级解析（此时文本仍是原文，标记可被正确识别）
 *          → 各叶子节点在 renderInline 内统一转义 → 回填代码块。
 *
 * @param {string} src LLM 返回的答案文本。
 * @returns {string} HTML 片段；所有外来文本均已转义，可安全写入 innerHTML。
 */
function renderMarkdown(src){
  const { text, store } = extractFencedCode(src);
  let html;
  try{
    html = renderBlocks(text);
  }catch(e){
    // 渲染异常时退回纯文本，绝不把异常暴露到页面上
    console.error('Markdown 渲染失败，已退回纯文本', e);
    const safe = escapeHtml(text);
    html = `<p>${safe.replace(/\n/g, '<br />')}</p>`;
  }
  return html.replace(/\u0000CODE(\d+)\u0000/g, (m, i) => store[Number(i)] || '');
}

/* ==========================================================================
 * §5 AI 对话：渲染
 * ========================================================================== */

const chatEl      = $('chat');
const inputEl     = $('input');
const sendBtn     = $('send');
const btnClear    = $('btnClear');
const streamToggle= $('streamToggle');
const sessionInfo = $('sessionInfo');
const queryApiPill  = $('queryApiPill');
const importApiPill = $('importApiPill');

function scrollToBottom(){
  chatEl.scrollTop = chatEl.scrollHeight;
}

function addUserMsg(text, ts, displayText){
  const shown = (typeof displayText === 'string' && displayText) ? displayText : text;
  const html = `
    <div class="msg user">
      <div>
        <div class="bubble">${escapeHtml(shown)}</div>
        <div class="meta">${ts ? formatTime(ts) : nowTime()}</div>
      </div>
      <div class="avatar">我</div>
    </div>
  `;
  chatEl.insertAdjacentHTML('beforeend', html);
  scrollToBottom();
}

function addBotMsgWithTime(text, ts, imageUrls, options, question){
  const id = genId('bot-his');
  const html = `
    <div class="msg bot" id="${id}">
      <div class="avatar bot">AI</div>
      <div class="msg-body">
        <div class="bubble"><div class="answer"></div></div>
        <div class="meta">${ts ? formatTime(ts) : nowTime()}</div>
      </div>
    </div>
  `;
  chatEl.insertAdjacentHTML('beforeend', html);
  const el = document.getElementById(id);
  if(el){
    // 历史记录是完整答案，直接按 Markdown 渲染
    renderAnswerWithImages(el.querySelector('.answer'), text || '', imageUrls || [], true);
    // 历史回放时重建候选卡片（后端已持久化 option_item_names）
    if(Array.isArray(options) && options.length > 0){
      // 历史里的这条助手消息没有直接存"用户原问题"，由 readPendingQuestion 向上回溯
      renderChoiceCard(el, options, { stale: false, question: question || '' });
    }
  }
  scrollToBottom();
}

/** 创建一个带“打字动画 + 阶段进度”的机器人消息骨架，返回其 DOM。 */
function addBotMsgSkeleton(){
  const id = genId('bot');
  const html = `
    <div class="msg bot" id="${id}">
      <div class="avatar bot">AI</div>
      <div class="msg-body">
        <div class="bubble">
          <span class="typing"><span class="dot"></span><span class="dot"></span><span class="dot"></span></span>
          <details class="progress" open>
            <summary>阶段进度（等待中）</summary>
            <ul></ul>
          </details>
        </div>
        <div class="meta">${nowTime()}</div>
      </div>
    </div>
  `;
  chatEl.insertAdjacentHTML('beforeend', html);
  scrollToBottom();
  return document.getElementById(id);
}

const STATUS_TEXT = {
  processing: '处理中',
  completed : '已完成',
  failed    : '失败',
  pending   : '等待中',
};

function renderProgress(botMsgEl, doneList, runningList, status){
  if(!botMsgEl) return;
  const details = botMsgEl.querySelector('details.progress');
  if(!details) return;
  const summary = details.querySelector('summary');
  const ul = details.querySelector('ul');
  const done = Array.isArray(doneList) ? doneList : [];
  const running = Array.isArray(runningList) ? runningList : [];

  const displayStatus = STATUS_TEXT[status] || status || 'unknown';
  summary.textContent = `阶段进度（已完成${done.length}，进行中${running.length}，状态：${displayStatus}）`;

  const lines = [
    ...done.map(x => `✅ ${x}`),
    ...running.map(x => `⏳ ${x}`)
  ];
  ul.innerHTML = '';
  if(lines.length === 0){
    ul.insertAdjacentHTML('beforeend', '<li>暂无进度</li>');
    return;
  }
  for(const line of lines){
    ul.insertAdjacentHTML('beforeend', `<li>${escapeHtml(line)}</li>`);
  }
}

/**
 * 结束骨架消息：渲染最终答案或错误；进度面板收起并保留在气泡内。
 *
 * @param {HTMLElement} botMsgEl
 * @param {string} answer      答案文本
 * @param {string} error       错误信息
 * @param {string[]} imageUrls 候选图片
 * @param {object[]} [options] 后端给出的候选商品列表（结构化字段优先，缺失时按文案兜底）
 */
function finalizeBotAnswer(botMsgEl, answer, error, imageUrls, options, originalQuery){
  if(!botMsgEl) return;
  const bubble = botMsgEl.querySelector('.bubble');
  const progress = botMsgEl.querySelector('details.progress');
  const err = (error || '').trim();

  if(progress) progress.removeAttribute('open');   // 完成后自动收起
  bubble.textContent = '';

  const answerEl = document.createElement('div');
  answerEl.className = 'answer';
  bubble.appendChild(answerEl);

  if(err){
    answerEl.textContent = `抱歉，本次处理失败：\n${err}`;
  }else{
    // 最终帧：按 Markdown 渲染（答案完整，标记不会残缺）
    renderAnswerWithImages(answerEl, answer || '', imageUrls || [], true);
  }

  if(progress) bubble.appendChild(progress);

  // 候选卡片：仅在没有错误时渲染
  if(!err){
    presentChoiceCard(botMsgEl, answer || '', options, originalQuery);
  }

  scrollToBottom();
}

/**
 * 渲染候选卡片的统一入口（含死循环保护）。
 *
 * 保护策略：同一候选集在一次会话里只给出一次可点击卡片。若用户已经点选过、
 * 后端仍然返回同一组候选（说明该型号分数仍落在候选区间），则不再重复给出按钮，
 * 而是补一句提示，避免「点了又让选」的死循环。
 *
 * @param {HTMLElement} botMsgEl
 * @param {string} answerText
 * @param {object[]} [options]
 */
function presentChoiceCard(botMsgEl, answerText, options, originalQuery){
  const maybeOptions = Array.isArray(options) ? options : null;
  const names = extractOptions({
    structured: maybeOptions,
    answer: answerText,
    // 后端已明确告知「没有候选」时不再尝试文案兜底，避免把兜底话术误解析成选项
    skipTextFallback: maybeOptions === null && isNoItemAnswer(answerText),
  });
  if(names.length === 0) return;

  // 候选已能用按钮表达，就不必再重复打印一句「请在以下选项中选择…['A','B']」。
  // 仅在确实解析出候选时才隐藏原文案；解析不出来时保留原文，避免丢信息。
  hideOptionListText(botMsgEl, answerText);

  const signature = optionSetSignature(names);
  if(hasLiveCardForSignature(signature)){
    // 同一候选集已给出且用户尚未选择：不再重复给按钮，避免「点了又让选」
    const tip = document.createElement('div');
    tip.className = 'choice-note';
    tip.textContent = '以上候选仍无法唯一定位该产品，请输入更完整的型号，或直接描述你的问题。';
    const body = botMsgEl.querySelector('.msg-body');
    if(body) body.appendChild(tip);
    scrollToBottom();
    return;
  }

  const card = renderChoiceCard(botMsgEl, names, { stale: false, question: originalQuery });
  if(card) card.setAttribute('data-opt-sig', signature);
}

/**
 * 把答案文本里的「请在以下选项中选择…: ['A','B']」这类选项清单从界面上隐去。
 *
 * 只在文案里除了选项清单之外还有别的内容时保留其余部分；若整段就是选项清单，
 * 则完全清空文本块（留出选择卡片本身）。解析不出选项时不做任何改动。
 *
 * @param {HTMLElement} botMsgEl 机器人消息元素
 * @param {string} answerText    原始答案文本
 */
function hideOptionListText(botMsgEl, answerText){
  const raw = String(answerText == null ? '' : answerText);
  const at = raw.indexOf(OPTION_ANSWER_PREFIX);
  if(at === -1) return;   // 不是选项提示文案，不动

  const answerEl = botMsgEl.querySelector('.answer-text');
  if(!answerEl) return;

  // 前缀 + 紧随的冒号 + 方括号清单，整体去掉；保留清单之前可能存在的说明文字
  const rest = raw
    .slice(0, at)
    .concat(raw.slice(at).replace(OPTION_LIST_PATTERN, ''))
    .trim();

  if(rest){
    // 文案里还有别的说明，仅去掉选项清单部分
    answerEl.classList.add('md-body');
    answerEl.innerHTML = renderMarkdown(rest);
  }else{
    // 整段就是选项清单：清空文本块，只留卡片
    answerEl.textContent = '';
    answerEl.classList.add('is-empty');
  }
}

function setSending(flag){
  state.sending = flag;
  sendBtn.disabled = flag;
  sendBtn.textContent = flag ? '处理中' : '发送';
}

/* ==========================================================================
 * §6 AI 对话：API 与 SSE
 * ========================================================================== */

async function apiHealth(){
  const check = async (base, pill, label) => {
    try{
      const res = await fetch(`${base}/health`, { cache: 'no-store' });
      if(!res.ok) throw new Error(`HTTP ${res.status}`);
      pill.classList.add('is-ok');
      pill.classList.remove('is-err');
      pill.innerHTML = `<span class="dot-ind"></span>${label}：已连接`;
    }catch(e){
      pill.classList.add('is-err');
      pill.classList.remove('is-ok');
      pill.innerHTML = `<span class="dot-ind"></span>${label}：未连接`;
    }
  };
  await Promise.all([
    check(QUERY_API,  queryApiPill,  '查询服务'),
    check(IMPORT_API, importApiPill, '导入服务'),
  ]);
}

async function loadHistory(){
  try{
    const res = await fetch(`${QUERY_API}/history/${encodeURIComponent(state.sessionId)}`);
    if(!res.ok) return;
    const data = await res.json();
    const items = Array.isArray(data.items) ? data.items : [];
    if(items.length === 0) return;

    // 保留首条欢迎消息，其余先清空再渲染历史
    const nodes = Array.from(chatEl.querySelectorAll('.msg'));
    for(let i = 1; i < nodes.length; i++) nodes[i].remove();

    items.reverse();   // 后端按 ts 倒序返回，翻转为正序渲染
    for(const item of items){
      if(item.role === 'user'){
        addUserMsg(item.text || '', item.ts);
      }else{
        addBotMsgWithTime(item.text || '', item.ts, item.image_urls || [], item.option_item_names || []);
      }
    }
    // 只保留最后一条消息的候选可点，更早的全部失效
    markStaleChoiceCards();
    scrollToBottom();
  }catch(_){ /* 历史加载失败不影响主流程 */ }
}

async function submitQuery(text, isStream, forceItemNames){
  const body = {
    query: text,
    session_id: state.sessionId,
    is_stream: isStream
  };
  // 直选：把选定的标准型号显式告知后端，跳过主体确认环节的二次检索
  if(Array.isArray(forceItemNames) && forceItemNames.length > 0){
    body.force_item_names = forceItemNames;
  }
  const res = await fetch(`${QUERY_API}/query`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  if(!res.ok) throw new Error(await readErrorMessage(res));
  return await res.json();
}

/**
 * 建立 SSE 连接。
 * 后端 create_sse_queue 在 BackgroundTasks 中执行，与前端连接存在竞态：
 * 队列未就绪时 sse_generator 会立即结束流。因此在收到首个事件前做有限重连。
 */
function openQueryStream(sessionId, handlers){
  let closedByUs = false;
  let gotFirstEvent = false;
  let retries = 0;
  let es = null;
  let retryTimer = null;

  const cleanup = () => {
    closedByUs = true;
    if(retryTimer) clearTimeout(retryTimer);
    if(es) es.close();
  };

  const connect = () => {
    if(closedByUs) return;
    es = new EventSource(`${QUERY_API}/stream/${encodeURIComponent(sessionId)}`);

    const markAlive = () => { gotFirstEvent = true; };

    es.addEventListener('ready', (e) => {
      markAlive();
      handlers.onReady && handlers.onReady(e);
    });
    es.addEventListener('progress', (e) => { markAlive(); handlers.onProgress(e); });
    es.addEventListener('delta',    (e) => { markAlive(); handlers.onDelta(e); });
    es.addEventListener('final',    (e) => { markAlive(); handlers.onFinal(e); cleanup(); });
    es.addEventListener('final_answer', (e) => { markAlive(); handlers.onFinal(e); cleanup(); });

    // 注意：网络级错误也会触发 addEventListener('error')，需按“是否已收到事件”区分
    es.addEventListener('error', (e) => {
      if(closedByUs) return;
      let payload = null;
      if(e && typeof e.data === 'string' && e.data){
        try{ payload = JSON.parse(e.data); }catch(_){ payload = null; }
      }
      if(payload){                       // 后端推送的 SSEEvent.ERROR
        handlers.onServerError(payload.error || 'SSE 连接中断/失败');
        cleanup();
        return;
      }
      if(!gotFirstEvent && retries < SSE_RETRY_LIMIT){
        retries += 1;
        es.close();
        retryTimer = setTimeout(connect, SSE_RETRY_DELAY_MS);
        return;
      }
      handlers.onServerError('SSE 连接中断/失败');
      cleanup();
    });
  };

  connect();
  return { close: cleanup };
}

/**
 * 发起一次提问。
 *
 * @param {string} [overrideText] 直接指定提问内容（候选卡片点击时传入选中的型号）
 * @param {object} [opts]
 * @param {boolean} [opts.fromChoice] 是否由候选卡片触发（为真时不清理输入框）
 * @param {string[]} [opts.forceItemNames] 直选：选定的标准型号数组，随请求发送给后端，
 *                                    使其跳过主体确认环节的二次检索
 * @param {object}  [opts.injected]   测试钩子：注入后端响应以驱动同一套渲染链路，
 *                                    形如 { is_stream, post:{}, progress:{}, deltas:[], final:{} }。
 *                                    生产调用不传此参数。
 */
async function onSend(overrideText, opts){
  const text = (typeof overrideText === 'string' ? overrideText : (inputEl.value || '')).trim();
  if(!text || state.sending) return;
  const fromChoice = !!(opts && opts.fromChoice);
  const forceItemNames = (opts && Array.isArray(opts.forceItemNames)) ? opts.forceItemNames : null;
  // 用户气泡显示文本：直选时显示选中的型号（更直观），而实际发送的 query 仍带完整意图
  const displayText = (opts && typeof opts.displayText === 'string') ? opts.displayText : '';
  // 测试钩子：允许注入响应数据，使端到端验证无需真实后端（生产环境不传）
  const injected = (opts && opts.injected) || null;

  if(!fromChoice) inputEl.value = '';
  // 新的一轮对话开始：之前的候选卡片全部失效，避免点击过期选项
  markStaleChoiceCards();

  addUserMsg(text, undefined, displayText);
  const botMsgEl = addBotMsgSkeleton();
  setSending(true);

  const isStream = injected ? !!injected.is_stream : streamToggle.checked;

  try{
    const data = injected ? (injected.post || {}) : await submitQuery(text, isStream, forceItemNames);

    if(!isStream){
      // 非流式：{ session_id, message, answer, done_list, image_urls, option_item_names }
      const payload = injected ? (injected.final || {}) : data;
      renderProgress(botMsgEl, payload.done_list || [], [], 'completed');
      finalizeBotAnswer(botMsgEl, payload.answer, payload.error, payload.image_urls || [], payload.option_item_names || null, text);
      setSending(false);
      return;
    }

    // 流式：POST 只返回 { session_id, message }，答案通过 SSE 推送
    const sessionId = data.session_id || state.sessionId;
    const bubble = botMsgEl.querySelector('.bubble');
    let answerEl = bubble.querySelector('.answer');
    if(!answerEl){
      answerEl = document.createElement('div');
      answerEl.className = 'answer';
      bubble.insertBefore(answerEl, bubble.firstChild);
    }

    let rawAnswerText = '';
    const removeTyping = () => {
      const typing = botMsgEl.querySelector('.typing');
      if(typing) typing.remove();
    };

    const handlers = {
      onProgress: (e) => {
        try{
          const d = JSON.parse(e.data || '{}');
          renderProgress(botMsgEl, d.done_list, d.running_list, d.status);
          if(d && d.status === 'completed') removeTyping();   // 兜底，避免按钮一直禁用
        }catch(_){}
      },
      onDelta: (e) => {
        try{
          const d = JSON.parse(e.data || '{}');
          const delta = d.delta || '';
          if(!delta) return;
          removeTyping();
          rawAnswerText += delta;
          // 流式过程中的半截文本按纯文本渲染，最终帧再切换为 Markdown
          renderAnswerWithImages(answerEl, rawAnswerText, [], false);
          scrollToBottom();
        }catch(_){}
      },
      onFinal: (e) => {
        removeTyping();
        try{
          const d = JSON.parse(e.data || '{}');
          // 流式 delta 不含【图片】块，最终包 d.answer 才是完整答案，优先使用
          const finalText = (d && typeof d.answer === 'string' && d.answer.trim().length > 0)
            ? d.answer
            : (rawAnswerText || '');
          renderProgress(botMsgEl, (d.done_list || []), [], 'completed');
          finalizeBotAnswer(botMsgEl, finalText, d.error, d.image_urls || [], d.option_item_names || null, text);
        }catch(_){
          finalizeBotAnswer(botMsgEl, rawAnswerText, '', [], null, text);
        }
        setSending(false);
      },
      onServerError: (msg) => {
        removeTyping();
        rawAnswerText += `\n\n（错误：${msg}）`;
        finalizeBotAnswer(botMsgEl, rawAnswerText, '', [], null, text);
        setSending(false);
      },
    };

    if(injected){
      // 测试注入路径：按真实 SSE 的事件顺序驱动同一批 handler
      const mk = (obj) => ({ data: JSON.stringify(obj) });
      if(injected.progress) handlers.onProgress(mk(injected.progress));
      if(Array.isArray(injected.deltas)) for(const dl of injected.deltas) handlers.onDelta(mk({ delta: dl }));
      handlers.onFinal(mk(injected.final || {}));
      return;
    }

    openQueryStream(sessionId, handlers);
  }catch(e){
    const bubble = botMsgEl.querySelector('.bubble');
    const typing = botMsgEl.querySelector('.typing');
    const progress = botMsgEl.querySelector('details.progress');
    if(typing) typing.remove();
    bubble.textContent = '';
    const errEl = document.createElement('div');
    errEl.className = 'answer';
    errEl.textContent = `请求失败：${e.message || e}`;
    bubble.appendChild(errEl);
    if(progress) bubble.appendChild(progress);
    setSending(false);
  }
}

async function clearHistory(){
  const ok = await confirmModal('确定要清空当前会话的历史记录吗？这将无法恢复。', '清空对话');
  if(!ok) return;

  try{
    const res = await fetch(`${QUERY_API}/history/${encodeURIComponent(state.sessionId)}`, {
      method: 'DELETE'
    });
    if(!res.ok){
      console.error('Failed to clear history backend, status:', res.status);
      window.alert('服务端清空失败，仅清空本地显示');
    }
  }catch(e){
    console.error('Failed to clear history backend', e);
    window.alert('服务端清空失败，仅清空本地显示');
  }

  // 清空除首条欢迎消息外的内容
  const nodes = Array.from(chatEl.querySelectorAll('.msg'));
  for(let i = 1; i < nodes.length; i++) nodes[i].remove();
  scrollToBottom();
}

/* ==========================================================================
 * §7 知识库导入：上传 + 状态轮询
 * ========================================================================== */

const dropZone   = $('dropZone');
const fileInput  = $('fileInput');
const fileList   = $('fileList');
const kbEmpty    = $('kbEmpty');
const kbSummary  = $('kbSummary');
const kbClearBtn = $('kbClearBtn');

function refreshKbSummary(){
  const entries = Array.from(state.files.values());
  if(entries.length === 0){
    kbSummary.textContent = '尚未上传文件';
    kbEmpty.hidden = false;
    kbClearBtn.disabled = true;
    return;
  }
  kbEmpty.hidden = true;
  const counts = { uploading:0, processing:0, completed:0, error:0 };
  for(const f of entries) counts[f.status] = (counts[f.status] || 0) + 1;
  kbSummary.textContent =
    `共 ${entries.length} 个文件 · 已完成 ${counts.completed || 0} · 处理中 ${counts.processing || 0}` +
    ` · 上传中 ${counts.uploading || 0} · 失败 ${counts.error || 0}`;
  kbClearBtn.disabled = entries.every(f => f.status === 'uploading' || f.status === 'processing');
}

/** 与后端任务追踪保持一致的中文日志格式（沿用原 import.html 的实现）。 */
function normalizeDoneLog(text){
  if(typeof text !== 'string') return String(text);
  return text.endsWith('已完成') ? text : `${text}已完成`;
}

function normalizeRunningLog(text){
  if(typeof text !== 'string') return String(text);
  if(text.startsWith('正在进行')) return text.endsWith('...') ? text : `${text}...`;
  return `正在进行${text}...`;
}

function renderLogs(itemEl, doneList, runningList){
  const logSummary = itemEl.querySelector('.log-details summary');
  const logListEl = itemEl.querySelector('.log-list');
  const details = itemEl.querySelector('.log-details');
  if(!logSummary || !logListEl || !details) return;

  const done = Array.isArray(doneList) ? doneList : [];
  const running = Array.isArray(runningList) ? runningList : [];

  // 即使收起也能看到进度
  logSummary.textContent = `日志（已完成${done.length}，进行中${running.length}，点击展开）`;

  logListEl.innerHTML = '';
  const lines = [
    ...done.map(normalizeDoneLog),
    ...running.map(normalizeRunningLog),
  ];

  if(lines.length === 0){
    const li = document.createElement('li');
    li.textContent = '暂无日志';
    logListEl.appendChild(li);
    details.open = false;
    return;
  }
  for(const line of lines){
    const li = document.createElement('li');
    li.textContent = line;
    logListEl.appendChild(li);
  }
}

function setStatusBadge(itemEl, status, text){
  const badge = itemEl.querySelector('.status-badge');
  if(!badge) return;
  const map = {
    uploading:  'status-uploading',
    processing: 'status-processing',
    completed:  'status-completed',
    error:      'status-error',
  };
  badge.className = `status-badge ${map[status] || 'status-uploading'}`;
  badge.textContent = text;
}

function setProgress(itemEl, percentage, color){
  const bar = itemEl.querySelector('.progress-bar');
  const container = itemEl.querySelector('.progress-bar-container');
  if(!bar || !container) return;
  container.style.display = 'block';
  bar.style.width = `${percentage}%`;
  bar.style.backgroundColor = color || (percentage >= 100 ? 'var(--brand)' : 'var(--warn)');
}

function updateFileState(cardId, patch){
  const rec = state.files.get(cardId);
  if(rec) Object.assign(rec, patch);
  refreshKbSummary();
}

/** 创建一个文件卡片，返回其 DOM 与卡片 ID。 */
function createFileCard(file){
  const cardId = genId('file');
  const html = `
    <div class="file-item is-new" id="${cardId}">
      <div class="file-info">
        <span class="file-name" title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</span>
        <span class="file-size">${formatSize(file.size)}</span>
        <div class="progress-bar-container">
          <div class="progress-bar"></div>
        </div>
        <details class="log-details">
          <summary>日志（点击展开）</summary>
          <ul class="log-list"></ul>
        </details>
      </div>
      <div class="status-badge status-uploading">上传中...</div>
    </div>
  `;
  fileList.insertAdjacentHTML('afterbegin', html);
  const itemEl = document.getElementById(cardId);
  itemEl.classList.remove('is-new');
  return { cardId, itemEl };
}

/**
 * 上传单个文件。
 * 使用 XMLHttpRequest 以获得真实的 upload.onprogress（fetch 无法提供）。
 * 请求体与 fetch 版本完全一致：FormData，字段名 files。
 */
function uploadFile(file){
  const { cardId, itemEl } = createFileCard(file);
  state.files.set(cardId, { name: file.name, status: 'uploading', taskId: null });
  refreshKbSummary();

  setProgress(itemEl, 0, 'var(--warn)');

  const formData = new FormData();
  formData.append('files', file);

  const xhr = new XMLHttpRequest();
  xhr.open('POST', `${IMPORT_API}/upload`, true);

  xhr.upload.onprogress = (evt) => {
    if(!evt.lengthComputable) return;
    // 上传阶段占 0-20%，留出后端解析阶段的展示空间
    const pct = Math.round((evt.loaded / evt.total) * 20);
    setProgress(itemEl, pct, 'var(--warn)');
  };

  xhr.upload.onload = () => {
    setStatusBadge(itemEl, 'processing', '处理中...');
    setProgress(itemEl, 20, 'var(--brand)');
    updateFileState(cardId, { status: 'processing' });
  };

  xhr.onerror = () => {
    setStatusBadge(itemEl, 'error', '失败');
    setProgress(itemEl, 100, 'var(--danger)');
    updateFileState(cardId, { status: 'error' });
  };

  xhr.onload = () => {
    if(xhr.status < 200 || xhr.status >= 300){
      console.error('Upload failed', xhr.status, xhr.responseText);
      setStatusBadge(itemEl, 'error', '失败');
      setProgress(itemEl, 100, 'var(--danger)');
      updateFileState(cardId, { status: 'error' });
      return;
    }
    let result = null;
    try{
      result = JSON.parse(xhr.responseText || '{}');
    }catch(e){
      console.error('Upload response parse error', e);
    }
    const taskId = Array.isArray(result?.task_ids) ? result.task_ids[0] : null;
    if(!taskId){
      setStatusBadge(itemEl, 'error', '失败');
      setProgress(itemEl, 100, 'var(--danger)');
      updateFileState(cardId, { status: 'error' });
      return;
    }
    setStatusBadge(itemEl, 'processing', '处理中...');
    setProgress(itemEl, 20, 'var(--brand)');
    updateFileState(cardId, { status: 'processing', taskId });
    pollStatus(taskId, cardId, itemEl);
  };

  xhr.send(formData);
}

function renderLogsIfPresent(itemEl, data){
  renderLogs(itemEl, data.done_list, data.running_list);
}

/** 轮询导入任务状态：/status/{task_id} -> { status, done_list, running_list } */
function pollStatus(taskId, cardId, itemEl){
  const tick = async () => {
    const rec = state.tasks.get(taskId);
    if(!rec || rec.stopped) return;

    try{
      const res = await fetch(`${IMPORT_API}/status/${encodeURIComponent(taskId)}`, { cache: 'no-store' });
      if(!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();

      renderLogsIfPresent(itemEl, data);

      if(data.status === 'completed'){
        setStatusBadge(itemEl, 'completed', '已完成');
        setProgress(itemEl, 100, 'var(--brand)');
        rec.stopped = true;
        clearInterval(rec.timer);
        updateFileState(cardId, { status: 'completed' });
        return;
      }
      if(data.status === 'failed'){
        setStatusBadge(itemEl, 'error', '失败');
        setProgress(itemEl, 100, 'var(--danger)');
        rec.stopped = true;
        clearInterval(rec.timer);
        updateFileState(cardId, { status: 'error' });
        return;
      }

      // processing / pending：保持处理中展示
      setStatusBadge(itemEl, 'processing', '处理中...');
      setProgress(itemEl, 60, 'var(--brand)');
    }catch(e){
      // 轮询错误不终止任务，等待下一次重试（后端进度为内存态，重启即丢失）
      console.error('Polling error', e);
    }
  };

  const timer = setInterval(tick, IMPORT_POLL_MS);
  state.tasks.set(taskId, { cardId, itemEl, timer, stopped: false });
  tick();
}

function handleFiles(files){
  const list = Array.from(files || []);
  if(list.length === 0) return;
  const accepted = list.filter(f => /\.(pdf|md)$/i.test(f.name));
  const rejected = list.filter(f => !/\.(pdf|md)$/i.test(f.name));
  if(rejected.length > 0){
    window.alert(`以下文件类型不支持，已跳过：\n${rejected.map(f => f.name).join('\n')}\n\n仅支持 .pdf 和 .md`);
  }
  accepted.forEach(uploadFile);
}

function clearFinishedFiles(){
  const finished = Array.from(state.files.entries())
    .filter(([, rec]) => rec.status === 'completed' || rec.status === 'error');

  for(const [cardId, rec] of finished){
    const el = document.getElementById(cardId);
    if(el) el.remove();
    if(rec.taskId){
      const task = state.tasks.get(rec.taskId);
      if(task){
        clearInterval(task.timer);
        task.stopped = true;
        state.tasks.delete(rec.taskId);
      }
    }
    state.files.delete(cardId);
  }
  refreshKbSummary();
}

/* ==========================================================================
 * §8 确认弹窗 / 事件绑定 / 初始化
 * ========================================================================== */

const modalMask   = $('modalMask');
const modalText   = $('modalText');
const modalOk     = $('modalOk');
const modalCancel = $('modalCancel');

/** 轻量确认弹窗，替代阻塞式 confirm()。 */
function confirmModal(message, okText){
  return new Promise((resolve) => {
    modalText.textContent = message;
    modalOk.textContent = okText || '确定';
    modalMask.hidden = false;

    const finish = (value) => {
      modalMask.hidden = true;
      modalOk.removeEventListener('click', onOk);
      modalCancel.removeEventListener('click', onCancel);
      modalMask.removeEventListener('click', onMask);
      document.removeEventListener('keydown', onKey);
      resolve(value);
    };
    const onOk = () => finish(true);
    const onCancel = () => finish(false);
    const onMask = (e) => { if(e.target === modalMask) finish(false); };
    const onKey = (e) => { if(e.key === 'Escape') finish(false); };

    modalOk.addEventListener('click', onOk);
    modalCancel.addEventListener('click', onCancel);
    modalMask.addEventListener('click', onMask);
    document.addEventListener('keydown', onKey);
    modalOk.focus();
  });
}

function bindEvents(){
  // --- 对话 ---
  sendBtn.addEventListener('click', onSend);
  inputEl.addEventListener('keydown', (e) => {
    if(e.key === 'Enter' && !e.shiftKey){
      e.preventDefault();
      onSend();
    }
  });
  btnClear.addEventListener('click', clearHistory);

  // --- 上传 ---
  dropZone.addEventListener('click', () => fileInput.click());
  dropZone.addEventListener('keydown', (e) => {
    if(e.key === 'Enter' || e.key === ' '){
      e.preventDefault();
      fileInput.click();
    }
  });
  fileInput.addEventListener('change', (e) => {
    handleFiles(e.target.files);
    e.target.value = '';   // 允许重复选择同一文件
  });
  dropZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropZone.classList.add('is-dragover');
  });
  dropZone.addEventListener('dragleave', (e) => {
    e.preventDefault();
    dropZone.classList.remove('is-dragover');
  });
  dropZone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropZone.classList.remove('is-dragover');
    handleFiles(e.dataTransfer.files);
  });
  // 阻止拖到页面其他位置时浏览器直接打开文件
  window.addEventListener('dragover', (e) => e.preventDefault());
  window.addEventListener('drop', (e) => e.preventDefault());

  kbClearBtn.addEventListener('click', clearFinishedFiles);

  // --- 服务状态手动重检 ---
  queryApiPill.addEventListener('click', apiHealth);
  importApiPill.addEventListener('click', apiHealth);
}

function initSession(){
  let sessionId = localStorage.getItem('kb_session_id');
  if(!sessionId){
    sessionId = 'sess-' + Math.random().toString(36).slice(2) + Date.now().toString(36);
    localStorage.setItem('kb_session_id', sessionId);
  }
  state.sessionId = sessionId;
  if(sessionInfo){
    const short = sessionId.length > 14 ? `${sessionId.slice(0, 10)}…` : sessionId;
    sessionInfo.textContent = `会话：${short}`;
  }
}

function init(){
  bindEvents();
  initSession();
  refreshKbSummary();

  apiHealth();
  setInterval(apiHealth, HEALTH_INTERVAL_MS);

  loadHistory();
  setTimeout(() => inputEl.focus(), 200);
}

init();
