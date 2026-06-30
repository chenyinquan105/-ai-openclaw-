/* ======================================================================
   chat-core.js — 通用LLM聊天核心逻辑
   SSE流处理 · 消息渲染 · 工具调用可视化 · 状态管理
   ====================================================================== */

// ── 全局状态 ──
let chatExpanded = false;
let chatSessionId = null;
let chatEventSource = null;
let isChatStreaming = false;
let chatUnreadCount = 0;
let lastUserScrollTop = 0;

// ── DOM缓存 ──
let $chatContainer, $chatMiniBar, $chatPanel, $chatMessages, $chatInput;
let $chatSendBtn, $chatTyping, $chatQuickActions, $chatScrollBottom;
let $chatBadge;

// ── 初始化 ──
function initChat() {
  // 缓存DOM引用
  $chatContainer = document.getElementById('chat-container');
  $chatMiniBar = document.getElementById('chat-mini-bar');
  $chatPanel = document.getElementById('chat-panel');
  $chatMessages = document.getElementById('chat-messages');
  $chatInput = document.getElementById('chat-input');
  $chatSendBtn = document.getElementById('chat-send-btn');
  $chatTyping = document.getElementById('chat-typing');
  $chatQuickActions = document.getElementById('chat-quick-actions');
  $chatScrollBottom = document.getElementById('chat-scroll-bottom');
  $chatBadge = document.getElementById('chat-mini-badge');

  // 绑定事件
  if ($chatMiniBar) {
    $chatMiniBar.addEventListener('click', function(e) {
      e.stopPropagation();
      expandChat();
    });
  }
  if ($chatInput) {
    $chatInput.addEventListener('keydown', function(e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendChatMessage();
      }
    });
    // 自动调整高度
    $chatInput.addEventListener('input', function() {
      this.style.height = 'auto';
      this.style.height = Math.min(this.scrollHeight, 100) + 'px';
    });
  }
  if ($chatSendBtn) {
    $chatSendBtn.addEventListener('click', sendChatMessage);
  }
  if ($chatScrollBottom) {
    $chatScrollBottom.addEventListener('click', scrollChatToBottom);
  }

  // 监听消息区滚动，控制"滚到底"按钮显隐
  if ($chatMessages) {
    $chatMessages.addEventListener('scroll', function() {
      var distToBottom = this.scrollHeight - this.scrollTop - this.clientHeight;
      if ($chatScrollBottom) {
        $chatScrollBottom.classList.toggle('show', distToBottom > 100);
      }
    });
  }

  // 点击面板外关闭（点击mini-bar区域外）
  document.addEventListener('click', function(e) {
    if (chatExpanded && $chatContainer && $chatPanel) {
      var insideContainer = $chatContainer.contains(e.target);
      if (!insideContainer) {
        collapseChat();
      }
    }
  });

  // 初始化快捷操作
  renderQuickActions();

  // 尝试加载历史
  loadChatHistory();

  console.log('[chat] 聊天模块初始化完成');
}

// ── 展开/收起 ──
function expandChat() {
  if (chatExpanded) return;
  chatExpanded = true;
  chatUnreadCount = 0;
  updateBadge();
  if ($chatPanel) $chatPanel.classList.add('expanded');
  if ($chatMiniBar) $chatMiniBar.style.display = 'none';
  if ($chatInput) {
    setTimeout(function() { $chatInput.focus(); }, 450);
  }
  scrollChatToBottom();
}

function collapseChat() {
  if (!chatExpanded) return;
  chatExpanded = false;
  if ($chatPanel) $chatPanel.classList.remove('expanded');
  if ($chatMiniBar) $chatMiniBar.style.display = 'flex';
}

function toggleChat() {
  if (chatExpanded) {
    collapseChat();
  } else {
    expandChat();
  }
}

// ── 发送消息 ──
async function sendChatMessage() {
  if (isChatStreaming) return;
  var text = ($chatInput && $chatInput.value) ? $chatInput.value.trim() : '';
  if (!text) return;

  // 清除输入
  if ($chatInput) {
    $chatInput.value = '';
    $chatInput.style.height = 'auto';
  }

  // 移除空状态
  var emptyState = $chatMessages && $chatMessages.querySelector('.chat-empty-state');
  if (emptyState) emptyState.remove();

  // 渲染用户气泡
  renderChatBubble('user', text);

  // 显示打字指示器
  showTyping();

  // 禁用输入
  setInputEnabled(false);

  // 构建上下文
  var context = buildChatContext();

  // 连接SSE
  try {
    await connectChatSSE({ message: text, session_id: chatSessionId, context: context });
  } catch (e) {
    hideTyping();
    setInputEnabled(true);
    renderChatError('网络异常，请稍后重试');
    console.error('[chat] SSE连接失败:', e);
  }
}

// ── SSE连接 ──
function connectChatSSE(body) {
  return new Promise(function(resolve, reject) {
    // 关闭旧连接
    if (chatEventSource) {
      chatEventSource.close();
      chatEventSource = null;
    }

    isChatStreaming = true;

    // EventSource只支持GET，改用fetch+ReadableStream解析SSE
    var controller = new AbortController();
    var timeoutId = setTimeout(function() {
      controller.abort();
      if (isChatStreaming) {
        isChatStreaming = false;
        hideTyping();
        setInputEnabled(true);
        renderChatError('请求超时，请重试');
      }
    }, 60000); // 60秒超时

    fetch('/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: controller.signal
    }).then(function(response) {
      if (!response.ok) {
        clearTimeout(timeoutId);
        isChatStreaming = false;
        hideTyping();
        setInputEnabled(true);
        return response.json().then(function(err) {
          renderChatError(err.error || '请求失败');
          reject(new Error(err.error || 'HTTP ' + response.status));
        }).catch(function() {
          renderChatError('服务异常 (' + response.status + ')');
          reject(new Error('HTTP ' + response.status));
        });
      }

      var reader = response.body.getReader();
      var decoder = new TextDecoder();
      var buffer = '';
      var currentAssistantBubble = null;
      var assistantContent = '';
      var done = false;

      function readStream() {
        reader.read().then(function(result) {
          if (result.done) {
            clearTimeout(timeoutId);
            if (currentAssistantBubble && assistantContent) {
              finalizeAssistantBubble(currentAssistantBubble);
            }
            isChatStreaming = false;
            hideTyping();
            setInputEnabled(true);
            scrollChatToBottom();
            if (!done) {
              renderChatError('响应意外结束');
            }
            resolve();
            return;
          }

          buffer += decoder.decode(result.value, { stream: true });
          var lines = buffer.split('\n');
          // 保留最后一个可能不完整的行
          buffer = lines.pop() || '';

          var eventType = '';
          var eventData = '';

          for (var i = 0; i < lines.length; i++) {
            var line = lines[i];
            if (line.startsWith('event: ')) {
              eventType = line.slice(7).trim();
            } else if (line.startsWith('data: ')) {
              eventData = line.slice(6);
              // SSE支持多行data，但我们简单处理
              try {
                var payload = JSON.parse(eventData);
                handleChatSSEEvent(eventType, payload, {
                  setBubble: function(b) { currentAssistantBubble = b; },
                  getBubble: function() { return currentAssistantBubble; },
                  appendContent: function(c) { assistantContent += c; },
                  getContent: function() { return assistantContent; },
                  resetContent: function() { assistantContent = ''; },
                  markDone: function() { done = true; }
                });
              } catch (e) {
                // 非JSON数据（如keepalive注释），忽略
              }
              eventType = '';
              eventData = '';
            }
          }

          if (!result.done) {
            readStream();
          }
        }).catch(function(err) {
          clearTimeout(timeoutId);
          if (err.name !== 'AbortError') {
            console.error('[chat] 读取流失败:', err);
            isChatStreaming = false;
            hideTyping();
            setInputEnabled(true);
            var errorShown = document.querySelector('.chat-error-toast');
            if (!errorShown) {
              renderChatError('连接中断，请重试');
            }
          }
          resolve();
        });
      }

      readStream();
    }).catch(function(err) {
      clearTimeout(timeoutId);
      if (err.name !== 'AbortError') {
        console.error('[chat] fetch失败:', err);
        isChatStreaming = false;
        hideTyping();
        setInputEnabled(true);
        renderChatError('网络异常，请稍后重试');
      }
      reject(err);
    });
  });
}

// ── SSE事件处理 ──
function handleChatSSEEvent(type, payload, streamState) {
  switch (type) {
    case 'status':
      if (payload.type === 'thinking') {
        showTyping();
      }
      break;

    case 'message':
      if (payload.role === 'user') {
        // 用户消息已在前端渲染，后端回显可忽略或去重
      } else if (payload.role === 'assistant') {
        hideTyping();
        if (payload.content) {
          var bubble = streamState.getBubble();
          if (!bubble) {
            bubble = createAssistantBubble('');
            streamState.setBubble(bubble);
          }
          appendToBubble(bubble, payload.content);
          streamState.appendContent(payload.content);
          scrollChatToBottom();
        }
      }
      break;

    case 'tool_call':
      hideTyping();
      // 渲染工具调用卡片
      var toolCard = createToolCard(payload);
      if ($chatMessages) $chatMessages.appendChild(toolCard);
      scrollChatToBottom();
      // 重置assistant气泡（工具调用后可能有新的assistant回复）
      streamState.setBubble(null);
      streamState.resetContent();
      break;

    case 'tool_result':
      // 更新对应的工具卡片
      updateToolCard(payload);
      scrollChatToBottom();
      break;

    case 'error':
      hideTyping();
      renderChatError(payload.message || '出错了，请重试');
      break;

    case 'done':
      streamState.markDone();
      if (payload.session_id) {
        chatSessionId = payload.session_id;
      }
      // 有assistant气泡则标记完成
      var bubble = streamState.getBubble();
      if (bubble) {
        finalizeAssistantBubble(bubble);
      }
      hideTyping();
      break;

    default:
      // 未知事件类型，忽略
      break;
  }
}

// ── 消息渲染 ──
function renderChatBubble(role, content) {
  if (!$chatMessages) return;
  var bubble = document.createElement('div');
  bubble.className = 'chat-bubble ' + role;
  if (role === 'assistant') {
    bubble.classList.add('streaming');
  }
  bubble.innerHTML = '<span class="msg-content">' + escapeHtml(content) + '</span>' +
    '<span class="msg-time">' + formatChatTime() + '</span>';
  $chatMessages.appendChild(bubble);
  scrollChatToBottom();
  return bubble;
}

function createAssistantBubble(initialContent) {
  if (!$chatMessages) return null;
  var bubble = document.createElement('div');
  bubble.className = 'chat-bubble assistant streaming';
  bubble.innerHTML = '<span class="msg-content">' + escapeHtml(initialContent) + '</span>' +
    '<span class="msg-time">' + formatChatTime() + '</span>';
  $chatMessages.appendChild(bubble);
  return bubble;
}

function appendToBubble(bubble, chunk) {
  if (!bubble) return;
  var contentEl = bubble.querySelector('.msg-content');
  if (contentEl) {
    contentEl.textContent += chunk;
  }
}

function finalizeAssistantBubble(bubble) {
  if (!bubble) return;
  bubble.classList.remove('streaming');
  // 更新最终时间
  var timeEl = bubble.querySelector('.msg-time');
  if (timeEl) {
    timeEl.textContent = formatChatTime();
  }
}

// ── 工具调用卡片 ──
var _toolCards = {}; // tool_call_id → DOM element

function createToolCard(tc) {
  var card = document.createElement('div');
  card.className = 'chat-tool-card';
  card.id = 'tool-card-' + (tc.id || Date.now());
  _toolCards[tc.id] = card;

  var icon = getToolIcon(tc.name);
  var label = getToolLabel(tc.name);

  var argsDisplay = '';
  if (tc.arguments) {
    try {
      var argsObj = typeof tc.arguments === 'string' ? JSON.parse(tc.arguments) : tc.arguments;
      argsDisplay = JSON.stringify(argsObj, null, 1);
    } catch(e) {
      argsDisplay = String(tc.arguments);
    }
  }

  card.innerHTML =
    '<div class="tool-header">' +
      '<span class="tool-icon">' + icon + '</span>' +
      '<span>' + escapeHtml(label) + '</span>' +
      '<div class="tool-spinner"></div>' +
    '</div>' +
    '<div class="tool-status-running">⏳ 执行中...</div>' +
    (argsDisplay ? '<div class="tool-params">' + escapeHtml(argsDisplay) + '</div>' : '');

  return card;
}

function updateToolCard(result) {
  var card = _toolCards[result.id];
  if (!card) return;

  var spinner = card.querySelector('.tool-spinner');
  if (spinner) spinner.style.display = 'none';

  var statusEl = card.querySelector('.tool-status-running');
  if (!statusEl) {
    statusEl = card.querySelector('.tool-status-done') || card.querySelector('.tool-status-error');
  }

  if (result.status === 'completed') {
    if (statusEl) {
      statusEl.className = 'tool-status-done';
      statusEl.textContent = '✅ 执行完成';
    }
    // 添加结果展示
    if (result.result) {
      var resultHtml = formatToolResult(result.result);
      if (resultHtml) {
        var resultDiv = document.createElement('div');
        resultDiv.className = 'tool-result';
        resultDiv.innerHTML = resultHtml;
        card.appendChild(resultDiv);
      }
    }
  } else if (result.status === 'failed') {
    if (statusEl) {
      statusEl.className = 'tool-status-error';
      statusEl.textContent = '❌ 执行失败';
    }
    if (result.error) {
      var errDiv = document.createElement('div');
      errDiv.className = 'tool-result error';
      errDiv.textContent = result.error;
      card.appendChild(errDiv);
    }
  }
}

// ── 错误提示 ──
function renderChatError(message) {
  if (!$chatMessages) return;
  var toast = document.createElement('div');
  toast.className = 'chat-error-toast';
  toast.innerHTML = '<span>⚠️ ' + escapeHtml(message) + '</span>' +
    '<button onclick="this.parentElement.remove();sendChatMessageRetry()">重试</button>';
  $chatMessages.appendChild(toast);
  scrollChatToBottom();
}

var _lastFailedMessage = null;
function sendChatMessageRetry() {
  if (_lastFailedMessage && !isChatStreaming) {
    var text = _lastFailedMessage;
    _lastFailedMessage = null;
    // 重新发送
    if ($chatInput) $chatInput.value = text;
    sendChatMessage();
  }
}

// ── 工具函数 ──
function escapeHtml(s) {
  var d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}

function formatChatTime() {
  var now = new Date();
  var h = now.getHours().toString().padStart(2, '0');
  var m = now.getMinutes().toString().padStart(2, '0');
  return h + ':' + m;
}

function setInputEnabled(enabled) {
  if ($chatInput) {
    $chatInput.disabled = !enabled;
    if (enabled) {
      $chatInput.placeholder = '输入消息...';
    } else {
      $chatInput.placeholder = 'AI思考中...';
    }
  }
  if ($chatSendBtn) {
    $chatSendBtn.disabled = !enabled;
  }
}

function showTyping() {
  if ($chatTyping) $chatTyping.classList.add('active');
}

function hideTyping() {
  if ($chatTyping) $chatTyping.classList.remove('active');
}

function scrollChatToBottom() {
  if ($chatMessages) {
    requestAnimationFrame(function() {
      $chatMessages.scrollTop = $chatMessages.scrollHeight;
    });
  }
}

function updateBadge() {
  if ($chatBadge) {
    if (chatUnreadCount > 0 && !chatExpanded) {
      $chatBadge.classList.add('show');
      $chatBadge.textContent = chatUnreadCount > 99 ? '99+' : chatUnreadCount;
    } else {
      $chatBadge.classList.remove('show');
    }
  }
}

// ── 工具名称映射 ──
function getToolIcon(name) {
  var icons = {
    'search_poi': '🔍', 'plan_route': '🗺️', 'hail_taxi': '🚕',
    'plan_transit': '🚇', 'add_reminder': '⏰', 'remove_reminder': '🔕',
    'list_reminders': '📋', 'check_weather': '🌤️', 'read_profile': '👤',
    'update_profile': '✏️', 'check_queue': '🪧', 'start_trip': '🚀',
    'edit_trip': '📝', 'cancel_trip': '❌', 'get_trip_status': '📊'
  };
  return icons[name] || '🔧';
}

function getToolLabel(name) {
  var labels = {
    'search_poi': '搜索周边商户', 'plan_route': '规划路线',
    'hail_taxi': '虚拟打车', 'plan_transit': '公共交通规划',
    'add_reminder': '添加提醒', 'remove_reminder': '删除提醒',
    'list_reminders': '查看提醒列表', 'check_weather': '查询天气',
    'read_profile': '读取偏好设置', 'update_profile': '更新偏好设置',
    'check_queue': '查询排队状态', 'start_trip': '发起新行程',
    'edit_trip': '修改行程', 'cancel_trip': '取消行程',
    'get_trip_status': '查看行程状态'
  };
  return labels[name] || name;
}

function formatToolResult(result) {
  if (!result) return '';
  var data = result.data || result;

  // ★ 品类分组结果（新版 search_poi 返回）→ 按品类展示Top3
  if (data.categories && Array.isArray(data.categories)) {
    var html = '<div class="poi-list">';
    for (var ci = 0; ci < data.categories.length; ci++) {
      var cat = data.categories[ci];
      var label = cat.label || cat.category;
      html += '<div style="font-weight:700;color:#374151;margin:6px 0 2px">' + escapeHtml(label) + '</div>';
      var shops = cat.shops || [];
      for (var si = 0; si < Math.min(shops.length, 3); si++) {
        var s = shops[si];
        html += '<div class="poi-item">' +
          '<div><span class="poi-name">' + escapeHtml(s.name || '未知') + '</span></div>' +
          '<div class="poi-meta">' +
            (s.rating ? '★' + s.rating + ' ' : '') +
            (s.distance ? s.distance : '') +
          '</div>' +
        '</div>';
      }
    }
    html += '</div>';
    if (result.message) {
      html += '<div style="margin-top:4px;color:#6b7280;font-size:11px">' + escapeHtml(result.message) + '</div>';
    }
    return html;
  }

  // POI搜索结果 → 商户列表
  if (data.shops || data.search_results) {
    var shops = data.shops || [];
    if (data.search_results) {
      for (var cat in data.search_results) {
        shops = shops.concat(data.search_results[cat]);
      }
    }
    if (shops.length > 0) {
      var html = '<div class="poi-list">';
      for (var i = 0; i < Math.min(shops.length, 8); i++) {
        var s = shops[i];
        html += '<div class="poi-item">' +
          '<div><span class="poi-name">' + escapeHtml(s.name || s.shop_name || '未知') + '</span></div>' +
          '<div class="poi-meta">' +
            (s.rating ? '★' + s.rating + ' ' : '') +
            (s.distance ? formatDistance(s.distance) : '') +
          '</div>' +
        '</div>';
      }
      html += '</div>';
      return html;
    }
  }

  // 路线规划结果 → 步骤列表
  if (data.route && Array.isArray(data.route)) {
    var html = '<div class="route-steps">';
    for (var i = 0; i < data.route.length; i++) {
      var step = data.route[i];
      var modeIcon = {'步行': '🚶', '打车': '🚕', '地铁': '🚇', '公交': '🚌'};
      html += '<div class="route-step">' +
        '<span class="step-icon">' + (modeIcon[step.transport_mode] || '➡️') + '</span>' +
        '<span>' + escapeHtml(step.from) + ' → ' + escapeHtml(step.to) + '</span>' +
        '<span style="margin-left:auto;color:#6b7280;font-size:11px">' +
          step.transport_mode + ' ' + formatDistance(step.distance_meters) + ' ' + step.duration_minutes + '分钟</span>' +
      '</div>';
    }
    html += '</div>';
    if (data.total_travel_minutes) {
      html += '<div style="margin-top:6px;font-weight:700;color:#374151">总计: ' +
        data.total_travel_minutes + '分钟路程 + ' + (data.total_activity_minutes || 0) + '分钟活动</div>';
    }
    return html;
  }

  // 打车结果
  if (data.taxi || result.message) {
    return '<div style="font-weight:600">' + escapeHtml(result.message || data.message || '操作完成') + '</div>';
  }

  // 其他结果 → JSON
  return '<pre style="font-size:11px;white-space:pre-wrap">' + escapeHtml(JSON.stringify(result, null, 1)) + '</pre>';
}

function formatDistance(meters) {
  if (meters == null) return '';
  if (meters < 1000) return Math.round(meters) + 'm';
  return (meters / 1000).toFixed(1) + 'km';
}

// ── 上下文构建 ──
function buildChatContext() {
  var ctx = {
    has_active_trip: false,
    trip_destinations: [],
    trip_transport: '步行',
    virtual_time: null,
    clock_enabled: false,
    active_anomalies: []
  };

  // 从全局状态获取
  if (typeof currentPhase !== 'undefined') {
    ctx.has_active_trip = (currentPhase === 'done');
  }
  if (typeof sessionState !== 'undefined' && sessionState) {
    var pairs = sessionState.selected_pairs || [];
    ctx.trip_destinations = pairs.map(function(p) { return p[2] || ''; });
  }
  if (typeof selectedTransport !== 'undefined') {
    ctx.trip_transport = selectedTransport;
  }
  if (typeof clockPowerOn !== 'undefined') {
    ctx.clock_enabled = clockPowerOn;
  }
  if (typeof activeExceptions !== 'undefined' && activeExceptions) {
    ctx.active_anomalies = activeExceptions.map(function(e) { return e.type || ''; });
  }
  // 虚拟时间从时钟面板获取
  var clockDisplay = document.getElementById('clock-display');
  if (clockDisplay) {
    ctx.virtual_time = clockDisplay.textContent.trim();
  }

  return ctx;
}

// ── 对话历史管理 ──
async function loadChatHistory() {
  try {
    var res = await fetch('/api/chat/history');
    if (!res.ok) return;
    var data = await res.json();
    if (data.session_id) {
      chatSessionId = data.session_id;
    }
    if (data.messages && data.messages.length > 0) {
      // 清空空状态
      var emptyState = $chatMessages && $chatMessages.querySelector('.chat-empty-state');
      if (emptyState) emptyState.remove();

      for (var i = 0; i < data.messages.length; i++) {
        var msg = data.messages[i];
        if (msg.role === 'user' || msg.role === 'assistant') {
          renderChatBubble(msg.role, msg.content);
        }
      }
      scrollChatToBottom();
    }
  } catch (e) {
    console.log('[chat] 历史加载失败（可能无历史文件）:', e.message);
  }
}

async function clearChatHistory() {
  try {
    await fetch('/api/chat/clear', { method: 'POST' });
  } catch(e) {
    console.error('[chat] 清空失败:', e);
  }
  if ($chatMessages) {
    $chatMessages.innerHTML =
      '<div class="chat-empty-state">' +
        '<div class="empty-icon">💬</div>' +
        '<div class="empty-text">有什么可以帮你的？</div>' +
        '<div class="empty-hint">试试说：提醒我下午3点买菜</div>' +
      '</div>';
  }
  chatSessionId = null;
  _toolCards = {};
  renderQuickActions();
}

// ── 快捷操作渲染 ──
function renderQuickActions() {
  if (!$chatQuickActions) return;
  var chips = [];
  var hasActiveTrip = (typeof currentPhase !== 'undefined' && currentPhase === 'done');

  if (hasActiveTrip) {
    chips = [
      { text: '📝 修改行程', primary: true, action: "帮我调整一下当前行程" },
      { text: '🚕 打车', primary: false, action: "帮我打车" },
      { text: '🚇 公交路线', primary: false, action: "帮我规划公共交通路线" },
      { text: '⏰ 设提醒', primary: false, action: "帮我设置一个提醒" },
      { text: '🌤️ 查天气', primary: false, action: "今天天气怎么样" },
      { text: '❌ 取消行程', primary: false, action: "取消当前行程" },
    ];
  } else {
    chips = [
      { text: '🗺️ 规划行程', primary: true, action: "帮我规划一个行程" },
      { text: '⏰ 设置提醒', primary: false, action: "提醒我下午3点买菜" },
      { text: '🌤️ 查天气', primary: false, action: "今天天气怎么样" },
      { text: '🚕 打车', primary: false, action: "帮我打车去三里屯" },
      { text: '🚇 公交路线', primary: false, action: "从我家到国贸怎么坐车最方便" },
      { text: '🔍 搜商户', primary: false, action: "帮我找附近的咖啡店" },
    ];
  }

  $chatQuickActions.innerHTML = '';
  for (var i = 0; i < chips.length; i++) {
    var chip = chips[i];
    var el = document.createElement('span');
    el.className = 'quick-chip' + (chip.primary ? ' primary' : '');
    el.textContent = chip.text;
    el.title = chip.action;
    el.addEventListener('click', (function(action) {
      return function() {
        if (!chatExpanded) expandChat();
        if ($chatInput && !isChatStreaming) {
          $chatInput.value = action;
          sendChatMessage();
        }
      };
    })(chip.action));
    $chatQuickActions.appendChild(el);
  }
}
