/* ======================================================================
   chat-ui.js — 聊天UI交互增强
   面板动画 · 键盘快捷键 · 上下文同步 · 事件桥接
   ====================================================================== */

// ── 键盘快捷键 ──
document.addEventListener('keydown', function(e) {
  // Ctrl+K / Cmd+K → 快速打开聊天
  if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
    e.preventDefault();
    toggleChat();
  }
  // Escape → 关闭聊天
  if (e.key === 'Escape' && chatExpanded) {
    e.preventDefault();
    collapseChat();
  }
});

// ── 同步快捷操作到上下文变化 ──
// 当 currentPhase 变化时（由主脚本设置），刷新快捷操作
var _origRenderQuickActions = renderQuickActions;
renderQuickActions = function() {
  _origRenderQuickActions();
};

// 监听行程状态变化 → 刷新快捷操作
// 通过 MutationObserver 监听 dynamic-content 的变化来推断行程状态
(function() {
  var dynamicContent = document.getElementById('dynamic-content');
  if (dynamicContent) {
    var observer = new MutationObserver(function() {
      // 延迟刷新，等DOM更新完
      setTimeout(function() {
        if (typeof renderQuickActions === 'function') {
          renderQuickActions();
        }
      }, 500);
    });
    observer.observe(dynamicContent, { childList: true, subtree: true });
  }
})();

// ── 同步exec-cover中的信息到聊天 ──
// 当用户在exec-cover中编辑行程后，聊天也能感知
var _origSendExecMessage = sendExecMessage;
if (typeof sendExecMessage !== 'undefined') {
  sendExecMessage = function() {
    _origSendExecMessage();
    // 行程编辑后更新聊天快捷操作
    setTimeout(function() {
      if (typeof renderQuickActions === 'function') {
        renderQuickActions();
      }
    }, 1000);
  };
}

// ── 未读计数（当聊天收起且有新AI消息时） ──
var _origRenderChatBubble = renderChatBubble;
renderChatBubble = function(role, content) {
  var bubble = _origRenderChatBubble(role, content);
  if (role === 'assistant' && !chatExpanded) {
    chatUnreadCount++;
    updateBadge();
  }
  return bubble;
};

var _origCreateAssistantBubble = createAssistantBubble;
createAssistantBubble = function(content) {
  var bubble = _origCreateAssistantBubble(content);
  if (!chatExpanded) {
    chatUnreadCount++;
    updateBadge();
  }
  return bubble;
};

// 展开时清除未读
var _origExpandChat = expandChat;
expandChat = function() {
  _origExpandChat();
  chatUnreadCount = 0;
  updateBadge();
};

// ── 聊天消息通知（可选：通过系统通知API） ──
function _notifyIfNeeded(role, content) {
  if (role === 'assistant' && !chatExpanded && document.hidden && 'Notification' in window && Notification.permission === 'granted') {
    try {
      new Notification('AI管家', {
        body: content.substring(0, 100) + (content.length > 100 ? '...' : ''),
        icon: 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y=".9em" font-size="90">🤖</text></svg>'
      });
    } catch(e) { /* 静默 */ }
  }
}

// 请求通知权限（用户触发）
function requestChatNotificationPermission() {
  if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission();
  }
}

// ── 处理 exec-cover 和 chat-panel 的层级关系 ──
// 当exec-cover打开时，chat应该自动收起
var _origOpenExecCover = typeof openExecCover === 'function' ? openExecCover : null;
if (_origOpenExecCover) {
  openExecCover = function() {
    if (chatExpanded) collapseChat();
    _origOpenExecCover();
  };
}

// ── 面板拖拽调整高度（可选交互） ──
(function() {
  var isDragging = false;
  var startY = 0;
  var startHeight = 0;

  document.addEventListener('mousedown', function(e) {
    var panel = document.getElementById('chat-panel');
    if (!panel || !chatExpanded) return;
    // 检测是否在面板顶部边缘（拖拽handle区域）
    var rect = panel.getBoundingClientRect();
    var handleTop = rect.top;
    if (Math.abs(e.clientY - handleTop) < 20 && e.clientY > handleTop - 10) {
      isDragging = true;
      startY = e.clientY;
      startHeight = panel.offsetHeight;
      panel.style.transition = 'none';
      e.preventDefault();
    }
  });

  document.addEventListener('mousemove', function(e) {
    if (!isDragging) return;
    var panel = document.getElementById('chat-panel');
    if (!panel) return;
    var container = document.getElementById('main-phone-container');
    var containerHeight = container ? container.offsetHeight : window.innerHeight;
    var newHeight = startHeight + (startY - e.clientY);
    // 限制在 30% ~ 85% 之间
    newHeight = Math.max(containerHeight * 0.3, Math.min(containerHeight * 0.85, newHeight));
    panel.style.height = newHeight + 'px';
  });

  document.addEventListener('mouseup', function() {
    if (isDragging) {
      isDragging = false;
      var panel = document.getElementById('chat-panel');
      if (panel) {
        panel.style.transition = 'height 0.4s cubic-bezier(0.4, 0, 0.2, 1)';
      }
    }
  });
})();

// ── 聊天窗口resize时自适应 ──
window.addEventListener('resize', function() {
  if (chatExpanded) {
    var panel = document.getElementById('chat-panel');
    if (panel) {
      // 移除可能残留的inline height，恢复CSS class控制
      panel.style.height = '';
    }
  }
});

// ── 从聊天消息中同步提醒到管家面板 ──
// 当聊天中添加/删除提醒后，刷新管家背面列表
function syncRemindersFromChat() {
  if (typeof reminderRefreshList === 'function') {
    reminderRefreshList();
  }
  if (typeof butlerRenderContent === 'function') {
    butlerRenderContent();
  }
}

// 在tool_result处理中检测提醒变化
var _origUpdateToolCard = updateToolCard;
updateToolCard = function(result) {
  _origUpdateToolCard(result);
  // 如果工具调用涉及提醒，同步UI
  var card = _toolCards[result.id];
  if (card && result.name) {
    var reminderTools = ['add_reminder', 'remove_reminder', 'list_reminders'];
    if (reminderTools.indexOf(result.name) >= 0 || reminderTools.indexOf(result.name) >= 0) {
      setTimeout(syncRemindersFromChat, 800);
    }
  }
};

console.log('[chat-ui] UI交互增强模块加载完成');
