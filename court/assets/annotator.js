/*
 * Studio annotation overlay — dependency-free, isolated in a closed shadow
 * DOM so it never interferes with page scripts. Toggled with
 * Ctrl+Shift+A (or Cmd+Shift+A). Placeholders {{ANNOTATE_ENDPOINT}} and
 * {{WORKTREE}} are substituted by court/browser.py before injection.
 */
(function () {
  "use strict";
  try {
    if (location.protocol !== "http:" && location.protocol !== "https:") {
      return;
    }
    if (window !== window.top) {
      return;
    }
    // A `window` object survives navigation, so the double-install guard must
    // be DOM-based: if this document already hosts the overlay, do nothing.
    // addScriptToEvaluateOnNewDocument re-runs this script in every new
    // document, and the fresh document has no overlay root yet.
    if (document.querySelector('[data-studio-annotator="root"]')) {
      return;
    }
    window.__studioAnnotator = true;

    var ENDPOINT = "{{ANNOTATE_ENDPOINT}}";
    var WORKTREE = "{{WORKTREE}}";
    var MAX_TEXT = 140;
    var MAX_SELECTOR = 200;

    var api = {
      mode: false,
      saved: 0,
      hovered: null,
      pending: null,
      boxOpen: false,
    };
    var ui = {};
    var wired = false;

    function el(tag, style, text) {
      var node = document.createElement(tag);
      if (style) {
        node.style.cssText = style;
      }
      if (text) {
        node.textContent = text;
      }
      return node;
    }

    function buildUi() {
      try {
        var host = document.body || document.documentElement;
        if (!host) {
          // Pinned scripts (addScriptToEvaluateOnNewDocument) can run before
          // any DOM node exists. Retry until the document can host the
          // overlay (~5s), then mount once.
          var attempts = 0;
          var timer = setInterval(function () {
            attempts += 1;
            try {
              if (document.body || document.documentElement) {
                clearInterval(timer);
                buildUi();
              }
            } catch (err) {
              clearInterval(timer);
            }
            if (attempts > 50) {
              clearInterval(timer);
            }
          }, 100);
          return;
        }
        var root = document.createElement("div");
        root.setAttribute("data-studio-annotator", "root");
        root.style.cssText = "all:initial; position:fixed; left:0; top:0; width:0; height:0; z-index:2147483647;";
        host.appendChild(root);
        var shadow = root.attachShadow({ mode: "closed" });

      var style = document.createElement("style");
      style.textContent = [
        "* { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; }",
        ".badge { position: fixed; right: 12px; bottom: 12px; padding: 6px 12px; border-radius: 8px;",
        "  background: rgba(24, 26, 32, 0.85); color: #f4f5f7; font-size: 12px; line-height: 1.4;",
        "  box-shadow: 0 2px 8px rgba(0,0,0,0.35); user-select: none; white-space: nowrap; }",
        ".badge b { color: #ffd479; font-weight: 600; }",
        ".chip { position: fixed; padding: 2px 8px; border-radius: 6px; background: rgba(24,26,32,0.9);",
        "  color: #ffd479; font-size: 11px; pointer-events: none; display: none; z-index: 2147483647; }",
        ".panel { position: fixed; left: 50%; top: 40%; transform: translate(-50%, -50%); width: 380px;",
        "  max-width: calc(100vw - 32px); background: #fff; color: #1c1e24; border-radius: 10px;",
        "  box-shadow: 0 8px 32px rgba(0,0,0,0.35); padding: 14px; display: none; }",
        ".panel h3 { margin: 0 0 8px; font-size: 13px; font-weight: 600; }",
        ".panel .meta { font-size: 11px; color: #5c6270; margin-bottom: 8px; word-break: break-all; }",
        ".panel textarea { width: 100%; height: 72px; resize: vertical; border: 1px solid #c9cedb;",
        "  border-radius: 6px; padding: 6px 8px; font: inherit; font-size: 13px; }",
        ".panel .row { display: flex; gap: 8px; justify-content: flex-end; margin-top: 10px; }",
        "button { font: inherit; font-size: 12px; padding: 5px 12px; border-radius: 6px; border: 1px solid #c9cedb;",
        "  background: #f2f3f7; color: #1c1e24; cursor: pointer; }",
        "button.save { background: #2f6fed; border-color: #2f6fed; color: #fff; font-weight: 600; }",
        ".hint { font-size: 10px; color: #8a90a0; margin-top: 8px; }",
        ".toast { position: fixed; top: 14px; left: 50%; transform: translateX(-50%); padding: 8px 16px;",
        "  border-radius: 8px; background: rgba(24,26,32,0.92); color: #f4f5f7; font-size: 12px;",
        "  box-shadow: 0 2px 8px rgba(0,0,0,0.35); transition: opacity 0.4s; opacity: 0; pointer-events: none; }",
        ".toast.err { background: rgba(178, 34, 52, 0.95); }",
      ].join("\n");
      shadow.appendChild(style);

      ui.badge = el("div", null, "");
      ui.badge.className = "badge";
      shadow.appendChild(ui.badge);

      ui.chip = el("div", null, "");
      ui.chip.className = "chip";
      shadow.appendChild(ui.chip);

      ui.panel = el("div", null, "");
      ui.panel.className = "panel";
      ui.panelTitle = el("h3", null, "Add a note");
      ui.panelMeta = el("div", null, "");
      ui.panelMeta.className = "meta";
      ui.textarea = document.createElement("textarea");
      ui.textarea.placeholder = "Describe what should change here…";
      var row = el("div", null, "");
      row.className = "row";
      ui.cancelBtn = el("button", null, "Cancel");
      ui.saveBtn = el("button", null, "Save note");
      ui.saveBtn.className = "save";
      row.appendChild(ui.cancelBtn);
      row.appendChild(ui.saveBtn);
      ui.hint = el("div", null, "Enter saves · Shift+Enter adds a line · Esc cancels");
      ui.hint.className = "hint";
      ui.panel.appendChild(ui.panelTitle);
      ui.panel.appendChild(ui.panelMeta);
      ui.panel.appendChild(ui.textarea);
      ui.panel.appendChild(row);
      ui.panel.appendChild(ui.hint);
      shadow.appendChild(ui.panel);

      ui.toast = el("div", null, "");
      ui.toast.className = "toast";
      shadow.appendChild(ui.toast);

      api.root = root;
      ui.shadow = shadow;
      renderBadge();
      wireUi();
      } catch (err) { /* the overlay must never break the page */ }
    }

    function renderBadge() {
      try {
        if (!ui.badge) {
          return;
        }
        if (api.mode) {
          ui.badge.innerHTML = "";
          var b = document.createElement("b");
          b.textContent = "Annotate mode on";
          ui.badge.appendChild(b);
          ui.badge.appendChild(document.createTextNode(" — Esc to exit · " + api.saved + " saved"));
        } else {
          ui.badge.textContent = "Annotate mode off — Ctrl+Shift+A to start · " + api.saved + " saved";
        }
      } catch (e) { /* keep the badge silent */ }
    }

    var toastTimer = null;
    function toast(message, isError) {
      try {
        if (!ui.toast) {
          return;
        }
        ui.toast.textContent = message;
        ui.toast.className = isError ? "toast err" : "toast";
        ui.toast.style.opacity = "1";
        if (toastTimer) {
          clearTimeout(toastTimer);
        }
        toastTimer = setTimeout(function () {
          ui.toast.style.opacity = "0";
        }, 2600);
      } catch (e) { /* ignore */ }
    }

    // ------------------------------------------------------------------
    // Selector generation — short, deterministic, verified unique.
    // ------------------------------------------------------------------

    function cssEscape(value) {
      try {
        if (window.CSS && CSS.escape) {
          return CSS.escape(value);
        }
      } catch (e) { /* fall through */ }
      return String(value).replace(/([^\w-])/g, "\\$1");
    }

    function uniqueCount(sel) {
      try {
        return document.querySelectorAll(sel).length;
      } catch (e) {
        return -1;
      }
    }

    function segmentFor(node) {
      var tag = node.tagName.toLowerCase();
      var nth = 1;
      var sib = node;
      while ((sib = sib.previousElementSibling)) {
        if (sib.tagName === node.tagName) {
          nth += 1;
        }
      }
      return tag + ":nth-of-type(" + nth + ")";
    }

    function buildSelector(element) {
      try {
        if (element.id) {
          var idSel = "#" + cssEscape(element.id);
          if (uniqueCount(idSel) === 1) {
            return idSel;
          }
        }
        var testId = element.getAttribute("data-testid");
        if (testId) {
          var testSel = "[data-testid='" + String(testId).replace(/\\/g, "\\\\").replace(/'/g, "\\'") + "']";
          if (uniqueCount(testSel) === 1) {
            return testSel;
          }
        }
        var parts = [];
        var node = element;
        while (node && node.nodeType === 1) {
          if (node.id) {
            parts.unshift("#" + cssEscape(node.id));
            break;
          }
          parts.unshift(segmentFor(node));
          if (node === document.documentElement) {
            break;
          }
          var candidate = parts.join(" > ");
          if (parts.length >= 2 && uniqueCount(candidate) === 1) {
            break;
          }
          node = node.parentNode;
        }
        var sel = parts.join(" > ");
        if (sel.length > MAX_SELECTOR) {
          while (parts.length > 1) {
            parts.shift();
            var trimmed = parts.join(" > ");
            if (trimmed.length <= MAX_SELECTOR && uniqueCount(trimmed) === 1) {
              return trimmed;
            }
          }
        }
        return sel;
      } catch (e) {
        return element.tagName ? element.tagName.toLowerCase() : "*";
      }
    }

    // ------------------------------------------------------------------
    // Mode / hover / capture
    // ------------------------------------------------------------------

    function clearHover() {
      try {
        if (api.hovered) {
          api.hovered.style.outline = api.hovered.__prevOutline || "";
          api.hovered.style.outlineOffset = api.hovered.__prevOffset || "";
          delete api.hovered.__prevOutline;
          delete api.hovered.__prevOffset;
          api.hovered = null;
        }
        if (ui.chip) {
          ui.chip.style.display = "none";
        }
      } catch (e) { /* ignore */ }
    }

    function isOwnNode(node) {
      try {
        return !!api.root && (node === api.root || api.root.contains(node));
      } catch (e) {
        return false;
      }
    }

    function onMouseMove(ev) {
      try {
        if (!api.mode || api.boxOpen) {
          return;
        }
        var target = ev.target;
        if (!target || target.nodeType !== 1 || isOwnNode(target)) {
          clearHover();
          return;
        }
        if (api.hovered !== target) {
          clearHover();
          api.hovered = target;
          target.__prevOutline = target.style.outline;
          target.__prevOffset = target.style.outlineOffset;
          target.style.outline = "2px solid #2f6fed";
          target.style.outlineOffset = "1px";
        }
        if (ui.chip) {
          var label = target.tagName.toLowerCase();
          if (target.id) {
            label += "#" + target.id;
          } else if (target.className && typeof target.className === "string") {
            var cls = target.className.trim().split(/\s+/)[0];
            if (cls) {
              label += "." + cls;
            }
          }
          ui.chip.textContent = label;
          ui.chip.style.display = "block";
          ui.chip.style.left = Math.min(ev.clientX + 14, window.innerWidth - 120) + "px";
          ui.chip.style.top = Math.max(ev.clientY - 24, 4) + "px";
        }
      } catch (e) { /* ignore */ }
    }

    function describe(element) {
      var ts = new Date().toISOString();
      var text = "";
      try {
        text = (element.textContent || "").replace(/\s+/g, " ").trim().slice(0, MAX_TEXT);
      } catch (e) { /* ignore */ }
      return {
        url: location.href,
        route: location.pathname,
        selector: buildSelector(element),
        tag: element.tagName.toLowerCase(),
        text: text,
        ts: ts,
      };
    }

    function onClick(ev) {
      try {
        if (!api.mode || api.boxOpen) {
          return;
        }
        if (isOwnNode(ev.target)) {
          return;
        }
        ev.preventDefault();
        ev.stopPropagation();
        var element = ev.target;
        if (!element || element.nodeType !== 1) {
          return;
        }
        clearHover();
        api.pending = describe(element);
        openPanel();
      } catch (e) { /* ignore */ }
    }

    function openPanel() {
      try {
        api.boxOpen = true;
        ui.panelMeta.textContent = api.pending.tag + "  ·  " + api.pending.selector;
        ui.textarea.value = "";
        ui.panel.style.display = "block";
        setTimeout(function () {
          ui.textarea.focus();
        }, 30);
      } catch (e) { /* ignore */ }
    }

    function closePanel() {
      try {
        api.boxOpen = false;
        api.pending = null;
        ui.panel.style.display = "none";
      } catch (e) { /* ignore */ }
    }

    // ------------------------------------------------------------------
    // Save
    // ------------------------------------------------------------------

    function payloadFor(note) {
      return {
        worktree: WORKTREE,
        url: api.pending.url,
        route: api.pending.route,
        selector: api.pending.selector,
        tag: api.pending.tag,
        text: api.pending.text,
        note: note,
        ts: api.pending.ts,
      };
    }

    function sendBeacon(payload) {
      try {
        if (navigator.sendBeacon) {
          return navigator.sendBeacon(ENDPOINT, new Blob([JSON.stringify(payload)], { type: "application/json" }));
        }
      } catch (e) { /* ignore */ }
      return false;
    }

    function saveNote() {
      if (!api.pending) {
        return;
      }
      var note = ui.textarea.value.trim();
      if (!note) {
        toast("Write a note first", true);
        return;
      }
      var payload = payloadFor(note);
      closePanel();
      var done = function () {
        api.saved += 1;
        renderBadge();
        toast("Annotation saved");
      };
      var failed = function () {
        if (sendBeacon(payload)) {
          api.saved += 1;
          renderBadge();
          toast("Annotation saved");
        } else {
          toast("Could not save annotation", true);
        }
      };
      try {
        fetch(ENDPOINT, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
          keepalive: true,
        }).then(function (resp) {
          if (resp && resp.ok) {
            done();
          } else {
            failed();
          }
        }).catch(failed);
      } catch (e) {
        failed();
      }
    }

    // ------------------------------------------------------------------
    // Wiring
    // ------------------------------------------------------------------

    function setMode(on) {
      try {
        api.mode = on;
        if (!on) {
          clearHover();
        }
        renderBadge();
      } catch (e) { /* ignore */ }
    }

    function onKeyDown(ev) {
      try {
        var combo = (ev.ctrlKey || ev.metaKey) && ev.shiftKey && (ev.key === "a" || ev.key === "A");
        if (combo) {
          ev.preventDefault();
          ev.stopPropagation();
          setMode(!api.mode);
          return;
        }
        if (ev.key === "Escape") {
          if (api.boxOpen) {
            ev.stopPropagation();
            closePanel();
            return;
          }
          if (api.mode) {
            ev.preventDefault();
            ev.stopPropagation();
            setMode(false);
          }
        }
      } catch (e) { /* ignore */ }
    }

    function onPanelKey(ev) {
      try {
        ev.stopPropagation();
        if (ev.key === "Enter" && !ev.shiftKey) {
          ev.preventDefault();
          saveNote();
        }
      } catch (e) { /* ignore */ }
    }

    // Node-level wiring must happen on mount: pinned scripts can run before
    // <body> exists, so buildUi may mount via the retry timer instead of
    // synchronously. The wired flag keeps exactly-once semantics if the
    // retry path re-enters buildUi.
    function wireUi() {
      if (wired) {
        return;
      }
      try {
        ui.cancelBtn.addEventListener("click", function (ev) {
          ev.stopPropagation();
          closePanel();
        });
        ui.saveBtn.addEventListener("click", function (ev) {
          ev.stopPropagation();
          saveNote();
        });
        ui.textarea.addEventListener("keydown", onPanelKey);
        ui.panel.addEventListener("click", function (ev) {
          ev.stopPropagation();
        });
        wired = true;
      } catch (e) { /* the overlay must never break the page */ }
    }

    buildUi();

    document.addEventListener("keydown", onKeyDown, true);
    document.addEventListener("mousemove", onMouseMove, true);
    document.addEventListener("click", onClick, true);
  } catch (e) { /* the overlay must never break the page */ }
})();
